import asyncio
import time
import traceback
from typing import cast

import discord
from discord import Interaction, app_commands
from discord.ext import commands

import functions.ask_ai as ask_ai_module
from config import (
    AI_AUTO_RETRY_MAX_PER_MESSAGE,
    AI_CHAT_CHANNEL_ID,
    AI_DISSATISFACTION_WINDOW_SECONDS,
    AI_RETRY_ACTION_COOLDOWN_SECONDS,
    AI_RETRY_ACTION_MAX_PER_WINDOW,
    AI_RETRY_ACTION_WINDOW_SECONDS,
    GUILD_ID,
)
from functions.anti_spam import detect_cross_channel_spam
from functions.ask_ai import ask_ai
from functions.dm import sendDMReply
from functions.grow_tree_manager import delete_messages_except_grow_tree
from functions.lucky_member import handle_lucky_member_message, initialize_lucky_member_feature
from functions.status_manager import StatusManager
from functions.verifier import check_and_assign_role, is_blacklisted
from functions.webhook import send_webhook_message

from rainyai.core import (
    AI_RETRY_ACTIVITY,
    LAST_AI_INTERACTIONS,
    append_tried_model,
    bot,
    bot_health_heartbeat,
    fetch_data_task,
    is_dissatisfied_followup,
    is_model_switch_request,
    periodic_status_updater,
    run_dashboard_analytics_once,
    run_fetch_data_once,
    update_dashboard_analytics,
)


async def _run_initial_fetch_pipeline():
    try:
        await run_fetch_data_once()
        await run_dashboard_analytics_once()
        bot._rainyai_initial_fetch_done = True
    except Exception as e:
        tb = traceback.format_exc()
        print(f"Error in initial fetch pipeline: {e}")
        print(tb)
        await send_webhook_message(f"Error in initial fetch pipeline: {e}\n```{tb[-1500:]}```")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ This command is on cooldown. Try again <t:{int(time.time() + error.retry_after)}:R>.")
        return
    else:
        await ctx.send("An error occurred while processing your request.")
        await send_webhook_message(f"Command error: {error}")
        print(error)
        return


@bot.tree.error
async def on_app_command_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandInvokeError):
        error = cast(app_commands.AppCommandError, error.original)

    if isinstance(error, app_commands.CommandOnCooldown):
        retry_after = error.retry_after
        cooldown_message = f"⏳ **Cooldown!** Try again <t:{int(time.time() + retry_after)}:R>."
        if interaction.response.is_done():
            await interaction.followup.send(cooldown_message, ephemeral=True)
        else:
            await interaction.response.send_message(cooldown_message, ephemeral=True)
        return


@bot.event
async def on_ready():  # type: ignore
    await bot.wait_until_ready()
    print("Bot is ready!")
    guild = bot.get_guild(GUILD_ID)
    if guild:
        print(f"Connected to guild: {guild.name} (ID: {guild.id})")

    try:
        if not getattr(bot, "_rainyai_commands_synced", False):
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} commands: {[cmd.name for cmd in synced]}")
            guild_synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
            print(f"Synced {len(guild_synced)} commands: {[cmd.name for cmd in guild_synced]} in guild: {GUILD_ID}")
            bot._rainyai_commands_synced = True

        if not hasattr(bot, "status_manager"):
            bot.status_manager = StatusManager(bot)
            print("StatusManager has been initialized.")
            await send_webhook_message("StatusManager has been initialized.")

        initialize_lucky_member_feature(bot)
        print("Lucky Member feature initialized.")

        if not getattr(bot, "_rainyai_initial_dashboard_seeded", False):
            await run_dashboard_analytics_once()
            bot._rainyai_initial_dashboard_seeded = True

        if not getattr(bot, "_rainyai_initial_fetch_started", False):
            bot._rainyai_initial_fetch_started = True
            bot._rainyai_initial_fetch_task = asyncio.create_task(_run_initial_fetch_pipeline())

        if not update_dashboard_analytics.is_running():
            update_dashboard_analytics.start()
        if not periodic_status_updater.is_running():
            periodic_status_updater.start()
        if not fetch_data_task.is_running():
            fetch_data_task.start()
        if not bot_health_heartbeat.is_running():
            bot_health_heartbeat.start()

        await send_webhook_message(f"Bot {bot.user} is online and ready!")
    except Exception as e:
        tb = traceback.format_exc()
        await send_webhook_message(f"Error starting tasks: {e}\n```{tb[-1500:]}```")
        print(f"Error starting tasks: {e}")
        print(tb)


@bot.event
async def on_disconnect():  # type: ignore
    print("Bot disconnected from Discord gateway.")
    await send_webhook_message("Bot disconnected from Discord gateway.")


@bot.event
async def on_resumed():  # type: ignore
    print("Bot Discord gateway session resumed.")
    await send_webhook_message("Bot Discord gateway session resumed.")


@bot.event
async def on_member_join(member):
    if member.bot:
        return
    if member.guild.id != GUILD_ID:
        return
    if hasattr(bot, "status_manager"):
        await bot.status_manager.on_new_member(member)


@bot.event
async def on_member_update(before, after):
    if after.bot:
        return
    if after.guild.id != GUILD_ID:
        return
    if is_blacklisted(after.id):
        return
    await check_and_assign_role(after)


@bot.event
async def on_presence_update(before, after):
    if after.bot:
        return
    if after.guild.id != GUILD_ID:
        return
    if is_blacklisted(after.id):
        return
    await check_and_assign_role(after)


@bot.event
async def on_user_update(before, after):
    if after.bot:
        return
    if is_blacklisted(after.id):
        return
    guild = bot.get_guild(GUILD_ID)
    if guild:
        member = guild.get_member(after.id)
        if member:
            await check_and_assign_role(member)


@bot.event
async def on_message(message):  # type: ignore
    if message.guild is None:
        if message.author.bot:
            return
        print(f"[DM] {message.author} sent a DM: {message.content}")
        user = bot.get_user(510796282139508756)
        if user is not None:
            await user.send(f"DM from {message.author}: {message.content}")
            await sendDMReply(message.author.id)
        return

    if message.guild.id != GUILD_ID:
        return
    if message.channel.id == 1369497844544704543:
        try:
            if message.channel.is_news() and message.author.bot:
                await message.publish()
                print(f"Published message from {message.author} in {message.channel}")
                await send_webhook_message(f"Published message from {message.author} in {message.channel}")
        except Exception as e:
            print(f"[Auto-publish error] Failed to publish message: {e}")
            await send_webhook_message(
                f"[Auto-publish error] Failed to publish message from {message.author} in {message.channel}: {e}"
            )

    await delete_messages_except_grow_tree(message)
    if message.author.bot:
        return
    if message.author == bot.user:
        return

    spam_detected = await detect_cross_channel_spam(
        message,
        webhook_func=send_webhook_message,
    )
    if spam_detected:
        return

    if hasattr(bot, "status_manager"):
        await bot.status_manager.on_new_message(message)

    member = message.guild.get_member(message.author.id)
    await handle_lucky_member_message(message, bot)

    if message.channel.id == AI_CHAT_CHANNEL_ID:
        try:
            retry_timestamps = AI_RETRY_ACTIVITY.get(message.author.id, [])
            current_time = time.time()
            retry_timestamps = [
                ts for ts in retry_timestamps
                if current_time - ts <= AI_RETRY_ACTION_WINDOW_SECONDS
            ]
            AI_RETRY_ACTIVITY[message.author.id] = retry_timestamps

            previous_ai_interaction = LAST_AI_INTERACTIONS.get(message.author.id)
            if previous_ai_interaction:
                interaction_age = current_time - previous_ai_interaction.get("timestamp", 0)
                retry_request = is_model_switch_request(message.content) or is_dissatisfied_followup(message.content)
                if retry_request and retry_timestamps:
                    last_retry_age = current_time - retry_timestamps[-1]
                    if last_retry_age < AI_RETRY_ACTION_COOLDOWN_SECONDS:
                        await message.reply(
                            f"⏳ Try again <t:{int(current_time + (AI_RETRY_ACTION_COOLDOWN_SECONDS - last_retry_age))}:R>.",
                            delete_after=6,
                        )
                        return
                if retry_request and len(retry_timestamps) >= AI_RETRY_ACTION_MAX_PER_WINDOW:
                    await message.reply(
                        "⚠️ Too many retry/model-switch requests in a short time. Wait a bit before trying again.",
                        delete_after=8,
                    )
                    return

                if (
                    interaction_age <= AI_DISSATISFACTION_WINDOW_SECONDS
                    and previous_ai_interaction.get("auto_retry_count", 0) < AI_AUTO_RETRY_MAX_PER_MESSAGE
                    and is_model_switch_request(message.content)
                ):
                    async with message.channel.typing():
                        retry_meta = await ask_ai(
                            str(message.author.id),
                            previous_ai_interaction["prompt"],
                            member=member,
                            excluded_models=previous_ai_interaction.get("tried_models"),
                            dissatisfaction_reason="The user explicitly asked for another model. Re-answer the same request with a different approach.",
                            return_metadata=True,
                        )
                        retry_reply = retry_meta.get("reply", "").strip()
                        if retry_reply:
                            if previous_ai_interaction.get("model"):
                                ask_ai_module.update_model_feedback(previous_ai_interaction["model"], "dissatisfied")
                            if retry_meta.get("model"):
                                ask_ai_module.update_model_feedback(retry_meta["model"], "rescue")
                                ask_ai_module.set_preferred_model(retry_meta["model"])
                            sent_message = await message.reply(retry_reply)
                            LAST_AI_INTERACTIONS[message.author.id] = {
                                "timestamp": time.time(),
                                "prompt": previous_ai_interaction["prompt"],
                                "reply": retry_reply,
                                "model": retry_meta.get("model"),
                                "tried_models": append_tried_model(
                                    previous_ai_interaction.get("tried_models"),
                                    retry_meta.get("model"),
                                ),
                                "source_message_id": previous_ai_interaction.get("source_message_id"),
                                "response_message_id": sent_message.id,
                                "auto_retry_count": previous_ai_interaction.get("auto_retry_count", 0) + 1,
                            }
                            await send_webhook_message(
                                f"[ask_ai manual-switch] user={message.author} old_model={previous_ai_interaction.get('model')} "
                                f"new_model={retry_meta.get('model')} latency_ms={retry_meta.get('latency_ms')}"
                            )
                            retry_timestamps.append(current_time)
                            AI_RETRY_ACTIVITY[message.author.id] = retry_timestamps
                            return

                if (
                    interaction_age <= AI_DISSATISFACTION_WINDOW_SECONDS
                    and previous_ai_interaction.get("auto_retry_count", 0) < AI_AUTO_RETRY_MAX_PER_MESSAGE
                    and is_dissatisfied_followup(message.content)
                ):
                    async with message.channel.typing():
                        retry_meta = await ask_ai(
                            str(message.author.id),
                            previous_ai_interaction["prompt"],
                            member=member,
                            excluded_models=previous_ai_interaction.get("tried_models"),
                            dissatisfaction_reason=message.content,
                            return_metadata=True,
                        )
                        retry_reply = retry_meta.get("reply", "").strip()
                        if retry_reply:
                            if previous_ai_interaction.get("model"):
                                ask_ai_module.update_model_feedback(previous_ai_interaction["model"], "dissatisfied")
                            if retry_meta.get("model"):
                                ask_ai_module.update_model_feedback(retry_meta["model"], "rescue")
                                ask_ai_module.set_preferred_model(retry_meta["model"])
                            sent_message = await message.reply(retry_reply)
                            LAST_AI_INTERACTIONS[message.author.id] = {
                                "timestamp": time.time(),
                                "prompt": previous_ai_interaction["prompt"],
                                "reply": retry_reply,
                                "model": retry_meta.get("model"),
                                "tried_models": append_tried_model(
                                    previous_ai_interaction.get("tried_models"),
                                    retry_meta.get("model"),
                                ),
                                "source_message_id": previous_ai_interaction.get("source_message_id"),
                                "response_message_id": sent_message.id,
                                "auto_retry_count": previous_ai_interaction.get("auto_retry_count", 0) + 1,
                            }
                            await send_webhook_message(
                                f"[ask_ai autoretry] user={message.author} old_model={previous_ai_interaction.get('model')} "
                                f"new_model={retry_meta.get('model')} latency_ms={retry_meta.get('latency_ms')}"
                            )
                            retry_timestamps.append(current_time)
                            AI_RETRY_ACTIVITY[message.author.id] = retry_timestamps
                            return

            if message.reference:
                message_reference = message.reference.resolved
                if message_reference:
                    reference_author = message_reference.author
                    if reference_author is not None and reference_author.id not in [bot.user.id, 1026022417363124255]:
                        return
            async with message.channel.typing():
                reply_meta = await ask_ai(
                    str(message.author.id),
                    message.content,
                    member=member,
                    return_metadata=True,
                )
                reply = reply_meta.get("reply", "").strip()
                try:
                    if reply:
                        print(f"[ask_ai] Replying to {message.author} in {message.channel} with: {reply}")
                        sent_message = await message.reply(reply)
                        LAST_AI_INTERACTIONS[message.author.id] = {
                            "timestamp": time.time(),
                            "prompt": message.content,
                            "reply": reply,
                            "model": reply_meta.get("model"),
                            "tried_models": append_tried_model([], reply_meta.get("model")),
                            "source_message_id": message.id,
                            "response_message_id": sent_message.id,
                            "auto_retry_count": 0,
                        }
                        if reply_meta.get("used_fallback") or reply_meta.get("sanitized"):
                            await send_webhook_message(
                                f"[ask_ai] user={message.author} model={reply_meta.get('model')} "
                                f"fallback={reply_meta.get('used_fallback')} sanitized={reply_meta.get('sanitized')} "
                                f"context={reply_meta.get('context_count')} latency_ms={reply_meta.get('latency_ms')}"
                            )
                    else:
                        print(f"[ask_ai] No reply generated for {message.author} in {message.channel}")
                        await message.reply("<a:PepeSmokes:1369959531425038416> No reply generated.", delete_after=5)
                except Exception as e:
                    print(f"[ask_ai error] Failed to send reply: {e}")
                    await message.reply("<a:PepeSmokes:1369959531425038416> Failed to send reply.", delete_after=5)
                    await send_webhook_message(f"Failed to send reply to {message.author} in {message.channel}: {e}")
        except Exception as e:
            await message.reply("⚠️ Failed to process your message. Please try again later.", delete_after=5)
            await send_webhook_message(f"ask_ai processing error for {message.author} in {message.channel}: {e}")
            print(f"[ask_ai error] {e}")

    if not is_blacklisted(message.author.id) and member:
        await check_and_assign_role(member)

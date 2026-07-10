import asyncio
import re
import traceback
import time
from typing import Optional

import discord
from discord import Interaction, app_commands

import functions.ask_ai as ask_ai_module
from config import AI_CHAT_CHANNEL_ID, AI_STATUS_GUILD_COOLDOWN_SECONDS, GUILD_ID
from functions.ask_ai import GROQ_MODEL_PRIORITY_LIST, get_ai_limit_status
from functions.gen_poll import generate_poll
from functions.guildsmanager import (
    add_guild_tag,
    create_validation_report,
    get_guild_by_id,
    get_guild_by_invite_code_or_resolved_server,
    get_guild_count,
    merge_validation_report,
    search_guild_by_tag,
    validate_guild_for_search,
)
from functions.verifier import verify_member
from functions.webhook import send_webhook_message

from rainyai.core import (
    AI_STATUS_GUILD_COOLDOWNS,
    MODEL_COMMAND_GUILD_COOLDOWNS,
    TAG_BACKGROUND_VALIDATE_DELAY,
    TAG_DELAYED_STATUS,
    TAG_EMBED_PURPLE,
    TAG_VERIFIED_STATUS,
    TAG_VERIFYING_STATUS,
    bot,
    build_model_choices,
    build_tag_validation_log,
    send_ephemeral_interaction_message,
)
from rainyai.views import GroqStatusView, GuildTagList, PaginatorView

rainy_group = app_commands.Group(
    name="rainyai",
    description="Rainy AI features.",
)
bot.tree.add_command(rainy_group, guild=discord.Object(id=GUILD_ID))

poll_group = app_commands.Group(
    name="poll",
    description="Commands related to poll suggestions.",
    parent=rainy_group,
)

tag_group = app_commands.Group(
    name="tag",
    description="Search or Add guilds by tag.",
)
bot.tree.add_command(tag_group)


@tag_group.command(name="showcase", description="Posts the tag feature showcase in this channel (Admin Only).")
@app_commands.checks.has_permissions(administrator=True)
async def tag_showcase(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_count = await asyncio.to_thread(get_guild_count)
    tag_help_container = GuildTagList(guild_count=guild_count)
    await interaction.channel.send(view=tag_help_container)


@tag_showcase.error
async def on_tag_help_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ Sorry, this command is for administrators only.",
            ephemeral=True,
        )
    else:
        print(f"Error in /tag help command: {error}")
        try:
            await interaction.response.send_message(
                "An unexpected error occurred. Please try again later.",
                ephemeral=True,
            )
        except discord.errors.InteractionResponded:
            await interaction.followup.send(
                "An unexpected error occurred. Please try again later.",
                ephemeral=True,
            )


async def validate_tag_search_message(
    message,
    view: PaginatorView,
    log_label: str,
    search_tag_filter: Optional[str] = None,
):
    async def edit_no_valid_results():
        embed = discord.Embed(
            title="No valid servers found",
            description=f"No joinable servers are currently available for `{log_label}`.",
            color=TAG_EMBED_PURPLE,
        )
        embed.add_field(
            name="Status",
            value="<a:Cute_twerk:1377974771509104691> Verification finished.",
            inline=False,
        )
        await message.edit(embed=embed, view=None)

    async def edit_current_result():
        if not view.guilds:
            await edit_no_valid_results()
            return
        await message.edit(embed=view.create_embed(), view=view)

    try:
        report = create_validation_report()
        validated_server_ids = set()

        while True:
            server_id = view.pop_next_validation_target(validated_server_ids)
            if not server_id:
                break

            current_index = view.find_guild_index(server_id)
            if current_index is None:
                validated_server_ids.add(server_id)
                continue

            guild = view.guilds[current_index]
            was_current = current_index == view.current_page
            view.validation_status_by_server[server_id] = TAG_VERIFYING_STATUS
            if was_current:
                await edit_current_result()

            validated_guild, guild_report = await asyncio.to_thread(
                validate_guild_for_search,
                guild,
                search_tag_filter,
            )
            validated_server_ids.add(server_id)
            merge_validation_report(report, guild_report)

            current_index = view.find_guild_index(server_id)
            if current_index is None:
                continue
            is_current_now = current_index == view.current_page

            if validated_guild:
                status = TAG_DELAYED_STATUS if guild_report.get("failed") else TAG_VERIFIED_STATUS
                view.replace_guild_at(current_index, validated_guild, status=status)
                if was_current or is_current_now:
                    await edit_current_result()
            else:
                view.remove_guild_at(current_index)
                if not view.guilds:
                    await edit_no_valid_results()
                    break
                if was_current or current_index <= view.current_page:
                    view.prioritize_current_guild()
                    await edit_current_result()

            if guild_report.get("failed") and any(
                "rate limited" in failure.casefold() for failure in guild_report.get("failures", [])
            ):
                break

            await asyncio.sleep(TAG_BACKGROUND_VALIDATE_DELAY)

        if view.guilds:
            await edit_current_result()

        await send_webhook_message(build_tag_validation_log(log_label, report))

    except Exception as e:
        print(f"Error validating /tag search results for '{log_label}': {e}")
        traceback.print_exc()
        await send_webhook_message(f"Error validating /tag search results for `{log_label}`: {e}")


@tag_group.command(name="search", description="Search for guilds by tag.")
@app_commands.describe(tag="The tag you want to search for (e.g., 'rain')")
@app_commands.checks.cooldown(2, 40.0, key=lambda i: i.user.id)
async def tag_search(interaction: Interaction, tag: str):
    await interaction.response.defer(ephemeral=True)
    results = await asyncio.to_thread(search_guild_by_tag, tag)

    if not results:
        await interaction.followup.send(
            f"Sorry, I couldn't find any guilds with the tag `{tag}`.",
            ephemeral=True,
        )
        return

    view = PaginatorView(guilds_list=results, search_tag=tag)
    message = await interaction.followup.send(
        embed=view.create_embed(),
        view=view,
        ephemeral=True,
        wait=True,
    )
    asyncio.create_task(validate_tag_search_message(message, view, tag, tag))


@tag_group.command(name="server", description="Find a tagged guild by server ID.")
@app_commands.describe(server_id="The Discord server ID to search for")
@app_commands.checks.cooldown(2, 40.0, key=lambda i: i.user.id)
async def tag_server_search(interaction: Interaction, server_id: str):
    await interaction.response.defer(ephemeral=True)

    server_id = server_id.strip()
    if not server_id.isdigit():
        await interaction.followup.send(
            "Please provide a valid numeric server ID.",
            ephemeral=True,
        )
        return

    guild = await asyncio.to_thread(get_guild_by_id, server_id)
    if not guild:
        await interaction.followup.send(
            f"I couldn't find a tagged guild with server ID `{server_id}`.",
            ephemeral=True,
        )
        return

    view = PaginatorView(guilds_list=[guild], search_tag=f"server:{server_id}")
    message = await interaction.followup.send(
        embed=view.create_embed(),
        view=view,
        ephemeral=True,
        wait=True,
    )
    asyncio.create_task(validate_tag_search_message(message, view, f"server ID {server_id}"))


@tag_group.command(name="add", description="Add a new guild by invite code.")
@app_commands.describe(invite_code="The guild's invite code (e.g., 'valorant')")
async def tag_add(interaction: Interaction, invite_code: str):
    await interaction.response.defer(ephemeral=True)

    invite_input = invite_code.strip().rstrip("/")
    match = re.search(r"(?:discord\.(?:gg|com/invite)/)?([a-zA-Z0-9-]+)$", invite_input)
    if not match:
        await interaction.followup.send(
            "Sorry, that doesn't look like a valid invite format. Please use a format like `discord.gg/code` or just `code`.",
            ephemeral=True,
        )
        return
    invite_code = match.group(1)
    status = await asyncio.to_thread(add_guild_tag, invite_code)

    guild = None
    if status in {0, 1, 2}:
        guild = await asyncio.to_thread(get_guild_by_invite_code_or_resolved_server, invite_code)

    if guild:
        view = PaginatorView(guilds_list=[guild], search_tag=guild.get("tag", ""))
        server_id = guild.get("serverID")
        if status == 1:
            add_status = "<a:Cute_twerk:1377974771509104691> Guild added."
        elif status == 2:
            add_status = "<a:Cute_twerk:1377974771509104691> Guild updated."
        else:
            add_status = "<a:Cute_twerk:1377974771509104691> Guild already exists."
        if server_id:
            view.validation_status_by_server[server_id] = add_status

        await interaction.followup.send(
            embed=view.create_embed(),
            view=view,
            ephemeral=True,
        )
        return

    if status == 1:
        await interaction.followup.send("Success! Guild was added.")
    elif status == 2:
        await interaction.followup.send("Guild already exists and was updated.")
    elif status == 0:
        await interaction.followup.send("This guild is already in the database.")
    elif status == -1:
        await interaction.followup.send("That invite is valid, but the guild doesn't have a tag.")
    elif status == -2:
        await interaction.followup.send("Sorry, that invite code is invalid or expired.")
    else:
        await interaction.followup.send("An unknown error occurred.")


@tag_search.error
async def on_tag_search_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_ephemeral_interaction_message(
            interaction,
            f"You're using this too fast! Try again in {error.retry_after:.1f} seconds.",
        )
    else:
        await send_ephemeral_interaction_message(
            interaction,
            "An unexpected error occurred. Please try again later.",
        )
        print(f"Error in /tag search command: {error}")


@tag_server_search.error
async def on_tag_server_search_error(interaction: Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CommandOnCooldown):
        await send_ephemeral_interaction_message(
            interaction,
            f"You're using this too fast! Try again in {error.retry_after:.1f} seconds.",
        )
    else:
        await send_ephemeral_interaction_message(
            interaction,
            "An unexpected error occurred. Please try again later.",
        )
        print(f"Error in /tag server command: {error}")


@rainy_group.command(name="status", description="Get the status and rate limits for the Groq AI service.")
@app_commands.checks.cooldown(2, 40.0, key=lambda i: i.user.id)
async def ai_status_command(interaction: Interaction):
    current_time = time.time()
    guild_last_used = AI_STATUS_GUILD_COOLDOWNS.get(interaction.guild_id, 0.0)
    if interaction.guild_id and current_time - guild_last_used < AI_STATUS_GUILD_COOLDOWN_SECONDS:
        retry_after = int(AI_STATUS_GUILD_COOLDOWN_SECONDS - (current_time - guild_last_used)) + 1
        await interaction.response.send_message(
            f"⏳ AI status is on guild cooldown. Try again <t:{int(current_time + retry_after)}:R>.",
            ephemeral=True,
        )
        return

    if interaction.guild_id:
        AI_STATUS_GUILD_COOLDOWNS[interaction.guild_id] = current_time
    await interaction.response.defer(ephemeral=True)
    initial_embed = await asyncio.to_thread(get_ai_limit_status)
    status_view = GroqStatusView()
    await interaction.followup.send(embed=initial_embed, view=status_view)


@rainy_group.command(name="model", description="Set the default Rainy AI model for future prompts.")
@app_commands.describe(model="Choose the model Rainy AI should prefer for new prompts.")
@app_commands.choices(model=build_model_choices())
@app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
async def ai_model_command(interaction: Interaction, model: str):
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message(
            "❌ This command can only be used in the Rainy Season server.",
            ephemeral=True,
        )
        return

    if interaction.channel_id != AI_CHAT_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ This command can only be used in <#{AI_CHAT_CHANNEL_ID}>.",
            ephemeral=True,
        )
        return

    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member or not member.guild_permissions.change_nickname:
        await interaction.response.send_message(
            "❌ You need the `Change Nickname` permission to use this command.",
            ephemeral=True,
        )
        return

    current_time = time.time()
    guild_last_used = MODEL_COMMAND_GUILD_COOLDOWNS.get(interaction.guild_id, 0.0)
    guild_cooldown_seconds = 12.0
    if current_time - guild_last_used < guild_cooldown_seconds:
        retry_after = int(guild_cooldown_seconds - (current_time - guild_last_used)) + 1
        await interaction.response.send_message(
            f"⏳ Model switching is on guild cooldown. Try again <t:{int(current_time + retry_after)}:R>.",
            ephemeral=True,
        )
        return

    if model not in GROQ_MODEL_PRIORITY_LIST:
        await interaction.response.send_message(
            "❌ That model is not in the configured model list.",
            ephemeral=True,
        )
        return

    ask_ai_module.set_preferred_model(model)
    MODEL_COMMAND_GUILD_COOLDOWNS[interaction.guild_id] = current_time
    await interaction.response.send_message(
        f"✅ Rainy AI will now prefer `{model}` for new prompts.",
        ephemeral=True,
    )
    await send_webhook_message(
        f"[rainyai model] user={interaction.user} set preferred_model={model} channel={interaction.channel_id}"
    )


@rainy_group.command(name="verify", description="Verify a member's clan affiliation.")
@app_commands.checks.cooldown(2, 40.0, key=lambda i: i.user.id)
async def verify_clan_slash(interaction: Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    message = await verify_member(member)
    if not message:
        message = "No clan data found for this member."
    try:
        await interaction.followup.send(message, ephemeral=True)
    except Exception as e:
        await send_webhook_message(f"[verify clan] Failed to send followup: {e}")
        print(f"Failed to send followup: {e}")


@poll_group.command(name="suggest", description="Generate a poll suggestion based on a topic.")
@app_commands.checks.cooldown(2, 40.0, key=lambda i: i.user.id)
async def suggest_poll(interaction: Interaction, topic: str | None = None):
    member = interaction.user if isinstance(interaction.user, discord.Member) else None
    if not member or not member.guild_permissions.manage_messages:
        await interaction.response.send_message("❌ You don't have permission to use this command.", ephemeral=True)
        return
    await interaction.response.defer()
    suggestion = await generate_poll(topic)
    await interaction.followup.send(suggestion)


@bot.tree.context_menu(name="Verify Clan")
async def verify_clan_context(interaction: Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)
    message = await verify_member(member)
    if not message:
        message = "No clan data found for this member."
    try:
        await interaction.followup.send(message, ephemeral=True)
    except Exception as e:
        await send_webhook_message(f"[Verify Clan] Failed to send followup: {e}")
        print(f"Failed to send followup: {e}")

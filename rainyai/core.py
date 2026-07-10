import asyncio
import os
import re
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import HTTPException, Interaction, app_commands
from discord.ext import commands, tasks

from config import BOT_TOKEN
from functions.app import (
    build_standard_filter_definitions,
    create_app,
    dashboard_filter_cache,
    finalize_filtered_dashboard_stats,
    process_data,
    sanitize_processed_stats_for_storage,
)
from functions.ask_ai import GROQ_MODEL_PRIORITY_LIST
from functions.discord_fetcher import db, fetch_and_save_data, update_status_and_activities
from functions.webhook import send_webhook_message

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"

intents = discord.Intents.all()
intents.members = True
intents.presences = True
intents.messages = True
intents.message_content = True

# cryst = commands.Bot(command_prefix="?", intents=intents)
bot = commands.Bot(command_prefix="!", intents=intents)

TAG_VERIFYING_STATUS = "<a:ChickDance:1373959268046340139> Verifying server info..."
TAG_VERIFIED_STATUS = "<a:Cute_twerk:1377974771509104691> Server verified."
TAG_PENDING_STATUS = "Waiting to verify this server..."
TAG_DELAYED_STATUS = "Verification delayed. Try again later."
TAG_EMBED_PURPLE = 0x5E4CA1
TAG_BACKGROUND_VALIDATE_DELAY = 2.0

LAST_AI_INTERACTIONS = {}
MODEL_COMMAND_GUILD_COOLDOWNS = {}
AI_RETRY_ACTIVITY = {}
AI_STATUS_GUILD_COOLDOWNS = {}

dashboard_analytics_lock = asyncio.Lock()
fetch_data_lock = asyncio.Lock()

app = create_app()


def normalize_feedback_text(content: str) -> str:
    return re.sub(r"\s+", " ", content.lower()).strip()


def is_dissatisfied_followup(content: str) -> bool:
    normalized = normalize_feedback_text(content)
    if not normalized:
        return False

    exact_triggers = {
        "what",
        "what?",
        "huh",
        "huh?",
        "no",
        "no?",
        "wrong",
        "bad answer",
        "that is wrong",
        "doesn't make sense",
        "doesnt make sense",
        "not what i asked",
        "that is not what i asked",
        "try again",
        "answer properly",
        "you missed the point",
    }
    if normalized in exact_triggers:
        return True

    substring_triggers = (
        "not what i asked",
        "doesn't answer",
        "doesnt answer",
        "answer my question",
        "that's wrong",
        "thats wrong",
        "wrong answer",
        "what are you talking about",
        "you are wrong",
        "missed the point",
        "try another model",
        "bad response",
        "this is wrong",
    )
    if any(trigger in normalized for trigger in substring_triggers):
        return True

    return False


def is_model_switch_request(content: str) -> bool:
    normalized = normalize_feedback_text(content)
    switch_phrases = (
        "try another model",
        "use another model",
        "switch model",
        "change model",
        "use a different model",
        "try a different model",
    )
    return any(phrase in normalized for phrase in switch_phrases)


def append_tried_model(existing_models, model_name):
    tried_models = list(existing_models or [])
    if model_name and model_name not in tried_models:
        tried_models.append(model_name)
    return tried_models


def build_model_choices():
    return [
        app_commands.Choice(name=model_name, value=model_name)
        for model_name in GROQ_MODEL_PRIORITY_LIST
    ]


def discord_id_to_unix(discord_id) -> Optional[int]:
    try:
        snowflake = int(discord_id)
    except (TypeError, ValueError):
        return None
    return int(((snowflake >> 22) + 1420070400000) / 1000)


def milliseconds_to_unix(timestamp_ms) -> Optional[int]:
    try:
        return int(int(timestamp_ms) / 1000)
    except (TypeError, ValueError):
        return None


def discord_timestamp(timestamp: Optional[int], style: str = "R") -> str:
    if not timestamp:
        return "N/A"
    return f"<t:{timestamp}:{style}>"


def format_member_count(member_count) -> str:
    try:
        return f"{int(member_count):,}"
    except (TypeError, ValueError):
        return "N/A"


def build_tag_validation_log(tag: str, report: dict) -> str:
    lines = [
        f"**/tag search validation**",
        f"Search: `{tag}`",
        f"Checked: {report.get('checked', 0)}",
        f"Updated: {report.get('updated', 0)}",
        f"Removed: {report.get('removed', 0)}",
        f"Failed: {report.get('failed', 0)}",
    ]

    if report.get("changes"):
        lines.append("\n**Changes**")
        lines.extend(f"- {change}" for change in report["changes"])

    if report.get("removed_entries"):
        lines.append("\n**Removed**")
        lines.extend(f"- {entry}" for entry in report["removed_entries"])

    if report.get("failures"):
        lines.append("\n**Validation failures**")
        lines.extend(f"- {failure}" for failure in report["failures"])

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1890] + "\n...truncated"
    return message


async def send_ephemeral_interaction_message(interaction: Interaction, content: str):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


def _task_health_line(task_loop, name: str) -> str:
    current_task = task_loop.get_task()
    if current_task and current_task.done():
        exc = current_task.exception()
        if exc:
            return f"{name}=done error={exc.__class__.__name__}: {exc}"
    return f"{name}=running:{task_loop.is_running()}"


@tasks.loop(minutes=15)
async def update_dashboard_analytics():
    if dashboard_analytics_lock.locked():
        print("BACKGROUND TASK: Dashboard analytics already running, skipping overlapping run.")
        return

    async with dashboard_analytics_lock:
        print("BACKGROUND TASK: Starting dashboard analytics processing...")
        try:
            chat_logs = await db["chat_logs"].find({}).to_list(length=None)
            server_info_data = await db["server_info"].find_one({"_id": "server_config"})
            boosters = await db["boosters"].find({}).to_list(length=None)
            emojis_json = await db["emojis"].find({}).to_list(length=None)
            members_list = await db["members"].find({}).to_list(length=None)

            for doc in chat_logs:
                doc.pop("_id", None)
            if server_info_data:
                server_info_data.pop("_id", None)
            for doc in boosters:
                doc.pop("_id", None)
            for doc in emojis_json:
                doc.pop("_id", None)
            for doc in members_list:
                doc.pop("_id", None)

            processed_stats = await asyncio.to_thread(
                process_data,
                chat_logs,
                server_info_data or {},
                boosters,
                emojis_json,
                members_list,
            )

            sanitize_processed_stats_for_storage(processed_stats)

            standard_filters = build_standard_filter_definitions()
            precomputed_filters = {}
            for key, filters in standard_filters.items():
                filter_start = filters["start"]
                filter_end = filters["end"]

                if key == "lifetime":
                    filtered_logs = chat_logs
                else:
                    filtered_logs = []
                    for entry in chat_logs:
                        raw_timestamp = entry.get("timestamp")
                        if not raw_timestamp:
                            continue
                        try:
                            entry_dt = datetime.fromisoformat(str(raw_timestamp).replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if entry_dt.tzinfo is None:
                            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                        entry_date = entry_dt.astimezone(timezone.utc).date()
                        if filter_start <= entry_date <= filter_end:
                            filtered_logs.append(entry)

                precomputed_stats = await asyncio.to_thread(
                    process_data,
                    filtered_logs,
                    server_info_data or {},
                    [],
                    emojis_json,
                    members_list,
                )
                precomputed_filters[key] = finalize_filtered_dashboard_stats(precomputed_stats, filters)

            processed_stats["precomputed_filters"] = precomputed_filters
            processed_stats["last_processed_at"] = datetime.now(timezone.utc).isoformat()

            await db["dashboard_analytics"].update_one(
                {"_id": "live_stats"},
                {"$set": processed_stats},
                upsert=True,
            )
            dashboard_filter_cache.clear()
            print("BACKGROUND TASK: Dashboard analytics successfully updated.")

        except Exception as e:
            print(f"BACKGROUND TASK: Error in update_dashboard_analytics: {e}")
            traceback.print_exc()


async def run_dashboard_analytics_once():
    await update_dashboard_analytics.coro()


async def run_fetch_data_once():
    if fetch_data_lock.locked():
        print(f"[{datetime.now()}] Immediate data fetch skipped because another fetch is already running.")
        return

    async with fetch_data_lock:
        await fetch_and_save_data(bot)


@tasks.loop(minutes=60)
async def fetch_data_task():
    if fetch_data_lock.locked():
        print(f"[{datetime.now()}] Scheduled data fetch skipped because another fetch is already running.")
        return

    async with fetch_data_lock:
        print(f"[{datetime.now()}] Running scheduled data fetch...")
        try:
            await fetch_and_save_data(bot)
        except Exception as e:
            print(f"[{datetime.now()}] Error during scheduled data fetch: {e}")
            await send_webhook_message(f"Error during scheduled data fetch: {e}")
        print(f"[{datetime.now()}] Scheduled data fetch complete.")


@tasks.loop(seconds=20)
async def periodic_status_updater():
    try:
        await update_status_and_activities(bot)
    except Exception as e:
        print(f"[{datetime.now()}] Error during status update: {e}")
        await send_webhook_message(f"Error during status update: {e}")


@tasks.loop(minutes=30)
async def bot_health_heartbeat():
    latency_ms = round(bot.latency * 1000, 2) if bot.latency is not None else "unknown"
    message = (
        f"[health] bot_alive=True latency_ms={latency_ms} guilds={len(bot.guilds)} "
        f"{_task_health_line(update_dashboard_analytics, 'dashboard')} "
        f"{_task_health_line(fetch_data_task, 'fetch_data')} "
        f"{_task_health_line(periodic_status_updater, 'status')}"
    )
    print(message)
    await send_webhook_message(message)


@bot_health_heartbeat.before_loop
async def before_bot_health_heartbeat():
    await bot.wait_until_ready()


async def run_bot_with_retry(client, token, bot_name):
    while True:
        try:
            print(f"Starting bot {bot_name}...")
            await client.start(token)
            print(f"Bot {bot_name} stopped without an exception. Restarting in 60s...")
            if bot_name is not None:
                await send_webhook_message(f"Bot {bot_name} stopped without an exception. Restarting in 60s...")
            await asyncio.sleep(60)

        except HTTPException as e:
            if e.status == 429 and "Cloudflare" not in str(e):
                retry_after = e.response.headers.get("Retry-After", 900)
                try:
                    retry_after = int(retry_after)
                except ValueError:
                    retry_after = 900
                message = (
                    f"Bot {bot_name} rate limited by Discord API. "
                    f"Waiting for {(retry_after / 60):.2f} minutes..."
                )
                print(message)
                if bot_name is not None:
                    await send_webhook_message(message)
                await asyncio.sleep(retry_after)
                print("Retrying bot login...")
                await send_webhook_message("Retrying bot login...")

            elif "Error 1015" in str(e) or "Cloudflare" in str(e):
                retry_after = e.response.headers.get("Retry-After", 900)
                try:
                    retry_after = int(retry_after)
                except ValueError:
                    retry_after = 900
                message = (
                    f"Bot {bot_name} rate limited by Cloudflare/IP ban. "
                    f"Waiting for {(retry_after / 60):.2f} minutes..."
                )
                print(message)
                if bot_name is not None:
                    await send_webhook_message(message)
                await asyncio.sleep(retry_after)
                print("Retrying bot login after Cloudflare ban...")
                await send_webhook_message("Retrying bot login after Cloudflare ban...")
            else:
                message = f"Bot {bot_name} failed to start due to HTTP error: {e}. Retrying in 60s..."
                print(message)
                if bot_name is not None:
                    await send_webhook_message(message)
                await asyncio.sleep(60)

        except Exception as e:
            tb = traceback.format_exc()
            print(f"Bot {bot_name} crashed with a non-HTTP error: {e}. Restarting in 60s...")
            print(tb)
            if bot_name is not None:
                await send_webhook_message(
                    f"Bot {bot_name} crashed with a non-HTTP error: {e}. Restarting in 60s.\n```{tb[-1500:]}```"
                )
            await asyncio.sleep(60)


async def run_dashboard_with_retry():
    while True:
        try:
            await app.run_task(host="0.0.0.0", port=24606, debug=False)
            print("Dashboard server stopped without an exception. Restarting in 30s...")
            await send_webhook_message("Dashboard server stopped without an exception. Restarting in 30s...")
        except Exception:
            tb = traceback.format_exc()
            print("Dashboard server crashed. Restarting in 30s...")
            print(tb)
            await send_webhook_message(f"Dashboard server crashed. Restarting in 30s.\n```{tb[-1500:]}```")
        await asyncio.sleep(30)


async def main():
    await asyncio.gather(
        run_bot_with_retry(bot, BOT_TOKEN, "RainyAI"),
        run_dashboard_with_retry(),
    )

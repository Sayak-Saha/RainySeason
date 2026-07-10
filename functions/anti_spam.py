import discord
import re
import asyncio
from collections import defaultdict
from datetime import timedelta

# Configuration
SPAM_CHANNEL_LIMIT = 3
SPAM_TIME_WINDOW = 30   # seconds
SPAM_TIMEOUT_MINUTES = 10

# In-memory tracker
cross_channel_tracker = defaultdict(list)


def normalize_message(content: str) -> str:
    content = content.lower().strip()
    content = re.sub(r"[^\w\s]", "", content)
    content = re.sub(r"\s+", " ", content)
    return content


async def detect_cross_channel_spam(message: discord.Message, webhook_func=None) -> bool:
    """
    Detect identical messages sent across multiple channels.
    Timeout user first, DM reason, then delete all copies.
    Returns True if anti-spam action was triggered.
    """

    if message.guild is None:
        return False

    if message.author.bot:
        return False

    if not message.content.strip():
        return False

    key = (message.guild.id, message.author.id)
    now = discord.utils.utcnow()

    normalized = normalize_message(message.content)

    # Remove expired entries
    cross_channel_tracker[key] = [
        item for item in cross_channel_tracker[key]
        if (now - item["time"]).total_seconds() <= SPAM_TIME_WINDOW
    ]

    # Store current message
    cross_channel_tracker[key].append({
        "content": normalized,
        "channel_id": message.channel.id,
        "message_obj": message,
        "time": now
    })

    # Find same messages
    same_messages = [
        item for item in cross_channel_tracker[key]
        if item["content"] == normalized
    ]

    unique_channels = {item["channel_id"] for item in same_messages}

    # Trigger anti-spam
    if len(unique_channels) >= SPAM_CHANNEL_LIMIT:
        timeout_applied = False

        try:
            member = await message.guild.fetch_member(message.author.id)
            bot_member = message.guild.me

            if (
                bot_member
                and bot_member.guild_permissions.moderate_members
                and bot_member.top_role > member.top_role
            ):
                until = discord.utils.utcnow() + timedelta(
                    minutes=SPAM_TIMEOUT_MINUTES
                )

                await member.timeout(
                    until,
                    reason="Cross-channel duplicate spam"
                )

                timeout_applied = True

                # Send DM to user
                try:
                    await member.send(
                        f"⚠️ You have been timed out in **{message.guild.name}** "
                        f"for **{SPAM_TIMEOUT_MINUTES} minutes**.\n\n"
                        f"Reason: Sending the same message across "
                        f"**{len(unique_channels)} channels** within a short period.\n\n"
                        f"Please avoid cross-channel spam."
                    )
                except discord.Forbidden:
                    pass

                print(f"[ANTI-SPAM] Timeout applied to {member}")

            else:
                print(
                    f"[ANTI-SPAM] Spam detected for {member}, "
                    f"timeout skipped due to hierarchy."
                )

        except Exception as e:
            print(f"[ANTI-SPAM] Moderation error: {e}")

        # Delete all matching messages AFTER timeout
        await asyncio.sleep(0.5)

        for item in same_messages:
            try:
                await item["message_obj"].delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        # Optional webhook log
        if webhook_func:
            await webhook_func(
                f"🚨 Cross-channel spam detected | "
                f"User: {message.author} | "
                f"Channels: {len(unique_channels)} | "
                f"Timeout: {timeout_applied} | "
                f"Message: {message.content[:100]}"
            )

        cross_channel_tracker[key].clear()
        return True

    return False
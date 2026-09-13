import aiohttp
from config import WEBHOOK_URL

WEBHOOK_TIMEOUT = aiohttp.ClientTimeout(total=10)
MAX_WEBHOOK_CONTENT_LENGTH = 1900


async def send_webhook_message(content, webhook_url=WEBHOOK_URL):
    """Sends a message to a Discord webhook."""
    content = str(content)
    if len(content) > MAX_WEBHOOK_CONTENT_LENGTH:
        content = content[:MAX_WEBHOOK_CONTENT_LENGTH] + "... [truncated]"

    payload = {
        "content": content
    }
    active_proxy = None
    try:
        from rainyai.proxy_manager import discord_proxy_manager
        if discord_proxy_manager is not None:
            active_proxy = discord_proxy_manager.get_active_proxy()
    except Exception:
        active_proxy = None

    async with aiohttp.ClientSession(timeout=WEBHOOK_TIMEOUT) as session:
        try:
            async with session.post(webhook_url, json=payload, proxy=active_proxy) as response:
                if response.status != 204: # 204 No Content is a successful webhook status
                    print(f"Failed to send webhook message: HTTP {response.status}")
        except Exception as e:
            print(f"Error sending webhook message: {e}")

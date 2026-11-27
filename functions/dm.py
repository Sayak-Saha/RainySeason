import asyncio
import aiohttp
from config import BOT_TOKEN

DISCORD_API_BASE = "https://discord.com/api/v10"
IS_COMPONENTS_V2 = 1 << 15


def _link_button(url: str, label: str, emoji_name: str | None = None) -> dict:
    btn = {"type": 2, "style": 5, "url": url, "label": label}
    if emoji_name:
        btn["emoji"] = {"name": emoji_name}
    return btn


def _action_row(*components: dict) -> dict:
    return {"type": 1, "components": list(components)}


def _text_display(content: str) -> dict:
    return {"type": 10, "content": content}


def _separator(large: bool = False) -> dict:
    return {"type": 14, "divider": True, "spacing": 2 if large else 1}


def _media_gallery(*urls: str) -> dict:
    return {"type": 12, "items": [{"media": {"url": u}} for u in urls]}


def _container(*components: dict) -> dict:
    return {"type": 17, "components": list(components)}


added_tag = [
    _container(
        _media_gallery(
            "https://res.cloudinary.com/yatoez/image/upload/w_607,h_341,c_fill/v1753483304/rainyseason.gif"
        ),
        _separator(large=True),
        _text_display(
            "## 🌧️ Welcome to Rainy Season\n⠀⠀⠀Thanks for choosing the <:raintag_0:1377671025105571912><:raintag_1:1377671022966476942> tag — it means a lot. You're what makes this community glow."
        ),
        _separator(large=False),
        _text_display("## ☁️ Get Set Up"),
        _action_row(
            _link_button(
                "https://discord.com/channels/1369397376175050863/1369397376745345056",
                "Join chats →",
            )
        ),
        _action_row(
            _link_button(
                "https://discord.com/channels/1369397376175050863/1397670643167658145",
                "Want to chat with me?😉 →",
            )
        ),
        _action_row(
            _link_button(
                "https://discord.com/channels/1369397376175050863/1369693594000167003",
                "Pick your roles →",
            )
        ),
        _action_row(
            _link_button(
                "https://discord.com/channels/1369397376175050863/1370368688762523688",
                "Select your colors →",
            )
        ),
        _separator(large=False),
        _text_display("💫 You're Part of It Now"),
    ),
    _action_row(
        _link_button("https://discord.gg/CvU77YA65K", "Rainy Season Server", "💙")
    ),
]


removed_tag = [
    _container(
        _media_gallery(
            "https://res.cloudinary.com/yatoez/image/upload/w_607,h_341,c_fill/v1753483304/rainyseason.gif"
        ),
        _separator(large=True),
        _text_display(
            "## ☁️ All Seasons Change\n"
            "⠀We noticed you’re not using the rain tag anymore — and that’s perfectly okay.\n\n"
            "⠀Thanks for repping it while you did — you made the vibe better just by being part of it.\n"
            "⠀Whenever you feel like bringing back the rain, know it's always welcome. ☔"
        ),
        _separator(large=False),
        _text_display("💙 Either way, we're glad you're still here with us. <3"),
    ),
    _action_row(
        _link_button("https://discord.gg/CvU77YA65K", "Jump back in", "🌧️")
    ),
]


DMReply = [
    _container(
        _text_display("<a:CatPeek:1371836907725656195> | **Want to chat with me?**"),
        _separator(large=False),
        _action_row(
            _link_button(
                "https://discord.com/channels/1369397376175050863/1397670643167658145",
                "Yes",
            ),
            _link_button(
                "https://discord.com/channels/1369397376175050863/1397670643167658145",
                "No",
            ),
        ),
    )
]


async def _discord_post_with_retry(
    session: aiohttp.ClientSession, url: str, headers: dict, payload: dict
) -> dict:
    for attempt in range(3):
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 429:
                data = await resp.json()
                retry_after = float(data.get("retry_after", 1.0))
                if attempt < 2:
                    await asyncio.sleep(retry_after)
                    continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError(f"Discord POST failed after retries: {url}")


async def _send_components_dm(user_id: int, components: list) -> None:
    headers = {
        "Authorization": f"Bot {BOT_TOKEN}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as session:
            dm_channel = await _discord_post_with_retry(
                session,
                f"{DISCORD_API_BASE}/users/@me/channels",
                headers,
                {"recipient_id": str(user_id)},
            )
            await _discord_post_with_retry(
                session,
                f"{DISCORD_API_BASE}/channels/{dm_channel['id']}/messages",
                headers,
                {"flags": IS_COMPONENTS_V2, "components": components},
            )
            print(f"Sent DM to user {user_id}")
    except Exception as e:
        print(f"Error sending DM: {e}")


async def send_thanks(user_id: int):
    await _send_components_dm(user_id, added_tag)


async def send_bye(user_id: int):
    await _send_components_dm(user_id, removed_tag)


async def sendDMReply(user_id: int):
    await _send_components_dm(user_id, DMReply)

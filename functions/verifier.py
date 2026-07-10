import aiohttp
import asyncio
import time
import discord
from functions.dm import *
from functions.webhook import send_webhook_message
from config import BOT_TOKEN, ROLE_ID, TARGET_BADGE, GUILD_ID, WEBHOOK_URL
blacklist = {}
headers = {"Authorization": f"Bot {BOT_TOKEN}"}

def is_blacklisted(user_id):
    return blacklist.get(user_id, 0) > time.time()

def blacklist_user(user_id, cooldown=900):
    blacklist[user_id] = time.time() + cooldown

def get_cooldown_expiry(user_id):
    cooldown_ts=None
    expires_at = blacklist.get(user_id, 0)
    if expires_at > time.time():
        cooldown_ts = int(expires_at)
    return cooldown_ts

async def check_and_assign_role(member: discord.Member, GUILD_ID=GUILD_ID, ROLE_ID=ROLE_ID):
    
    if is_blacklisted(member.id):
        return f"⏳ This member is on cooldown. Try again in <t:{get_cooldown_expiry(member.id)}:R>."
    blacklist_user(member.id, 300)
    
    if member.bot:
        return "🤖 Bots don't need to be verified."
    if not member.guild:
        return "❌ Member is not in Rainy Season Server."
    if member.guild.id != GUILD_ID:
        return "❌ Member is not in Rainy Season Server."
    
    guild = member.guild
    role = guild.get_role(ROLE_ID)
    primary_guild = getattr(member, 'primary_guild', None)
    primary_guild_id = getattr(primary_guild, 'id', None)
    primary_guild_badge = getattr(primary_guild, 'badge', None)
    
    if primary_guild_id == GUILD_ID and primary_guild_badge is not None:
        if role and role not in member.roles:
            try:
                await member.add_roles(role)
                await send_thanks(member.id)
                print(f"✅ Added role to {member} for matching Primary Guild ID.")
                await send_webhook_message(f"✅ Added role to {member} for matching Primary Guild ID.")
                return f"✅ Added role to {member} for matching Primary Guild ID."
            except Exception as e:
                print(f"⚠️ Failed to add role to {member}: {e}")
                await send_webhook_message(f"⚠️ Failed to add role to {member}: {e}")
                return f"⚠️ Failed to add role to {member}: {e}"
        else:
            return f"☑️{member} already has the role."
    else:
        #print(f"❌ {member} Primary Guild ID: {primary_guild_id}. Target ID: {GUILD_ID}") 
        if role and role in member.roles:
            try:
                await member.remove_roles(role)
                # await send_bye(member.id)
                print(f"❎ Removed role from {member} (Primary Guild ID mismatch).")
                await send_webhook_message(f"❎ Removed role from {member} (Primary Guild ID mismatch).")
                return f"❎ Removed role from {member} (Primary Guild ID mismatch)."
            except Exception as e:
                print(f"⚠️ Failed to remove role from {member}: {e}")
                await send_webhook_message(f"⚠️ Failed to remove role from {member}: {e}")
                return f"⚠️ Failed to remove role to {member}: {e}"
        else:
            if primary_guild_id is None:
                 return f"⛔ {member} does not have a Primary Guild set."
            else:
                 return f"❌ {member} Primary Guild ID does not match target."


async def verify_member(member: discord.Member, GUILD_ID=GUILD_ID, ROLE_ID=ROLE_ID):
    res = await check_and_assign_role(member, GUILD_ID, ROLE_ID)
    if not res:
        return "No clan data found for this member."
    return f"{res}"



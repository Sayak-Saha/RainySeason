# lucky_members.py
import time
import discord
from discord.ext import commands, tasks
import random
import asyncio
from datetime import datetime, timedelta, timezone
from config import MONGO_URI
from pymongo import MongoClient
from functions.webhook import send_webhook_message
# --- Global Variables & Constants ---
mongo_client = MongoClient(MONGO_URI)
mongo_db = mongo_client["rainy_season_db"]
lucky_members_collection = mongo_db["lucky_members"]
cooldowns = {}

GUILD_ID = 1369397376175050863
LUCKY_MEMBER_ROLE_ID = 1402221566493724846
LUCKY_MEMBER_CHANNEL_ID = 1402225984593461350
LUCKY_LIST_TITLE = "Lucky Member Hall of Fame"

# Moved this dictionary to the global scope for efficiency.
# This avoids recreating it every single time a lucky member is found.
PERMISSION_CATEGORIES = {
    "Server Admin": {"icon": "👑", "permissions": ["Administrator", "Manage Guild", "View Audit Log", "View Guild Insights"]},
    "Member Management": {"icon": "👥", "permissions": ["Kick Members", "Ban Members", "Manage Nicknames", "Create Instant Invite"]},
    "Channel & Role Management": {"icon": "🛠️", "permissions": ["Manage Channels", "Manage Roles", "Manage Webhooks", "Manage Guild Expressions"]},
    "Text Channel Perks": {"icon": "💬", "permissions": ["Send Messages","Send TTS Messages","Manage Messages","Embed Links","Attach Files","Read Message History","Add Reactions","Use External Emojis", "Mention Everyone","External Stickers", "Send Voice Messages"]},
    "Voice Channel Perks": {"icon": "🎤", "permissions": ["Connect (Voice)", "Speak (Voice)", "Stream", "Priority Speaker", "Mute Members", "Deafen Members", "Move Members", "Use VAD (Voice Activity Detection)","Use Soundboard","Use External Sounds"]},
    "General User Perks": {"icon": "👤", "permissions": ["View Channel", "Change Nickname"]},
    "Miscellaneous Perks": {"icon": "✨", "permissions": ["Request To Speak"]} # <- Left empty
}

# --- Helper Functions ---

async def get_newly_granted_permissions(member: discord.Member, new_role: discord.Role):
    """Returns a list of newly granted permission names."""
    initial_perms = member.guild_permissions
    granted_perms = []
    for perm_name, value in new_role.permissions:
        if value and not getattr(initial_perms, perm_name):
            granted_perms.append(perm_name.replace('_', ' ').title())
    return granted_perms

async def give_lucky_role(member: discord.Member, guild: discord.Guild):
    """Gives the 'Lucky Member' role to a member."""
    role = guild.get_role(LUCKY_MEMBER_ROLE_ID)
    if not role: return False
    try:
        await member.add_roles(role, reason="Lucky Member event")
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Failed to add role to {member.display_name}: {e}")
        return False

async def get_lucky_member_channel(client: commands.Bot):
    channel = client.get_channel(LUCKY_MEMBER_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(LUCKY_MEMBER_CHANNEL_ID)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as e:
            print(f"Lucky Member channel fetch failed: {e}")
            await send_webhook_message(f"Lucky Member channel fetch failed: {e}")
            return None

    if not isinstance(channel, discord.TextChannel):
        print(f"Lucky Member channel is not a text channel: {type(channel).__name__}")
        await send_webhook_message(
            f"Lucky Member channel is not a text channel: {type(channel).__name__}"
        )
        return None

    return channel

def is_lucky_list_message(message: discord.Message, client: commands.Bot) -> bool:
    if client.user is None or message.author.id != client.user.id:
        return False
    return any(
        embed.title and LUCKY_LIST_TITLE in embed.title
        for embed in message.embeds
    )

async def find_lucky_list_message(channel: discord.TextChannel, client: commands.Bot):
    try:
        async for message in channel.history(limit=50):
            if is_lucky_list_message(message, client):
                return message
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Failed to search Lucky Member channel history: {e}")
        await send_webhook_message(f"Failed to search Lucky Member channel history: {e}")
    return None

async def recover_missing_lucky_members(
    guild: discord.Guild,
    lucky_role: discord.Role,
    existing_member_ids: set[str],
    current_unix_time: int,
) -> int:
    """
    Rebuilds MongoDB records from current Discord role holders.
    Original grant time is not available from Discord, so recovered rows use
    the recovery time as their timestamp.
    """
    if not guild.chunked:
        try:
            await guild.chunk()
        except discord.HTTPException as e:
            print(f"Failed to chunk guild for Lucky Member recovery: {e}")
            await send_webhook_message(
                f"Failed to chunk guild for Lucky Member recovery: {e}"
            )

    recovered_count = 0
    for member in lucky_role.members:
        member_id = str(member.id)
        if member_id in existing_member_ids:
            continue

        lucky_members_collection.update_one(
            {"member_id": member_id},
            {
                "$set": {
                    "member_id": member_id,
                    "username": member.display_name,
                    "unix_timestamp": current_unix_time
                }
            },
            upsert=True,
        )
        existing_member_ids.add(member_id)
        recovered_count += 1

    if recovered_count:
        print(f"Recovered {recovered_count} Lucky Member records from Discord roles.")
        await send_webhook_message(
            f"Recovered {recovered_count} Lucky Member records from Discord roles."
        )

    return recovered_count

async def update_lucky_list_embed(client: commands.Bot):
    """
    Updates the lucky members list using multiple embeds correctly.
    - Title/description are on the first embed only.
    - Footer/timestamp are on the last embed only.
    """
    channel = await get_lucky_member_channel(client)
    if channel is None:
        return

    lucky_members_data = list(lucky_members_collection.find({}, {"_id": 0}))
    sorted_members = sorted(lucky_members_data, key=lambda item: item.get("unix_timestamp", 0), reverse=True)

    embeds_to_send = []

    if not sorted_members:
        embed = discord.Embed(
            title="✨ Lucky Member Hall of Fame ✨",
            description="Currently, no one holds this prestigious role. Be active to get lucky!",
            color=discord.Color.gold()
        )
        embeds_to_send.append(embed)

    else:
        # --- NEW: Simplified Logic ---

        # 1. Create and add the main embed first
        main_embed = discord.Embed(
            title="✨ Lucky Member Hall of Fame ✨",
            description=(
                "Fortune favors the active! Below are the latest members to be "
                "granted the coveted **Lucky Member** role. Who will be next?\n\n"
            ),
            color=discord.Color.gold()
        )
        embeds_to_send.append(main_embed)

        # 2. Split members into chunks of 25
        chunk_size = 25
        member_chunks = [sorted_members[i:i + chunk_size] for i in range(0, len(sorted_members), chunk_size)]
        guild = client.get_guild(GUILD_ID)
        # 3. Populate the first embed with the first chunk
        for index, data in enumerate(member_chunks[0], 1):
            received_timestamp = data.get("unix_timestamp", 0)
            member_id = data.get('member_id', '0')
            member_mention = f"<@{data.get('member_id', '0')}>"
            field_value = (
                f"Mention: {member_mention}\n"
                f"**Received:** <t:{received_timestamp}:f> (<t:{received_timestamp}:R>)"
                )
            guild_member = guild.get_member(int(member_id)) if guild else None
            username_to_use = (
                        guild_member.name if guild_member 
                        else data.get('username', 'Unknown User')
                    )
            main_embed.add_field(
                name=f"👑 {index}. {username_to_use}",
                value=field_value,
                inline=False
            )

        # 4. Loop through ONLY the remaining chunks to create continuation embeds
        current_index = len(member_chunks[0]) + 1
        if len(member_chunks) > 1:
            for chunk in member_chunks[1:10]: # Start from the second chunk
                continuation_embed = discord.Embed(color=discord.Color.gold())
                for data in chunk:
                    received_timestamp = data.get("unix_timestamp", 0)
                    member_id = data.get('member_id', '0')
                    member_mention = f"Mention: <@{data.get('member_id', '0')}>"
                    field_value = (
                    f"Mention: {member_mention}\n"
                    f"**Received:** <t:{received_timestamp}:f> (<t:{received_timestamp}:R>)"
                    )
                    guild_member = guild.get_member(int(member_id)) if guild else None
                    username_to_use = (
                        guild_member.name if guild_member 
                        else data.get('username', 'Unknown User')
                    )
                    continuation_embed.add_field(
                        name=f"👑 {current_index}. {username_to_use}",
                        value=field_value,
                        inline=False
                    )
                    current_index += 1
                embeds_to_send.append(continuation_embed)
    
    # After all embeds are created, add the footer ONLY to the very last one.
    if embeds_to_send:
        last_embed = embeds_to_send[-1]
        last_embed.set_footer(text="Last updated")
        last_embed.timestamp = datetime.now(timezone.utc)
    
    # The sending/editing logic remains the same
    try:
        list_message = await find_lucky_list_message(channel, client)
        
        if list_message:
            await list_message.edit(embeds=embeds_to_send)
        else:
            await channel.send(embeds=embeds_to_send)
            
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"Failed to update Hall of Fame embed: {e}")
        await send_webhook_message(f"Failed to update Hall of Fame embed: {e}")

# --- Background Task Loop ---
async def sync_lucky_members(client: commands.Bot):
    """
    Syncs lucky member roles and the hall-of-fame embed.
    - Removes role from expired members.
    - Re-adds role to valid DB members who lost it.
    - Cleans up members who have left.
    - Recovers DB rows for current Discord role holders.
    """
    print("Running role removal/sync check...")
    guild = client.get_guild(GUILD_ID)
    role_to_sync = guild.get_role(LUCKY_MEMBER_ROLE_ID) if guild else None
    if not guild or not role_to_sync: 
        print("DEBUG: Guild or Role not found.")
        return
    update_req = False
    current_unix_time = int(time.time())
    all_db_members = list(lucky_members_collection.find({}, {"_id": 0}))
    existing_member_ids = {
        str(member_data.get("member_id"))
        for member_data in all_db_members
        if member_data.get("member_id")
    }
    recovered_count = await recover_missing_lucky_members(
        guild,
        role_to_sync,
        existing_member_ids,
        current_unix_time,
    )
    if recovered_count:
        update_req = True
        all_db_members = list(lucky_members_collection.find({}, {"_id": 0}))

    if not all_db_members:
        print("DEBUG: No members in lucky DB, updating empty embed.")
        await update_lucky_list_embed(client)
        return 
    ids_to_remove_from_db = [] 
    
    for member_data in all_db_members:
        member_id_str = member_data['member_id']
        member_id_int = int(member_id_str)
        timestamp = member_data['unix_timestamp']
        
        # Check if the member is expired
        is_expired = timestamp <= (current_unix_time - 86400)
        
        # Get the member object from the guild
        member = guild.get_member(member_id_int)
        
        if is_expired:
            # --- Handle EXPIRED members ---
            if member and role_to_sync in member.roles:
                # Member is in the guild and has the role, remove it
                try:
                    await member.remove_roles(role_to_sync, reason="Lucky Member role expired")
                    print(f"Removed lucky role from {member.display_name}")
                except (discord.Forbidden, discord.HTTPException) as e:
                    print(f"Failed to remove role from {member.display_name}: {e}")
                    
            ids_to_remove_from_db.append(member_id_str)
            update_req = True # Need to update embed because someone was removed
            
        else:
            # --- Handle VALID (unexpired) members ---
            if member:
                # Member is in the guild, check if they have the role
                if role_to_sync not in member.roles:
                    # This is your new logic: Re-add the role if missing
                    try:
                        await member.add_roles(role_to_sync, reason="Lucky Member synced")
                        print(f"Re-added missing lucky role to {member.display_name}")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        print(f"Failed to add role to {member.display_name}: {e}")
            else:
                # Member is not in the guild (they left)
                print(f"Member has left the server, removing from DB: {member_id_str}")
                ids_to_remove_from_db.append(member_id_str)
                update_req = True # Need to update embed because someone was removed
                
    # 3. After the loop, do ONE efficient database removal
    if ids_to_remove_from_db:
        print(f"DEBUG: Removing {len(ids_to_remove_from_db)} IDs from database.")
        lucky_members_collection.delete_many({"member_id": {"$in": ids_to_remove_from_db}})
    
    if update_req or ids_to_remove_from_db:
        print("DEBUG: Changes detected, updating embed.")
    else:
        print("DEBUG: No role changes detected, refreshing embed from DB.")
    await update_lucky_list_embed(client)

@tasks.loop(minutes=5)
async def role_remover_loop(client: commands.Bot):
    """Periodically syncs lucky member roles and the hall-of-fame embed."""
    await sync_lucky_members(client)

# --- Initialization & Message Handling ---
def initialize_lucky_member_feature(client: commands.Bot):
    """Initializes the lucky member feature."""
    print("Initializing Lucky Member feature...")
    if not role_remover_loop.is_running():
        role_remover_loop.start(client)
    if not getattr(client, "_lucky_member_initial_sync_started", False):
        client._lucky_member_initial_sync_started = True
        asyncio.create_task(_run_initial_lucky_member_sync(client))


async def _run_initial_lucky_member_sync(client: commands.Bot):
    try:
        await sync_lucky_members(client)
    except Exception as e:
        print(f"Initial lucky member sync failed: {e}")
        await send_webhook_message(f"Initial lucky member sync failed: {e}")

async def handle_lucky_member_message(message: discord.Message, client: commands.Bot):
    """Handles messages to check for lucky member eligibility."""
    if message.author.bot or not message.guild or message.guild.id != GUILD_ID:
        return

    # Basic cooldown to prevent spamming random checks
    user_id = message.author.id
    current_time = time.time()
    if user_id in cooldowns and (current_time - cooldowns[user_id]) < 4:
        return
    cooldowns[user_id] = current_time

    # The chance of becoming a lucky member (0.3%)
    if random.random() < 0.004:
        member = message.author
        lucky_role = message.guild.get_role(LUCKY_MEMBER_ROLE_ID)
        if not lucky_role: return

        # Check if member already has the role or is in the DB
        if lucky_role in member.roles or lucky_members_collection.find_one({"member_id": str(member.id)}):
            return

        new_permissions = await get_newly_granted_permissions(member, lucky_role)
        if not await give_lucky_role(member, message.guild):
            return

        # Store the new lucky member in the database
        now_utc = datetime.now(timezone.utc)
        current_unix_timestamp = int(now_utc.timestamp())
        
        lucky_members_collection.update_one(
            {"member_id": str(member.id)},
            {
                "$set": {
                    "member_id": str(member.id),
                    "username": member.display_name,
                    "unix_timestamp": current_unix_timestamp
                }
            },
            upsert=True,
        )

        # --- Create and Send Congratulations Embed ---
        expiry_timestamp = current_unix_timestamp + 86400
        embed = None
        try:
            embed = discord.Embed(
                title=f"🎁 A Gift of Perks has Arrived!<a:konatahype:1375515481611440159>",
                description=f"Congratulations, {member.mention}! You've been chosen as a **Lucky Member**!\nEnjoy your temporary privileges, which will expire **<t:{expiry_timestamp}:R>**.",
                color=discord.Color.gold(),
                timestamp=now_utc
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text="Your journey continues with new abilities!", icon_url=message.guild.icon.url if message.guild.icon else None)
        except Exception as e:
            print(f"Failed to create embed: {e}")
            await message.channel.send(f"Failed to create embed: {e}")
            return await update_lucky_list_embed(client)
        # Categorize and add permissions
        if new_permissions:
            categorized_perms = {cat: [] for cat in PERMISSION_CATEGORIES}
            uncategorized_perms = []
            for perm in new_permissions:
                found = False
                for cat_name, cat_data in PERMISSION_CATEGORIES.items():
                    if perm in cat_data["permissions"]:
                        categorized_perms[cat_name].append(f"• {perm}")
                        found = True
                        break
                if not found: uncategorized_perms.append(f"• {perm}")
            
            if uncategorized_perms:
                categorized_perms["✨ Miscellaneous Perks"].extend(uncategorized_perms)

            for cat_name, perms_list in categorized_perms.items():
                if perms_list:
                    embed.add_field(name=f"{PERMISSION_CATEGORIES[cat_name]['icon']} **{cat_name}**", value="\n".join(perms_list), inline=False)
        else:
            embed.add_field(name="No Unique Permissions Granted", value="You have been granted new permissions, but none were unique to this role.", inline=False)
        
        try:
            await message.channel.send(embed=embed)
        except discord.Forbidden:
            print(f"Bot lacks permissions to send message in {message.channel.name}")
            await send_webhook_message(f"Bot lacks permissions to send message in {message.channel.name}")
        except discord.HTTPException as e:
            print(f"Failed to send message: {e}")
            await send_webhook_message(f"Failed to send message: {e}")
        
        await update_lucky_list_embed(client)

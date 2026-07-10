import discord
import json
import re
from datetime import datetime, timedelta, timezone
from config import * # Your config.py
import os
import motor.motor_asyncio
import asyncio
from discord.errors import HTTPException
from functions.webhook import send_webhook_message
from pymongo import UpdateOne

# --- NEW MONGODB SETUP ---
MONGO_DB_URL = MONGO_URI
if not MONGO_DB_URL:
    print(f"[{datetime.now()}] 🛑 CRITICAL: MONGO_DB_URL environment variable not set.")
    exit() # Cannot run without this

mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DB_URL)
db = mongo_client["rainy_season_db"]  # Your database name

# Define your collections
members_collection = db["members"]
emojis_collection = db["emojis"]
boosters_collection = db["boosters"]
server_info_collection = db["server_info"]
chat_logs_collection = db["chat_logs"]

print(f"[{datetime.now()}] Connected to MongoDB.")
# --- END MONGODB SETUP ---

def extract_emojis(text, valid_emojis):
    # ... (your function is unchanged) ...
    unicode_emoji_pattern = re.compile("[\U00010000-\U0010FFFF]", flags=re.UNICODE)
    custom_emoji_pattern = re.compile(r"<a?:(\w+):(\d+)>")
    standard_emojis = unicode_emoji_pattern.findall(text)
    custom_emoji_names = [name for name, _ in custom_emoji_pattern.findall(text)]
    valid_custom_emojis_extracted = [name for name in custom_emoji_names if name in valid_emojis]
    return standard_emojis + valid_custom_emojis_extracted

def is_valid_message(message):
    # ... (your function is unchanged) ...
    invalid_starts = tuple(invalid_message_starts) if isinstance(invalid_message_starts, list) else invalid_message_starts
    return (
        not message.author.bot and
        not message.content.strip().startswith(invalid_starts) and
        len(message.content.strip()) > 1
    )

# --- REFACTORED FOR MONGODB ---
# This is your fast, 15-second loop
async def update_status_and_activities(client):
    await client.wait_until_ready()
    # print(f"[{datetime.now()}] Running quick status/activity update...") # Optional: too spammy for 15s
    
    guild = client.get_guild(GUILD_ID)
    if not guild or not guild.chunked:
        # print(f"[{datetime.now()}] Guild {GUILD_ID} not found or not chunked. Skipping status update.")
        return

    bulk_operations = []
    for m in guild.members:
        try:
            activities = [{
                'name': a.name, 'type': str(a.type).split('.')[-1],
                'details': getattr(a, 'details', None), 'state': getattr(a, 'state', None),
                'emoji': str(getattr(a, 'emoji', None)) if getattr(a, 'emoji', None) else None,
                'url': getattr(a, 'url', None)
            } for a in m.activities] if m.activities else []

            # This is a lightweight $set operation
            # It only touches these specific fields
            updated_fields = {
                'status': str(m.status),
                'desktop_status': str(m.desktop_status),
                'mobile_status': str(m.mobile_status),
                'web_status': str(m.web_status),
                'activities': activities
            }
            
            bulk_operations.append(
                UpdateOne(
                    {"id": str(m.id)},              # Filter by user ID
                    {"$set": updated_fields},       # Only set these fields
                    upsert=True                     # Create member if they don't exist
                )
            )
        except Exception as e:
            print(f"[{datetime.now()}] Error in lightweight status update for {m.name}: {e}")

    # Run the bulk write operation
    if bulk_operations:
        try:
            await members_collection.bulk_write(bulk_operations, ordered=False)
            # print(f"[{datetime.now()}] Quick status update complete for {len(bulk_operations)} members.")
        except Exception as e:
            print(f"[{datetime.now()}] ERROR in lightweight status bulk_write: {e}")
            await send_webhook_message(f"ERROR in lightweight status bulk_write: {e}")
    
    # print(f"[{datetime.now()}] Status & activities updated for {len(bulk_operations)} members.")


# --- REFACTORED FOR MONGODB ---
# This is your slow, hourly/daily loop
async def fetch_and_save_data(client):
    await client.wait_until_ready()
    print(f"[{datetime.now()}] Starting HEAVY data fetch and save for guild {GUILD_ID}...")
    
    custom_emojis = []
    server_metadata = {}  # <--- THIS WAS THE MISSING LINE
    
    guild = client.get_guild(GUILD_ID)
    if guild is None:
        print(f"[{datetime.now()}] Error: Guild with ID {GUILD_ID} not found.")
        await send_webhook_message(f"Error: Guild with ID {GUILD_ID} not found.")
        return

    if not guild.chunked:
        print(f"[{datetime.now()}] Guild not chunked. Chunking now...")
        await guild.chunk()
        print(f"[{datetime.now()}] Guild chunking complete.")
    
    # --- 1. Emojis (Simple rewrite) ---
    try:
        for e in guild.emojis:
            custom_emojis.append({"name": e.name, "url": str(e.url), "id": str(e.id), "animated": e.animated})
        
        await emojis_collection.delete_many({}) # Clear old data
        if custom_emojis:
            await emojis_collection.insert_many(custom_emojis) # Insert new
        
        valid_custom_emojis_names = {e['name'] for e in custom_emojis}
        print(f"[{datetime.now()}] Saved {len(custom_emojis)} emojis to MongoDB.")
    except Exception as e:
        valid_custom_emojis_names = set()
        print(f"[{datetime.now()}] Error saving emojis: {e}")
        await send_webhook_message(f"Error saving emojis: {e}")


    # --- 2. Members (Full data update) ---
    print(f"[{datetime.now()}] Processing {len(guild.members)} members for full update...")
    rain_keeper_role_obj = discord.utils.get(guild.roles, id=rainkeeper_id)
    
    OWNER_ROLE_NAMES = ["Rainmaster (Owner)"]
    ADMIN_ROLE_NAMES = ["Skywatcher (Admin)"]
    MOD_ROLE_NAMES = ["Cloudstaff (Mod)"]
    LEVEL_ROLE_PATTERN = re.compile(r"^(.+?)・Lv(\d+)$")
    GENDER_PRONOUN_ROLES = ["He/Him", "She/Her", "They/Them", "Ask me my Pronoun(s)"]
    COMMON_MEMBER_ROLES_TO_EXCLUDE = ["Rainfolk (Member)", "Cloud Booster", "@everyone"]
    
    member_bulk_operations = []
    
    for m in guild.members: # Use guild.members (from cache) instead of get_all_members()
        try:
            member_roles_list = []
            is_owner = (m.id == guild.owner_id)
            _has_admin_role_found = False
            _has_mod_role_found = False
            is_rain_keeper = False
            level_role = None
            gender_pronouns = None
            extracted_role_ids = set()

            # --- Role processing logic (unchanged) ---
            for role in m.roles:
                if role.name in COMMON_MEMBER_ROLES_TO_EXCLUDE:
                    extracted_role_ids.add(role.id)
                    continue
                if role.name in OWNER_ROLE_NAMES:
                    extracted_role_ids.add(role.id)
                if role.name in ADMIN_ROLE_NAMES:
                    _has_admin_role_found = True
                    extracted_role_ids.add(role.id)
                elif role.name in MOD_ROLE_NAMES:
                    _has_mod_role_found = True
                    extracted_role_ids.add(role.id)
                if rain_keeper_role_obj and role.id == rain_keeper_role_obj.id:
                    is_rain_keeper = True
                    extracted_role_ids.add(role.id)
                if gender_pronouns is None and role.name in GENDER_PRONOUN_ROLES:
                    gender_pronouns = role.name
                    extracted_role_ids.add(role.id)
                if level_role is None and LEVEL_ROLE_PATTERN.match(role.name):
                    level_role = role.name
                    extracted_role_ids.add(role.id)

            is_admin = _has_admin_role_found and not is_owner
            is_mod = _has_mod_role_found and not is_owner and not is_admin

            for role in m.roles:
                if role.id not in extracted_role_ids:
                    member_roles_list.append(role.name)
            # --- End role processing ---

            now = datetime.now(timezone.utc)
            
            # This document has the "deep" data
            member_doc = {
                'id': str(m.id),
                'name': m.name,
                'global_name': m.global_name or m.name,
                'display_name': m.display_name,
                'avatar': str(m.display_avatar.url),
                'banner': str(m.display_banner.url) if m.display_banner else None,
                'accent_color': str(m.accent_color) if m.accent_color else None,
                'joined_at': m.joined_at.isoformat() if m.joined_at else None,
                'account_created_at': m.created_at.isoformat(),
                'is_owner': is_owner,
                'is_admin': is_admin,
                'is_mod': is_mod,
                'is_rain_keeper': is_rain_keeper,
                'level_role': level_role,
                'gender_pronouns': gender_pronouns,
                'is_booster': bool(m.premium_since),
                'boosted_since': m.premium_since.isoformat() if m.premium_since else None,
                'timed_out_until': m.timed_out_until.isoformat() if (m.timed_out_until and m.timed_out_until > now) else None,
                'public_flags': m.public_flags.value,
                'guild_permissions_value': m.guild_permissions.value,
                'top_role_name': m.top_role.name if m.top_role else None,
                'roles': member_roles_list,
            }
            
            # This $set operation will *merge* with the status fields
            # set by the lightweight function.
            member_bulk_operations.append(
                UpdateOne(
                    {"id": str(m.id)},     # Filter by user ID
                    {"$set": member_doc},  # Set all the deep fields
                    upsert=True            # Create if not found
                )
            )

        except Exception as e:
            print(f"[{datetime.now()}] ERROR processing member {m.name} ({m.id}): {e}")
            await send_webhook_message(f"ERROR processing member {m.name} ({m.id}): {e}")

    if member_bulk_operations:
        try:
            await members_collection.bulk_write(member_bulk_operations, ordered=False)
            print(f"[{datetime.now()}] Upserted {len(member_bulk_operations)} members (full data) to MongoDB.")
        except Exception as e:
            print(f"[{datetime.now()}] ERROR in member full data bulk_write: {e}")
            await send_webhook_message(f"ERROR in member full data bulk_write: {e}")

    # --- 3. Boosters (Simple rewrite) ---
    try:
        boosters = [m for m in guild.members if m.premium_since]
        booster_data = [{"id": str(m.id), "name": str(m.display_name), "avatar": str(m.display_avatar.url), "boosted_since": m.premium_since.isoformat()} for m in boosters]
        sorted_booster_data = sorted(booster_data, key=lambda x: datetime.fromisoformat(x["boosted_since"]))
        
        await boosters_collection.delete_many({}) # Clear and rewrite
        if sorted_booster_data:
            await boosters_collection.insert_many(sorted_booster_data)
        print(f"[{datetime.now()}] Saved {len(sorted_booster_data)} boosters to MongoDB.")
    except Exception as e:
        print(f"[{datetime.now()}] Error saving boosters: {e}")

    # --- 4. Chat Logs (INCREMENTAL FETCH - THE BIG FIX) ---
    print(f"[{datetime.now()}] Starting incremental chat log fetch...")
    total_new_messages = 0

    for i, channel in enumerate(guild.text_channels):
        if channel.id in EXCLUDED_CHANNEL_IDS:
            continue
            
        try:
            # PROACTIVE SLEEP between channels
            if i > 0:
                print(f"[{datetime.now()}] --- PROACTIVE SLEEP --- Waiting 5 minutes before next channel...")
                await asyncio.sleep(300) 
        
            print(f"[{datetime.now()}] Starting fetch for #{channel.name}")
            
            # 1. Find the MOST RECENT message we have for this channel
            last_message = await chat_logs_collection.find_one(
                {"channel_id": str(channel.id)},
                sort=[("timestamp", -1)] # -1 = descending
            )
            
            # 2. Set 'after' date
            if last_message:
                after_date = datetime.fromisoformat(last_message['timestamp'])
                print(f"[{datetime.now()}]     ... fetching new messages after {after_date}")
            else:
                after_date = None#(datetime.now(timezone.utc) - timedelta(days=30)).replace(microsecond=0)
                print(f"[{datetime.now()}]     ... no data. Fetching ALL messages from the beginning.")

            message_count_in_channel = 0
            log_bulk_operations = []
            
            # 3. Use the history iterator
            async for message in channel.history(limit=None, after=after_date):
                if not is_valid_message(message):
                    continue
                
                member_obj = guild.get_member(message.author.id)
                message_data = {
                    "message_id": str(message.id), # <-- ADDED for unique key
                    "channel": channel.name,
                    "channel_id": str(channel.id),
                    "author": str(message.author),
                    "user_id": str(message.author.id),
                    "avatar": str(message.author.display_avatar.url),
                    "roles": [r.name for r in member_obj.roles if r.name != "@everyone"] if isinstance(member_obj, discord.Member) else [],
                    "is_booster": bool(member_obj.premium_since) if isinstance(member_obj, discord.Member) else False,
                    "content": message.content,
                    "timestamp": message.created_at.isoformat(),
                    "emojis": extract_emojis(message.content, valid_custom_emojis_names)
                }
                
                # 4. Add to bulk list
                log_bulk_operations.append(
                    UpdateOne(
                        {"message_id": str(message.id)},
                        {"$set": message_data},
                        upsert=True
                    )
                )
                
                message_count_in_channel += 1
                total_new_messages += 1

                # 5. SAFETY SLEEP (Critical for first scrape)
                # Also, write to DB in chunks of 500 to avoid memory issues
                if (message_count_in_channel % 500 == 0):
                    if log_bulk_operations: # Make sure list is not empty
                        await chat_logs_collection.bulk_write(log_bulk_operations, ordered=False)
                        log_bulk_operations = [] # Clear the list
                    print(f"[{datetime.now()}]     ... saved {message_count_in_channel} messages, sleeping for 2s ...")
                    await asyncio.sleep(2) 

            # 6. Save any remaining messages
            if log_bulk_operations:
                await chat_logs_collection.bulk_write(log_bulk_operations, ordered=False)

            print(f"[{datetime.now()}] Finished processing {message_count_in_channel} new messages from #{channel.name}")

        except discord.errors.Forbidden:
            print(f"[{datetime.now()}] Warning: Missing permissions in #{channel.name} ({channel.id}). Skipping.")
        except HTTPException as e:
            if e.status == 429:
                print(f"🛑 CRITICAL: API Rate Limit/Cloudflare 1015 hit in #{channel.name}. Aborting task.")
                retry_after = int(e.response.headers.get("Retry-After", 900))
                print(f"[{datetime.now()}] Waiting for {(retry_after / 60):.2f} minutes...")
                await send_webhook_message(f"🛑 CRITICAL: API Rate Limit hit in #{channel.name}. Waiting for {(retry_after / 60):.2f}mins")
                await asyncio.sleep(retry_after)
                continue
            else:
                print(f"[{datetime.now()}] HTTP Error in #{channel.name} ({channel.id}): {e}")
        except Exception as e:
            print(f"[{datetime.now()}] Unknown error in #{channel.name} ({channel.id}): {e}")

    print(f"[{datetime.now()}] Saved/Updated {total_new_messages} total new chat logs to MongoDB.")


    # --- 5. Server Info (Simple rewrite) ---
    try:
        server_metadata.update({
            "name": guild.name,
            "description": guild.description,
            "icon": guild.icon.url if guild.icon else None,
            "total_users": guild.member_count,
            "active_users": len([m for m in guild.members if m.status != discord.Status.offline and not m.bot]),
            "banner": guild.banner.url if guild.banner else None,
            "splash": guild.splash.url if guild.splash else None,
            "discovery_splash": guild.discovery_splash.url if guild.discovery_splash else None
        })

        rain_keeper_role = discord.utils.get(guild.roles, id=rainkeeper_id)
        server_metadata["rain_keeper_count"] = len([m for m in guild.members if rain_keeper_role in m.roles]) if rain_keeper_role else 0
        
        server_metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        
        # Get invite code
        server_metadata["invite_code"] = None
        if guild.vanity_url_code: # Use vanity code if available
            server_metadata["invite_code"] = guild.vanity_url_code
        else:
            try:
                # Fallback to creating an invite in a specific channel
                invite_channel = guild.get_channel(1369397376745345056) # Make sure this ID is correct
                if invite_channel:
                    invite_code = await invite_channel.create_invite(max_age=0, max_uses=0, unique=False)
                    server_metadata["invite_code"] = invite_code.code if invite_code else None
            except Exception as e:
                print(f"[{datetime.now()}] Could not create fallback invite: {e}")

        # Use replace_one with upsert=True to update the single server info doc
        await server_info_collection.replace_one(
            {"_id": "server_config"}, # Use a fixed, known ID
            server_metadata,          # The new data
            upsert=True               # Create it if it doesn't exist
        )
        print(f"[{datetime.now()}] Saved server info to MongoDB.")

    except Exception as e:
        print(f"[{datetime.now()}] Error saving server_info: {e}")
        await send_webhook_message(f"Error saving server_info: {e}")

    print(f"[{datetime.now()}] ✅ All data fetching and saving complete.")
    await send_webhook_message(f"✅ All data fetching and saving complete.")
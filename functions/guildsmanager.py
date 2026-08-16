import requests
# from tinydb import TinyDB, Query  <-- No longer needed
from pymongo import MongoClient, TEXT
from pymongo.errors import ConnectionFailure, DuplicateKeyError
import os
import time
import datetime
import sys
from config import MONGO_URI
# import re # <-- No longer needed, $regex handles this

# --- Configuration & Setup ---
# --- NEW: MongoDB Configuration ---
# Set this in your server's environment variables for security
DB_NAME = "guild_tag_db"
COLLECTION_NAME = "guilds" #Default
#COLLECTION_NAME = "sorted_guilds" #pipeline | sorted
try:
    # 1. Connect to the MongoDB server
    client = MongoClient(MONGO_URI)
    
    # 2. Ping the server to check the connection
    client.admin.command('ping') 
    
    # 3. Select your database and collection
    db = client[DB_NAME]
    guilds_collection = db[COLLECTION_NAME] # This is your new 'db' object
    
    print(f"--- MongoDB connection successful! Connected to '{DB_NAME}'. ---")

    # 4. Set up database indexes (this is the magic of MongoDB)
    # This will run once and be ignored if the indexes already exist.
    print("Ensuring database indexes exist...")
    # Unique index on serverID to prevent duplicate guilds
    guilds_collection.create_index("serverID", unique=True)
    # Indexes to make searching by tag and name extremely fast
    guilds_collection.create_index("tag")
    guilds_collection.create_index("name")
    print("Indexes are set.")

except ConnectionFailure as e:
    print(f"--- FATAL: MongoDB connection failed. ---")
    print("Please check your MONGO_URI environment variable.")
    print(f"Error details: {e}")
    sys.exit(1) # Exit the script if we can't connect


INVITE_BASE_URL = "https://discord.com/api/v10/invites/"

# Setup a persistent session for requests
session = requests.Session()
session.headers.update({"User-Agent": "MyGuildBot (v1.0)"})

# --- Status Codes (Unchanged) ---
SUCCESS_ADDED = 1
SUCCESS_UPDATED = 2
INFO_ALREADY_EXISTS = 0
ERR_NO_TAG = -1
ERR_INVITE_INVALID = -2
ERR_RATELIMITED = -3
ERR_REQUEST_FAILED = -4

VALIDATION_STATUS_LABELS = {
    SUCCESS_ADDED: "valid",
    ERR_NO_TAG: "missing tag",
    ERR_INVITE_INVALID: "invalid invite",
    ERR_RATELIMITED: "rate limited",
    ERR_REQUEST_FAILED: "request failed",
}


# --- Helper Functions ---

def get_guild_count():
    """Returns the total number of guilds in the database."""
    # OLD: return len(db)
    return guilds_collection.count_documents({}) # <-- NEW

def safe_print(message: str) -> None:
    """
    Helper function to print unicode text safely to the Windows console.
    (Unchanged)
    """
    try:
        print(message)
    except UnicodeEncodeError:
        safe_message = message.encode(sys.stdout.encoding, errors='replace').decode(sys.stdout.encoding)
        print(safe_message)

def get_asset_url(asset_type, guild_id, asset_hash):
    """
    Builds a Discord CDN URL from an asset hash.
    (Unchanged)
    """
    if not asset_hash:
        return None
    extension = "gif" if asset_hash.startswith("a_") else "png"
    return f"https://cdn.discordapp.com/{asset_type}/{guild_id}/{asset_hash}.{extension}"

def _format_change(field_name: str, old_value, new_value) -> str:
    labels = {
        "name": "name",
        "members": "members",
        "icon": "icon",
        "banner": "banner",
        "description": "description",
        "serverID": "server ID",
        "value": "invite",
        "tag": "tag",
        "tagHash": "tag badge",
    }

    if field_name in {"icon", "banner", "tagHash"}:
        if old_value and new_value:
            return f"{labels.get(field_name, field_name)} changed"
        if new_value:
            return f"{labels.get(field_name, field_name)} added"
        return f"{labels.get(field_name, field_name)} removed"

    return f"{labels.get(field_name, field_name)}: {old_value!r} -> {new_value!r}"

def fetch_invite_data(invite_code: str):
    """
    Fetches and processes invite data.
    Returns a (status_code, data_dict) tuple.
    (Unchanged - This logic is perfect)
    """
    if not invite_code:
        return (ERR_INVITE_INVALID, None)
        
    url = f"{INVITE_BASE_URL}{invite_code}?with_counts=true"
    
    try:
        response = session.get(url)

        if response.status_code == 200:
            invite_data = response.json()
            guild_data = invite_data.get('guild')
            profile_data = invite_data.get('profile') # This is the "tag" info

            if not guild_data:
                return (ERR_INVITE_INVALID, None)
            
            if not profile_data or not profile_data.get('tag'):
                return (ERR_NO_TAG, None)
            
            server_id_str = guild_data.get('id')
            
            formatted_data = {
                "name": guild_data.get('name'),
                "members": invite_data.get('approximate_member_count'),
                "icon": get_asset_url("icons", server_id_str, guild_data.get('icon')),
                "banner": get_asset_url("banners", server_id_str, guild_data.get('banner')),
                "description": guild_data.get('description'),
                "serverID": server_id_str,
                "value": invite_code, # Save the invite code that worked
                "tag": profile_data.get('tag'),
                "tagHash": profile_data.get('badge_hash'),
                "lastFetch": int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
            }
            return (SUCCESS_ADDED, formatted_data)

        elif response.status_code == 404:
            return (ERR_INVITE_INVALID, None)
        elif response.status_code == 429:
            safe_print("--- RATE LIMITED ---")
            return (ERR_RATELIMITED, None)
        else:
            return (ERR_REQUEST_FAILED, None)
            
    except requests.exceptions.RequestException as e:
        safe_print(f"Network error for invite {invite_code}: {e}")
        return (ERR_REQUEST_FAILED, None)


# --- New Function (Converted to MongoDB) ---

def add_guild_tag(new_invite_code: str):
    """
    Adds a guild to the DB if it has a tag and doesn't exist.
    Updates the invite code if it exists and the old one is invalid.
    Returns an integer status code.
    """
    
    # 1. Check the new invite code (Unchanged)
    status, new_data = fetch_invite_data(new_invite_code)

    # 2. Handle all failure cases for the new invite (Unchanged)
    if status != SUCCESS_ADDED:
        return status
    
    if new_data is None:
        return ERR_REQUEST_FAILED

    server_id = new_data['serverID']
    
    # 3. Check if this guild (by serverID) is already in the database
    # OLD: existing_guild = db.get(Guild.serverID == server_id)
    existing_guild = guilds_collection.find_one({"serverID": server_id}) # <-- NEW
    
    if not existing_guild:
        # --- Guild is new ---
        # Add the 'created' timestamp
        new_data['created'] = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
        try:
            # OLD: db.insert(new_data)
            guilds_collection.insert_one(new_data) # <-- NEW
            return SUCCESS_ADDED
        except DuplicateKeyError:
            # This is a rare race condition, but good to handle.
            # It means another process added this guild *just* as we were.
            # We can just treat it as if it already existed.
            return INFO_ALREADY_EXISTS
    
    # --- Guild already exists ---
    # 4. Check if the *existing* invite code is still valid
    
    old_invite_code = existing_guild.get('value')
    
    # Optimization (Unchanged)
    if old_invite_code == new_invite_code:
        return INFO_ALREADY_EXISTS

    old_status, _ = fetch_invite_data(old_invite_code)
    
    if old_status == ERR_INVITE_INVALID:
        # Old invite is bad, so update the guild with the new data
        safe_print(f"Updating invite for {new_data['name']} ({server_id}). Old: {old_invite_code}, New: {new_invite_code}")
        
        # We use $set to update the fields from new_data,
        # which preserves other fields like the original 'created' timestamp.
        # OLD: db.update(new_data, Guild.serverID == server_id)
        guilds_collection.update_one(
            {"serverID": server_id},  # Filter: Find the doc with this serverID
            {"$set": new_data}        # Update: Set the fields from new_data
        ) # <-- NEW
        
        return SUCCESS_UPDATED
    
    elif old_status == SUCCESS_ADDED:
        # Old invite is still valid, so do nothing (Unchanged)
        return INFO_ALREADY_EXISTS
        
    else:
        # We were rate-limited or failed while checking the *old* code. (Unchanged)
        return old_status

def create_validation_report():
    return {
        "checked": 0,
        "updated": 0,
        "removed": 0,
        "failed": 0,
        "changes": [],
        "removed_entries": [],
        "failures": [],
    }

def merge_validation_report(target: dict, source: dict):
    for key in ("checked", "updated", "removed", "failed"):
        target[key] = target.get(key, 0) + source.get(key, 0)
    for key in ("changes", "removed_entries", "failures"):
        target.setdefault(key, []).extend(source.get(key, []))
    return target

def validate_guild_for_search(guild: dict, search_tag: str = None):
    """
    Refreshes one searched guild record from Discord invite data.
    Returns (valid_guild_or_none, report). If the guild changed to a
    different tag, MongoDB is updated but the guild is hidden from this search.
    """
    report = create_validation_report()
    report["checked"] = 1

    comparable_fields = [
        "name",
        "members",
        "icon",
        "banner",
        "description",
        "serverID",
        "value",
        "tag",
        "tagHash",
    ]

    invite_code = guild.get("value")
    server_id = guild.get("serverID")
    display_name = guild.get("name") or server_id or invite_code or "Unknown guild"

    if not invite_code:
        report["removed"] += 1
        report["removed_entries"].append(f"{display_name}: removed because no invite code is stored")
        if server_id:
            guilds_collection.delete_one({"serverID": server_id})
        return None, report

    status, fresh_data = fetch_invite_data(invite_code)

    if status == SUCCESS_ADDED and fresh_data:
        changes = [
            _format_change(field, guild.get(field), fresh_data.get(field))
            for field in comparable_fields
            if guild.get(field) != fresh_data.get(field)
        ]

        update_data = dict(fresh_data)
        if guild.get("created"):
            update_data["created"] = guild.get("created")

        guilds_collection.update_one(
            {"serverID": server_id},
            {"$set": update_data},
            upsert=True
        )

        if changes:
            report["updated"] += 1
            report["changes"].append(f"{display_name}: " + "; ".join(changes))

        refreshed_tag = update_data.get("tag") or ""
        if search_tag and search_tag.casefold() not in refreshed_tag.casefold():
            report["removed"] += 1
            report["removed_entries"].append(
                f"{display_name}: hidden from `{search_tag}` because tag changed to `{refreshed_tag}`"
            )
            return None, report

        return update_data, report

    status_label = VALIDATION_STATUS_LABELS.get(status, f"status {status}")

    if status in {ERR_NO_TAG, ERR_INVITE_INVALID}:
        report["removed"] += 1
        report["removed_entries"].append(f"{display_name}: removed because {status_label}")
        if server_id:
            guilds_collection.delete_one({"serverID": server_id})
        return None, report

    report["failed"] += 1
    report["failures"].append(f"{display_name}: kept cached result because validation failed ({status_label})")
    return guild, report

def validate_guild_search_results(guilds: list, search_tag: str = None):
    """
    Refreshes searched guild records from Discord invite data.
    Returns (valid_guilds, report) where report is meant for private logging.
    """
    valid_guilds = []
    report = create_validation_report()

    for guild in guilds:
        valid_guild, guild_report = validate_guild_for_search(guild, search_tag)
        merge_validation_report(report, guild_report)
        if valid_guild:
            valid_guilds.append(valid_guild)

    return valid_guilds, report


# --- Search Functions (Converted to MongoDB) ---

def search_guild_by_tag(tag_query: str):
    """
    Searches the database for guilds matching a specific tag (case-insensitive).
    """
    print(f"Searching for tag: {tag_query}") # For debugging
    
    # OLD: results = db.search(Guild.tag.search(tag_query, flags=re.IGNORECASE))
    
    # NEW: Use a regex query with the 'i' option for case-insensitivity.
    # The index we created on "tag" will make this fast.
    results = guilds_collection.find({
        "tag": {"$regex": tag_query, "$options": "i"}
    }).sort("created", 1)
    
    # We convert the 'cursor' (iterator) to a list to match TinyDB's behavior
    return list(results) 

def search_guild_by_name(name_query: str):
    """
    Searches for guilds where the name contains the query string (case-insensitive).
    """
    # OLD: results = db.search(Guild.name.search(name_query, flags=re.IGNORECASE))
    
    # NEW:
    results = guilds_collection.find({
        "name": {"$regex": name_query, "$options": "i"}
    }).sort("created", 1)
    return list(results)

def get_guild_by_id(server_id: str):
    """
    Gets a single guild by its unique serverID.
    """
    # OLD: result = db.get(Guild.serverID == server_id)
    result = guilds_collection.find_one({"serverID": server_id}) # <-- NEW
    return result

def get_guild_by_invite_code(invite_code: str):
    """
    Gets a single guild by its stored invite code.
    """
    result = guilds_collection.find_one({"value": invite_code})
    return result

def get_guild_by_invite_code_or_resolved_server(invite_code: str):
    """
    Gets a guild by stored invite code. If that exact invite is not stored,
    resolves the invite through Discord and finds the guild by serverID.
    """
    result = get_guild_by_invite_code(invite_code)
    if result:
        return result

    status, invite_data = fetch_invite_data(invite_code)
    if status == SUCCESS_ADDED and invite_data:
        return get_guild_by_id(invite_data.get("serverID"))

    return None

# --- Example of how to use it in main.py (Unchanged) ---
# if __name__ == "__main__":
    #search by tag
    # results = search_guild_by_tag("rain")
    # for r in results:
    #     safe_print(str(r['name'])+" "+str(r['value']+" "+str(r['tag'])))
    
    # --- Test Search ---
    # Example 1: A valid invite with a tag (replace 'valorant' with a real one)
    # code = "valorant" 
    # result = add_guild_tag(code)
    # safe_print(f"Result for '{code}': {result}")

    # Example 2: An invite that's invalid
    # code = "invalidcode123xyz"
    # result = add_guild_tag(code)
    # safe_print(f"Result for '{code}': {result}")

    # Example 3: A valid invite but for a server with no tag
    # code = "35" # (Minecraft doesn't have a tag)
    # result = add_guild_tag(code)
    # safe_print(f"Result for '{code}': {result}")
    
    # print("Done")


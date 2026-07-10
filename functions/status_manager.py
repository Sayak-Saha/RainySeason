import discord
import asyncio
import time
from discord.ext import tasks, commands
from config import *
from datetime import datetime, timedelta
# --- Configuration ---
# ❗️ CHANGE 1: Replaced the role name variable with a role ID variable.
# You need to replace the placeholder numbers with your actual role ID.
RAIN_KEEPER_ROLE_ID = rainkeeper_id # ᐊ-- Replace with your actual "Rain Keeper" role ID
AUTOMOD_RULE_ID = 1370702577297133653

class StatusManager(commands.Cog): # Assuming this is a Cog
    def __init__(self, bot):
        self.bot = bot
        self.priority_task = None
        self.last_message_update_time = 0
        # self.default_status_loop.start() # Ensure this is defined elsewhere
        
        # Cache settings
        self._automod_cache = None
        self._cache_expiry = datetime.min
        self.AUTOMOD_RULE_ID = AUTOMOD_RULE_ID  # Replace with your actual ID

    async def get_automod_config(self, guild):
        """Retrieves cached rules or fetches them if expired (e.g., every 10 mins)."""
        if self._automod_cache and datetime.now() < self._cache_expiry:
            return self._automod_cache

        try:
            # Fetch specifically to rebuild cache
            all_rules = await guild.fetch_automod_rules()
            target_rule = discord.utils.get(all_rules, id=self.AUTOMOD_RULE_ID)
            
            if target_rule:
                self._update_local_cache(target_rule)
                return self._automod_cache
        except discord.Forbidden:
            print("Error: Bot lacks 'Manage Server' permission.")
        except Exception as e:
            print(f"Failed to fetch AutoMod rules: {e}")
        
        return None

    def _update_local_cache(self, rule):
        """Helper to format and store the rule data."""
        self._automod_cache = {
            "blocked": [w.lower() for w in rule.trigger.keyword_filter],
            "allowed": [w.lower() for w in rule.trigger.allow_list]
        }
        self._cache_expiry = datetime.now() + timedelta(minutes=10)
        print(f"AutoMod cache refreshed for rule: {rule.name}")
    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule):
        """
        Listens for changes to AutoMod rules. 
        If our target rule is updated, we invalidate/refresh the cache immediately.
        """
        if rule.id == self.AUTOMOD_RULE_ID:
            self._update_local_cache(rule)
            
    async def is_nsfw(self, name: str, guild: discord.Guild):
        config = await self.get_automod_config(guild)
        if not config:
            return False
        
        lower_name = name.lower()
        blocked_keywords = config["blocked"]
        allowed_keywords = config["allowed"]
        
        # 1. Identify which blocked words are present in the string
        found_blocked_words = [word for word in blocked_keywords if word in lower_name]
        if not found_blocked_words:
            return False
        
        # 2. Identify which allowed words are present in the string
        found_allowed_words = [word for word in allowed_keywords if word in lower_name]
        
        # 3. Check if any blocked word is NOT 'covered' by an allowed word
        for b_word in found_blocked_words:
            if not any(b_word in a_word for a_word in found_allowed_words):
                return True 
        return False

    @tasks.loop(minutes=10)
    async def default_status_loop(self):
        """The default status loop that runs when no priority task is active."""
        if self.priority_task and not self.priority_task.done():
            return

        guild = self.bot.guilds[0]
        if not guild:
            return

        # ❗️ CHANGE 2: Fetch the role directly by its ID. This is faster and more reliable.
        rainkeeper_role = guild.get_role(RAIN_KEEPER_ROLE_ID)
        
        if rainkeeper_role:
            member_count = len(rainkeeper_role.members)
            # ❗️ CHANGE 3: Use the role object's name for the status.
            # This ensures the status always shows the current role name.
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{member_count} people under the umbrella ☔"
            )
            await self.bot.change_presence(activity=activity)

    @default_status_loop.before_loop
    async def before_default_status_loop(self):
        """Ensures the bot is ready before the loop starts."""
        await self.bot.wait_until_ready()

    async def set_priority_status(self, activity, duration):
        """Sets a temporary, high-priority status and reverts after a duration."""
        if self.priority_task and not self.priority_task.done():
            self.priority_task.cancel()

        await self.bot.change_presence(activity=activity)
        self.priority_task = asyncio.create_task(self.revert_after_delay(duration))

    async def revert_after_delay(self, delay):
        """Waits for a specified duration and then allows the default loop to resume."""
        await asyncio.sleep(delay)
        self.priority_task = None
        self.default_status_loop.restart()

    # --- Event Handlers ---
    async def on_new_member(self, member):
        """Handles the new member spotlight status."""
        if await self.is_nsfw(member.display_name, member.guild):
            print(f"Skipped welcome status for NSFW name based on AutoMod: {member.display_name}")
            return
        current_time = time.time()
        if current_time - self.last_message_update_time < MESSAGE_STATUS_COOLDOWN:
            return
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{member.display_name} join the server! 👋"
        )
        self.last_message_update_time = current_time
        await self.set_priority_status(activity, duration=300)

    async def on_new_message(self, message):
        """Handles the 'last user to chat' status."""
        if not message.guild or message.author.bot:
            return
        current_time = time.time()
        if current_time - self.last_message_update_time < MESSAGE_STATUS_COOLDOWN:
            return
        if await self.is_nsfw(message.author.display_name, message.guild):
            print(f"Skipped welcome status for NSFW name based on AutoMod: {message.author.display_name}")
            return
        everyone_role = message.guild.default_role
        permissions = message.channel.permissions_for(everyone_role)
        if not permissions.view_channel:
            return
        activity = discord.Activity(
            type=discord.ActivityType.watching,
            name=f"{message.author.display_name} in #{message.channel.name}"
        )
        self.last_message_update_time = current_time
        await self.set_priority_status(activity, duration=120)
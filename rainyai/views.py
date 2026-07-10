import asyncio
import time

import discord
from discord import ButtonStyle, Interaction
from discord.ui import Button, View

from config import AI_STATUS_REFRESH_GUILD_COOLDOWN_SECONDS
from functions.ask_ai import get_ai_limit_status

from rainyai.core import (
    AI_STATUS_GUILD_COOLDOWNS,
    TAG_EMBED_PURPLE,
    TAG_PENDING_STATUS,
    TAG_VERIFIED_STATUS,
    TAG_VERIFYING_STATUS,
    discord_id_to_unix,
    discord_timestamp,
    format_member_count,
    milliseconds_to_unix,
)


class GroqStatusView(View):
    _cooldowns = {}
    COOLDOWN_SECONDS = 25

    @discord.ui.button(label="Refresh Status", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        user_id = interaction.user.id
        current_time = time.time()
        guild_id = interaction.guild_id

        last_used = self._cooldowns.get(user_id, 0)
        time_elapsed = current_time - last_used

        if time_elapsed < self.COOLDOWN_SECONDS:
            remaining = self.COOLDOWN_SECONDS - time_elapsed
            await interaction.response.send_message(
                f"⏳ You are on cooldown! Try refreshing again <t:{int(current_time + remaining)}:R>.",
                ephemeral=True,
            )
            return
        if guild_id:
            guild_last_used = AI_STATUS_GUILD_COOLDOWNS.get(guild_id, 0.0)
            guild_elapsed = current_time - guild_last_used
            if guild_elapsed < AI_STATUS_REFRESH_GUILD_COOLDOWN_SECONDS:
                remaining = AI_STATUS_REFRESH_GUILD_COOLDOWN_SECONDS - guild_elapsed
                await interaction.response.send_message(
                    f"⏳ Status refresh is on guild cooldown. Try again <t:{int(current_time + remaining)}:R>.",
                    ephemeral=True,
                )
                return
        self._cooldowns[user_id] = current_time
        if guild_id:
            AI_STATUS_GUILD_COOLDOWNS[guild_id] = current_time

        await interaction.response.defer()
        button.disabled = True
        await interaction.edit_original_response(view=self)
        embed = None
        try:
            embed = await asyncio.to_thread(get_ai_limit_status)
            button.disabled = False
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception as e:
            embed = discord.Embed(
                title="❌ Refresh Failed",
                description=f"Status check failed: `{e.__class__.__name__}`. Try again.",
                color=0xAA0000,
            )
        button.disabled = False
        await interaction.edit_original_response(embed=embed, view=self)


class PaginatorView(View):
    def __init__(self, guilds_list: list, search_tag: str):
        super().__init__(timeout=300)
        self.guilds = guilds_list
        self.search_tag = search_tag
        self.current_page = 0
        self.validation_status_by_server = {}
        self.validation_priority = []
        for guild in self.guilds:
            server_id = guild.get("serverID")
            if server_id:
                self.validation_status_by_server[server_id] = TAG_PENDING_STATUS
                self.validation_priority.append(server_id)
        if self.guilds and self.guilds[0].get("serverID"):
            self.validation_status_by_server[self.guilds[0]["serverID"]] = TAG_VERIFYING_STATUS

        self.join_button = discord.ui.Button(
            label="Join Guild",
            style=ButtonStyle.link,
            url="https://discord.gg/CvU77YA65K",
            row=0,
        )
        self.add_item(self.join_button)
        self.update_view()

    def create_embed(self) -> discord.Embed:
        guild_data = self.guilds[self.current_page]
        created_unix = discord_id_to_unix(guild_data.get("serverID"))
        last_fetch_unix = milliseconds_to_unix(guild_data.get("lastFetch"))

        embed = discord.Embed(
            title=guild_data.get("name", "Unknown Server"),
            description=f"Showing result {self.current_page + 1} of {len(self.guilds)}",
            color=TAG_EMBED_PURPLE,
        )
        if guild_data.get("tagHash"):
            embed.set_thumbnail(
                url=f"https://cdn.discordapp.com/clan-badges/{guild_data['serverID']}/{guild_data['tagHash']}.png"
            )
        embed.add_field(name="Status", value=self.get_current_validation_status(), inline=False)
        embed.add_field(name="Tag", value=guild_data.get("tag", "N/A"), inline=True)
        embed.add_field(name="Members", value=format_member_count(guild_data.get("members")), inline=True)
        embed.add_field(name="Created", value=discord_timestamp(created_unix, "D"), inline=True)
        embed.add_field(name="Last Updated", value=discord_timestamp(last_fetch_unix, "R"), inline=True)
        if guild_data.get("banner"):
            embed.set_image(url=guild_data["banner"] + "?size=300")
        embed.set_footer(text=f"Server ID: {guild_data.get('serverID', 'N/A')}")
        return embed

    def set_guilds(self, guilds_list: list):
        self.guilds = guilds_list
        if self.guilds and self.current_page >= len(self.guilds):
            self.current_page = len(self.guilds) - 1
        if self.current_page < 0:
            self.current_page = 0
        if self.guilds:
            self.update_view()

    def replace_guild_at(self, index: int, guild: dict, status: str = TAG_VERIFIED_STATUS):
        if 0 <= index < len(self.guilds):
            self.guilds[index] = guild
            server_id = guild.get("serverID")
            if server_id:
                self.validation_status_by_server[server_id] = status
            self.update_view()

    def remove_guild_at(self, index: int):
        if 0 <= index < len(self.guilds):
            removed = self.guilds.pop(index)
            removed_server_id = removed.get("serverID")
            if removed_server_id:
                self.validation_status_by_server.pop(removed_server_id, None)
                self.validation_priority = [
                    server_id for server_id in self.validation_priority if server_id != removed_server_id
                ]
        if self.guilds and self.current_page >= len(self.guilds):
            self.current_page = len(self.guilds) - 1
        if self.current_page < 0:
            self.current_page = 0
        self.update_view()

    def get_current_server_id(self):
        if not self.guilds:
            return None
        return self.guilds[self.current_page].get("serverID")

    def get_current_validation_status(self):
        server_id = self.get_current_server_id()
        if not server_id:
            return TAG_PENDING_STATUS
        return self.validation_status_by_server.get(server_id, TAG_PENDING_STATUS)

    def prioritize_current_guild(self):
        server_id = self.get_current_server_id()
        if not server_id:
            return
        current_status = self.validation_status_by_server.get(server_id)
        if current_status == TAG_VERIFIED_STATUS:
            return
        self.validation_priority = [
            existing_id for existing_id in self.validation_priority if existing_id != server_id
        ]
        self.validation_priority.insert(0, server_id)
        if current_status != TAG_VERIFYING_STATUS:
            self.validation_status_by_server[server_id] = TAG_VERIFYING_STATUS

    def pop_next_validation_target(self, validated_server_ids: set):
        while self.validation_priority:
            server_id = self.validation_priority.pop(0)
            if server_id not in validated_server_ids:
                return server_id
        for guild in self.guilds:
            server_id = guild.get("serverID")
            if server_id and server_id not in validated_server_ids:
                return server_id
        return None

    def find_guild_index(self, server_id):
        return next(
            (index for index, guild in enumerate(self.guilds) if guild.get("serverID") == server_id),
            None,
        )

    def update_view(self):
        if not self.guilds:
            self.prev_button.disabled = True
            self.next_button.disabled = True
            self.join_button.url = "https://discord.gg/CvU77YA65K"
            return

        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == (len(self.guilds) - 1)
        current_guild = self.guilds[self.current_page]
        self.join_button.url = f"https://discord.gg/{current_guild.get('value', '')}"

    @discord.ui.button(label="<", style=ButtonStyle.secondary, custom_id="prev", row=0)
    async def prev_button(self, interaction: Interaction, button: Button):
        if self.current_page > 0:
            self.current_page -= 1
            self.prioritize_current_guild()
            self.update_view()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label=">", style=ButtonStyle.secondary, custom_id="next", row=0)
    async def next_button(self, interaction: Interaction, button: Button):
        if self.current_page < (len(self.guilds) - 1):
            self.current_page += 1
            self.prioritize_current_guild()
            self.update_view()
            await interaction.response.edit_message(embed=self.create_embed(), view=self)


class GuildTagList(discord.ui.LayoutView):
    def __init__(self, guild_count: int):
        super().__init__()
        count_str = f"{guild_count:,}"
        self.container1 = discord.ui.Container(
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media="https://i.postimg.cc/3wsn6rMz/Gemini-Generated-Image-f0uav2f0uav2f0ua.png",
                ),
            ),
            discord.ui.TextDisplay(
                content=f"# Guild Tag List\n### You can search our database of {count_str}+ tagged guilds or add your own!"
            ),
            discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
            discord.ui.TextDisplay(
                content="## 🔍 How to Search for a Guild\n### Use the `/tag search` command to find guilds.\n- Usage: `/tag search <tag>`"
            ),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media="https://i.postimg.cc/yNf5WCZg/image.png",
                ),
            ),
            discord.ui.TextDisplay(
                content="## 📥 How to Add a Guild\n### Use the `/tag add` command to add a new guild.\n- Usage: `/tag add <invite_link_or_code>`"
            ),
            discord.ui.MediaGallery(
                discord.MediaGalleryItem(
                    media="https://i.postimg.cc/Px9RzbKG/image.png",
                ),
            ),
            discord.ui.TextDisplay(
                content="-# **Note:** The guild *must* have a discoverable tag (like 'CvU77YA65K' or 'anime') to be successfully added."
            ),
        )
        self.add_item(self.container1)

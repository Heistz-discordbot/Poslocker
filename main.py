from pathlib import Path

code = r'''"""
Railway-friendly Discord position-management bot.

This version manages panels, profile listings, private tickets, reviews and logs.
It does NOT log into Roblox with account cookies or automate mass game joins.
Keep Roblox .ROBLOSECURITY cookies out of Discord messages and source control.

Install:
    pip install -U discord.py python-dotenv

Railway variables:
    DISCORD_TOKEN       required
    OWNER_ID            your Discord user ID (recommended)
    PANEL_CHANNEL_ID    1560668012120645693
    PANEL_CATEGORY_ID   1560667887159742487
    TICKET_CATEGORY_ID  1560667942927077556
    LOG_CHANNEL_ID      1560668040025346068

Files beside main.py:
    alts.json           public account metadata only (no cookies needed)
    bot_data.json       auto-created persistent data; use a Railway Volume
"""

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)

PANEL_CHANNEL_ID = int(os.getenv("PANEL_CHANNEL_ID", "1560668012120645693"))
PANEL_CATEGORY_ID = int(os.getenv("PANEL_CATEGORY_ID", "1560667887159742487"))
TICKET_CATEGORY_ID = int(os.getenv("TICKET_CATEGORY_ID", "1560667942927077556"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1560668040025346068"))

BASE = Path(__file__).resolve().parent
ALTS_FILE = BASE / "alts.json"
DATA_FILE = Path(os.getenv("DATA_FILE", str(BASE / "bot_data.json")))

COLORS = {
    "info": discord.Color.blurple(),
    "ok": discord.Color.green(),
    "warn": discord.Color.gold(),
    "err": discord.Color.red(),
    "neutral": discord.Color.from_rgb(43, 45, 49),
}


def embed(title: str, description: str = "", kind: str = "info") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLORS.get(kind, COLORS["info"]))


def load_data() -> dict:
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}
    data.setdefault("guilds", {})
    data.setdefault("tickets", {})
    data.setdefault("next_ticket", {})
    data.setdefault("created_channels", {})
    return data


def save_data(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = DATA_FILE.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    temp.replace(DATA_FILE)


def guild_data(data: dict, guild_id: int) -> dict:
    return data["guilds"].setdefault(str(guild_id), {})


def safe_slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9-]", "-", value.lower())
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:30] or "member"


def load_alts() -> list[dict]:
    """Read display-safe account metadata only; never return cookie/session fields."""
    if not ALTS_FILE.exists():
        return []
    with ALTS_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    items = raw.get("alts", []) if isinstance(raw, dict) else raw
    results = []
    for i, item in enumerate(items):
        if isinstance(item, str):
            username = item.strip().lstrip("@")
            if not username:
                continue
            alt = {"name": username, "username": username}
        elif isinstance(item, dict):
            username = str(item.get("username") or item.get("roblox_username") or item.get("name") or "").strip().lstrip("@")
            display = str(item.get("display_name") or item.get("name") or username or f"Account {i + 1}").strip()
            alt = {
                "name": display[:80],
                "username": username[:80],
                "user_id": str(item.get("user_id") or item.get("id") or "").strip(),
                "profile_url": str(item.get("profile_url") or "").strip(),
            }
        else:
            continue

        # Deliberately ignore cookie, roblosecurity, password, token and other secret fields.
        url = alt.get("profile_url", "")
        if not url and alt.get("user_id").isdigit():
            url = f"https://www.roblox.com/users/{alt['user_id']}/profile"
        if not url and alt.get("username"):
            url = f"https://www.roblox.com/search/users?keyword={alt['username']}"
        alt["profile_url"] = url if url.startswith("https://www.roblox.com/") else ""
        alt["key"] = alt.get("user_id") or alt.get("username") or f"account-{i + 1}"
        if alt.get("username") or alt.get("profile_url"):
            results.append(alt)
    return results


def is_owner(interaction: discord.Interaction) -> bool:
    if OWNER_ID and interaction.user.id == OWNER_ID:
        return True
    return bool(interaction.guild and interaction.guild.owner_id == interaction.user.id)


async def require_owner(interaction: discord.Interaction) -> bool:
    if is_owner(interaction):
        return True
    await interaction.response.send_message(
        embed=embed("Not authorized", "Only the configured bot owner or server owner can use this command.", "err"),
        ephemeral=True,
    )
    return False


async def send_log(guild: discord.Guild, title: str, description: str, *, view=None) -> Optional[discord.Message]:
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        gdata = load_data()["guilds"].get(str(guild.id), {})
        channel = guild.get_channel(int(gdata.get("log_channel_id", 0) or 0))
    if isinstance(channel, discord.TextChannel):
        try:
            return await channel.send(embed=embed(title, description, "neutral"), view=view)
        except discord.HTTPException:
            return None
    return None


def get_ticket(data: dict, channel_id: int) -> Optional[dict]:
    return data["tickets"].get(str(channel_id))


async def update_ticket_message(channel: discord.TextChannel, ticket: dict) -> None:
    message_id = ticket.get("panel_message_id")
    if not message_id:
        return
    try:
        msg = await channel.fetch_message(int(message_id))
        await msg.edit(embed=ticket_embed(ticket), view=TicketView())
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def ticket_embed(ticket: dict) -> discord.Embed:
    lines = [
        f"**Ticket:** Pos-Locking #{ticket['number']}",
        f"**Requested by:** <@{ticket['user_id']}>",
        f"**Status:** {ticket.get('status', 'Open')}",
        f"**Created:** <t:{int(ticket.get('created_at', time.time()))}:R>",
        "",
        "**Selected profile records**",
    ]
    alts = ticket.get("alts", [])
    if alts:
        for a in alts[:20]:
            status = a.get("status", "Not reviewed")
            link = f"[{discord.utils.escape_markdown(a.get('name', 'Account'))}]({a['profile_url']})" if a.get("profile_url") else discord.utils.escape_markdown(a.get("name", "Account"))
            lines.append(f"• {link} — **{status}**")
        if len(alts) > 20:
            lines.append(f"…and {len(alts) - 20} more.")
    else:
        lines.append("No profiles selected.")
    if ticket.get("server_url"):
        lines.extend(["", f"[Open supplied server link]({ticket['server_url']})"])
    e = embed("Position Management Ticket", "\n".join(lines), "neutral")
    e.set_footer(text="This panel records requests; it does not automate Roblox clients.")
    return e


class ProfileSelect(discord.ui.Select):
    def __init__(self, alts: list[dict], selected: set[str]):
        options = []
        for a in alts[:25]:
            options.append(discord.SelectOption(
                label=a["name"][:100],
                value=a["key"][:100],
                description=(f"@{a.get('username')}" if a.get("username") else "Roblox profile")[:100],
                default=a["key"] in selected,
            ))
        super().__init__(
            placeholder="Select profile records for this request",
            min_values=0,
            max_values=max(1, len(options)),
            options=options,
            custom_id="pos:profile_select",
        )

    async def callback(self, interaction: discord.Interaction):
        view: "RequestView" = self.view  # type: ignore
        view.selected = set(self.values)
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(
            f"Selected **{len(view.selected)}** profile record(s). Press **Create Ticket** when ready.",
            ephemeral=True,
        )


class RequestView(discord.ui.View):
    def __init__(self, alts: list[dict]):
        super().__init__(timeout=300)
        self.alts = alts
        self.selected: set[str] = set()
        self.add_item(ProfileSelect(alts, self.selected) if alts else EmptySelect())

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.primary, custom_id="pos:create_ticket")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_data()
        gdata = data["guilds"].get(str(interaction.guild.id), {})
        category_id = int(gdata.get("ticket_category_id", TICKET_CATEGORY_ID) or TICKET_CATEGORY_ID)
        category = interaction.guild.get_channel(category_id)
        if not isinstance(category, discord.CategoryChannel):
            return await interaction.followup.send(
                "The ticket category was not found. Ask the server owner to run `/authorize` and check the category ID.",
                ephemeral=True,
            )
        number = int(data["next_ticket"].get(str(interaction.guild.id), 0)) + 1
        data["next_ticket"][str(interaction.guild.id)] = number
        chosen = [a.copy() for a in self.alts if a["key"] in self.selected]
        for a in chosen:
            a["status"] = "Not reviewed"
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        me = interaction.guild.me
        if me:
            overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, read_message_history=True)
        # Let server staff with Manage Channels view and handle tickets.
        for role in interaction.guild.roles:
            if role.permissions.manage_channels or role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
        channel = await interaction.guild.create_text_channel(
            name=f"pos-locking-{number}",
            category=category,
            overwrites=overwrites,
            topic=f"Position-management ticket #{number}; requester={interaction.user.id}",
            reason=f"Position management ticket requested by {interaction.user}",
        )
        ticket = {
            "guild_id": interaction.guild.id,
            "channel_id": channel.id,
            "number": number,
            "user_id": interaction.user.id,
            "status": "Open",
            "created_at": int(time.time()),
            "alts": chosen,
            "server_url": "",
            "review": None,
            "panel_message_id": None,
            "log_message_id": None,
        }
        data["tickets"][str(channel.id)] = ticket
        save_data(data)
        msg = await channel.send(
            content=f"<@{interaction.user.id}>",
            embed=ticket_embed(ticket),
            view=TicketView(),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        ticket["panel_message_id"] = msg.id
        data["tickets"][str(channel.id)] = ticket
        save_data(data)
        await send_log(interaction.guild, f"Ticket opened · Pos-Locking #{number}",
                       f"Channel: {channel.mention}\nRequester: {interaction.user.mention}\nProfile records: {len(chosen)}")
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)


class EmptySelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="No profiles found in alts.json", min_values=0, max_values=1,
                         options=[discord.SelectOption(label="No profiles configured", value="none")], disabled=True)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)


class ReviewView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=86400)
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="Positive", style=discord.ButtonStyle.success)
    async def positive(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, "Positive")

    @discord.ui.button(label="Neutral", style=discord.ButtonStyle.secondary)
    async def neutral(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, "Neutral")

    @discord.ui.button(label="Needs improvement", style=discord.ButtonStyle.danger)
    async def negative(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.finish(interaction, "Needs improvement")

    async def finish(self, interaction: discord.Interaction, rating: str):
        data = load_data()
        ticket = data["tickets"].get(str(self.ticket_channel_id))
        if not ticket:
            return await interaction.response.send_message("Ticket record not found.", ephemeral=True)
        if interaction.user.id != int(ticket["user_id"]) and not is_owner(interaction):
            return await interaction.response.send_message("Only the requester or bot owner can submit this review.", ephemeral=True)
        ticket["review"] = {"rating": rating, "user_id": interaction.user.id, "at": int(time.time())}
        data["tickets"][str(self.ticket_channel_id)] = ticket
        save_data(data)
        await interaction.response.send_message(f"Review recorded: **{rating}**. Thank you.", ephemeral=True)
        if interaction.guild:
            await send_log(interaction.guild, f"Review · Pos-Locking #{ticket['number']}",
                           f"Rating: **{rating}**\nTicket: <#{self.ticket_channel_id}>\nReviewer: <@{interaction.user.id}>")


class TicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def ticket_context(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This control must be used in a ticket.", ephemeral=True)
            return None, None
        data = load_data()
        ticket = get_ticket(data, interaction.channel.id)
        if not ticket:
            await interaction.response.send_message("Ticket record not found.", ephemeral=True)
            return None, None
        return data, ticket

    @discord.ui.button(label="End", style=discord.ButtonStyle.success, custom_id="pos:ticket_end", row=0)
    async def end_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, ticket = await self.ticket_context(interaction)
        if not ticket:
            return
        if interaction.user.id != int(ticket["user_id"]) and not is_owner(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_channels or perms.administrator)):
                return await interaction.response.send_message("Only the requester or staff can end this ticket.", ephemeral=True)
        ticket["status"] = "Ended"
        ticket["ended_at"] = int(time.time())
        data["tickets"][str(interaction.channel.id)] = ticket
        save_data(data)
        await interaction.response.send_message("Ticket ended. Please leave a review below.", view=ReviewView(interaction.channel.id))
        await send_log(interaction.guild, f"Ticket ended · Pos-Locking #{ticket['number']}",
                       f"Ticket: {interaction.channel.mention}\nEnded by: {interaction.user.mention}",
                       view=LogTicketView(interaction.channel.id))

    @discord.ui.button(label="Join Server", style=discord.ButtonStyle.link, url="https://www.roblox.com/games/")
    async def join_server_placeholder(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Link buttons do not dispatch callbacks. The static destination is a safe Roblox landing page.
        pass

    @discord.ui.button(label="Log Profile", style=discord.ButtonStyle.secondary, custom_id="pos:ticket_log_profile", row=1)
    async def log_profile(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, ticket = await self.ticket_context(interaction)
        if not ticket:
            return
        if interaction.user.id != int(ticket["user_id"]) and not is_owner(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_channels or perms.administrator)):
                return await interaction.response.send_message("Only the requester or staff can update this ticket.", ephemeral=True)
        pending = [a for a in ticket.get("alts", []) if a.get("status") != "Logged"]
        if not pending:
            return await interaction.response.send_message("All profile records are already logged.", ephemeral=True)
        target = pending[0]
        target["status"] = "Logged"
        target["logged_at"] = int(time.time())
        target["logged_by"] = interaction.user.id
        data["tickets"][str(interaction.channel.id)] = ticket
        save_data(data)
        await interaction.response.edit_message(embed=ticket_embed(ticket), view=self)
        await send_log(interaction.guild, f"Profile logged · Pos-Locking #{ticket['number']}",
                       f"Profile: **{target['name']}**\nTicket: {interaction.channel.mention}\nUpdated by: {interaction.user.mention}")

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, custom_id="pos:ticket_delete", row=1)
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, ticket = await self.ticket_context(interaction)
        if not ticket:
            return
        if not is_owner(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_channels or perms.administrator)):
                return await interaction.response.send_message("Only the bot owner or server staff can delete a ticket.", ephemeral=True)
        ticket["status"] = "Deleted"
        ticket["deleted_at"] = int(time.time())
        data["tickets"][str(interaction.channel.id)] = ticket
        save_data(data)
        await interaction.response.send_message("Ticket record saved. Deleting this channel in 5 seconds.")
        await send_log(interaction.guild, f"Ticket deleted · Pos-Locking #{ticket['number']}",
                       f"Ticket channel: `{interaction.channel.name}`\nDeleted by: {interaction.user.mention}",
                       view=LogTicketView(interaction.channel.id))
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket deleted by {interaction.user}")
        except discord.HTTPException:
            pass


class LogTicketView(discord.ui.View):
    def __init__(self, ticket_channel_id: int):
        super().__init__(timeout=None)
        self.ticket_channel_id = ticket_channel_id

    @discord.ui.button(label="View Record", style=discord.ButtonStyle.secondary)
    async def view_record(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        ticket = data["tickets"].get(str(self.ticket_channel_id))
        if not ticket:
            return await interaction.response.send_message("No saved ticket record.", ephemeral=True)
        if not is_owner(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_channels or perms.administrator)):
                return await interaction.response.send_message("Staff only.", ephemeral=True)
        e = ticket_embed(ticket)
        await interaction.response.send_message(embed=e, ephemeral=True)

    @discord.ui.button(label="Reopen", style=discord.ButtonStyle.primary)
    async def reopen(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        ticket = data["tickets"].get(str(self.ticket_channel_id))
        if not ticket:
            return await interaction.response.send_message("No saved ticket record.", ephemeral=True)
        if not is_owner(interaction):
            perms = getattr(interaction.user, "guild_permissions", None)
            if not (perms and (perms.manage_channels or perms.administrator)):
                return await interaction.response.send_message("Staff only.", ephemeral=True)
        if not interaction.guild:
            return await interaction.response.send_message("Use this in a server.", ephemeral=True)
        channel = interaction.guild.get_channel(self.ticket_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "The original ticket channel was deleted. The record is preserved, but this version does not recreate deleted tickets.",
                ephemeral=True,
            )
        ticket["status"] = "Open"
        ticket.pop("ended_at", None)
        data["tickets"][str(self.ticket_channel_id)] = ticket
        save_data(data)
        await channel.set_permissions(interaction.guild.default_role, view_channel=False)
        member = interaction.guild.get_member(int(ticket["user_id"]))
        if member:
            await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
        await update_ticket_message(channel, ticket)
        await interaction.response.send_message(f"Reopened {channel.mention}.", ephemeral=True)
        await send_log(interaction.guild, f"Ticket reopened · Pos-Locking #{ticket['number']}",
                       f"Ticket: {channel.mention}\nReopened by: {interaction.user.mention}")


class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Check Available Alts", style=discord.ButtonStyle.secondary, custom_id="pos:check_alts", row=0)
    async def check_alts(self, interaction: discord.Interaction, button: discord.ui.Button):
        alts = load_alts()
        if not alts:
            return await interaction.response.send_message(
                embed=embed("No profiles configured", "Add display-safe profile metadata to `alts.json` and redeploy.", "warn"),
                ephemeral=True,
            )
        lines = []
        for a in alts[:30]:
            name = discord.utils.escape_markdown(a["name"])
            lines.append(f"• [{name}]({a['profile_url']})" if a["profile_url"] else f"• {name} — profile link unavailable")
        if len(alts) > 30:
            lines.append(f"…and {len(alts) - 30} more.")
        await interaction.response.send_message(
            embed=embed(f"Registered Profiles · {len(alts)}", "\n".join(lines), "info"),
            ephemeral=True,
        )

    @discord.ui.button(label="Create Request", style=discord.ButtonStyle.primary, custom_id="pos:create_request", row=0)
    async def create_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        alts = load_alts()
        if not alts:
            return await interaction.response.send_message(
                embed=embed("No profiles configured", "There are no profile records in `alts.json` yet.", "warn"),
                ephemeral=True,
            )
        view = RequestView(alts)
        await interaction.response.send_message(
            embed=embed("Create Position Request",
                        "Select the profile records you want listed in your private ticket, then press **Create Ticket**.\n\n"
                        "This creates a tracking ticket only; it does not launch Roblox clients or join a game server.",
                        "info"),
            view=view,
            ephemeral=True,
        )


class ReadMeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Position Panel", style=discord.ButtonStyle.primary, custom_id="pos:open_panel")
    async def open_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        gdata = data["guilds"].get(str(interaction.guild_id), {}) if interaction.guild_id else {}
        channel_id = int(gdata.get("panel_channel_id", PANEL_CHANNEL_ID) or PANEL_CHANNEL_ID)
        channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(f"Use the position panel in {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message("The position panel has not been configured by the server owner.", ephemeral=True)


async def ensure_readme(guild: discord.Guild):
    existing = discord.utils.get(guild.text_channels, name="read-me")
    if existing:
        return existing
    try:
        channel = await guild.create_text_channel(
            "read-me",
            position=0,
            topic="Read this before using the position management bot.",
            reason="Create bot information channel",
        )
        e = embed("Welcome to Position Management",
                  "This bot helps organize profile records and position-related requests.\n\n"
                  "• Use the position panel to review registered Roblox profiles.\n"
                  "• Create a private ticket to track a request.\n"
                  "• End tickets and leave a review when finished.\n"
                  "• Staff can review saved records in the log channel.\n\n"
                  "This bot does not automatically join Roblox servers or launch Roblox clients.",
                  "neutral")
        await channel.send(embed=e, view=ReadMeView())
        data = load_data()
        data["created_channels"].setdefault(str(guild.id), [])
        data["created_channels"][str(guild.id)].append(channel.id)
        save_data(data)
        return channel
    except discord.HTTPException:
        return None


class PositionBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(MainPanelView())
        self.add_view(TicketView())
        self.add_view(ReadMeView())
        # Persistent views attached to old log messages are recreated when those messages are interacted with
        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            print(f"Command sync warning: {exc}")

    async def on_ready(self):
        print(f"Logged in as {self.user} (ID: {self.user.id})")
        print(f"Loaded {len(load_alts())} display-safe profile records.")
        print("Bot is ready.")

    async def on_guild_join(self, guild: discord.Guild):
        await ensure_readme(guild)


bot = PositionBot()


@bot.tree.command(name="authorize", description="Owner-only: configure the position management panel and channels")
@app_commands.guild_only()
async def authorize(interaction: discord.Interaction):
    if not await require_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if guild is None:
        return await interaction.followup.send("This command only works in a server.", ephemeral=True)

    data = load_data()
    # Prefer the supplied channel IDs, but gracefully report missing permissions/resources.
    panel_channel = guild.get_channel(PANEL_CHANNEL_ID)
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    ticket_category = guild.get_channel(TICKET_CATEGORY_ID)
    panel_category = guild.get_channel(PANEL_CATEGORY_ID)

    # Create missing categories/channels and record which resources the bot created.
    created = data["created_channels"].setdefault(str(guild.id), [])
    if not isinstance(panel_category, discord.CategoryChannel):
        panel_category = await guild.create_category("Position Management", reason="Position bot authorization")
        created.append(panel_category.id)
    if not isinstance(ticket_category, discord.CategoryChannel):
        ticket_category = await guild.create_category("Position Tickets", reason="Position bot authorization")
        created.append(ticket_category.id)
    if not isinstance(panel_channel, discord.TextChannel):
        panel_channel = await guild.create_text_channel("position-panel", category=panel_category,
                                                        reason="Position bot authorization")
        created.append(panel_channel.id)
    if not isinstance(log_channel, discord.TextChannel):
        log_channel = await guild.create_text_channel("position-logs", category=panel_category,
                                                      reason="Position bot authorization")
        created.append(log_channel.id)

    data["guilds"][str(guild.id)] = {
        "panel_channel_id": panel_channel.id,
        "panel_category_id": panel_category.id,
        "ticket_category_id": ticket_category.id,
        "log_channel_id": log_channel.id,
        "authorized_by": interaction.user.id,
        "authorized_at": int(time.time()),
    }
    save_data(data)

    e = embed("Position Management",
              "**What is this?**\n"
              "A central panel for viewing registered Roblox profiles and opening private request tickets.\n\n"
              "**Benefits**\n"
              "• Private tickets and saved history.\n"
              "• Profile records and staff logs.\n"
              "• Review and reopen supported tickets.\n\n"
              "**Rules**\n"
              "• Only use accounts you are authorized to manage.\n"
              "• Never share Roblox session cookies or passwords.\n"
              "• Profile status is recorded manually; the bot does not claim to launch clients.\n\n"
              "**Create a request**\n"
              "Choose **Check Available Alts** to review profile links or **Create Request** to open a ticket.",
              "neutral")
    await panel_channel.send(embed=e, view=MainPanelView())
    await ensure_readme(guild)
    await interaction.followup.send(
        embed=embed("Authorization complete",
                    f"Panel: {panel_channel.mention}\n"
                    f"Ticket category: **{ticket_category.name}**\n"
                    f"Logs: {log_channel.mention}\n"
                    "The bot is configured. Existing channels were reused when their configured IDs were valid.",
                    "ok"),
        ephemeral=True,
    )


@bot.tree.command(name="deauthorize", description="Owner-only: remove bot-created channels and leave this server")
@app_commands.guild_only()
async def deauthorize(interaction: discord.Interaction):
    if not await require_owner(interaction):
        return
    guild = interaction.guild
    if guild is None:
        return await interaction.response.send_message("This command only works in a server.", ephemeral=True)
    await interaction.response.send_message(
        embed=embed("Confirm deauthorization",
                    "This removes channels tracked as created by this bot, then makes the bot leave this server. "
                    "Existing channels that were merely reused will not be deleted.", "warn"),
        view=DeauthorizeConfirmView(guild.id),
        ephemeral=True,
    )


class DeauthorizeConfirmView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=60)
        self.guild_id = guild_id

    @discord.ui.button(label="Confirm and leave", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_owner(interaction) or not interaction.guild or interaction.guild.id != self.guild_id:
            return await interaction.response.send_message("Only the bot owner can confirm this action.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = load_data()
        ids = data["created_channels"].get(str(self.guild_id), [])
        deleted = 0
        for channel_id in list(ids):
            channel = interaction.guild.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.delete(reason="Bot deauthorized by owner")
                    deleted += 1
                except discord.HTTPException:
                    pass
        data["guilds"].pop(str(self.guild_id), None)
        data["created_channels"].pop(str(self.guild_id), None)
        save_data(data)
        await interaction.followup.send(f"Removed {deleted} bot-created channel(s). Leaving the server now.", ephemeral=True)
        await bot.close_guild(self.guild_id)


async def close_guild(guild_id: int):
    guild = bot.get_guild(guild_id)
    if guild:
        await guild.leave()


# Attach method after class definition to keep the confirmation callback simple.
PositionBot.close_guild = close_guild  # type: ignore[attr-defined]


@bot.tree.command(name="test", description="Show all registered Roblox profiles and quick links")
@app_commands.guild_only()
async def test_profiles(interaction: discord.Interaction):
    alts = load_alts()
    if not alts:
        return await interaction.response.send_message(
            embed=embed("No profiles found", "Add public profile metadata to `alts.json` first.", "warn"),
            ephemeral=True,
        )
    lines = []
    for i, a in enumerate(alts[:40], 1):
        name = discord.utils.escape_markdown(a["name"])
        if a["profile_url"]:
            lines.append(f"{i}. [{name}]({a['profile_url']})")
        else:
            lines.append(f"{i}. {name} — no profile URL")
    if len(alts) > 40:
        lines.append(f"…and {len(alts) - 40} more.")
    await interaction.response.send_message(
        embed=embed(f"Registered Roblox Profiles ({len(alts)})",
                    "\n".join(lines) + "\n\nSelect a link to open its Roblox profile. Discord cannot force a user's browser to open many tabs at once.",
                    "info"),
        ephemeral=True,
    )


@bot.tree.command(name="unlink", description="Remove your linked Roblox username from the bot's local link records")
@app_commands.guild_only()
async def unlink(interaction: discord.Interaction):
    data = load_data()
    links = data.setdefault("links", {})
    removed = links.pop(str(interaction.user.id), None)
    save_data(data)
    if removed:
        await interaction.response.send_message("Your saved link record has been removed.", ephemeral=True)
    else:
        await interaction.response.send_message("You do not have a saved link record in this version.", ephemeral=True)


@bot.tree.command(name="ticket-record", description="Staff: view a saved ticket record by ticket channel ID")
@app_commands.describe(channel_id="The original ticket channel ID")
@app_commands.guild_only()
async def ticket_record(interaction: discord.Interaction, channel_id: str):
    perms = getattr(interaction.user, "guild_permissions", None)
    if not is_owner(interaction) and not (perms and (perms.manage_channels or perms.administrator)):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    if not channel_id.isdigit():
        return await interaction.response.send_message("Enter a numeric channel ID.", ephemeral=True)
    ticket = load_data()["tickets"].get(channel_id)
    if not ticket or int(ticket.get("guild_id", 0)) != interaction.guild_id:
        return await interaction.response.send_message("No saved ticket record for this server.", ephemeral=True)
    await interaction.response.send_message(embed=ticket_embed(ticket), ephemeral=True)


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    # Keep records when ticket channels are deleted so staff can still inspect the history.
    data = load_data()
    ticket = data["tickets"].get(str(channel.id))
    if ticket and ticket.get("status") != "Deleted":
        ticket["status"] = "Channel deleted; record retained"
        data["tickets"][str(channel.id)] = ticket
        save_data(data)


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("Missing DISCORD_TOKEN environment variable.")
    bot.run(TOKEN)
'''
path = Path("/mnt/data/main.py")
path.write_text(code, encoding="utf-8")
print(f"Created {path} ({len(code.splitlines())} lines).")

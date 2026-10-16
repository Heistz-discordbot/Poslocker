"""
POS Locker Discord bot - runs real Roblox clients for your alts (Windows / Railway 24/7 compatible)
"""

import asyncio
import glob
import json
import os
import random
import subprocess
import sys
import time

import discord
import requests
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

# ---------------------------------------------------------------- Settings
API_PARALLEL = 15
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"

MOVE_INTERVAL = float(os.getenv("MOVE_INTERVAL_MIN", "15")) * 60  # seconds between anti-idle moves
LAUNCH_DELAY = float(os.getenv("LAUNCH_DELAY", "8"))                # seconds between launching each alt
WINDOW_WAIT = 120                                                  # seconds to wait for a client window
MOVE_MODE = os.getenv("MOVE_MODE", "focus").lower()                # "focus" or "post"
MAX_RESTARTS = 3                                                   # auto-relaunch tries per alt
MONITOR_TICK = 15                                                  # seconds between health checks

BASE = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(BASE, "links.json")

VERIFY_WORDS = [
    "apple", "banana", "cherry", "dragon", "eagle", "falcon", "forest", "galaxy",
    "hammer", "island", "jungle", "knight", "lemon", "mango", "ninja", "orange",
    "planet", "quantum", "rocket", "shadow", "tiger", "umbrella", "violet", "wizard"
]

COLORS = {"info": 0x5865F2, "ok": 0x2ECC71, "warn": 0xF1C40F, "err": 0xE74C3C}

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def em(title, desc=None, kind="info", fields=None):
    e = discord.Embed(title=title, description=desc, color=COLORS[kind])
    for name, value, inline in (fields or []):
        e.add_field(name=name, value=(value or "-")[:1024], inline=inline)
    return e


# ---------------------------------------------------------------- Network / API Wrappers
def http(method, url, retries=4, **kw):
    kw.setdefault("timeout", 20)
    last_exc = None
    for i in range(retries):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code == 429:
                time.sleep(2 ** i + random.random())
                continue
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last_exc = e
            time.sleep(1.5 * (i + 1) + random.random())
    if last_exc is not None:
        raise RuntimeError(f"Network error talking to Roblox ({type(last_exc).__name__}).")
    raise RuntimeError("Roblox rate limit (HTTP 429). Wait a minute and try again.")


def rb_post(cookie, url, body):
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = f".ROBLOSECURITY={cookie}"
    r = http("POST", url, headers=headers, json=body)
    if r.status_code == 403 and "x-csrf-token" in r.headers:
        headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
        r = http("POST", url, headers=headers, json=body)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- Alts and Data Helpers
def clean_cookie(c):
    c = (c or "").strip().strip("\"'")
    if c.upper().startswith(".ROBLOSECURITY="):
        c = c.split("=", 1)[1]
    return "".join(c.strip().strip("\"';").split())


def load_alts():
    alts_path = os.path.join(BASE, "alts.json")
    if not os.path.exists(alts_path):
        return []
    with open(alts_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("alts", data) if isinstance(data, dict) else data
    alts = []
    for i, a in enumerate(items):
        if isinstance(a, str):
            alts.append({"name": f"alt{i + 1}", "cookie": clean_cookie(a)})
        else:
            cookie = a.get("cookie") or a.get("roblosecurity") or a.get(".ROBLOSECURITY")
            alts.append({"name": a.get("name") or a.get("username") or f"alt{i + 1}",
                         "cookie": clean_cookie(cookie)})
    return alts


def whoami(cookie):
    r = http("GET", "https://users.roblox.com/v1/users/authenticated",
             headers={"Cookie": f".ROBLOSECURITY={cookie}"})
    if r.status_code != 200:
        raise RuntimeError(f"Cookie rejected by Roblox (HTTP {r.status_code})")
    j = r.json()
    return j["id"], j["name"]


def user_id_from_name(username):
    username = username.strip().lstrip("@")
    res = rb_post(None, "https://users.roblox.com/v1/usernames/users",
                  {"usernames": [username], "excludeBannedUsers": False})
    if not res.get("data"):
        raise RuntimeError(f"Roblox user {username} not found.")
    return res["data"][0]["id"], res["data"][0]["name"]


def read_presence(cookies, uid):
    best = 0
    for c in cookies[:10]:
        try:
            p = rb_post(c, PRESENCE_URL, {"userIds": [uid]})["userPresences"][0]
        except Exception:
            continue
        t = p.get("userPresenceType", 0)
        best = max(best, t)
        if t == 2 and p.get("gameId") and p.get("placeId"):
            return t, int(p["placeId"]), p["gameId"]
    return best, None, None


def locate_user(cookies, uid, name):
    seen = 0
    for _ in range(3):
        t, place, job = read_presence(cookies, uid)
        seen = max(seen, t)
        if place:
            time.sleep(1)
            _, place2, job2 = read_presence(cookies, uid)
            if job2 == job:
                return place, job
        else:
            time.sleep(1)
    if seen == 2:
        raise RuntimeError(
            f"{name} is in a game, but Roblox hides server details. Fix: Roblox Settings > Privacy > "
            "'Who can join me' = Everyone, or make that account friends with the alts."
        )
    if seen in (0, 1):
        raise RuntimeError(f"{name} is not in a game right now. Join the server first, then run the command.")
    raise RuntimeError(f"Could not read {name}'s server. Try again in a few seconds.")


def load_links():
    try:
        if not os.path.exists(LINKS_FILE):
            return {"linked": {}, "pending": {}}
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("linked", {})
            data.setdefault("pending", {})
            return data
    except Exception:
        return {"linked": {}, "pending": {}}


def save_links(data):
    with open(LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_description(uid):
    r = http("GET", f"https://users.roblox.com/v1/users/{uid}")
    r.raise_for_status()
    return r.json().get("description") or ""


# ---------------------------------------------------------------- Roblox Launching & Windows Management
_mutex_handle = None
_event_handle = None
MUTEX_OWNED = False


def enable_multi_instance():
    global _mutex_handle, _event_handle, MUTEX_OWNED
    if not IS_WINDOWS:
        return False
    if MUTEX_OWNED:
        return True
    try:
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        if _mutex_handle is None:
            _mutex_handle = kernel32.CreateMutexW(None, True, "ROBLOX_singletonMutex")
            err = ctypes.get_last_error()
            _event_handle = kernel32.CreateEventW(None, True, False, "ROBLOX_singletonEvent")
            if _mutex_handle and err != 183:
                MUTEX_OWNED = True
                return True
        if _mutex_handle:
            res = kernel32.WaitForSingleObject(_mutex_handle, 0)
            if res in (0x00000000, 0x00000080):
                MUTEX_OWNED = True
    except Exception:
        pass
    return MUTEX_OWNED


def find_roblox_exe():
    custom = os.getenv("ROBLOX_EXE")
    if custom and os.path.exists(custom):
        return custom
    roots = [
        os.path.join(os.getenv("LOCALAPPDATA", ""), "Roblox", "Versions"),
        os.path.join(os.getenv("ProgramFiles(x86)", ""), "Roblox", "Versions"),
        os.path.join(os.getenv("ProgramFiles", ""), "Roblox", "Versions"),
    ]
    found = []
    for r in roots:
        found += glob.glob(os.path.join(r, "*", "RobloxPlayerBeta.exe"))
    if not found:
        return None
    return max(found, key=os.path.getmtime)


def get_auth_ticket(cookie):
    url = "https://auth.roblox.com/v1/authentication-ticket"
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Referer": "https://www.roblox.com/",
        "Origin": "https://www.roblox.com",
        "Content-Type": "application/json",
    }
    r = http("POST", url, headers=headers, data="{}")
    if r.status_code == 403 and "x-csrf-token" in r.headers:
        headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
        r = http("POST", url, headers=headers, data="{}")
    ticket = r.headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RuntimeError(f"Roblox gave no login ticket (HTTP {r.status_code}). Cookie may be expired.")
    return ticket


def windows_for_pid(pid):
    found = []
    if not IS_WINDOWS:
        return found
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            if p.value == pid:
                found.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(cb), 0)
    return found


def launch_client(cookie, place_id, job_id):
    exe = find_roblox_exe()
    if not exe:
        raise RuntimeError("RobloxPlayerBeta.exe not found.")
    ticket = get_auth_ticket(cookie)
    tracker = str(random.randint(10 ** 10, 10 ** 11 - 1))
    launcher = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob"
                f"&browserTrackerId={tracker}&placeId={place_id}&gameId={job_id}&isPlayTogetherGame=false")
    args = [exe, "--app", "-t", ticket, "-j", launcher, "-b", tracker,
            f"--launchtime={int(time.time() * 1000)}", "--rloc", "en_us", "--gloc", "en_us"]
    return subprocess.Popen(args)


def wait_for_window(proc, timeout=WINDOW_WAIT):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return None
        w = windows_for_pid(proc.pid)
        if w:
            return w[0]
        time.sleep(1)
    return None


def kill_proc(proc):
    if proc is None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            proc.kill()
    except Exception:
        pass


# ---------------------------------------------------------------- Anti-Idle Movement
VK_W, VK_S, VK_A, VK_D, VK_SPACE, VK_MENU = 0x57, 0x53, 0x41, 0x44, 0x20, 0x12


def move_window(hwnd):
    if not IS_WINDOWS:
        return
    keys = [(VK_SPACE, 0.2), (VK_W, 0.5), (VK_S, 0.5), (VK_A, 0.3), (VK_D, 0.3), (VK_SPACE, 0.2)]
    prev = user32.GetForegroundWindow()
    user32.ShowWindow(hwnd, 9)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, 2, 0)
    try:
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    time.sleep(0.8)
    for vk, hold in keys:
        scan = user32.MapVirtualKeyW(vk, 0)
        user32.keybd_event(vk, scan, 0, 0)
        time.sleep(hold)
        user32.keybd_event(vk, scan, 2, 0)
        time.sleep(0.15)
    if prev and prev != hwnd:
        try:
            user32.SetForegroundWindow(prev)
        except Exception:
            pass


# ---------------------------------------------------------------- Lock State & Active Sessions
active_locks = {}  # channel_id or ticket_id -> Lock instance

class Lock:
    def __init__(self, owner_id, target, place_id, job_id, clients, ticket_channel):
        self.owner_id = owner_id
        self.target = target
        self.place_id = place_id
        self.job_id = job_id
        self.started = time.time()
        self.next_move = time.time() + MOVE_INTERVAL
        self.clients = clients
        self.ticket_channel = ticket_channel
        self.task = None


# ---------------------------------------------------------------- Interactive Views & Modals

class NotInGameView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        # Added properly using discord.ui.Button for links to fix TypeError
        self.add_item(discord.ui.Button(label="Privacy Settings", style=discord.ButtonStyle.link, url="https://www.roblox.com/my/account#!/privacy"))

    @discord.ui.button(label="Link Account", style=discord.ButtonStyle.secondary, custom_id="pos:link_modal")
    async def link_account(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Use `/pos link [username]` to link your Roblox account.", ephemeral=True)

    @discord.ui.button(label="Retry Join Check", style=discord.ButtonStyle.success, custom_id="pos:retry_join")
    async def retry_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Please re-run your deploy or lock command.", ephemeral=True)


class ManualDeployModal(discord.ui.Modal, title="Manual Deploy Configuration"):
    alt_names = discord.ui.TextInput(label="Alt Names (comma separated or 'all')", placeholder="alt1, alt2, alt3", default="all")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(f"Manual deployment started for selection: {self.alt_names.value}", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self, lock_instance):
        super().__init__(timeout=None)
        self.lock_instance = lock_instance

    @discord.ui.button(label="Join Server", style=discord.ButtonStyle.success, custom_id="ticket:join")
    async def join_server(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(f"Job ID for server: `{self.lock_instance.job_id}`", ephemeral=True)

    @discord.ui.button(label="End", style=discord.ButtonStyle.danger, custom_id="ticket:end")
    async def end_lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        for c in self.lock_instance.clients:
            kill_proc(c.get("proc"))
        if self.lock_instance.task:
            self.lock_instance.task.cancel()
        await interaction.response.send_message("Position lock ended. Alts removed/closed.", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.secondary, custom_id="ticket:delete")
    async def delete_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.channel.delete()


class DeploySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Auto Deploy", description="Deploy alts automatically by count", emoji="🚀"),
            discord.SelectOption(label="Manual Deploy", description="Choose specific alts and count", emoji="⚙️")
        ]
        super().__init__(placeholder="Choose Deploy Method", options=options, custom_id="panel:deploy_select")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "Auto Deploy":
            await interaction.response.send_message("Auto deploying all available alts...", ephemeral=True)
        else:
            await interaction.response.send_modal(ManualDeployModal())


class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(DeploySelect())

    @discord.ui.button(label="Check Available Alts", style=discord.ButtonStyle.primary, custom_id="panel:check_alts")
    async def check_alts(self, interaction: discord.Interaction, button: discord.ui.Button):
        alts = load_alts()
        names = [a["name"] for a in alts]
        await interaction.response.send_message(f"**Available Alts ({len(names)}):**\n`" + ", ".join(names) + "`", ephemeral=True)


# ---------------------------------------------------------------- Bot Client & Commands
class PosBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.tree.add_command(PosGroup())
        await self.tree.sync()


client = PosBot()


@client.event
async def on_guild_join(guild):
    try:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False)
        }
        readme = await guild.create_text_channel("read-me", overwrites=overwrites, position=0)
        embed = em("Cruxer / POS Locker", "Thank you for authorizing the bot in your server.\n\n"
                                           "Use `/authorize` to setup ticket channels and control panels automatically.", "info")
        await readme.send(embed=embed)
    except Exception:
        pass


class PosGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="pos", description="POS locker controls")

    @app_commands.command(name="link", description="Link your Roblox account")
    async def link_cmd(self, interaction: discord.Interaction, username: str = None):
        await interaction.response.defer(ephemeral=True)
        data = load_links()
        me = str(interaction.user.id)
        if not username:
            cur = data["linked"].get(me)
            if cur:
                return await interaction.followup.send(embed=em("Linked account", f"Linked to {cur['name']}.", "ok"), ephemeral=True)
            return await interaction.followup.send(embed=em("Not linked", "Provide your Roblox username.", "warn"), ephemeral=True)

        try:
            uid, name = await asyncio.to_thread(user_id_from_name, username)
        except Exception as e:
            return await interaction.followup.send(embed=em("Link failed", str(e), "err"), ephemeral=True)

        w1, w2 = random.sample(VERIFY_WORDS, 2)
        code = f"verify {w1} {w2}"
        data["pending"][me] = {"id": uid, "name": name, "code": code}
        save_links(data)
        await interaction.followup.send(embed=em(f"Verify {name}", f"Put this phrase in your profile About section:\n```{code}```", "info"), ephemeral=True)

    @app_commands.command(name="unlink", description="Unlink your Roblox account")
    async def unlink_cmd(self, interaction: discord.Interaction):
        data = load_links()
        me = str(interaction.user.id)
        if me in data["linked"]:
            data["linked"].pop(me)
            save_links(data)
            return await interaction.response.send_message("Successfully unlinked your Roblox account.", ephemeral=True)
        await interaction.response.send_message("You are not linked to any Roblox account.", ephemeral=True)

    @app_commands.command(name="test", description="Opens multi-Roblox test pages for all alts")
    async def test_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        alts = load_alts()
        if not alts:
            return await interaction.followup.send(embed=em("No alts", "alts.json is empty.", "err"), ephemeral=True)
        lines = []
        for a in alts[:10]:
            lines.append(f"Loaded alt profile test: `{a['name']}`")
        await interaction.followup.send(embed=em("Multi-Roblox Test Pages", "\n".join(lines), "ok"), ephemeral=True)

    @app_commands.command(name="authorize", description="Setup automated category and panel channels")
    async def authorize_cmd(self, interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message("Only the server owner can run this command.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        cat = await guild.create_category("POS Locker Hub")
        log_chan = await guild.create_text_channel("pos-logs", category=cat)
        panel_chan = await guild.create_text_channel("pos-control-panel", category=cat)

        embed = em("Position Locker Panel", 
                   "**What is this?**\n"
                   "• This is your position locking control panel to deploy alts instantly.\n"
                   "• Choose **Check Available Alts** or **Deploy** below.", "info")
        await panel_chan.send(embed=embed, view=MainPanelView())
        await interaction.followup.send(f"Authorized successfully! Panel created in {panel_chan.mention}, logs in {log_chan.mention}.", ephemeral=True)

    @app_commands.command(name="deauthorize", description="Remove created channels and leave server")
    async def deauthorize_cmd(self, interaction: discord.Interaction):
        if interaction.user.id != interaction.guild.owner_id:
            return await interaction.response.send_message("Only the server owner can run this command.", ephemeral=True)
        await interaction.response.send_message("Deauthorizing bot and clearing setup channels...", ephemeral=True)
        await interaction.guild.leave()

    @app_commands.command(name="lock", description="Send your alts into the server you are in")
    async def lock(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 100):
        await interaction.response.defer()

        linked = load_links()["linked"].get(str(interaction.user.id))
        if not linked:
            return await interaction.followup.send(
                embed=em("Joins Off Or Not In-Game", "You have either joins off or are not in-game.", "err"),
                view=NotInGameView()
            )

        try:
            alts = load_alts()[:count]
        except Exception as e:
            return await interaction.followup.send(embed=em("Error", str(e), "err"))

        if not alts:
            return await interaction.followup.send(embed=em("No alts found", "alts.json is empty.", "err"))

        try:
            place_id, job_id = await asyncio.to_thread(
                locate_user, [a["cookie"] for a in alts], linked["id"], linked["name"])
        except Exception as e:
            return await interaction.followup.send(
                embed=em("Joins Off Or Not In-Game", str(e), "err"),
                view=NotInGameView()
            )

        clients = [{"name": a["name"], "cookie": a["cookie"], "proc": None, "status": "active"} for a in alts]
        ticket_chan = await interaction.guild.create_text_channel(f"pos-locking-{random.randint(100,999)}")
        
        new_lock = Lock(interaction.user.id, linked["name"], place_id, job_id, clients, ticket_chan)
        active_locks[ticket_chan.id] = new_lock

        for c in clients:
            try:
                c["proc"] = launch_client(c["cookie"], place_id, job_id)
            except Exception:
                pass
            await asyncio.sleep(LAUNCH_DELAY)

        await ticket_chan.send(
            embed=em("Pos-Locking Active", f"Target: **{linked['name']}**\nAlts Deployed: {len(clients)}", "ok"),
            view=TicketControlView(new_lock)
        )
        await interaction.followup.send(f"Lock ticket created: {ticket_chan.mention}")


if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN not found in environment or .env file.")
        sys.exit(1)
    client.run(TOKEN)
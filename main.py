"""
POS Locker Discord bot - runs real Roblox clients for your alts (Windows only)

Commands:
  /pos link [username]  - Link your Roblox account using a word phrase
  /pos lock [count]     - Finds the server you are in and makes your alts join it
                          (one Roblox window per alt, they auto-move every 15 min)
  /pos stats            - Show the current lock: alts, uptime, next move
  /pos stop             - End the lock and close every alt's Roblox window
  /pos check            - Test every cookie in alts.json

Needs:  pip install discord.py requests python-dotenv
Files next to this one:
  .env      ->  DISCORD_TOKEN=xxxx
                (optional) GUILD_ID, ROBLOX_EXE, MOVE_INTERVAL_MIN, MOVE_MODE, LAUNCH_DELAY,
                CLOSE_HOST_ROBLOX=1
  alts.json ->  [{"name": "alt1", "cookie": "_|WARNING..."}, {"name": "alt2", "cookie": "..."}]
  links.json -> (auto-generated)

Run this on a Windows PC with the normal Roblox player (roblox.com, not the Microsoft
Store version). Players use /pos lock from their OWN devices, after they are already in
the server. The bot PC itself should not have its own Roblox open.
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

MOVE_INTERVAL = float(os.getenv("MOVE_INTERVAL_MIN", "15")) * 60   # seconds between anti-idle moves
LAUNCH_DELAY = float(os.getenv("LAUNCH_DELAY", "8"))               # seconds between launching each alt
WINDOW_WAIT = 120                                                  # seconds to wait for a client window
MOVE_MODE = os.getenv("MOVE_MODE", "focus").lower()                # "focus" (reliable) or "post" (silent, less reliable)
CLOSE_HOST_ROBLOX = os.getenv("CLOSE_HOST_ROBLOX", "0") == "1"     # auto-close stray Roblox on the bot PC before locking
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


# ---------------------------------------------------------------- Roblox client launching (Windows)
_mutex_handle = None
_event_handle = None
MUTEX_OWNED = False


def enable_multi_instance():
    """Own Roblox's singleton mutex so several clients can run side by side on this PC.
    Safe to call as often as you like: if another Roblox on this PC held the mutex earlier
    and has since closed, this picks the mutex up on the next call.
    Returns True if this bot owns the mutex."""
    global _mutex_handle, _event_handle, MUTEX_OWNED
    if not IS_WINDOWS:
        return False
    if MUTEX_OWNED:
        return True
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    if _mutex_handle is None:
        _mutex_handle = kernel32.CreateMutexW(None, True, "ROBLOX_singletonMutex")
        err = ctypes.get_last_error()
        _event_handle = kernel32.CreateEventW(None, True, False, "ROBLOX_singletonEvent")
        if _mutex_handle and err != 183:           # 183 = it already existed, owned by someone else
            MUTEX_OWNED = True
            return True
    if _mutex_handle:
        # the previous owner (a Roblox that was open) may have closed: try to take it over
        res = kernel32.WaitForSingleObject(_mutex_handle, 0)
        if res in (0x00000000, 0x00000080):          # WAIT_OBJECT_0 or WAIT_ABANDONED
            MUTEX_OWNED = True
    return MUTEX_OWNED


def roblox_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe"],
                             capture_output=True, text=True, timeout=10).stdout
        return "RobloxPlayerBeta.exe" in out
    except Exception:
        return False


def close_all_roblox():
    """Closes every Roblox client on THIS PC (only called when no lock is running)."""
    try:
        subprocess.run(["taskkill", "/IM", "RobloxPlayerBeta.exe", "/F", "/T"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


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
    """Start one Roblox client logged in as the cookie's account, joining the exact server."""
    exe = find_roblox_exe()
    if not exe:
        raise RuntimeError("RobloxPlayerBeta.exe not found. Install Roblox from roblox.com "
                           "(not the Microsoft Store version) or set ROBLOX_EXE in .env.")
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


# ---------------------------------------------------------------- Anti-idle movement
VK_W, VK_S, VK_A, VK_D, VK_SPACE, VK_MENU = 0x57, 0x53, 0x41, 0x44, 0x20, 0x12


def _tap_focus(vk, hold):
    scan = user32.MapVirtualKeyW(vk, 0)
    user32.keybd_event(vk, scan, 0, 0)
    time.sleep(hold)
    user32.keybd_event(vk, scan, 2, 0)
    time.sleep(0.15)


def _tap_post(hwnd, vk, hold):
    scan = user32.MapVirtualKeyW(vk, 0)
    down = 1 | (scan << 16)
    up = 1 | (scan << 16) | (1 << 30) | (1 << 31)
    user32.PostMessageW(hwnd, 0x0100, vk, down)
    time.sleep(hold)
    user32.PostMessageW(hwnd, 0x0101, vk, up)
    time.sleep(0.15)


def move_window(hwnd):
    """Walk around a little so Roblox does not kick the alt for being idle."""
    keys = [(VK_SPACE, 0.2), (VK_W, 0.5), (VK_S, 0.5), (VK_A, 0.3), (VK_D, 0.3), (VK_SPACE, 0.2)]
    if MOVE_MODE == "post":
        for vk, hold in keys:
            _tap_post(hwnd, vk, hold)
        return
    prev = user32.GetForegroundWindow()
    user32.ShowWindow(hwnd, 9)                       # restore if minimized
    user32.keybd_event(VK_MENU, 0, 0, 0)             # lets us take the foreground
    user32.keybd_event(VK_MENU, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    for vk, hold in keys:
        _tap_focus(vk, hold)
    if prev and prev != hwnd:
        user32.SetForegroundWindow(prev)             # give the PC back to the user


# ---------------------------------------------------------------- Lock state
class Lock:
    def __init__(self, owner_id, target, place_id, job_id):
        self.owner_id = owner_id
        self.target = target
        self.place_id = place_id
        self.job_id = job_id
        self.started = time.time()
        self.next_move = time.time() + MOVE_INTERVAL
        self.clients = []          # dicts: name, roblox, cookie, proc, hwnd, restarts, last_move, status
        self.task = None


lock_state = None                 # the active Lock or None


def start_one(client, place_id, job_id):
    """Blocking: launch one alt and wait for its window. Returns True on success."""
    try:
        proc = launch_client(client["cookie"], place_id, job_id)
    except Exception as e:
        client["status"] = f"launch failed: {e}"
        client["proc"] = None
        return False
    client["proc"] = proc
    hwnd = wait_for_window(proc)
    client["hwnd"] = hwnd
    if hwnd:
        client["status"] = "in game"
        client["last_move"] = time.time()
        return True
    client["status"] = "window never appeared" if proc.poll() is None else "client closed on start"
    return False


def start_all(clients, place_id, job_id):
    """Blocking: launch alts one by one."""
    ok = 0
    for i, c in enumerate(clients):
        if i:
            time.sleep(LAUNCH_DELAY)
        if start_one(c, place_id, job_id):
            ok += 1
    return ok


def move_all(clients):
    for c in clients:
        proc = c.get("proc")
        if not proc or proc.poll() is not None:
            continue
        hwnd = c.get("hwnd")
        if not hwnd or not user32.IsWindow(hwnd):
            w = windows_for_pid(proc.pid)
            hwnd = w[0] if w else None
            c["hwnd"] = hwnd
        if not hwnd:
            continue
        try:
            move_window(hwnd)
            c["last_move"] = time.time()
        except Exception as e:
            c["status"] = f"move failed: {e}"
        time.sleep(1)


def stop_all(lock):
    for c in lock.clients:
        kill_proc(c.get("proc"))
        c["proc"] = None
        c["status"] = "closed"


async def monitor_loop(lock):
    """Keeps alts alive: relaunches crashed clients and moves everyone every MOVE_INTERVAL."""
    while lock_state is lock:
        await asyncio.sleep(MONITOR_TICK)
        if lock_state is not lock:
            break
        for c in lock.clients:
            proc = c.get("proc")
            dead = proc is None or proc.poll() is not None
            if dead and c["restarts"] < MAX_RESTARTS and c.get("status") != "closed":
                c["restarts"] += 1
                c["status"] = f"relaunching (try {c['restarts']}/{MAX_RESTARTS})"
                await asyncio.to_thread(start_one, c, lock.place_id, lock.job_id)
                await asyncio.sleep(LAUNCH_DELAY)
            elif dead:
                c["status"] = "dead (out of retries)"
        if lock_state is lock and time.time() >= lock.next_move:
            await asyncio.to_thread(move_all, lock.clients)
            lock.next_move = time.time() + MOVE_INTERVAL


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


# ---------------------------------------------------------------- Verify View
class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="pos:verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        data = load_links()
        me = str(interaction.user.id)
        pend = data["pending"].get(me)
        if not pend:
            return await interaction.followup.send(
                embed=em("Nothing to verify", "Run /pos link with your Roblox username first.", "warn"),
                ephemeral=True)
        try:
            desc = await asyncio.to_thread(get_description, pend["id"])
        except Exception as e:
            return await interaction.followup.send(embed=em("Check failed", str(e), "err"), ephemeral=True)

        if pend["code"].lower().strip() not in desc.lower():
            return await interaction.followup.send(
                embed=em("Code not found",
                         f"I can't see `{pend['code']}` in the About section of {pend['name']} yet. "
                         "Save your profile, wait a few seconds, then press Verify again.", "warn"),
                ephemeral=True)

        data["linked"][me] = {"id": pend["id"], "name": pend["name"]}
        data["pending"].pop(me, None)
        save_links(data)
        await interaction.followup.send(
            embed=em("Account linked",
                     f"Successfully linked to **{pend['name']}**! You can remove the phrase from your profile now.\n"
                     "Join your server, then run `/pos lock`", "ok"),
            ephemeral=True)


# ---------------------------------------------------------------- Command Group
class PosGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="pos", description="POS locker controls")

    @app_commands.command(name="link", description="Link your Roblox account")
    @app_commands.describe(username="Your Roblox username (leave empty to see current link)")
    async def link_cmd(self, interaction: discord.Interaction, username: str = None):
        await interaction.response.defer(ephemeral=True)
        data = load_links()
        me = str(interaction.user.id)

        if not username:
            cur = data["linked"].get(me)
            if cur:
                return await interaction.followup.send(
                    embed=em("Linked account", f"You are linked to {cur['name']}.", "ok"), ephemeral=True)
            return await interaction.followup.send(
                embed=em("Not linked", "Run /pos link with your Roblox username.", "warn"), ephemeral=True)

        try:
            uid, name = await asyncio.to_thread(user_id_from_name, username)
        except Exception as e:
            return await interaction.followup.send(embed=em("Link failed", str(e), "err"), ephemeral=True)

        w1, w2 = random.sample(VERIFY_WORDS, 2)
        code = f"verify {w1} {w2}"
        data["pending"][me] = {"id": uid, "name": name, "code": code}
        save_links(data)

        await interaction.followup.send(
            embed=em(f"Verify {name}",
                     "Put this exact phrase anywhere in your Roblox profile About section, save it, "
                     f"then press **Verify** below.\n\n```{code}```", "info"),
            view=VerifyView(), ephemeral=True)

    @app_commands.command(name="lock", description="Send your alts into the server you are in")
    @app_commands.describe(count="How many alts to send (default: all in alts.json)")
    async def lock(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 100):
        global lock_state
        await interaction.response.defer()

        if not IS_WINDOWS:
            return await interaction.followup.send(embed=em(
                "Windows only", "Launching Roblox clients only works when the bot runs on a Windows PC.", "err"))

        linked = load_links()["linked"].get(str(interaction.user.id))
        if not linked:
            return await interaction.followup.send(
                embed=em("No account linked", "Run `/pos link` with your Roblox username first.", "err"))

        if lock_state is not None:
            return await interaction.followup.send(embed=em(
                "Lock already running",
                f"A lock for **{lock_state.target}** is active. Run `/pos stop` first.", "warn"))

        if not find_roblox_exe():
            return await interaction.followup.send(embed=em(
                "Roblox not found",
                "Install Roblox from roblox.com on the PC running this bot (not the Microsoft Store version), "
                "or set ROBLOX_EXE in .env.", "err"))

                # Best effort: close stray Roblox on the bot PC, then try to grab the mutex
        if not enable_multi_instance():
            await asyncio.to_thread(close_all_roblox)
            await asyncio.sleep(3)
            enable_multi_instance()

        try:
            alts = load_alts()
        except Exception as e:
            return await interaction.followup.send(embed=em("Cannot read alts.json", str(e), "err"))
        if not alts:
            return await interaction.followup.send(embed=em("No alts", "alts.json is empty.", "err"))
        alts = alts[:count]

        # validate cookies first so we only launch working alts
        async def verify_alt(a):
            try:
                _, rname = await asyncio.to_thread(whoami, a["cookie"])
                return {"name": a["name"], "roblox": rname, "cookie": a["cookie"], "proc": None,
                        "hwnd": None, "restarts": 0, "last_move": 0, "status": "queued"}, None
            except Exception as e:
                return None, f"{a['name']}: {e}"

        results = await asyncio.gather(*(verify_alt(a) for a in alts))
        clients = [c for c, _ in results if c]
        bad = [err for _, err in results if err]
        if not clients:
            return await interaction.followup.send(embed=em(
                "No working alts", "\n".join(bad)[:3900] or "All cookies were rejected.", "err"))

        # find the requester's server (they are already in it)
        try:
            place_id, job_id = await asyncio.to_thread(
                locate_user, [c["cookie"] for c in clients], linked["id"], linked["name"])
        except Exception as e:
            return await interaction.followup.send(embed=em("Server not found", str(e), "err"))

        new_lock = Lock(interaction.user.id, linked["name"], place_id, job_id)
        new_lock.clients = clients
        lock_state = new_lock

        await interaction.followup.send(embed=em(
            "Server found - launching alts",
            f"Locking **{linked['name']}**'s server with {len(clients)} alt(s). "
            "This takes about a minute per alt, I will post when it finishes.", "info"))

        ok = await asyncio.to_thread(start_all, clients, place_id, job_id)

        if lock_state is not new_lock:           # /pos stop was used while launching
            stop_all(new_lock)
            return
        if ok == 0:
            stop_all(new_lock)
            lock_state = None
            lines = "\n".join(f"{c['name']}: {c['status']}" for c in clients)
            return await interaction.channel.send(embed=em(
                "Lock failed", f"No alt could join.\n{lines}"[:3900], "err"))

        new_lock.next_move = time.time() + MOVE_INTERVAL
        new_lock.task = asyncio.create_task(monitor_loop(new_lock))

        lines = [f"[{'OK' if c['status'] == 'in game' else 'FAIL'}] {c['name']} ({c['roblox']}) - {c['status']}"
                 for c in clients] + [f"[SKIPPED] {b}" for b in bad]
        await interaction.channel.send(embed=em(
            "Position lock active", "\n".join(lines)[:3900], "ok",
            [("Target", linked["name"], True),
             ("Alts in server", f"{ok} / {len(clients)}", True),
             ("Move interval", f"{int(MOVE_INTERVAL // 60)} min", True),
             ("Job / Server ID", f"`{job_id}`", False)]))

    @app_commands.command(name="stats", description="Show the active position lock")
    async def stats(self, interaction: discord.Interaction):
        lk = lock_state
        if lk is None:
            return await interaction.response.send_message(
                embed=em("Idle", "No position lock is running.", "info"))
        lines = []
        for c in lk.clients:
            alive = c["proc"] is not None and c["proc"].poll() is None
            moved = f"moved {fmt_dur(time.time() - c['last_move'])} ago" if c["last_move"] else "not moved yet"
            lines.append(f"[{'UP' if alive else 'DOWN'}] {c['name']} ({c['roblox']}) - {c['status']}, {moved}, "
                         f"restarts {c['restarts']}")
        up = sum(1 for c in lk.clients if c["proc"] is not None and c["proc"].poll() is None)
        await interaction.response.send_message(embed=em(
            "Position lock active", "\n".join(lines)[:3900], "ok",
            [("Target", lk.target, True),
             ("Requested by", f"<@{lk.owner_id}>", True),
             ("Alts up", f"{up} / {len(lk.clients)}", True),
             ("Running for", fmt_dur(time.time() - lk.started), True),
             ("Next move in", fmt_dur(lk.next_move - time.time()), True),
             ("Job / Server ID", f"`{lk.job_id}`", False)]))

    @app_commands.command(name="stop", description="End the position lock and close the alts")
    async def stop(self, interaction: discord.Interaction):
        global lock_state
        lk = lock_state
        if lk is None:
            return await interaction.response.send_message(
                embed=em("Idle", "No position lock is running.", "info"), ephemeral=True)
        perms = getattr(interaction.user, "guild_permissions", None)
        if interaction.user.id != lk.owner_id and not (perms and perms.manage_guild):
            return await interaction.response.send_message(embed=em(
                "Not allowed", "Only the person who started the lock or a server manager can stop it.", "err"),
                ephemeral=True)
        await interaction.response.defer()
        lock_state = None
        if lk.task:
            lk.task.cancel()
        await asyncio.to_thread(stop_all, lk)
        await interaction.followup.send(embed=em(
            "Position lock ended",
            f"Closed {len(lk.clients)} alt client(s). Ran for {fmt_dur(time.time() - lk.started)}.", "ok"))

    @app_commands.command(name="check", description="Test every cookie in alts.json")
    async def check(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            alts = load_alts()
        except Exception as e:
            return await interaction.followup.send(embed=em("Cannot read alts.json", str(e), "err"), ephemeral=True)

        sem = asyncio.Semaphore(API_PARALLEL)

        async def test(alt):
            async with sem:
                try:
                    _, name = await asyncio.to_thread(whoami, alt["cookie"])
                    return True, f"[OK] {alt['name']} - {name}"
                except Exception as e:
                    return False, f"[FAIL] {alt['name']} - {e}"

        res = await asyncio.gather(*(test(a) for a in alts))
        good = sum(1 for ok, _ in res if ok)
        await interaction.followup.send(
            embed=em("Cookie check", "\n".join(t for _, t in res)[:3900],
                     "ok" if good == len(res) else "warn", [("Working", f"{good} / {len(res)}", True)]),
            ephemeral=True)


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(VerifyView())
        self.tree.add_command(PosGroup())
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def close(self):
        global lock_state
        if lock_state is not None:
            lk, lock_state = lock_state, None
            if lk.task:
                lk.task.cancel()
            stop_all(lk)
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        if IS_WINDOWS:
            print(f"Roblox client: {find_roblox_exe() or 'NOT FOUND'}")
            if MUTEX_OWNED:
                print("Multi-client lock: OK")
            else:
                print("Multi-client lock: waiting (Roblox is open on this PC). "
                      "It is picked up automatically once that Roblox is closed.")
        else:
            print("WARNING: not Windows - /pos lock cannot launch Roblox clients here.")


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("DISCORD_TOKEN missing in .env")
    if IS_WINDOWS:
        enable_multi_instance()
    Bot().run(TOKEN)
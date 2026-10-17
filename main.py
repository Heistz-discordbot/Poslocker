"""
POS Locker - Discord bot (Railway, 24/7)  +  Windows agent (launches the real Roblox clients)

ONE file, two modes
  py main.py          -> the Discord bot (run this on Railway; set EMBEDDED_AGENT=1 to also run
                         the agent inside the same process on a Windows PC)
  py main.py agent    -> the Windows agent (run this on the PC that has Roblox installed)

NEVER run the bot twice with the same DISCORD_TOKEN (that is what caused error 40060).

Bot environment (Railway variables or .env)
  DISCORD_TOKEN=xxxx          AGENT_KEY=<long random secret, same value on the agent>
  OWNER_ID=<your discord id>  (optional, defaults to the application owner)
  GUILD_ID=<optional, instant command sync for one server>
  DATA_DIR=/data              (optional, point it at a Railway Volume so tickets survive redeploys)
  PORT                        (Railway sets this itself)

Agent environment (.env next to main.py on the Windows PC)
  BOT_URL=https://your-app.up.railway.app      AGENT_KEY=<same secret>
  (optional) ROBLOX_EXE, MOVE_INTERVAL_MIN, MOVE_MODE, LAUNCH_DELAY, CLOSE_HOST_ROBLOX=1
  alts.json -> [{"name": "alt1", "cookie": "_|WARNING..."}, ...]   (stays on the agent PC only)

Pip:  pip install -U "discord.py>=2.6" requests python-dotenv aiohttp playwright
      python -m playwright install chromium        (needed for /test)
      (discord.py 2.6+ is required for the new panel layout: Components V2)
"""

import asyncio
import glob
import hmac
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid

import aiohttp
import discord
import requests
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
AGENT_KEY = os.getenv("AGENT_KEY", "")
BOT_URL = os.getenv("BOT_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
EMBEDDED_AGENT = os.getenv("EMBEDDED_AGENT", "0") == "1"

# ---------------------------------------------------------------- Settings
API_PARALLEL = 15
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"
PRIVACY_URL = "https://www.roblox.com/my/account#!/privacy"

MOVE_INTERVAL = float(os.getenv("MOVE_INTERVAL_MIN", "15")) * 60
LAUNCH_DELAY = float(os.getenv("LAUNCH_DELAY", "8"))
WINDOW_WAIT = 120
MOVE_MODE = os.getenv("MOVE_MODE", "focus").lower()
CLOSE_HOST_ROBLOX = os.getenv("CLOSE_HOST_ROBLOX", "0") == "1"
MAX_RESTARTS = 3
MONITOR_TICK = 15
POLL_EVERY = 3            # agent -> bot poll seconds
TICKET_REFRESH = 20       # ticket message refresh seconds

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or BASE
os.makedirs(DATA_DIR, exist_ok=True)
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
DB_FILE = os.path.join(DATA_DIR, "db.json")
ALTS_FILE = os.getenv("ALTS_FILE") or os.path.join(BASE, "alts.json")

# Your server's existing channels (used by /authorize when they exist in the server it runs in)
DEFAULT_IDS = {
    "panel_category": 1560667887159742487,
    "panel_channel": 1560668012120645693,
    "log_channel": 1560668040025346068,
    "ticket_category": 1560667942927077556,
}

VERIFY_WORDS = [
    "apple", "banana", "cherry", "dragon", "eagle", "falcon", "forest", "galaxy",
    "hammer", "island", "jungle", "knight", "lemon", "mango", "ninja", "orange",
    "planet", "quantum", "rocket", "shadow", "tiger", "umbrella", "violet", "wizard"
]
COLORS = {"info": 0x5865F2, "ok": 0x2ECC71, "warn": 0xF1C40F, "err": 0xE74C3C}
AGENT_OFFLINE = ("The alt agent (the Windows PC that runs the Roblox clients) is offline. "
                 "Start `py main.py agent` on that PC and try again.")

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


def fmt_dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s"


class AgentError(RuntimeError):
    def __init__(self, msg, code="other"):
        super().__init__(msg)
        self.code = code


# ================================================================ Roblox HTTP helpers
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


def get_description(uid):
    r = http("GET", f"https://users.roblox.com/v1/users/{uid}")
    r.raise_for_status()
    return r.json().get("description") or ""


def fetch_headshots(ids):
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            r = http("GET", "https://thumbnails.roblox.com/v1/users/avatar-headshot",
                     params={"userIds": ",".join(map(str, chunk)), "size": "150x150", "format": "Png"})
            for d in r.json().get("data", []):
                if d.get("imageUrl"):
                    out[d["targetId"]] = d["imageUrl"]
        except Exception:
            pass
    return out


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
    """Finds the server the user is standing in. Raises AgentError with code
    'not_in_game' / 'hidden' so the bot can show the 'Joins Off Or Not In-Game' notice."""
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
        raise AgentError(f"{name} is in a game, but Roblox hides the server details.", "hidden")
    if seen in (0, 1):
        raise AgentError(f"{name} is not in a game right now.", "not_in_game")
    raise AgentError(f"Could not read {name}'s server. Try again in a few seconds.", "other")


# ================================================================ Storage (bot side)
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


def load_db():
    default = {"guilds": {}, "tickets": {}, "counter": 0}
    try:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k, v in default.items():
                d.setdefault(k, v)
            return d
    except Exception:
        pass
    return default


DB = load_db()


def save_db():
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, indent=2)
    os.replace(tmp, DB_FILE)


# ================================================================ AGENT SIDE (Windows)
_mutex_handle = None
_event_handle = None
MUTEX_OWNED = False


def enable_multi_instance():
    """Own Roblox's singleton mutex so several clients can run side by side."""
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
        if _mutex_handle and err != 183:
            MUTEX_OWNED = True
            return True
    if _mutex_handle:
        res = kernel32.WaitForSingleObject(_mutex_handle, 0)
        if res in (0x00000000, 0x00000080):
            MUTEX_OWNED = True
    return MUTEX_OWNED


def close_all_roblox():
    try:
        subprocess.run(["taskkill", "/IM", "RobloxPlayerBeta.exe", "/F", "/T"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def ensure_multi():
    if enable_multi_instance():
        return True
    if CLOSE_HOST_ROBLOX:
        close_all_roblox()
        time.sleep(3)
        return enable_multi_instance()
    return False


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


def wait_for_window(proc, client, timeout=WINDOW_WAIT):
    end = time.time() + timeout
    while time.time() < end:
        if client.get("cancel") or proc.poll() is not None:
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
    keys = [(VK_SPACE, 0.2), (VK_W, 0.5), (VK_S, 0.5), (VK_A, 0.3), (VK_D, 0.3), (VK_SPACE, 0.2)]
    if MOVE_MODE == "post":
        for vk, hold in keys:
            _tap_post(hwnd, vk, hold)
        return
    prev = user32.GetForegroundWindow()
    user32.ShowWindow(hwnd, 9)
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, 2, 0)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.8)
    for vk, hold in keys:
        _tap_focus(vk, hold)
    if prev and prev != hwnd:
        user32.SetForegroundWindow(prev)


def start_one(client, place_id, job_id):
    """Blocking: launch one alt and wait for its window."""
    if client.get("cancel"):
        return False
    client["status"] = "launching"
    try:
        proc = launch_client(client["cookie"], place_id, job_id)
    except Exception as e:
        client["status"] = f"launch failed: {e}"[:120]
        client["proc"] = None
        return False
    client["proc"] = proc
    if client.get("cancel"):
        kill_proc(proc)
        return False
    hwnd = wait_for_window(proc, client)
    if client.get("cancel"):
        kill_proc(proc)
        return False
    client["hwnd"] = hwnd
    if hwnd:
        client["status"] = "in game"
        client["last_move"] = time.time()
        return True
    client["status"] = "window never appeared" if proc.poll() is None else "client closed on start"
    return False


def move_all(clients):
    for c in clients:
        if c.get("cancel"):
            continue
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
            c["status"] = f"move failed: {e}"[:120]
        time.sleep(1)


class AgentLock:
    def __init__(self, tid, place_id, job_id, clients):
        self.tid = tid
        self.place_id = place_id
        self.job_id = job_id
        self.clients = clients          # name -> dict
        self.next_move = time.time() + MOVE_INTERVAL
        self.task = None
        self.stopped = False


class Agent:
    """Runs on the Windows PC. Polls the bot for commands, launches/monitors Roblox clients."""

    def __init__(self, base_url, key):
        self.url = base_url.rstrip("/") + "/agent/poll"
        self.key = key
        self.cookies = {}
        self.alt_info = []
        self.last_refresh = 0
        self.locks = {}
        self.pending_results = []
        self.launch_sem = asyncio.Semaphore(1)
        self.move_lock = asyncio.Lock()
        self._pw = None
        self._browser = None

    # ---- alts
    async def refresh_alts(self):
        try:
            alts = await asyncio.to_thread(load_alts)
        except Exception as e:
            print(f"[agent] cannot read alts.json: {e}")
            self.alt_info = []
            return
        self.cookies = {a["name"]: a["cookie"] for a in alts}
        sem = asyncio.Semaphore(API_PARALLEL)

        async def one(a):
            async with sem:
                try:
                    uid, rn = await asyncio.to_thread(whoami, a["cookie"])
                    return {"name": a["name"], "roblox": rn, "user_id": uid, "ok": True, "error": None}
                except Exception as e:
                    return {"name": a["name"], "roblox": None, "user_id": None, "ok": False, "error": str(e)[:100]}

        self.alt_info = list(await asyncio.gather(*(one(a) for a in alts)))
        self.last_refresh = time.time()
        good = sum(1 for i in self.alt_info if i["ok"])
        print(f"[agent] alts checked: {good}/{len(self.alt_info)} working")

    def info(self, name):
        for i in self.alt_info:
            if i["name"] == name:
                return i
        return None

    def snapshot(self):
        now = time.time()
        out = {}
        for tid, lock in self.locks.items():
            cl = {}
            for name, c in lock.clients.items():
                proc = c.get("proc")
                cl[name] = {
                    "status": c["status"],
                    "alive": bool(proc) and proc.poll() is None,
                    "moved_ago": (now - c["last_move"]) if c.get("last_move") else None,
                    "restarts": c["restarts"],
                }
            out[str(tid)] = {"clients": cl, "next_move_in": max(0, lock.next_move - now)}
        return out

    # ---- lock lifecycle
    async def run_lock(self, lock):
        try:
            async with self.launch_sem:
                first = True
                for c in list(lock.clients.values()):
                    if lock.stopped:
                        return
                    if c["status"] == "removed":
                        continue
                    if not first:
                        await asyncio.sleep(LAUNCH_DELAY)
                    first = False
                    await asyncio.to_thread(start_one, c, lock.place_id, lock.job_id)
            lock.next_move = time.time() + MOVE_INTERVAL
            await self.monitor(lock)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[agent] lock {lock.tid} crashed: {e}")

    async def monitor(self, lock):
        while not lock.stopped:
            await asyncio.sleep(MONITOR_TICK)
            for c in list(lock.clients.values()):
                if lock.stopped or c.get("cancel") or c["status"] == "removed":
                    continue
                proc = c.get("proc")
                dead = proc is None or proc.poll() is not None
                if dead and c["restarts"] < MAX_RESTARTS:
                    c["restarts"] += 1
                    c["status"] = f"relaunching (try {c['restarts']}/{MAX_RESTARTS})"
                    async with self.launch_sem:
                        await asyncio.to_thread(start_one, c, lock.place_id, lock.job_id)
                    await asyncio.sleep(LAUNCH_DELAY)
                elif dead:
                    c["status"] = "dead (out of retries)"
            if not lock.stopped and time.time() >= lock.next_move:
                active = [c for c in lock.clients.values() if c["status"] != "removed"]
                async with self.move_lock:
                    await asyncio.to_thread(move_all, active)
                lock.next_move = time.time() + MOVE_INTERVAL

    async def stop_lock(self, tid):
        lock = self.locks.pop(tid, None)
        if not lock:
            return
        lock.stopped = True
        for c in lock.clients.values():
            c["cancel"] = True
        if lock.task:
            lock.task.cancel()
        await asyncio.to_thread(lambda: [kill_proc(c.get("proc")) for c in lock.clients.values()])

    # ---- commands
    async def c_ping(self, cmd):
        return {}

    async def c_refresh(self, cmd):
        await self.refresh_alts()
        return {}

    async def c_locate(self, cmd):
        cookies = [self.cookies[i["name"]] for i in self.alt_info if i.get("ok") and i["name"] in self.cookies]
        if not cookies:
            raise AgentError("No working alt cookies on the agent PC.", "no_alts")
        place, job = await asyncio.to_thread(locate_user, cookies, cmd["user_id"], cmd["name"])
        return {"place_id": place, "job_id": job}

    async def c_deploy(self, cmd):
        if not IS_WINDOWS:
            raise AgentError("The agent must run on Windows to launch Roblox clients.", "platform")
        if not find_roblox_exe():
            raise AgentError("Roblox is not installed on the agent PC (roblox.com version, not Microsoft Store).",
                             "no_roblox")
        tid = int(cmd["tid"])
        if tid in self.locks:
            await self.stop_lock(tid)
        if not await asyncio.to_thread(ensure_multi):
            raise AgentError("Roblox is already open on the agent PC. Close it (or set CLOSE_HOST_ROBLOX=1 "
                             "in the agent's .env) so multiple clients can run.", "mutex")
        clients = {}
        for n in cmd["names"]:
            info = self.info(n)
            if not info or not info.get("ok"):
                continue
            clients[n] = {"name": n, "roblox": info["roblox"], "cookie": self.cookies[n], "proc": None,
                          "hwnd": None, "restarts": 0, "last_move": 0, "status": "queued", "cancel": False}
        if not clients:
            raise AgentError("None of the selected alts have a working cookie.", "no_alts")
        lock = AgentLock(tid, cmd["place_id"], cmd["job_id"], clients)
        self.locks[tid] = lock
        lock.task = asyncio.create_task(self.run_lock(lock))
        return {"count": len(clients)}

    async def c_stop(self, cmd):
        await self.stop_lock(int(cmd["tid"]))
        return {}

    async def c_remove(self, cmd):
        lock = self.locks.get(int(cmd["tid"]))
        if not lock:
            return {}
        procs = []
        for n in cmd["names"]:
            c = lock.clients.get(n)
            if c:
                c["cancel"] = True
                c["status"] = "removed"
                procs.append(c.get("proc"))
        await asyncio.to_thread(lambda: [kill_proc(p) for p in procs])
        return {}

    async def close_homes(self):
        try:
            if self._browser:
                await self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._browser = None
        self._pw = None

    async def c_home(self, cmd):
        """/test: open the roblox.com home page of every alt, each in its own logged-in browser window."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise AgentError("Playwright is missing on the agent PC. Run:  pip install playwright   then   "
                             "python -m playwright install chromium", "playwright")
        await self.close_homes()
        self._pw = await async_playwright().start()
        try:
            try:
                self._browser = await self._pw.chromium.launch(channel="chrome", headless=False)
            except Exception:
                self._browser = await self._pw.chromium.launch(headless=False)
        except Exception as e:
            await self.close_homes()
            raise AgentError(f"Could not start a browser: {str(e)[:200]}", "browser")
        opened, failed = [], []
        for n in cmd["names"]:
            info = self.info(n)
            cookie = self.cookies.get(n)
            if not info or not info.get("ok") or not cookie:
                failed.append(f"{n}: cookie not working")
                continue
            try:
                ctx = await self._browser.new_context(no_viewport=True)
                await ctx.add_cookies([{"name": ".ROBLOSECURITY", "value": cookie, "domain": ".roblox.com",
                                        "path": "/", "httpOnly": True, "secure": True}])
                page = await ctx.new_page()
                await page.goto("https://www.roblox.com/home", wait_until="domcontentloaded", timeout=45000)
                opened.append(f"{n} ({info['roblox']})")
            except Exception as e:
                failed.append(f"{n}: {str(e)[:80]}")
        return {"opened": opened, "failed": failed}

    async def run_cmd(self, cmd):
        res = {"id": cmd["id"], "ok": True, "data": {}}
        try:
            fn = getattr(self, "c_" + cmd["type"], None)
            if fn is None:
                raise AgentError(f"Unknown command {cmd['type']}")
            res["data"] = await fn(cmd)
        except AgentError as e:
            res.update(ok=False, error=str(e), code=e.code)
        except Exception as e:
            res.update(ok=False, error=f"{type(e).__name__}: {e}"[:300], code="other")
        self.pending_results.append(res)

    async def run(self):
        print(f"[agent] connecting to {self.url}")
        await self.refresh_alts()
        async with aiohttp.ClientSession() as s:
            while True:
                body = {"alts": self.alt_info, "locks": self.snapshot(), "results": self.pending_results}
                sent = self.pending_results
                self.pending_results = []
                try:
                    async with s.post(self.url, json=body, headers={"X-Agent-Key": self.key},
                                      timeout=aiohttp.ClientTimeout(total=20)) as r:
                        if r.status == 401:
                            print("[agent] bot rejected AGENT_KEY - check it matches on both sides")
                            self.pending_results = sent + self.pending_results
                            await asyncio.sleep(10)
                            continue
                        data = await r.json()
                except Exception as e:
                    self.pending_results = sent + self.pending_results
                    print(f"[agent] bot unreachable ({type(e).__name__}), retrying...")
                    await asyncio.sleep(5)
                    continue
                for cmd in data.get("commands", []):
                    asyncio.create_task(self.run_cmd(cmd))
                if time.time() - self.last_refresh > 600:
                    self.last_refresh = time.time()
                    asyncio.create_task(self.refresh_alts())
                await asyncio.sleep(POLL_EVERY)

    def shutdown(self):
        for lock in self.locks.values():
            lock.stopped = True
            for c in lock.clients.values():
                c["cancel"] = True
                kill_proc(c.get("proc"))


async def agent_main(base_url, key):
    agent = Agent(base_url, key)
    if IS_WINDOWS:
        print(f"[agent] Roblox client: {find_roblox_exe() or 'NOT FOUND'}")
        print("[agent] Multi-client lock:", "OK" if MUTEX_OWNED else
              "waiting (Roblox is open on this PC - close it, or set CLOSE_HOST_ROBLOX=1)")
    else:
        print("[agent] WARNING: not Windows - this agent cannot launch Roblox clients.")
    try:
        await agent.run()
    finally:
        agent.shutdown()


# ================================================================ BOT SIDE: bridge to the agent
class Bridge:
    def __init__(self):
        self.commands = []
        self.futures = {}
        self.last_seen = 0.0
        self.snap_time = 0.0
        self.alts = []
        self.locks = {}

    @property
    def online(self):
        return time.time() - self.last_seen < 15

    def send(self, type_, **payload):
        self.commands.append({"id": uuid.uuid4().hex, "type": type_, **payload})

    async def call(self, type_, timeout=60, **payload):
        if not self.online:
            raise AgentError(AGENT_OFFLINE, "offline")
        cid = uuid.uuid4().hex
        fut = asyncio.get_running_loop().create_future()
        self.futures[cid] = fut
        self.commands.append({"id": cid, "type": type_, **payload})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise AgentError("The agent PC did not answer in time.", "timeout")
        finally:
            self.futures.pop(cid, None)

    def resolve(self, r):
        fut = self.futures.get(r.get("id"))
        if not fut or fut.done():
            return
        if r.get("ok"):
            fut.set_result(r.get("data") or {})
        else:
            fut.set_exception(AgentError(r.get("error", "Unknown agent error"), r.get("code", "other")))


bridge = Bridge()
PENDING = set()      # ticket ids being deployed right now (so reconcile does not kill them)


# ================================================================ BOT SIDE: data helpers
def active_tickets():
    return [r for r in DB["tickets"].values() if r["status"] == "active"]


def alts_in_use():
    use = {}
    for r in active_tickets():
        for a in r["alts"]:
            if a["name"] not in r["removed"]:
                use[a["name"]] = r["id"]
    return use


def available_alts():
    use = alts_in_use()
    return [a for a in bridge.alts if a.get("ok") and a["name"] not in use]


def guild_cfg(gid):
    return DB["guilds"].get(str(gid))


def can_manage(interaction, rec):
    uid = interaction.user.id
    if uid == rec["owner_id"] or uid in bot.owner_ids:
        return True
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def status_emoji(s):
    s = (s or "").lower()
    if s == "in game":
        return "🟢"
    if s.startswith(("launching", "queued", "relaunching", "waiting")):
        return "🟡"
    if s in ("logged out", "removed", "closed", "ended"):
        return "⚪"
    return "🔴"


def alt_rows(rec):
    snap = bridge.locks.get(str(rec["id"]))
    clients = (snap or {}).get("clients", {})
    off = time.time() - bridge.snap_time
    rows = []
    for a in rec["alts"]:
        n = a["name"]
        row = {"name": n, "roblox": a["roblox"], "status": "queued", "moved_ago": None, "restarts": 0}
        if rec["status"] != "active":
            row.update(rec.get("final", {}).get(n, {"status": "ended"}))
        elif n in rec["removed"]:
            row["status"] = "logged out"
        elif n in clients:
            c = clients[n]
            row.update(status=c["status"], restarts=c["restarts"],
                       moved_ago=None if c["moved_ago"] is None else c["moved_ago"] + off)
        elif not bridge.online:
            row["status"] = "agent offline"
        else:
            row["status"] = "waiting for agent"
        rows.append(row)
    return rows


def join_url(rec):
    return f"https://www.roblox.com/games/start?placeId={rec['place_id']}&gameInstanceId={rec['job_id']}"


def ticket_embed(rec):
    active = rec["status"] == "active"
    rows = alt_rows(rec)
    lines = []
    for r in rows:
        moved = f"moved {fmt_dur(r['moved_ago'])} ago" if r.get("moved_ago") is not None else "not moved yet"
        lines.append(f"{status_emoji(r['status'])} **{r['name']}** ({r['roblox']}) - {r['status']} · "
                     f"{moved} · restarts {r.get('restarts', 0)}")
    up = sum(1 for r in rows if r["status"] == "in game")
    e = discord.Embed(title=f"Pos-Locking #{rec['id']}" + ("" if active else " - Ended"),
                      description="\n".join(lines)[:4000],
                      color=COLORS["ok"] if active else COLORS["warn"])
    e.add_field(name="Owner", value=f"<@{rec['owner_id']}>", inline=True)
    e.add_field(name="Roblox", value=rec["roblox"], inline=True)
    e.add_field(name="Alts up", value=f"{up} / {len(rows)}", inline=True)
    if active:
        e.add_field(name="Started", value=f"<t:{int(rec['started'])}:R>", inline=True)
        snap = bridge.locks.get(str(rec["id"]))
        if snap:
            nxt = int(bridge.snap_time + snap["next_move_in"])
            e.add_field(name="Next move", value=f"<t:{nxt}:R>", inline=True)
        else:
            e.add_field(name="Next move", value="-", inline=True)
    else:
        e.add_field(name="Ran for", value=fmt_dur((rec.get("ended_at") or time.time()) - rec["started"]),
                    inline=True)
    e.add_field(name="Job / Server ID", value=f"`{rec['job_id']}`", inline=False)
    if active:
        e.set_footer(text="Auto-updates every 20s" + ("" if bridge.online else "  |  Agent offline - status may be stale"))
    return e


def log_embed(rec):
    active = rec["status"] == "active"
    rev = rec.get("review")
    rev_txt = "-" if not rev else f"{'⭐' * rev['stars']} ({rev['stars']}/5)" + (f"\n> {rev['comment']}" if rev.get("comment") else "")
    e = discord.Embed(title=f"Pos-Locking #{rec['id']} - {'Active' if active else 'Ended'}",
                      color=COLORS["ok"] if active else COLORS["warn"])
    e.add_field(name="Requested by", value=f"<@{rec['owner_id']}>", inline=True)
    e.add_field(name="Roblox", value=rec["roblox"], inline=True)
    e.add_field(name="Alts", value=str(len(rec["alts"])), inline=True)
    e.add_field(name="Started", value=f"<t:{int(rec['started'])}:f>", inline=True)
    e.add_field(name="Duration", value=fmt_dur((rec.get("ended_at") or time.time()) - rec["started"]), inline=True)
    e.add_field(name="Review", value=rev_txt[:1024], inline=True)
    e.add_field(name="Server", value=f"`{rec['job_id']}`", inline=False)
    return e


# ================================================================ BOT SIDE: dynamic (persistent) buttons
ACTION_HANDLERS = {}
DYN = {}


def _make_dyn(action, label, style):
    class _Btn(discord.ui.DynamicItem[discord.ui.Button], template=rf"pos:{action}:(?P<tid>\d+)"):
        def __init__(self, tid):
            super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"pos:{action}:{tid}"))
            self.tid = int(tid)

        @classmethod
        async def from_custom_id(cls, interaction, item, match, /):
            return cls(int(match["tid"]))

        async def callback(self, interaction):
            await ACTION_HANDLERS[action](interaction, self.tid)

    _Btn.__name__ = f"Dyn_{action}"
    DYN[action] = _Btn


BS = discord.ButtonStyle
_make_dyn("end", "End", BS.danger)
_make_dyn("logout", "Log Out Alt", BS.secondary)
_make_dyn("delete", "Delete Ticket", BS.danger)
_make_dyn("reopen", "Re-open Ticket", BS.success)
_make_dyn("viewalts", "View Alts", BS.secondary)


class RateButton(discord.ui.DynamicItem[discord.ui.Button], template=r"pos:rate:(?P<tid>\d+):(?P<n>[1-5])"):
    def __init__(self, tid, n):
        super().__init__(discord.ui.Button(label=f"{n} ⭐", style=BS.secondary, custom_id=f"pos:rate:{tid}:{n}"))
        self.tid, self.n = int(tid), int(n)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["tid"]), int(match["n"]))

    async def callback(self, interaction):
        rec = DB["tickets"].get(str(self.tid))
        if not rec:
            return await interaction.response.send_message(
                embed=em("Ticket not found", None, "err"), ephemeral=True)
        if interaction.user.id != rec["owner_id"] and interaction.user.id not in bot.owner_ids:
            return await interaction.response.send_message(
                embed=em("Not allowed", "Only the ticket owner can leave a review.", "err"), ephemeral=True)
        await interaction.response.send_modal(ReviewModal(self.tid, self.n))


class ReviewModal(discord.ui.Modal, title="Leave a review"):
    comment = discord.ui.TextInput(label="Comment (optional)", style=discord.TextStyle.paragraph,
                                   required=False, max_length=300)

    def __init__(self, tid, n):
        super().__init__()
        self.tid, self.n = tid, n

    async def on_submit(self, interaction):
        rec = DB["tickets"].get(str(self.tid))
        if rec:
            rec["review"] = {"stars": self.n, "comment": self.comment.value.strip()}
            save_db()
            await update_log(rec)
        await interaction.response.edit_message(
            embed=em("Thanks for the review!", f"You rated this session **{self.n}/5** ⭐", "ok"), view=None)


def ticket_view(rec):
    v = discord.ui.View(timeout=None)
    if rec["status"] == "active":
        v.add_item(DYN["end"](rec["id"]))
        v.add_item(DYN["logout"](rec["id"]))
        v.add_item(discord.ui.Button(label="Join Server", url=join_url(rec)))
    else:
        v.add_item(DYN["delete"](rec["id"]))
    return v


def rate_view(rec):
    v = discord.ui.View(timeout=None)
    for n in range(1, 6):
        v.add_item(RateButton(rec["id"], n))
    return v


def log_view(rec):
    v = discord.ui.View(timeout=None)
    v.add_item(DYN["reopen"](rec["id"]))
    v.add_item(DYN["viewalts"](rec["id"]))
    return v


async def update_log(rec):
    cfg = guild_cfg(rec["guild_id"])
    if not cfg:
        return
    ch = bot.get_channel(cfg.get("log_channel") or 0)
    if ch is None:
        return
    try:
        if rec.get("log_message_id"):
            try:
                await ch.get_partial_message(rec["log_message_id"]).edit(embed=log_embed(rec), view=log_view(rec))
                return
            except discord.NotFound:
                pass
        m = await ch.send(embed=log_embed(rec), view=log_view(rec))
        rec["log_message_id"] = m.id
        save_db()
    except discord.HTTPException as e:
        print(f"[bot] could not write log: {e}")


async def end_ticket(rec, by=None):
    rec["final"] = {r["name"]: {"status": r["status"], "moved_ago": r.get("moved_ago"),
                                "restarts": r.get("restarts", 0)} for r in alt_rows(rec)}
    rec["status"] = "ended"
    rec["ended_at"] = time.time()
    save_db()
    bridge.send("stop", tid=rec["id"])
    ch = bot.get_channel(rec["channel_id"]) if rec.get("channel_id") else None
    if ch is not None:
        try:
            await ch.get_partial_message(rec["message_id"]).edit(embed=ticket_embed(rec), view=ticket_view(rec))
            await ch.send(embed=em("How was your lock?",
                                   "The lock has ended and the alts were closed. Rate this session, "
                                   "then press **Delete Ticket** on the message above when you are done.", "info"),
                          view=rate_view(rec))
        except discord.HTTPException:
            pass
    await update_log(rec)


async def refresh_ticket(rec):
    if bot.get_guild(rec["guild_id"]) is None:
        return
    ch = bot.get_channel(rec["channel_id"]) if rec.get("channel_id") else None
    if ch is None:
        await end_ticket(rec)
        return
    if (bridge.online and str(rec["id"]) not in bridge.locks and rec["id"] not in PENDING
            and time.time() - rec["last_deploy"] > 120):
        names = [a["name"] for a in rec["alts"] if a["name"] not in rec["removed"]]
        if names:
            bridge.send("deploy", tid=rec["id"], place_id=rec["place_id"], job_id=rec["job_id"], names=names)
            rec["last_deploy"] = time.time()
            save_db()
    try:
        await ch.get_partial_message(rec["message_id"]).edit(embed=ticket_embed(rec), view=ticket_view(rec))
    except discord.NotFound:
        pass


async def ticket_loop():
    await bot.wait_until_ready()
    while True:
        await asyncio.sleep(TICKET_REFRESH)
        for rec in list(DB["tickets"].values()):
            if rec["status"] != "active":
                continue
            try:
                await refresh_ticket(rec)
            except Exception as e:
                print(f"[bot] ticket refresh error #{rec['id']}: {e}")


# ---- ticket button handlers
async def _get_rec(interaction, tid, need=None):
    rec = DB["tickets"].get(str(tid))
    if not rec:
        await interaction.response.send_message(embed=em("Ticket not found", "It may have been removed.", "err"),
                                                ephemeral=True)
        return None
    if need and rec["status"] != need:
        await interaction.response.send_message(
            embed=em("Not available", f"This ticket is **{rec['status']}**.", "warn"), ephemeral=True)
        return None
    if not can_manage(interaction, rec):
        await interaction.response.send_message(
            embed=em("Not allowed", "Only the ticket owner or a server manager can do that.", "err"), ephemeral=True)
        return None
    return rec


async def h_end(interaction, tid):
    rec = await _get_rec(interaction, tid, "active")
    if not rec:
        return
    await interaction.response.defer()
    await end_ticket(rec, interaction.user)


async def h_logout(interaction, tid):
    rec = await _get_rec(interaction, tid, "active")
    if not rec:
        return
    left = [a for a in rec["alts"] if a["name"] not in rec["removed"]]
    if not left:
        return await interaction.response.send_message(
            embed=em("Nothing to log out", "Every alt is already out. Press **End** to finish.", "warn"),
            ephemeral=True)
    await interaction.response.send_message(
        embed=em("Log out alts", "Pick the alts to remove from the server. Their Roblox windows will be closed.", "info"),
        view=LogoutView(rec, left), ephemeral=True)


async def h_delete(interaction, tid):
    rec = await _get_rec(interaction, tid, "ended")
    if not rec:
        return
    ch_id = rec.get("channel_id")
    rec["channel_id"] = None
    rec["message_id"] = None
    save_db()
    await update_log(rec)
    await interaction.response.send_message(embed=em("Deleting ticket", "This channel is being deleted.", "warn"),
                                            ephemeral=True)
    ch = bot.get_channel(ch_id) if ch_id else None
    if ch:
        try:
            await ch.delete(reason=f"Ticket #{tid} deleted")
        except discord.HTTPException:
            pass


async def h_reopen(interaction, tid):
    rec = DB["tickets"].get(str(tid))
    if not rec:
        return await interaction.response.send_message(embed=em("Ticket not found", None, "err"), ephemeral=True)
    if not can_manage(interaction, rec):
        return await interaction.response.send_message(
            embed=em("Not allowed", "Only the ticket owner or a server manager can re-open it.", "err"), ephemeral=True)
    if rec["status"] == "active":
        ch = f"<#{rec['channel_id']}>" if rec.get("channel_id") else "its channel"
        return await interaction.response.send_message(
            embed=em("Already active", f"This ticket is running in {ch}.", "warn"), ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await deploy_flow(interaction, [a["name"] for a in rec["alts"]], reopen=rec)


async def h_viewalts(interaction, tid):
    rec = DB["tickets"].get(str(tid))
    if not rec:
        return await interaction.response.send_message(embed=em("Ticket not found", None, "err"), ephemeral=True)
    rows = alt_rows(rec)
    lines = []
    for r in rows:
        moved = f"moved {fmt_dur(r['moved_ago'])} ago" if r.get("moved_ago") is not None else "not moved"
        lines.append(f"{status_emoji(r['status'])} **{r['name']}** ({r['roblox']}) - {r['status']} · {moved} · "
                     f"restarts {r.get('restarts', 0)}")
    await interaction.response.send_message(
        embed=em(f"Alts in Pos-Locking #{rec['id']}", "\n".join(lines)[:4000] or "-", "info",
                 [("State", rec["status"].title(), True), ("Alts", str(len(rows)), True)]),
        ephemeral=True)


ACTION_HANDLERS.update({"end": h_end, "logout": h_logout, "delete": h_delete,
                        "reopen": h_reopen, "viewalts": h_viewalts})


class LogoutView(discord.ui.View):
    def __init__(self, rec, left):
        super().__init__(timeout=180)
        self.rec = rec
        opts = [discord.SelectOption(label=a["name"][:100], value=a["name"],
                                     description=(a["roblox"] or "")[:100]) for a in left[:25]]
        self.sel = discord.ui.Select(placeholder="Choose alts to log out", min_values=1,
                                     max_values=len(opts), options=opts)
        self.sel.callback = self.done
        self.add_item(self.sel)

    async def done(self, interaction):
        rec = self.rec
        if rec["status"] != "active":
            return await interaction.response.edit_message(embed=em("Ticket already ended", None, "warn"), view=None)
        names = list(self.sel.values)
        rec["removed"] = list(set(rec["removed"]) | set(names))
        save_db()
        bridge.send("remove", tid=rec["id"], names=names)
        await interaction.response.edit_message(
            embed=em("Alts logged out", "Removed from the server: " + ", ".join(f"**{n}**" for n in names), "ok"),
            view=None)
        try:
            await refresh_ticket(rec)
        except Exception:
            pass


# ================================================================ BOT SIDE: linking
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
                embed=em("Nothing to verify", "Use /link or the panel's Configuration button first.", "warn"),
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
                     "Join your server, then use **Deploy Alts** on the panel.", "ok"),
            ephemeral=True)


async def start_link(interaction, username):
    """interaction must not be responded to yet."""
    await interaction.response.defer(ephemeral=True)
    try:
        uid, name = await asyncio.to_thread(user_id_from_name, username)
    except Exception as e:
        return await interaction.followup.send(embed=em("Link failed", str(e), "err"), ephemeral=True)
    w1, w2 = random.sample(VERIFY_WORDS, 2)
    code = f"verify {w1} {w2}"
    data = load_links()
    data["pending"][str(interaction.user.id)] = {"id": uid, "name": name, "code": code}
    save_links(data)
    await interaction.followup.send(
        embed=em(f"Verify {name}",
                 "Put this exact phrase anywhere in your Roblox profile About section, save it, "
                 f"then press **Verify** below.\n\n```{code}```", "info"),
        view=VerifyView(), ephemeral=True)


class LinkModal(discord.ui.Modal, title="Link Roblox Account"):
    username = discord.ui.TextInput(label="Roblox username", min_length=3, max_length=20)

    async def on_submit(self, interaction):
        await start_link(interaction, self.username.value)


class ConfigView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Link Account", style=discord.ButtonStyle.primary)
    async def link(self, interaction, button):
        await interaction.response.send_modal(LinkModal())

    @discord.ui.button(label="Unlink", style=discord.ButtonStyle.danger)
    async def unlink(self, interaction, button):
        data = load_links()
        had = data["linked"].pop(str(interaction.user.id), None)
        data["pending"].pop(str(interaction.user.id), None)
        save_links(data)
        await interaction.response.edit_message(
            embed=em("Unlinked", f"Removed {had['name']}." if had else "You had no linked account.", "ok"), view=None)


# ================================================================ BOT SIDE: deploy flow
async def reply(interaction, embed, view=None, edit=False):
    if edit:
        await interaction.edit_original_response(embed=embed, embeds=None, view=view)
    else:
        kw = {"embed": embed, "ephemeral": True}
        if view is not None:
            kw["view"] = view
        await interaction.followup.send(**kw)


JOIN_NOTICE = discord.Embed(
    title="Joins Off Or Not In-Game",
    description=("- You have either joins off for everyone, or you are not in-game right now or both.\n"
                 f"- To let your alts join you, set your experience joins to **Everyone** in "
                 f"[Roblox settings]({PRIVACY_URL}).\n"
                 "- **Not linked yet?** Tap **Link Account** below so I can check your status, then join your "
                 "game and press **Retry Join Check**."),
    color=COLORS["warn"])


class JoinNoticeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=900)
        self.add_item(discord.ui.Button(label="Privacy Settings", url=PRIVACY_URL))
        b1 = discord.ui.Button(label="Link Account", style=discord.ButtonStyle.secondary)
        b1.callback = self.link
        self.add_item(b1)
        b2 = discord.ui.Button(label="Retry Join Check", style=discord.ButtonStyle.success)
        b2.callback = self.retry
        self.add_item(b2)

    async def link(self, interaction):
        await interaction.response.send_modal(LinkModal())

    async def retry(self, interaction):
        await interaction.response.defer()
        await deploy_entry(interaction, edit=True)


async def deploy_entry(interaction, edit=False):
    """Checks link + in-game status. interaction must already be deferred."""
    linked = load_links()["linked"].get(str(interaction.user.id))
    if not linked:
        return await reply(interaction, em(
            "Link your Roblox account first",
            "Tap **Link Account**, verify your profile, then press **Retry Join Check**.", "warn"),
            JoinNoticeView(), edit)
    if not bridge.online:
        return await reply(interaction, em("Agent offline", AGENT_OFFLINE, "err"), None, edit)
    try:
        await bridge.call("locate", timeout=120, user_id=linked["id"], name=linked["name"])
    except AgentError as e:
        if e.code in ("not_in_game", "hidden"):
            return await reply(interaction, JOIN_NOTICE, JoinNoticeView(), edit)
        return await reply(interaction, em("Could not check your server", str(e), "err"), None, edit)
    avail = available_alts()
    if not avail:
        return await reply(interaction, em("No alts free",
                                           "Every alt is in use right now. Check the roster, or wait for a lock to end.",
                                           "warn"), None, edit)
    await reply(interaction, em(
        "Server found", f"**{linked['name']}** is in a game and joins are open. "
                        f"**{len(avail)}** alt(s) are free. Choose how to deploy.", "ok"), ModeView(), edit)


class AutoModal(discord.ui.Modal, title="Auto Deploy"):
    def __init__(self, maxn):
        super().__init__()
        self.count = discord.ui.TextInput(label=f"How many alts? (1-{maxn})", placeholder=str(maxn), max_length=3)
        self.add_item(self.count)

    async def on_submit(self, interaction):
        try:
            n = int(self.count.value.strip())
        except ValueError:
            return await interaction.response.send_message(embed=em("Invalid count", "Enter a number.", "warn"),
                                                           ephemeral=True)
        avail = available_alts()
        if n < 1 or n > len(avail):
            return await interaction.response.send_message(
                embed=em("Invalid count", f"Choose between 1 and {len(avail)}.", "warn"), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await deploy_flow(interaction, [a["name"] for a in avail[:n]])


class ModeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Auto Deploy", style=discord.ButtonStyle.primary, emoji="⚡")
    async def auto(self, interaction, button):
        avail = available_alts()
        if not avail:
            return await interaction.response.send_message(embed=em("No alts free", None, "warn"), ephemeral=True)
        await interaction.response.send_modal(AutoModal(len(avail)))

    @discord.ui.button(label="Manual Deploy", style=discord.ButtonStyle.secondary, emoji="🎛️")
    async def manual(self, interaction, button):
        avail = available_alts()
        if not avail:
            return await interaction.response.send_message(embed=em("No alts free", None, "warn"), ephemeral=True)
        extra = "\n(Showing the first 25.)" if len(avail) > 25 else ""
        await interaction.response.edit_message(
            embed=em("Manual Deploy", "Pick the alts you want to send, then press **Deploy Selected**." + extra, "info"),
            view=ManualView(avail))


class ManualView(discord.ui.View):
    def __init__(self, avail):
        super().__init__(timeout=300)
        opts = [discord.SelectOption(label=(a["roblox"] or a["name"])[:100], value=a["name"],
                                     description=f"label: {a['name']}"[:100]) for a in avail[:25]]
        self.sel = discord.ui.Select(placeholder="Choose the alts to send", min_values=1,
                                     max_values=len(opts), options=opts)
        self.sel.callback = self.on_select
        self.add_item(self.sel)
        go = discord.ui.Button(label="Deploy Selected", style=discord.ButtonStyle.success, emoji="🚀")
        go.callback = self.go
        self.add_item(go)
        self.chosen = []

    async def on_select(self, interaction):
        self.chosen = list(self.sel.values)
        await interaction.response.defer()

    async def go(self, interaction):
        if not self.chosen:
            return await interaction.response.send_message(
                embed=em("Nothing selected", "Choose at least one alt first.", "warn"), ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await deploy_flow(interaction, self.chosen)


async def get_member(guild, uid):
    m = guild.get_member(uid)
    if m:
        return m
    try:
        return await guild.fetch_member(uid)
    except discord.HTTPException:
        return None


async def deploy_flow(interaction, names, reopen=None):
    """interaction must already be deferred. Locates the server, launches the alts, opens the ticket."""
    guild = interaction.guild

    async def fail(title, desc, view=None):
        kw = {"embed": em(title, desc, "err"), "ephemeral": True}
        await interaction.followup.send(**kw)

    cfg = guild_cfg(guild.id) if guild else None
    if not cfg or not cfg.get("authorized"):
        return await fail("Not authorized", "The developer has not run /authorize in this server yet.")
    owner_id = reopen["owner_id"] if reopen else interaction.user.id
    if not bridge.online:
        return await fail("Agent offline", AGENT_OFFLINE)
    for r in active_tickets():
        if r["owner_id"] == owner_id and (not reopen or r["id"] != reopen["id"]):
            return await fail("Active lock found", f"Finish <#{r['channel_id']}> before starting another lock.")
    avail = {a["name"]: a for a in available_alts()}
    if not names or any(n not in avail for n in names):
        return await fail("Alts not available",
                          "One or more of those alts were taken or went offline. Open the roster and try again.")
    chosen = [avail[n] for n in names]
    linked = load_links()["linked"].get(str(owner_id))
    if not linked:
        return await fail("No account linked", "Link your Roblox account first (panel > Configuration).")
    try:
        loc = await bridge.call("locate", timeout=120, user_id=linked["id"], name=linked["name"])
    except AgentError as e:
        if e.code in ("not_in_game", "hidden"):
            return await interaction.followup.send(embed=JOIN_NOTICE, view=JoinNoticeView(), ephemeral=True)
        return await fail("Could not find your server", str(e))

    tid = reopen["id"] if reopen else DB["counter"] + 1
    PENDING.add(tid)
    new_channel = None
    try:
        try:
            await bridge.call("deploy", timeout=45, tid=tid, place_id=loc["place_id"], job_id=loc["job_id"],
                              names=[a["name"] for a in chosen])
        except AgentError as e:
            return await fail("Deploy failed", str(e))

        member = await get_member(guild, owner_id)
        dev = await get_member(guild, bot.dev_id) if bot.dev_id else None
        ch = bot.get_channel(reopen["channel_id"]) if reopen and reopen.get("channel_id") else None
        try:
            if ch is None:
                Ow = discord.PermissionOverwrite
                ow = {guild.default_role: Ow(view_channel=False),
                      guild.me: Ow(view_channel=True, send_messages=True, embed_links=True,
                                   read_message_history=True, manage_channels=True)}
                if member:
                    ow[member] = Ow(view_channel=True, send_messages=True, read_message_history=True,
                                    attach_files=True)
                if dev:
                    ow[dev] = Ow(view_channel=True, send_messages=True, read_message_history=True)
                cat = guild.get_channel(cfg.get("ticket_category") or 0)
                ch = await guild.create_text_channel(
                    f"pos-locking-{tid}", category=cat if isinstance(cat, discord.CategoryChannel) else None,
                    overwrites=ow, topic=f"Position lock #{tid} for {linked['name']}")
                new_channel = ch
        except discord.HTTPException as e:
            bridge.send("stop", tid=tid)
            return await fail("Could not create the ticket channel",
                              f"{e}\nThe bot needs Manage Channels and Manage Roles.")

        now = time.time()
        alts = [{"name": a["name"], "roblox": a["roblox"], "user_id": a["user_id"]} for a in chosen]
        if reopen:
            rec = reopen
            rec.update(channel_id=ch.id, place_id=loc["place_id"], job_id=loc["job_id"], alts=alts,
                       removed=[], status="active", started=now, ended_at=None, final={}, review=None,
                       last_deploy=now)
        else:
            rec = {"id": tid, "guild_id": guild.id, "channel_id": ch.id, "message_id": None,
                   "log_message_id": None, "owner_id": owner_id, "roblox": linked["name"],
                   "roblox_id": linked["id"], "place_id": loc["place_id"], "job_id": loc["job_id"],
                   "alts": alts, "removed": [], "status": "active", "created": now, "started": now,
                   "ended_at": None, "final": {}, "review": None, "last_deploy": now}
            DB["tickets"][str(tid)] = rec
            DB["counter"] = max(DB["counter"], tid)
        msg = await ch.send(content=member.mention if member else None,
                            embed=ticket_embed(rec), view=ticket_view(rec))
        rec["message_id"] = msg.id
        save_db()
        await update_log(rec)
        await interaction.followup.send(embed=em(
            "Deployment started", f"Ticket opened: {ch.mention}\nLaunching **{len(chosen)}** alt(s) into "
                                  f"**{linked['name']}**'s server. Live status is in the ticket.", "ok"), ephemeral=True)
    finally:
        PENDING.discard(tid)


# ================================================================ BOT SIDE: roster
class RosterView(discord.ui.View):
    PER = 5

    def __init__(self, summary, alt_embeds):
        super().__init__(timeout=300)
        self.summary, self.embeds, self.page = summary, alt_embeds, 0
        self._sync()

    def pages(self):
        return max(1, -(-len(self.embeds) // self.PER))

    def current(self):
        s = self.page * self.PER
        return [self.summary] + self.embeds[s:s + self.PER]

    def _sync(self):
        self.prev.disabled = self.page == 0
        self.next.disabled = self.page >= self.pages() - 1

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embeds=self.current(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embeds=self.current(), view=self)


async def show_roster(interaction):
    if not bridge.online:
        return await reply(interaction, em("Agent offline", AGENT_OFFLINE, "err"))
    alts = bridge.alts
    if not alts:
        return await reply(interaction, em("No alts", "The agent has no alts loaded (alts.json is empty).", "warn"))
    use = alts_in_use()
    heads = await asyncio.to_thread(fetch_headshots, [a.get("user_id") for a in alts])
    embeds = []
    free = used = bad = 0
    for a in alts:
        if not a.get("ok"):
            bad += 1
            st, color = f"🔴 Cookie invalid - {a.get('error') or 'rejected'}", COLORS["err"]
        elif a["name"] in use:
            used += 1
            st, color = f"🔒 In use - Pos-Locking #{use[a['name']]}", COLORS["warn"]
        else:
            free += 1
            st, color = "🟢 Available", COLORS["ok"]
        e = discord.Embed(title=a.get("roblox") or a["name"], description=f"{st}\nLabel: `{a['name']}`", color=color)
        if a.get("user_id"):
            e.url = f"https://www.roblox.com/users/{a['user_id']}/profile"
            if heads.get(a["user_id"]):
                e.set_thumbnail(url=heads[a["user_id"]])
        embeds.append(e)
    summary = em("Alt Roster", f"{len(alts)} alt(s) loaded on the agent.", "info",
                 [("Available", str(free), True), ("In use", str(used), True), ("Invalid", str(bad), True)])
    view = RosterView(summary, embeds)
    await interaction.followup.send(embeds=view.current(), view=view, ephemeral=True)


# ================================================================ BOT SIDE: panel (Components V2 card)
# One white card: text sections split by divider lines, the Configuration button and the
# dropdown sit INSIDE the card (same look as the Cruxer panel). Needs discord.py 2.6+.
def _panel_sep():
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


def panel_texts():
    mins = int(MOVE_INTERVAL // 60)
    intro = (
        "## What is this ❓\n"
        "- This is a **position locker** panel. This is where you can **deploy alts** into the exact Roblox "
        "server you are standing in, so your spot in that server stays held.\n"
        "- Simply choose **View Alt Roster** or **Deploy Alts** from the menu below and move further.")
    benefit = (
        "## What is the benefit of using this?\n"
        "- Every deployment gets its own **private ticket** with live alt status, move timers and controls. "
        "This bot is made to keep your position safe and make things more organised and easy.\n"
        f"- Alts **auto-move every {mins} minutes** to stay active, and crashed clients are **relaunched "
        "automatically**.\n"
        "- Every session is **logged**, so it can be reviewed or re-opened later.\n"
        "- **Try it yourself**. You will know it by your own.")
    rules = (
        "## Rules:\n"
        "- **Be In-Game First**: Join your server before deploying. The alts join the server you are in.\n"
        "- **Active Profile**: Deploy from the Roblox account you linked and are currently using.\n"
        "- **Joins On**: Set who can join you to **Everyone**, otherwise Roblox hides your server.\n"
        "- **One Lock At A Time**: Each member can run one active lock. End it when you are done.\n"
        "- **No Abuse**: Misuse of the alts will result in a blacklist.")
    config = (
        "## Configuration:\n"
        "- Here you can save your Roblox username before making a lock so it will be used automatically for "
        "every deploy. Once saved you can further Edit Configuration by tapping the Configuration button again.")
    deploy = (
        "## Deploy alts now:\n"
        "- Choose **View Alt Roster** or **Deploy Alts** from the menu below")
    return intro, benefit, rules, config, deploy


class PanelConfigRow(discord.ui.ActionRow):
    @discord.ui.button(label="Configuration", style=discord.ButtonStyle.secondary, custom_id="pos:panel:config")
    async def config(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_links()
        cur = data["linked"].get(str(interaction.user.id))
        txt = (f"You are linked to **[{cur['name']}](https://www.roblox.com/users/{cur['id']}/profile)**."
               if cur else "You have no Roblox account linked yet.")
        await interaction.response.send_message(
            embed=em("Configuration", txt + "\n\nUse the buttons below to link a new account or unlink.", "info"),
            view=ConfigView(), ephemeral=True)


class PanelMenuRow(discord.ui.ActionRow):
    @discord.ui.select(
        placeholder="Choose Roster or Deploy", custom_id="pos:panel:menu",
        options=[discord.SelectOption(label="View Alt Roster", value="roster", emoji="📋",
                                      description="See every alt, its Roblox profile and whether it is free"),
                 discord.SelectOption(label="Deploy Alts", value="deploy", emoji="🚀",
                                      description="Send alts into the server you are in")])
    async def menu(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            cfg = guild_cfg(interaction.guild_id) if interaction.guild_id else None
            if not cfg or not cfg.get("authorized"):
                await reply(interaction, em("Not authorized", "The developer has not run /authorize here.", "err"))
            elif choice == "roster":
                await show_roster(interaction)
            else:
                await deploy_entry(interaction)
        finally:
            try:
                await interaction.message.edit(view=self.view)      # resets the menu selection
            except Exception:
                pass


class PanelView(discord.ui.LayoutView):
    def __init__(self):
        super().__init__(timeout=None)
        intro, benefit, rules, config, deploy = panel_texts()
        card = discord.ui.Container()          # no accent colour -> no coloured side bar, like Cruxer
        card.add_item(discord.ui.TextDisplay(intro))
        card.add_item(_panel_sep())
        card.add_item(discord.ui.TextDisplay(benefit))
        card.add_item(_panel_sep())
        card.add_item(discord.ui.TextDisplay(rules))
        card.add_item(_panel_sep())
        card.add_item(discord.ui.TextDisplay(config))
        card.add_item(PanelConfigRow())
        card.add_item(_panel_sep())
        card.add_item(discord.ui.TextDisplay(deploy))
        card.add_item(PanelMenuRow())
        self.add_item(card)


async def post_panel(guild, cfg, channel):
    old = cfg.get("panel_message")
    if old:
        try:
            await channel.get_partial_message(old).delete()
        except discord.HTTPException:
            pass
    m = await channel.send(view=PanelView())
    cfg["panel_message"] = m.id
    save_db()


# ================================================================ BOT SIDE: read-me channel
def readme_embed(guild, authorized):
    name = bot.user.name
    e = discord.Embed(title=name, color=COLORS["info"])
    e.description = (f"Thank you for authorizing **{name}** in **{guild.name}**.\n\n"
                     f"{name} is a position-locking bot. It sends your alts into the Roblox server you are in, "
                     "opens a private ticket for every deployment, tracks each alt's status and moves, and records "
                     "the result once the lock is over.")
    e.add_field(name="Developer", value=f"<@{bot.dev_id}>" if bot.dev_id else "-", inline=True)
    e.add_field(name="Status", value="Authorized" if authorized else "Awaiting /authorize", inline=True)
    e.set_footer(text=name)
    return e


def readme_view():
    v = discord.ui.View(timeout=None)
    if bot.dev_id:
        v.add_item(discord.ui.Button(label="Open Developer Profile", url=f"https://discord.com/users/{bot.dev_id}"))
    return v


async def ensure_readme(guild):
    cfg = DB["guilds"].setdefault(str(guild.id), {"created": []})
    cfg.setdefault("created", [])
    embed, view = readme_embed(guild, bool(cfg.get("authorized"))), readme_view()
    ch = guild.get_channel(cfg.get("readme_channel") or 0)
    if ch is None:
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                              add_reactions=False, read_message_history=True),
              guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)}
        ch = await guild.create_text_channel("read-me", overwrites=ow, position=0, reason="Bot read-me")
        cfg["created"].append(ch.id)
        cfg["readme_channel"] = ch.id
        cfg["readme_message"] = None
    mid = cfg.get("readme_message")
    if mid:
        try:
            await ch.get_partial_message(mid).edit(embed=embed, view=view)
            save_db()
            return
        except discord.NotFound:
            pass
    m = await ch.send(embed=embed, view=view)
    cfg["readme_message"] = m.id
    save_db()


# ================================================================ BOT class + HTTP server for the agent
async def health(request):
    return web.Response(text="POS Locker bot is running")


async def agent_poll(request):
    if not AGENT_KEY or not hmac.compare_digest(request.headers.get("X-Agent-Key", ""), AGENT_KEY):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)
    bridge.last_seen = bridge.snap_time = time.time()
    bridge.alts = body.get("alts", [])
    bridge.locks = body.get("locks", {})
    for r in body.get("results", []):
        bridge.resolve(r)
    for key in list(bridge.locks):                 # kill locks the bot no longer wants
        rec = DB["tickets"].get(key)
        if int(key) in PENDING:
            continue
        if not rec or rec["status"] != "active":
            bridge.send("stop", tid=int(key))
    cmds, bridge.commands = bridge.commands, []
    return web.json_response({"commands": cmds})


class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self.owner_ids = set()
        self.dev_id = OWNER_ID or None
        self.runner = None
        self.bg = []

    async def setup_hook(self):
        info = await self.application_info()
        ids = {info.team.owner_id} if info.team else {info.owner.id}
        if OWNER_ID:
            ids = {OWNER_ID}
        self.owner_ids = ids
        self.dev_id = next(iter(ids))

        self.add_view(PanelView())
        self.add_view(VerifyView())
        self.add_dynamic_items(*DYN.values(), RateButton)
        self.tree.add_command(pos_group)
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

        app = web.Application()
        app.router.add_get("/", health)
        app.router.add_post("/agent/poll", agent_poll)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", PORT).start()
        print(f"[bot] agent endpoint listening on port {PORT}")
        self.bg.append(asyncio.create_task(ticket_loop()))
        if EMBEDDED_AGENT:
            self.bg.append(asyncio.create_task(agent_main(f"http://127.0.0.1:{PORT}", AGENT_KEY)))

    async def close(self):
        for t in self.bg:
            t.cancel()
        if self.runner:
            await self.runner.cleanup()
        await super().close()

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print(f"Owner id(s): {self.owner_ids}")

    async def on_guild_join(self, guild):
        try:
            await ensure_readme(guild)
        except discord.HTTPException as e:
            print(f"[bot] could not create read-me in {guild.name}: {e}")

    async def on_guild_remove(self, guild):
        DB["guilds"].pop(str(guild.id), None)
        save_db()


bot = Bot()
pos_group = app_commands.Group(name="pos", description="POS locker controls")


# ================================================================ Slash commands
async def deny(interaction):
    await interaction.response.send_message(
        embed=em("Developer only", "Only the bot's developer can use this command.", "err"), ephemeral=True)


@bot.tree.command(name="link", description="Link your Roblox account")
@app_commands.describe(username="Your Roblox username (leave empty to see current link)")
async def link_cmd(interaction: discord.Interaction, username: str = None):
    if username:
        return await start_link(interaction, username)
    await interaction.response.defer(ephemeral=True)
    cur = load_links()["linked"].get(str(interaction.user.id))
    if cur:
        return await interaction.followup.send(embed=em("Linked account", f"You are linked to {cur['name']}.", "ok"),
                                               ephemeral=True)
    await interaction.followup.send(embed=em("Not linked", "Run /link with your Roblox username.", "warn"),
                                    ephemeral=True)


@bot.tree.command(name="unlink", description="Unlink your Roblox account")
async def unlink_cmd(interaction: discord.Interaction):
    data = load_links()
    had = data["linked"].pop(str(interaction.user.id), None)
    data["pending"].pop(str(interaction.user.id), None)
    save_links(data)
    await interaction.response.send_message(
        embed=em("Unlinked", f"Removed **{had['name']}** from your account." if had
                 else "You did not have a linked account.", "ok" if had else "warn"), ephemeral=True)


@bot.tree.command(name="test", description="Open the Roblox home page of every alt (developer only)")
async def test_cmd(interaction: discord.Interaction):
    if interaction.user.id not in bot.owner_ids:
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    try:
        await bridge.call("refresh", timeout=120)
        names = [a["name"] for a in bridge.alts if a.get("ok")]
        if not names:
            return await interaction.followup.send(embed=em("No working alts", "No alt cookie passed the check.",
                                                            "err"), ephemeral=True)
        res = await bridge.call("home", timeout=300, names=names)
    except AgentError as e:
        return await interaction.followup.send(embed=em("Test failed", str(e), "err"), ephemeral=True)
    opened, failed = res.get("opened", []), res.get("failed", [])
    await interaction.followup.send(embed=em(
        "Roblox pages opened", f"Opened **{len(opened)}** logged-in window(s) on the agent PC.", "ok" if opened else "warn",
        [("Opened", "\n".join(opened)[:1000] or "-", False), ("Failed", "\n".join(failed)[:1000] or "-", False)]),
        ephemeral=True)


@bot.tree.command(name="authorize", description="Create the panel, log and ticket channels (developer only)")
@app_commands.guild_only()
async def authorize_cmd(interaction: discord.Interaction):
    if interaction.user.id not in bot.owner_ids:
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    guild, me = interaction.guild, interaction.guild.me
    cfg = DB["guilds"].setdefault(str(guild.id), {"created": []})
    cfg.setdefault("created", [])
    Ow = discord.PermissionOverwrite
    bot_ow = Ow(view_channel=True, send_messages=True, embed_links=True, read_message_history=True,
                manage_channels=True, manage_messages=True)

    async def get_or_make(key, kind, name, overwrites, category=None):
        ch = guild.get_channel(cfg.get(key) or 0) or guild.get_channel(DEFAULT_IDS[key])
        want = discord.CategoryChannel if kind == "category" else discord.TextChannel
        if isinstance(ch, want):
            cfg[key] = ch.id
            return ch, False
        if kind == "category":
            ch = await guild.create_category(name, overwrites=overwrites)
        else:
            ch = await guild.create_text_channel(name, category=category, overwrites=overwrites)
        cfg["created"].append(ch.id)
        cfg[key] = ch.id
        return ch, True

    try:
        pcat, c1 = await get_or_make("panel_category", "category", "Position Locker", {})
        pch, c2 = await get_or_make("panel_channel", "text", "pos-panel", {
            guild.default_role: Ow(view_channel=True, send_messages=False, add_reactions=False,
                                   read_message_history=True), me: bot_ow}, pcat)
        lch, c3 = await get_or_make("log_channel", "text", "pos-logs", {
            guild.default_role: Ow(view_channel=False),
            interaction.user: Ow(view_channel=True, read_message_history=True), me: bot_ow}, pcat)
        tcat, c4 = await get_or_make("ticket_category", "category", "Pos Tickets", {
            guild.default_role: Ow(view_channel=False), interaction.user: Ow(view_channel=True), me: bot_ow})
        cfg["authorized"] = True
        save_db()
        await post_panel(guild, cfg, pch)
        await ensure_readme(guild)
    except discord.Forbidden:
        return await interaction.followup.send(embed=em(
            "Missing permissions", "I need **Manage Channels**, **Manage Roles**, **Send Messages** and "
                                   "**Embed Links** (Administrator is easiest).", "err"), ephemeral=True)
    tag = lambda made: "created" if made else "existing"
    await interaction.followup.send(embed=em(
        "Authorized", "The panel is live.", "ok",
        [("Panel", f"{pch.mention} ({tag(c2)})", False), ("Logs", f"{lch.mention} ({tag(c3)}, private)", False),
         ("Tickets", f"{tcat.name} ({tag(c4)})", False)]), ephemeral=True)


@bot.tree.command(name="deauthorize", description="Delete the bot's channels and leave this server (developer only)")
@app_commands.guild_only()
async def deauthorize_cmd(interaction: discord.Interaction):
    if interaction.user.id not in bot.owner_ids:
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    await interaction.followup.send(embed=em(
        "Deauthorizing", "Closing tickets, removing my channels and leaving this server...", "warn"), ephemeral=True)
    for key, rec in list(DB["tickets"].items()):
        if rec["guild_id"] != guild.id:
            continue
        if rec["status"] == "active":
            bridge.send("stop", tid=rec["id"])
        ch = guild.get_channel(rec.get("channel_id") or 0)
        if ch:
            try:
                await ch.delete(reason="Deauthorized")
            except discord.HTTPException:
                pass
        DB["tickets"].pop(key, None)
    cfg = DB["guilds"].pop(str(guild.id), None) or {}
    created = cfg.get("created", [])
    pc = guild.get_channel(cfg.get("panel_channel") or 0)
    if pc and pc.id not in created and cfg.get("panel_message"):
        try:
            await pc.get_partial_message(cfg["panel_message"]).delete()
        except discord.HTTPException:
            pass
    for cid in reversed(created):
        ch = guild.get_channel(cid)
        if ch:
            try:
                await ch.delete(reason="Deauthorized")
            except discord.HTTPException:
                pass
    save_db()
    await guild.leave()


@pos_group.command(name="stats", description="Show active position locks")
async def stats_cmd(interaction: discord.Interaction):
    act = active_tickets()
    lines = []
    for r in act:
        up = sum(1 for x in alt_rows(r) if x["status"] == "in game")
        lines.append(f"**#{r['id']}** - {r['roblox']} - {up}/{len(r['alts'])} alts up - "
                     f"<#{r['channel_id']}>" if r.get("channel_id") else f"**#{r['id']}** - {r['roblox']}")
    await interaction.response.send_message(embed=em(
        "Position locks" if act else "Idle", "\n".join(lines)[:3900] or "No position lock is running.",
        "ok" if act else "info",
        [("Agent", "🟢 online" if bridge.online else "🔴 offline", True),
         ("Alts free", str(len(available_alts())), True), ("Active locks", str(len(act)), True)]))


@pos_group.command(name="check", description="Test every cookie in alts.json (developer only)")
async def check_cmd(interaction: discord.Interaction):
    if interaction.user.id not in bot.owner_ids:
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    try:
        await bridge.call("refresh", timeout=180)
    except AgentError as e:
        return await interaction.followup.send(embed=em("Check failed", str(e), "err"), ephemeral=True)
    lines = [(f"[OK] {a['name']} - {a['roblox']}" if a.get("ok") else f"[FAIL] {a['name']} - {a.get('error')}")
             for a in bridge.alts]
    good = sum(1 for a in bridge.alts if a.get("ok"))
    await interaction.followup.send(embed=em(
        "Cookie check", "\n".join(lines)[:3900] or "No alts.", "ok" if good == len(bridge.alts) else "warn",
        [("Working", f"{good} / {len(bridge.alts)}", True)]), ephemeral=True)


# ================================================================ Entry point
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() == "agent":
        if not BOT_URL or not AGENT_KEY:
            sys.exit("Agent mode needs BOT_URL and AGENT_KEY in .env")
        if IS_WINDOWS:
            enable_multi_instance()
        try:
            asyncio.run(agent_main(BOT_URL, AGENT_KEY))
        except KeyboardInterrupt:
            pass
    else:
        if not TOKEN:
            sys.exit("DISCORD_TOKEN missing")
        if not AGENT_KEY:
            sys.exit("AGENT_KEY missing - set the same long random secret on the bot and on the agent")
        if EMBEDDED_AGENT and IS_WINDOWS:
            enable_multi_instance()
        bot.run(TOKEN)
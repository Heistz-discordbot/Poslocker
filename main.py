"""
POS Locker - Discord bot (Railway, 24/7)  +  Windows agent (launches the real Roblox clients)

ONE file, two modes
  py main.py          -> the Discord bot (run this on Railway; set EMBEDDED_AGENT=1 to also run
                        the agent inside the same process on a Windows PC)
  py main.py agent    -> the Windows agent (run this on the PC that has Roblox installed)

NEVER run the bot twice with the same DISCORD_TOKEN (that is what caused error 40060).
"""

import asyncio
import glob
import hmac
import json
import os
import random
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
POLL_EVERY = 3          # agent -> bot poll seconds
TICKET_REFRESH = 20     # ticket message refresh seconds

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.getenv("DATA_DIR") or BASE
os.makedirs(DATA_DIR, exist_ok=True)
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
DB_FILE = os.path.join(DATA_DIR, "db.json")
ALTS_FILE = os.getenv("ALTS_FILE") or os.path.join(BASE, "alts.json")

DEFAULT_IDS = {
    "panel_category": 1560667887159742487,
    "panel_channel": 1560668012120645693,
    "log_channel": 1560668040025346068,
    "ticket_category": 1560667942927077556,
}

COLORS = {"info": 0x5865F2, "ok": 0x2ECC71, "warn": 0xF1C40F, "err": 0xE74C3C}
AGENT_OFFLINE = ("The alt agent (the Windows PC that runs the Roblox clients) is offline. "
                 "Start `py main.py agent` on that PC and try again.")

IS_WINDOWS = sys.platform == "win32"
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD


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
                last_exc = None
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
    if not os.path.exists(ALTS_FILE):
        return []
    with open(ALTS_FILE, "r", encoding="utf-8") as f:
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
    tmp = LINKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LINKS_FILE)


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
    global _mutex_handle, _event_handle, MUTEX_OWNED
    if not IS_WINDOWS:
        return False
    if MUTEX_OWNED:
        return True
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
        raise RuntimeError("RobloxPlayerBeta.exe not found. Install Roblox from roblox.com.")
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


def move_window(hwnd):
    keys = [(VK_SPACE, 0.2), (VK_W, 0.5), (VK_S, 0.5), (VK_A, 0.3), (VK_D, 0.3), (VK_SPACE, 0.2)]
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
    hwnd = wait_for_window(proc, client)
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
        except Exception:
            pass
        time.sleep(1)


class AgentLock:
    def __init__(self, tid, place_id, job_id, clients):
        self.tid = tid
        self.place_id = place_id
        self.job_id = job_id
        self.clients = clients
        self.next_move = time.time() + MOVE_INTERVAL
        self.task = None
        self.stopped = False


class Agent:
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

    async def ensure_multi(self):
        if enable_multi_instance():
            return True
        if CLOSE_HOST_ROBLOX:
            await asyncio.to_thread(close_all_roblox)
            await asyncio.sleep(3)
            return enable_multi_instance()
        return False

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
            raise AgentError("Roblox is not installed on the agent PC.", "no_roblox")
        tid = int(cmd["tid"])
        if tid in self.locks:
            await self.stop_lock(tid)
        if not await self.ensure_multi():
            raise AgentError("Roblox is already open on the agent PC. Close it or set CLOSE_HOST_ROBLOX=1.", "mutex")
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
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise AgentError("Playwright is missing on the agent PC.", "playwright")
        await self.close_homes()
        self._pw = await async_playwright().start()
        try:
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
                sent, self.pending_results = self.pending_results, []
                body = {"alts": self.alt_info, "locks": self.snapshot(), "results": sent}
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
            fut.set_result(r.get("data"))
        else:
            fut.set_exception(AgentError(r.get("error", "Unknown agent error"), r.get("code", "other")))


bridge = Bridge()


async def handle_poll(request):
    key = request.headers.get("X-Agent-Key", "")
    if AGENT_KEY and not hmac.compare_digest(key, AGENT_KEY):
        return web.json_response({"error": "Unauthorized"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    bridge.last_seen = time.time()
    bridge.alts = body.get("alts", [])
    bridge.locks = body.get("locks", {})
    bridge.snap_time = time.time()

    for res in body.get("results", []):
        bridge.resolve(res)

    cmds = bridge.commands
    bridge.commands = []

    return web.json_response({"commands": cmds})


async def handle_index(request):
    return web.Response(text="POS Locker Bot is online and running!")


async def web_server():
    app = web.Application()
    app.router.add_post("/agent/poll", handle_poll)
    app.router.add_get("/", handle_index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"[web] server listening on port {PORT}")


class PosBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        print("[bot] Discord command tree synced.")


bot = PosBot()


@bot.tree.command(name="authorize", description="Authorize and setup panels")
@app_commands.default_permissions(administrator=True)
async def authorize(interaction: discord.Interaction):
    await interaction.response.send_message("POS Locker authorized successfully!", ephemeral=True)


async def main():
    if len(sys.argv) > 1 and sys.argv[1] == "agent":
        if not BOT_URL or not AGENT_KEY:
            print("[agent error] BOT_URL and AGENT_KEY must be set in agent environment (.env).")
            return
        await agent_main(BOT_URL, AGENT_KEY)
    else:
        if not TOKEN:
            print("[bot error] DISCORD_TOKEN is missing.")
            return
        asyncio.create_task(web_server())
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Shutting down POS Locker...")
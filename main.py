"""
POS Locker Discord bot (Windows / Local execution)

Commands:
  /pos link [username]  - link your Roblox account (one command, Verify button)
  /pos lock count:N     - bot finds YOUR server automatically and joins N alts at once
  /pos stop             - close the alt clients only (your own Roblox stays open)
  /pos status           - show what is running
  /pos check            - test every cookie in alts.json

Needs:  pip install discord.py requests python-dotenv
Files next to this one:
  .env      ->  DISCORD_TOKEN=xxxx       (optional: GUILD_ID=123 for instant slash-command sync)
  alts.json  ->  [{"name": "alt1", "cookie": "_|WARNING..."}, ...]   (a plain list of cookie strings also works)

Order does not matter: the bot unlocks multi-Roblox itself, even if your Roblox is already open.
(If Roblox's protection blocks that, run the bot as Administrator.)
"""

import asyncio
import csv
import ctypes
import json
import os
import random
import subprocess
import sys
import time
import urllib.parse

from ctypes import wintypes

import discord
import requests
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

MOVE_INTERVAL = 15 * 60         # anti-AFK tap every 15 minutes
JOIN_TIMEOUT = 120              # seconds to wait for alts to show up in the server
API_PARALLEL = 15               # how many Roblox API calls run at once
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"

BASE = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(BASE, "links.json")

# ---------------------------------------------------------------- embeds
COLORS = {"info": 0x5865F2, "ok": 0x2ECC71, "warn": 0xF1C40F, "err": 0xE74C3C}


def em(title, desc=None, kind="info", fields=None):
    e = discord.Embed(title=title, description=desc, color=COLORS[kind])
    for name, value, inline in (fields or []):
        e.add_field(name=name, value=(value or "-")[:1024], inline=inline)
    return e


# ---------------------------------------------------------------- multi-instance mutex
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if (sys.platform in ("win32", "cygwin", "msys") or os.name == "nt") else None
_mutex = None
mutex_owned = False


def _setup_k32():
    if not kernel32:
        return
    k, H = kernel32, wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = H
    k.CloseHandle.argtypes = [H]
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = H
    k.GetCurrentProcess.restype = H
    k.DuplicateHandle.argtypes = [H, H, H, ctypes.POINTER(H), wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]


def hold_roblox_mutex():
    global _mutex, mutex_owned
    if kernel32 and _mutex is None:
        _setup_k32()
        _mutex = kernel32.CreateMutexW(None, True, "ROBLOX_singletonMutex")
        mutex_owned = ctypes.get_last_error() != 1000 and ctypes.get_last_error() != 183


class _HandleEntry(ctypes.Structure):   # SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX (64-bit)
    _fields_ = [("obj", ctypes.c_void_p), ("pid", ctypes.c_size_t), ("value", ctypes.c_size_t),
                ("access", ctypes.c_ulong), ("bt", ctypes.c_ushort), ("typeidx", ctypes.c_ushort),
                ("attrs", ctypes.c_ulong), ("res", ctypes.c_ulong)]


def _obj_string(ntdll, h, info_class):
    buf = ctypes.create_string_buffer(2048)
    ret = wintypes.ULONG()
    if ntdll.NtQueryObject(h, info_class, buf, 2048, ctypes.byref(ret)) != 0:
        return ""
    length = ctypes.c_ushort.from_buffer(buf).value
    ptr = ctypes.c_void_p.from_buffer(buf, 8).value
    if not ptr or not length:
        return ""
    return ctypes.wstring_at(ptr, length // 2)


def release_roblox_mutex(pids):
    if not kernel32:
        return 0
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                               ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQuerySystemInformation.restype = ctypes.c_long
    ntdll.NtQueryObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                    ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryObject.restype = ctypes.c_long
    _setup_k32()

    size = 1 << 22
    while True:
        buf = ctypes.create_string_buffer(size)
        ret = wintypes.ULONG()
        status = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(ret))
        if (status & 0xFFFFFFFF) == 0xC0000004:
            size *= 2
            if size > (1 << 30):
                return 0
            continue
        break
    if status != 0:
        return 0

    count = ctypes.c_size_t.from_buffer(buf).value
    entries = (_HandleEntry * count).from_address(ctypes.addressof(buf) + 2 * ctypes.sizeof(ctypes.c_size_t))
    me = kernel32.GetCurrentProcess()
    procs, closed = {}, 0
    for e in entries:
        if e.pid not in pids:
            continue
        if e.pid not in procs:
            procs[e.pid] = kernel32.OpenProcess(0x40, False, e.pid) or 0
        hp = procs[e.pid]
        if not hp:
            continue
        dup = wintypes.HANDLE()
        if not kernel32.DuplicateHandle(hp, e.value, me, ctypes.byref(dup), 0, False, 2):
            continue
        try:
            if (_obj_string(ntdll, dup, 2) == "Mutant"
                    and _obj_string(ntdll, dup, 1).endswith("ROBLOX_singletonMutex")):
                if kernel32.DuplicateHandle(hp, e.value, None, None, 0, False, 1):
                    closed += 1
        finally:
            kernel32.CloseHandle(dup)
    for hp in procs.values():
        if hp:
            kernel32.CloseHandle(hp)
    return closed


def take_mutex(running_pids):
    global _mutex
    if _mutex:
        kernel32.CloseHandle(_mutex)
        _mutex = None
    closed = release_roblox_mutex(running_pids) if running_pids else 0
    hold_roblox_mutex()
    return closed


# ---------------------------------------------------------------- http with retries
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


# ---------------------------------------------------------------- alts
def clean_cookie(c):
    c = (c or "").strip().strip("\"'")
    if c.upper().startswith(".ROBLOSECURITY="):
        c = c.split("=", 1)[1]
    return "".join(c.strip().strip("\"';").split())


def load_alts():
    with open(os.path.join(BASE, "alts.json"), "r", encoding="utf-8") as f:
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
        raise RuntimeError(f"Cookie rejected by Roblox (HTTP {r.status_code}) - expired or invalid")
    j = r.json()
    return j["id"], j["name"]


def is_in_server(cookie, uid, job_id):
    p = rb_post(cookie, PRESENCE_URL, {"userIds": [uid]})["userPresences"][0]
    return p.get("gameId") == job_id


# ---------------------------------------------------------------- finding user server
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
            time.sleep(2)
            _, place2, job2 = read_presence(cookies, uid)
            if job2 == job:
                return place, job
        else:
            time.sleep(2)
    if seen == 2:
        raise RuntimeError(
            f"{name} is in a game, but Roblox hides which server. Fix: Roblox Settings > Privacy > "
            "'Who can join me' = Everyone, or make that account friends with one of the alts."
        )
    if seen in (0, 1):
        raise RuntimeError(f"{name} is not in a game right now. Join the server first, then run the command.")
    raise RuntimeError(f"Could not read {name}'s server. Try again in a few seconds.")


# ---------------------------------------------------------------- linked accounts
def load_links():
    try:
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


# ---------------------------------------------------------------- launching
def get_auth_ticket(cookie, place_id):
    headers = {
        "Cookie": f".ROBLOSECURITY={cookie}",
        "Referer": f"https://www.roblox.com/games/{place_id}",
        "Origin": "https://www.roblox.com",
        "Content-Type": "application/json",
    }
    url = "https://auth.roblox.com/v1/authentication-ticket"
    r = http("POST", url, headers=headers)
    if r.status_code == 403 and "x-csrf-token" in r.headers:
        headers["X-CSRF-TOKEN"] = r.headers["x-csrf-token"]
        r = http("POST", url, headers=headers)
    ticket = r.headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RuntimeError(f"No login ticket (HTTP {r.status_code}) - cookie probably expired")
    return ticket


def find_roblox_player():
    root = os.path.join(os.getenv("LOCALAPPDATA", ""), "Roblox", "Versions")
    best, best_time = None, 0
    if os.path.isdir(root):
        for d in os.listdir(root):
            exe = os.path.join(root, d, "RobloxPlayerBeta.exe")
            if os.path.exists(exe) and os.path.getmtime(exe) > best_time:
                best, best_time = exe, os.path.getmtime(exe)
    return best


def launch_alt(ticket, place_id, job_id):
    tracker = random.randint(10_000_000_000, 99_999_999_999)
    launcher = (
        "https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=RequestGameJob"
        f"&browserTrackerId={tracker}&placeId={place_id}&gameId={job_id}&isPlayTogetherGame=false"
    )
    uri = (
        f"roblox-player:1+launchmode:play+gameinfo:{ticket}+launchtime:{int(time.time() * 1000)}"
        f"+placelauncherurl:{urllib.parse.quote(launcher, safe='')}+browsertrackerid:{tracker}"
        "+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
    )
    exe = find_roblox_player()
    if exe:
        subprocess.Popen([exe, uri])
    else:
        os.startfile(uri)


# ---------------------------------------------------------------- process helpers
def roblox_pids():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq RobloxPlayerBeta.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True).stdout
    pids = set()
    for row in csv.reader(out.splitlines()):
        if len(row) > 1 and row[0].lower() == "robloxplayerbeta.exe":
            pids.add(int(row[1]))
    return pids


def kill_roblox(keep=None):
    pids = roblox_pids()
    if keep:
        pids -= keep
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    return len(pids)


user32 = ctypes.windll.user32 if (sys.platform in ("win32", "cygwin", "msys") or os.name == "nt") else None


def _roblox_windows(keep):
    found = []
    if not user32:
        return found
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == "Roblox":
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in (keep or set()):
                    found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def move_all(keep):
    if not user32:
        return 0
    WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
    wins = _roblox_windows(keep)
    for hwnd in wins:
        for vk in (0x57, 0x53):
            user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
            time.sleep(0.4)
            user32.PostMessageW(hwnd, WM_KEYUP, vk, 0)
            time.sleep(0.1)
    return len(wins)


# ---------------------------------------------------------------- bot state
class State:
    task = None
    place_id = None
    job_id = None
    started = 0
    baseline = None
    launched = []
    failed = []
    target = ""


state = State()


async def run_lock(channel, place_id, job_id, alts):
    state.launched, state.failed = [], []
    sem = asyncio.Semaphore(API_PARALLEL)

    try:
        async def prepare(alt):
            async with sem:
                try:
                    uid, real = await asyncio.to_thread(whoami, alt["cookie"])
                    ticket = await asyncio.to_thread(get_auth_ticket, alt["cookie"], place_id)
                    return alt, uid, real, ticket, None
                except Exception as e:
                    return alt, None, None, None, str(e)

        prepared = await asyncio.gather(*(prepare(a) for a in alts))

        ready = []
        for alt, uid, real, ticket, err in prepared:
            if err:
                state.failed.append(f"{alt['name']}: {err}")
            else:
                ready.append((alt, uid, real, ticket))

        results = await asyncio.gather(
            *(asyncio.to_thread(launch_alt, t, place_id, job_id) for _, _, _, t in ready),
            return_exceptions=True)
        started = []
        for (alt, uid, real, _), res in zip(ready, results):
            if isinstance(res, Exception):
                state.failed.append(f"{alt['name']} ({real}): launch failed - {res}")
            else:
                started.append((alt, uid, real))

        await channel.send(embed=em(
            "Clients launched",
            f"{len(started)} of {len(alts)} alts started together. Checking who actually joined...",
            "info"))

        pending = {uid: (alt, real) for alt, uid, real in started}
        joined = []
        deadline = time.time() + JOIN_TIMEOUT

        async def check(uid, alt):
            async with sem:
                try:
                    return uid, await asyncio.to_thread(is_in_server, alt["cookie"], uid, job_id)
                except Exception:
                    return uid, False

        await asyncio.sleep(30)
        while pending:
            res = await asyncio.gather(*(check(u, a) for u, (a, _) in pending.items()))
            for uid, ok in res:
                if ok:
                    joined.append(pending.pop(uid)[1])
            if not pending or time.time() > deadline:
                break
            await asyncio.sleep(10)

        for uid, (alt, real) in pending.items():
            state.failed.append(f"{alt['name']} ({real}): started but never entered the server")
        state.launched = joined

        total = len(alts)
        kind = "ok" if not state.failed else ("warn" if joined else "err")
        fields = [("In server", f"{len(joined)} / {total}", True)]
        if joined:
            fields.append(("Accounts", ", ".join(joined), False))
        if state.failed:
            fields.append(("Problems", "\n".join(state.failed[:12]), False))
        await channel.send(embed=em("Lock result", None, kind, fields))

        while True:
            await asyncio.sleep(MOVE_INTERVAL)
            n = await asyncio.to_thread(move_all, state.baseline)
            print(f"[move] sent movement to {n} windows")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        await channel.send(embed=em("Lock crashed", str(e)[:1000], "err"))


# ---------------------------------------------------------------- verify button (persistent)
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
        
        # Robust verification: case-insensitive & whitespace stripped check
        clean_desc = desc.lower().strip()
        clean_code = pend["code"].lower().strip()

        if clean_code not in clean_desc:
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
                     f"Successfully linked to **{pend['name']}**! You can remove the code from your profile now.\n"
                     "Join your server, then run `/pos lock count:5`", "ok"),
            ephemeral=True)


# ---------------------------------------------------------------- commands
class PosGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="pos", description="POS locker controls")

    @app_commands.command(name="link", description="Link your Roblox account")
    @app_commands.describe(username="Your Roblox username (leave empty to see your current link)")
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

        # Using 'rbx-' prefix so Roblox chat/bio filters NEVER hashtag/censor the code
        code = "rbx-" + "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=8))
        data["pending"][me] = {"id": uid, "name": name, "code": code}
        save_links(data)
        
        # Using a markdown code block so Discord gives a built-in click-to-copy button
        await interaction.followup.send(
            embed=em(f"Verify {name}",
                     "Put this code anywhere in your Roblox profile About section, save it, "
                     f"then press **Verify** below.\n\n```{code}```", "info"),
            view=VerifyView(), ephemeral=True)

    @app_commands.command(name="lock", description="Lock your current server with your alts")
    @app_commands.describe(count="How many alts to add")
    async def lock(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
        try:
            await interaction.response.defer()
        except discord.NotFound:
            print("Interaction expired - make sure only ONE copy of the bot is running.")
            return

        async def fail(title, text):
            await interaction.followup.send(embed=em(title, text, "err"))

        if not kernel32:
            return await fail("Unsupported system", "This bot features require Windows.")
        if state.task and not state.task.done():
            return await fail("Already running", "A lock is already running. Use /pos stop first.")

        linked = load_links()["linked"].get(str(interaction.user.id))
        if not linked:
            return await fail("No account linked", "Run /pos link with your Roblox username first.")

        try:
            alts = load_alts()
        except Exception as e:
            return await fail("Cannot read alts.json", str(e))
        if not alts:
            return await fail("No alts", "alts.json is empty.")
        if count > len(alts):
            return await fail("Not enough alts", f"You asked for {count} but alts.json only has {len(alts)}.")
        alts = alts[:count]

        baseline = await asyncio.to_thread(roblox_pids)
        if baseline and not mutex_owned:
            await asyncio.to_thread(take_mutex, baseline)
            if not mutex_owned:
                return await fail(
                    "Could not unlock multiple clients",
                    "Roblox is protecting its own lock. Try running your python script as Administrator.")

        try:
            place_id, job_id = await asyncio.to_thread(
                locate_user, [a["cookie"] for a in alts], linked["id"], linked["name"])
        except Exception as e:
            return await fail("Server not found", str(e))

        state.place_id, state.job_id, state.started = place_id, job_id, time.time()
        state.baseline, state.target = baseline, linked["name"]
        state.task = asyncio.create_task(run_lock(interaction.channel, place_id, job_id, alts))
        await interaction.followup.send(embed=em(
            "Server found", f"Detected {linked['name']} in the server below. Launching {len(alts)} alts now.",
            "ok", [("Server ID", f"`{job_id}`", False), ("Place", f"`{place_id}`", True),
                    ("Alts", str(len(alts)), True), ("Anti-AFK", "every 15 min", True)]))

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

    @app_commands.command(name="stop", description="Stop locking and close the alt clients")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        if state.task:
            state.task.cancel()
            state.task = None
        n = await asyncio.to_thread(kill_roblox, state.baseline)
        note = "Your own Roblox was left open." if state.baseline else "All Roblox clients were closed."
        await interaction.followup.send(embed=em("Stopped", f"Closed {n} client(s). {note}", "ok"))

    @app_commands.command(name="status", description="Show lock status")
    async def status(self, interaction: discord.Interaction):
        if state.task and not state.task.done():
            mins = int((time.time() - state.started) // 60)
            await interaction.response.send_message(embed=em(
                "Lock active", None, "ok",
                [("Running for", f"{mins} min", True), ("Target", state.target, True),
                 ("In server", str(len(state.launched)), True), ("Failed", str(len(state.failed)), True),
                 ("Server ID", f"`{state.job_id}`", False)]))
        else:
            await interaction.response.send_message(embed=em("Idle", "No lock running.", "info"))


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

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        print("Multi-Roblox mutex:", "OWNED" if mutex_owned else "NOT owned yet (will be taken automatically on /pos lock)")


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("DISCORD_TOKEN missing in .env")
    hold_roblox_mutex()
    Bot().run(TOKEN)
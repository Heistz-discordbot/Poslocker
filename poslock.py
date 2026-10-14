"""
poslock.py - Pos locker cog for the Discord bot.

Setup
  1. pip install aiohttp psutil
  2. Create alts.json next to the bot:
       [{"name": "alt1", "cookie": "<.ROBLOSECURITY value>"},
        {"name": "alt2", "cookie": "<.ROBLOSECURITY value>"}]
  3. In your main file, BEFORE tree.sync():
       await bot.load_extension("poslock")
  4. Windows only. Close every Roblox window before starting the bot.

Commands
  /pos lock count:<n> server_link:<link>   (or player:<roblox username>)
  /pos unlock
  /pos status
"""
import asyncio
import json
import os
import random
import time
import urllib.parse

import aiohttp
import discord
import psutil
from discord import app_commands
from discord.ext import commands, tasks

OWNER_IDS = {1455426573741330495}   # add more Discord IDs here if needed
ALTS_FILE = "alts.json"
REFRESH_SECS = 15 * 60              # each alt rejoins every 15 minutes
GRACE_SECS = 75                     # time given to a client to load in
CHECK_SECS = 15                     # how often alts are checked
PRESENCE_URL = "https://presence.roblox.com/v1/presences/users"

_MUTEX = None  # lets several Roblox clients run at once (kept alive here)


class Alt:
    def __init__(self, name, cookie):
        self.name = name
        self.cookie = cookie
        self.user_id = None
        self.pid = None
        self.launched_at = 0.0
        self.misses = 0
        self.fails = 0
        self.busy = True


def load_alts():
    with open(ALTS_FILE, "r", encoding="utf-8") as f:
        return [Alt(a["name"], a["cookie"]) for a in json.load(f)]


def roblox_pids():
    return {p.pid for p in psutil.process_iter(["name"])
            if p.info["name"] == "RobloxPlayerBeta.exe"}


def kill(alt):
    if alt.pid:
        try:
            psutil.Process(alt.pid).kill()
        except psutil.Error:
            pass
    alt.pid = None


def parse_link(link):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link.strip()).query)

    def g(key):
        return q.get(key, [None])[0]

    if not g("placeId"):
        return None
    return {"place": g("placeId"), "instance": g("gameInstanceId"),
            "code": g("privateServerLinkCode")}


async def rbx_post(session, cookie, url, body=None, headers=None):
    """POST with the alt's cookie, handling Roblox's CSRF token dance."""
    h = {"Cookie": f".ROBLOSECURITY={cookie}", **(headers or {})}
    for _ in range(2):
        async with session.post(url, headers=h, json=body or {}) as r:
            if r.status == 403 and "x-csrf-token" in r.headers and "x-csrf-token" not in h:
                h["x-csrf-token"] = r.headers["x-csrf-token"]
                continue
            try:
                data = await r.json(content_type=None)
            except Exception:
                data = {}
            return r.status, r.headers.copy(), data
    return 0, {}, {}


async def launch(session, alt, target):
    """Start a Roblox client for this alt, joining the target server."""
    place = target["place"]
    _, hdrs, _ = await rbx_post(
        session, alt.cookie, "https://auth.roblox.com/v1/authentication-ticket",
        headers={"Referer": f"https://www.roblox.com/games/{place}"})
    ticket = hdrs.get("rbx-authentication-ticket")
    if not ticket:
        return False

    tracker = random.randint(10**10, 10**11)
    if target["code"]:
        req = f"RequestPrivateGame&placeId={place}&linkCode={target['code']}"
    elif target["instance"]:
        req = f"RequestGameJob&placeId={place}&gameId={target['instance']}"
    else:
        req = f"RequestGame&placeId={place}"
    url = ("https://assetgame.roblox.com/game/PlaceLauncher.ashx?request=" + req +
           f"&browserTrackerId={tracker}&isPlayTogetherGame=false")
    uri = (f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
           f"+launchtime:{int(time.time() * 1000)}"
           f"+placelauncherurl:{urllib.parse.quote(url, safe='')}"
           f"+browsertrackerid:{tracker}+robloxLocale:en_us+gameLocale:en_us"
           "+channel:+LaunchExp:InApp")

    before = roblox_pids()
    os.startfile(uri)
    for _ in range(25):
        await asyncio.sleep(1)
        new = roblox_pids() - before
        if new:
            alt.pid = next(iter(new))
            alt.launched_at = time.time()
            return True
    return False


class PosLock(commands.Cog):
    pos = app_commands.Group(name="pos", description="Pos locker controls")

    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.state = None            # {"target", "alts", "channel"}
        self.launch_lock = asyncio.Lock()

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self.watch.start()

    async def cog_unload(self):
        self.watch.cancel()
        self.stop_all()
        await self.session.close()

    # ---------- helpers ----------
    def stop_all(self):
        if self.state:
            for alt in self.state["alts"]:
                kill(alt)
        self.state = None

    async def notify(self, channel_id, text):
        ch = self.bot.get_channel(channel_id)
        if ch:
            await ch.send(text)

    async def whoami(self, alt):
        h = {"Cookie": f".ROBLOSECURITY={alt.cookie}"}
        async with self.session.get(
                "https://users.roblox.com/v1/users/authenticated", headers=h) as r:
            if r.status != 200:
                return None
            return (await r.json()).get("id")

    async def find_link(self, username):
        """Find a player's current server. Works only if Roblox shows it to us."""
        async with self.session.post(
                "https://users.roblox.com/v1/usernames/users",
                json={"usernames": [username], "excludeBannedUsers": False}) as r:
            data = await r.json()
        if not data.get("data"):
            return None
        uid = data["data"][0]["id"]
        _, _, pr = await rbx_post(self.session, os.getenv("ROBLOX_COOKIE", ""),
                                  PRESENCE_URL, {"userIds": [uid]})
        p = (pr.get("userPresences") or [{}])[0]
        if p.get("userPresenceType") == 2 and p.get("gameId") and p.get("placeId"):
            return {"place": str(p["placeId"]), "instance": p["gameId"], "code": None}
        return None

    async def in_target(self, alt, target):
        _, _, d = await rbx_post(self.session, alt.cookie, PRESENCE_URL,
                                 {"userIds": [alt.user_id]})
        p = (d.get("userPresences") or [{}])[0]
        if p.get("userPresenceType") != 2:
            return False
        gid = p.get("gameId")
        return gid is None or not target["instance"] or gid == target["instance"]

    async def relaunch(self, alt, reason):
        alt.busy = True
        try:
            async with self.launch_lock:
                if not self.state:
                    return
                channel = self.state["channel"]
                target = self.state["target"]
                kill(alt)
                ok = await launch(self.session, alt, target)
                await asyncio.sleep(5)   # spacing between clients
            alt.misses = 0
            if ok:
                alt.fails = 0
                if reason == "left":
                    await self.notify(channel, f"{alt.name} left the server and has rejoined.")
            else:
                alt.fails += 1
                alt.launched_at = time.time()   # retry after the grace period
                if alt.fails == 3:
                    await self.notify(channel, f"{alt.name} cannot join. The server may be full or closed.")
        finally:
            alt.busy = False

    async def initial_launch(self):
        for alt in list(self.state["alts"]):
            if not self.state:
                return
            await self.relaunch(alt, "start")

    # ---------- watcher ----------
    @tasks.loop(seconds=CHECK_SECS)
    async def watch(self):
        s = self.state
        if not s:
            return
        now = time.time()
        for alt in list(s["alts"]):
            try:
                if alt.busy or now - alt.launched_at < GRACE_SECS:
                    continue
                age = now - alt.launched_at
                alive = alt.pid is not None and psutil.pid_exists(alt.pid)
                inside = alive and await self.in_target(alt, s["target"])
                if inside:
                    alt.misses = 0
                    if age >= REFRESH_SECS:
                        asyncio.create_task(self.relaunch(alt, "refresh"))
                    continue
                alt.misses += 1
                if alt.misses >= 2 or not alive:
                    asyncio.create_task(self.relaunch(alt, "left"))
            except Exception as e:
                print("poslock watch error:", e)

    # ---------- commands ----------
    @pos.command(name="lock", description="Lock a server with your pos lockers")
    @app_commands.describe(count="How many pos lockers to use",
                           server_link="Roblox server link",
                           player="Roblox username to find the server from (if no link)")
    async def lock(self, interaction: discord.Interaction,
                   count: app_commands.Range[int, 1, 50],
                   server_link: str = None, player: str = None):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("You cannot use this command.", ephemeral=True)
        if os.name != "nt":
            return await interaction.response.send_message(
                "Pos lockers need Windows, because they run real Roblox clients.", ephemeral=True)
        if self.state:
            return await interaction.response.send_message(
                "A lock is already active. Use /pos unlock first.", ephemeral=True)

        await interaction.response.defer()
        target = None
        if server_link:
            target = parse_link(server_link)
        elif player:
            target = await self.find_link(player)
        if not target:
            return await interaction.followup.send(
                "Could not get a server. Give a valid server link, or a player who is in a game.")

        try:
            alts = load_alts()
        except Exception as e:
            return await interaction.followup.send(f"Could not read {ALTS_FILE}: {e}")

        ready = []
        for a in alts:
            if len(ready) >= count:
                break
            a.user_id = await self.whoami(a)
            if a.user_id:
                ready.append(a)
        if not ready:
            return await interaction.followup.send("No working alts found. Check the cookies in alts.json.")

        self.state = {"target": target, "alts": ready, "channel": interaction.channel_id}
        asyncio.create_task(self.initial_launch())
        await interaction.followup.send(
            f"Locking with {len(ready)} of {count} requested pos lockers. "
            "They join one by one, and each rejoins every 15 minutes or whenever it leaves.")

    @pos.command(name="unlock", description="Stop the lock and close all pos lockers")
    async def unlock(self, interaction: discord.Interaction):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("You cannot use this command.", ephemeral=True)
        if not self.state:
            return await interaction.response.send_message("No lock is active.", ephemeral=True)
        self.stop_all()
        await interaction.response.send_message("Lock stopped. All pos lockers closed.")

    @pos.command(name="status", description="Show the pos lockers")
    async def status(self, interaction: discord.Interaction):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("You cannot use this command.", ephemeral=True)
        if not self.state:
            return await interaction.response.send_message("No lock is active.", ephemeral=True)
        now = time.time()
        lines = []
        for a in self.state["alts"]:
            if a.busy:
                st = "joining"
            elif a.pid and psutil.pid_exists(a.pid) and a.misses == 0:
                st = f"in position, refresh in {max(0, int(REFRESH_SECS - (now - a.launched_at)) // 60)} min"
            else:
                st = "left, rejoining"
            lines.append(f"{a.name}: {st}")
        await interaction.response.send_message("\n".join(lines))


async def setup(bot):
    global _MUTEX
    if os.name == "nt":
        import ctypes
        _MUTEX = ctypes.windll.kernel32.CreateMutexW(None, True, "ROBLOX_singletonMutex")
    await bot.add_cog(PosLock(bot))

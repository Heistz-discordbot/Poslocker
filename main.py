"""
POS Locker Discord bot (Universal Cross-Platform: Mobile, Console, PC & Cloud)

Commands:
  /pos link [username]  - Link your Roblox account using an un-censorable word phrase
  /pos lock count:N     - Finds your server and provides direct join links & Job ID for all devices
  /pos stop             - Reset active lock state
  /pos status           - Show current lock status
  /pos check            - Test every cookie in alts.json

Needs:  pip install discord.py requests python-dotenv
Files next to this one:
  .env      ->  DISCORD_TOKEN=xxxx
  alts.json  ->  [{"name": "alt1", "cookie": "_|WARNING..."}, ...]
  links.json ->  (Auto-generated for persistent data storage)
"""

import asyncio
import json
import os
import random
import sys
import time
import urllib.parse

import discord
import requests
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

API_PARALLEL = 15               # Parallel Roblox API calls
PRESENCE_URL = "https://presence.roblox.com/v1/presence/users"

BASE = os.path.dirname(os.path.abspath(__file__))
LINKS_FILE = os.path.join(BASE, "links.json")

# Word list for un-censorable verification codes on Roblox
VERIFY_WORDS = [
    "apple", "banana", "cherry", "dragon", "eagle", "falcon", "forest", "galaxy",
    "hammer", "island", "jungle", "knight", "lemon", "mango", "ninja", "orange",
    "planet", "quantum", "rocket", "shadow", "tiger", "umbrella", "violet", "wizard"
]

COLORS = {"info": 0x5865F2, "ok": 0x2ECC71, "warn": 0xF1C40F, "err": 0xE74C3C}


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


# ---------------------------------------------------------------- Bot State
class State:
    place_id = None
    job_id = None
    started = 0
    target = ""


state = State()


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
                     f"Successfully linked to **{pend['name']}**! You can remove the phrase from your profile now.\n"
                     "Join your server, then run `/pos lock count:5`", "ok"),
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

    @app_commands.command(name="lock", description="Find active server and share instant join links")
    @app_commands.describe(count="How many alts to use for checking presence")
    async def lock(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]):
        await interaction.response.defer()

        linked = load_links()["linked"].get(str(interaction.user.id))
        if not linked:
            return await interaction.followup.send(
                embed=em("No account linked", "Run `/pos link` with your Roblox username first.", "err"))

        try:
            alts = load_alts()
        except Exception as e:
            return await interaction.followup.send(embed=em("Cannot read alts.json", str(e), "err"))
        if not alts:
            return await interaction.followup.send(embed=em("No alts", "alts.json is empty.", "err"))

        alts = alts[:count]

        try:
            place_id, job_id = await asyncio.to_thread(
                locate_user, [a["cookie"] for a in alts], linked["id"], linked["name"])
        except Exception as e:
            return await interaction.followup.send(embed=em("Server not found", str(e), "err"))

        state.place_id, state.job_id, state.started = place_id, job_id, time.time()
        state.target = linked["name"]

        # Universal Quick Join Links for Mobile, Console, & PC
        roblox_app_link = f"https://www.roblox.com/games/start?placeId={place_id}&gameInstanceId={job_id}"
        web_game_page = f"https://www.roblox.com/games/{place_id}"

        fields = [
            ("Target User", linked["name"], True),
            ("Place ID", f"`{place_id}`", True),
            ("Job / Server ID", f"`{job_id}`", False),
            ("Direct Join Link (Mobile/PC/Console)", f"[Click to Join Server]({roblox_app_link})", False),
            ("Game Page", f"[Open Roblox Game Page]({web_game_page})", False)
        ]

        await interaction.followup.send(embed=em(
            "Server Found & Target Locked",
            f"Successfully detected **{linked['name']}** in the server below. "
            "Anyone on Mobile, Console, or PC can use the links or Job ID to join immediately.",
            "ok", fields))

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

    @app_commands.command(name="stop", description="Reset active lock state")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        state.job_id = None
        state.target = ""
        await interaction.followup.send(embed=em("Stopped", "Lock state cleared successfully.", "ok"))

    @app_commands.command(name="status", description="Show active lock status")
    async def status(self, interaction: discord.Interaction):
        if state.job_id:
            mins = int((time.time() - state.started) // 60)
            await interaction.response.send_message(embed=em(
                "Lock Active", None, "ok",
                [("Active for", f"{mins} min", True),
                 ("Target", state.target, True),
                 ("Server ID", f"`{state.job_id}`", False)]))
        else:
            await interaction.response.send_message(embed=em("Idle", "No active lock server recorded.", "info"))


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
        print("Bot ready for cross-platform usage (Mobile, Console, PC, Cloud).")


if __name__ == "__main__":
    if not TOKEN:
        sys.exit("DISCORD_TOKEN missing in .env")
    Bot().run(TOKEN)
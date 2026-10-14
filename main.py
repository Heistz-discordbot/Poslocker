import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()


class PosBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        await self.load_extension("poslock")
        await self.tree.sync()


bot = PosBot()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")


bot.run(os.getenv("DISCORD_TOKEN"))

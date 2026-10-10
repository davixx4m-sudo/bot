"""
bot.py — exemplo de integração.
Se você já tem um bot.py, copie apenas o trecho marcado com  >>> CLEAR <<<
"""
import logging
import os

import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO)

intents = discord.Intents.default()  # não precisa de intents privilegiadas


class MeuBot(commands.Bot):
    async def setup_hook(self):
        # >>> CLEAR <<<
        await self.load_extension("clear_automatico")
        await self.tree.sync()  # registra o /clearconfig
        # >>> CLEAR <<<


bot = MeuBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Logado como {bot.user} ({bot.user.id})")


bot.run(os.getenv("DISCORD_TOKEN"))

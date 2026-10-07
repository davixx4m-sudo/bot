import os

import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise SystemExit("Defina a variável DISCORD_TOKEN no Railway.")

intents = discord.Intents.default()
intents.members = True          # Server Members Intent (ativar no Developer Portal)
intents.message_content = True  # Message Content Intent (para o prefixo "!")


class Bot(commands.Bot):
    async def setup_hook(self):
        await self.load_extension("controle_cargos")
        await self.load_extension("auto_cargo")
        await self.tree.sync()  # registra o /painelcargos

    async def on_ready(self):
        print(f"Online como {self.user} ({self.user.id}) em {len(self.guilds)} servidor(es)")


bot = Bot(command_prefix="!", intents=intents, help_command=None)

if __name__ == "__main__":
    bot.run(TOKEN)

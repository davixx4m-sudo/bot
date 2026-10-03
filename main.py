import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix=os.getenv("PREFIXO", "!"), intents=intents,
                   help_command=None, case_insensitive=True)

COGS = ["cogs.ajuda", "cogs.moderacao", "cogs.calls", "cogs.rank", "cogs.automod", "cogs.protecao"]


@bot.event
async def on_ready():
    print(f"Online como {bot.user} em {len(bot.guilds)} servidor(es)")


@bot.event
async def on_command_error(ctx, erro):
    if isinstance(erro, commands.CommandNotFound):
        return
    if isinstance(erro, commands.MissingPermissions):
        return await ctx.send("❌ Você não tem permissão para isso.")
    if isinstance(erro, commands.CheckFailure):
        return await ctx.send("❌ Você não pode usar esse comando.")
    if isinstance(erro, commands.MissingRequiredArgument):
        return await ctx.send(f"❌ Faltou: `{erro.param.name}`. Veja `{ctx.prefix}ajuda`.")
    if isinstance(erro, (commands.BadArgument, commands.UserNotFound, commands.MemberNotFound)):
        return await ctx.send("❌ Argumento inválido (usuário/cargo/canal não encontrado).")
    if isinstance(erro, commands.CommandInvokeError) and isinstance(erro.original, discord.Forbidden):
        return await ctx.send("❌ Não tenho permissão para isso (cargo do bot abaixo do alvo?).")
    print("Erro:", repr(erro))


async def main():
    async with bot:
        for c in COGS:
            await bot.load_extension(c)
        await bot.start(os.getenv("TOKEN"))


asyncio.run(main())

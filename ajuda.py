import discord
from discord.ext import commands


class Ajuda(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(aliases=["help", "comandos"])
    async def ajuda(self, ctx):
        p = ctx.prefix
        e = discord.Embed(title="Lista de comandos", color=discord.Color.blurple())
        e.add_field(name="Moderação", value=f"`{p}ban` `{p}unban` `{p}unbanall` `{p}kick` `{p}castigo` `{p}removecastigo` `{p}clear` `{p}logs`", inline=False)
        e.add_field(name="Blacklist", value=f"`{p}blacklist` `{p}removeblacklist` `{p}listblack` `{p}bl` (temporária) `{p}removebl`", inline=False)
        e.add_field(name="Calls", value=f"`{p}configcall` `{p}tranca` `{p}destranca` `{p}viradono` `{p}transferir` `{p}bancall` `{p}removerbancall` `{p}limite` `{p}renomear` `{p}ocultarcall` `{p}mostrarcall` `{p}callinfo`", inline=False)
        e.add_field(name="Ranking", value=f"`{p}rank` `{p}rankgeral` `{p}ranktempo` `{p}addcargorank` `{p}removecargorank` `{p}resetrank`", inline=False)
        e.add_field(name="AutoMod", value=f"`{p}automod` `{p}addpalavra` `{p}rmpalavra` `{p}listapalavras`", inline=False)
        e.add_field(name="Proteção de cargos", value=f"`{p}protegercargo` `{p}desprotegercargo` `{p}cargosprotegidos` `{p}wl` `{p}protecaopunir`", inline=False)
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Ajuda(bot))

import re
import time
from collections import defaultdict, deque
from datetime import timedelta
import discord
from discord.ext import commands
import db
from utils import enviar_log

OPCOES = {
    "antilink": "Apaga links",
    "antiinvite": "Apaga convites de outros servidores",
    "antispam": "Castiga quem manda 6+ msgs em 5s",
    "antieveryone": "Apaga @everyone/@here de quem não tem permissão",
    "antimencao": "Apaga mensagens com 5+ menções",
    "palavras": "Apaga palavras proibidas",
}
RE_LINK = re.compile(r"https?://|www\.", re.I)
RE_INVITE = re.compile(r"(discord\.gg|discord(?:app)?\.com/invite)/\w+", re.I)


class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hist = defaultdict(lambda: deque(maxlen=6))

    def _on(self, g, k):
        return db.cfg_get(g, "am_" + k, "0") == "1"

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def automod(self, ctx, opcao: str = None, estado: str = None):
        """!automod  -> status | !automod antilink on/off"""
        g = ctx.guild.id
        if opcao is None:
            linhas = [f"{'🟢' if self._on(g, k) else '🔴'} `{k}` — {d}" for k, d in OPCOES.items()]
            return await ctx.send(embed=discord.Embed(
                title="AutoMod", description="\n".join(linhas) + f"\n\nUse `{ctx.prefix}automod <opção> on/off`"))
        opcao = opcao.lower()
        if opcao not in OPCOES or estado not in ("on", "off"):
            return await ctx.send("❌ Use: `automod <opção> on/off`. Veja as opções com `automod`.")
        db.cfg_set(g, "am_" + opcao, "1" if estado == "on" else "0")
        await ctx.send(f"✅ `{opcao}` {'ativado' if estado == 'on' else 'desativado'}.")

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def addpalavra(self, ctx, *, palavra: str):
        db.run("INSERT OR IGNORE INTO palavras VALUES(?,?)", (ctx.guild.id, palavra.lower()))
        await ctx.send("✅ Palavra adicionada.")
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def rmpalavra(self, ctx, *, palavra: str):
        db.run("DELETE FROM palavras WHERE guild_id=? AND palavra=?", (ctx.guild.id, palavra.lower()))
        await ctx.send("✅ Palavra removida.")

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def listapalavras(self, ctx):
        rows = db.all_("SELECT palavra FROM palavras WHERE guild_id=?", (ctx.guild.id,))
        await ctx.author.send("Palavras: " + (", ".join(r["palavra"] for r in rows) or "nenhuma"))
        await ctx.send("📩 Enviei na sua DM.")

    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.author.bot or not msg.guild or not isinstance(msg.author, discord.Member):
            return
        if msg.author.guild_permissions.manage_messages:
            return
        g, c = msg.guild.id, msg.content
        motivo = None

        if self._on(g, "antiinvite") and RE_INVITE.search(c):
            motivo = "convite de outro servidor"
        elif self._on(g, "antilink") and RE_LINK.search(c):
            motivo = "link"
        elif self._on(g, "antieveryone") and ("@everyone" in c or "@here" in c):
            motivo = "menção @everyone/@here"
        elif self._on(g, "antimencao") and len(set(msg.mentions)) >= 5:
            motivo = "menção em massa"
        elif self._on(g, "palavras"):
            baixo = c.lower()
            if any(r["palavra"] in baixo for r in db.all_("SELECT palavra FROM palavras WHERE guild_id=?", (g,))):
                motivo = "palavra proibida"

        if motivo:
            try:
                await msg.delete()
            except discord.HTTPException:
                return
            await msg.channel.send(f"🛡️ {msg.author.mention}, mensagem removida ({motivo}).", delete_after=5)
            await enviar_log(msg.guild, "AutoMod", f"{msg.author.mention} em {msg.channel.mention}: **{motivo}**")
            return

        if self._on(g, "antispam"):
            h = self.hist[(g, msg.author.id)]
            h.append(time.time())
            if len(h) == 6 and h[-1] - h[0] <= 5:
                h.clear()
                try:
                    await msg.author.timeout(timedelta(minutes=5), reason="AutoMod: spam")
                    await msg.channel.send(f"🛡️ {msg.author.mention} em castigo por 5min (spam).", delete_after=8)
                    await enviar_log(msg.guild, "AutoMod", f"{msg.author.mention} castigado por spam (5min)")
                except discord.HTTPException:
                    pass


async def setup(bot):
    await bot.add_cog(AutoMod(bot))

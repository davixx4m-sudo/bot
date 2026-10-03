import time
import discord
from discord.ext import commands
import db
from utils import fmt_seg


class Rank(commands.Cog):
    """Rank de mensagens e de tempo em call."""

    def __init__(self, bot):
        self.bot = bot
        self.entrou = {}   # (guild, user) -> timestamp de entrada em call
        self.cd = {}       # anti-farm de mensagens

    # ---------- contagem ----------
    @commands.Cog.listener()
    async def on_message(self, msg):
        if msg.author.bot or not msg.guild:
            return
        k = (msg.guild.id, msg.author.id)
        agora = time.time()
        if agora - self.cd.get(k, 0) < 3:
            return
        self.cd[k] = agora
        db.run("INSERT INTO msgs VALUES(?,?,1) ON CONFLICT(guild_id,user_id) DO UPDATE SET total=total+1", k)
        total = db.one("SELECT total FROM msgs WHERE guild_id=? AND user_id=?", k)["total"]
        for r in db.all_("SELECT * FROM rank_cargos WHERE guild_id=? AND msgs<=?", (msg.guild.id, total)):
            cargo = msg.guild.get_role(r["role_id"])
            if cargo and cargo not in msg.author.roles:
                try:
                    await msg.author.add_roles(cargo, reason="Recompensa de rank")
                except discord.HTTPException:
                    pass

    @commands.Cog.listener()
    async def on_voice_state_update(self, m, antes, depois):
        if m.bot:
            return
        k = (m.guild.id, m.id)
        if depois.channel and not antes.channel:
            self.entrou[k] = time.time()
        elif antes.channel and not depois.channel:
            ini = self.entrou.pop(k, None)
            if ini:
                db.run("INSERT INTO voz VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET segundos=segundos+excluded.segundos",
                       (m.guild.id, m.id, int(time.time() - ini)))

    @commands.Cog.listener()
    async def on_ready(self):
        for g in self.bot.guilds:
            for vc in g.voice_channels:
                for m in vc.members:
                    if not m.bot:
                        self.entrou.setdefault((g.id, m.id), time.time())

    def _voz(self, g, u):
        r = db.one("SELECT segundos FROM voz WHERE guild_id=? AND user_id=?", (g, u))
        seg = r["segundos"] if r else 0
        if (g, u) in self.entrou:
            seg += int(time.time() - self.entrou[(g, u)])
        return seg

    # ---------- comandos ----------
    @commands.command()
    async def rank(self, ctx, membro: discord.Member = None):
        membro = membro or ctx.author
        g = ctx.guild.id
        r = db.one("SELECT total FROM msgs WHERE guild_id=? AND user_id=?", (g, membro.id))
        total = r["total"] if r else 0
        pos = db.one("SELECT COUNT(*)+1 AS p FROM msgs WHERE guild_id=? AND total>?", (g, total))["p"]
        e = discord.Embed(title=f"Rank de {membro.display_name}", color=discord.Color.gold())
        e.set_thumbnail(url=membro.display_avatar.url)
        e.add_field(name="💬 Mensagens", value=f"{total} (#{pos})")
        e.add_field(name="🎙️ Tempo em call", value=fmt_seg(self._voz(g, membro.id)))
        await ctx.send(embed=e)

    @commands.command(aliases=["rank_msgs", "centralrank"])
    async def rankgeral(self, ctx):
        rows = db.all_("SELECT user_id,total FROM msgs WHERE guild_id=? ORDER BY total DESC LIMIT 10", (ctx.guild.id,))
        if not rows:
            return await ctx.send("Ainda sem dados.")
        txt = "\n".join(f"**{i}.** <@{r['user_id']}> — {r['total']} msgs" for i, r in enumerate(rows, 1))
        await ctx.send(embed=discord.Embed(title="🏆 Top mensagens", description=txt, color=discord.Color.gold()))

    @commands.command(aliases=["rankcall"])
    async def ranktempo(self, ctx):
        g = ctx.guild.id
        ids = {r["user_id"] for r in db.all_("SELECT user_id FROM voz WHERE guild_id=?", (g,))}
        ids |= {u for (gg, u) in self.entrou if gg == g}
        top = sorted(((u, self._voz(g, u)) for u in ids), key=lambda x: x[1], reverse=True)[:10]
        if not top:
            return await ctx.send("Ainda sem dados.")
        txt = "\n".join(f"**{i}.** <@{u}> — {fmt_seg(s)}" for i, (u, s) in enumerate(top, 1))
        await ctx.send(embed=discord.Embed(title="🎙️ Top tempo em call", description=txt, color=discord.Color.gold()))

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def addcargorank(self, ctx, msgs: int, cargo: discord.Role):
        """Dá o cargo automaticamente ao atingir X mensagens."""
        db.run("INSERT OR REPLACE INTO rank_cargos VALUES(?,?,?)", (ctx.guild.id, msgs, cargo.id))
        await ctx.send(f"✅ {cargo.mention} será dado com {msgs} mensagens.")

    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def removecargorank(self, ctx, msgs: int):
        db.run("DELETE FROM rank_cargos WHERE guild_id=? AND msgs=?", (ctx.guild.id, msgs))
        await ctx.send("✅ Recompensa removida.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def resetrank(self, ctx):
        db.run("DELETE FROM msgs WHERE guild_id=?", (ctx.guild.id,))
        db.run("DELETE FROM voz WHERE guild_id=?", (ctx.guild.id,))
        self.entrou = {k: time.time() for k in self.entrou}
        await ctx.send("🗑️ Rank zerado.")


async def setup(bot):
    await bot.add_cog(Rank(bot))

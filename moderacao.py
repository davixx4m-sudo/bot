import time
from datetime import timedelta
import discord
from discord.ext import commands, tasks
import db
from utils import parse_tempo, enviar_log


def pode(ctx, alvo):
    """Hierarquia: não age no dono nem em cargo >= ao do autor."""
    if isinstance(alvo, discord.Member):
        if alvo.id == ctx.guild.owner_id:
            return False
        if ctx.author.id != ctx.guild.owner_id and alvo.top_role >= ctx.author.top_role:
            return False
    return True


class Moderacao(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        self.expirar.start()

    async def cog_unload(self):
        self.expirar.cancel()

    # ---------- logs ----------
    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def logs(self, ctx, canal: discord.TextChannel):
        db.cfg_set(ctx.guild.id, "logs", canal.id)
        await ctx.send(f"✅ Canal de logs: {canal.mention}")

    # ---------- bans ----------
    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def ban(self, ctx, alvo: discord.User, *, motivo="Sem motivo"):
        membro = ctx.guild.get_member(alvo.id)
        if membro and not pode(ctx, membro):
            return await ctx.send("❌ Não posso banir esse usuário (hierarquia).")
        await ctx.guild.ban(alvo, reason=f"{ctx.author}: {motivo}", delete_message_days=0)
        await ctx.send(f"🔨 {alvo} banido. Motivo: {motivo}")
        await enviar_log(ctx.guild, "Ban", f"{alvo} por {ctx.author.mention}\n{motivo}", discord.Color.red())

    @commands.command(aliases=["rmban"])
    @commands.has_permissions(ban_members=True)
    async def unban(self, ctx, alvo: discord.User):
        try:
            await ctx.guild.unban(alvo)
        except discord.NotFound:
            return await ctx.send("❌ Esse usuário não está banido.")
        await ctx.send(f"✅ {alvo} desbanido.")
        await enviar_log(ctx.guild, "Unban", f"{alvo} por {ctx.author.mention}")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def unbanall(self, ctx):
        n = 0
        async for b in ctx.guild.bans(limit=None):
            try:
                await ctx.guild.unban(b.user)
                n += 1
            except discord.HTTPException:
                pass
        await ctx.send(f"✅ {n} usuário(s) desbanido(s).")

    @commands.command()
    @commands.has_permissions(kick_members=True)
    async def kick(self, ctx, membro: discord.Member, *, motivo="Sem motivo"):
        if not pode(ctx, membro):
            return await ctx.send("❌ Não posso expulsar esse usuário (hierarquia).")
        await membro.kick(reason=f"{ctx.author}: {motivo}")
        await ctx.send(f"👢 {membro} expulso.")
        await enviar_log(ctx.guild, "Kick", f"{membro} por {ctx.author.mention}\n{motivo}")

    # ---------- castigo (timeout) ----------
    @commands.command()
    @commands.has_permissions(moderate_members=True)
    async def castigo(self, ctx, membro: discord.Member, tempo: str, *, motivo="Sem motivo"):
        seg = parse_tempo(tempo)
        if not seg or seg > 28 * 86400:
            return await ctx.send("❌ Tempo inválido. Ex: `10m`, `2h`, `1d` (máx 28d).")
        if not pode(ctx, membro):
            return await ctx.send("❌ Não posso castigar esse usuário (hierarquia).")
        await membro.timeout(timedelta(seconds=seg), reason=f"{ctx.author}: {motivo}")
        await ctx.send(f"🔇 {membro.mention} em castigo por `{tempo}`.")
        await enviar_log(ctx.guild, "Castigo", f"{membro} por {ctx.author.mention} ({tempo})\n{motivo}")

    @commands.command(aliases=["rmcastigo"])
    @commands.has_permissions(moderate_members=True)
    async def removecastigo(self, ctx, membro: discord.Member):
        await membro.timeout(None)
        await ctx.send(f"✅ Castigo removido de {membro.mention}.")

    @commands.command(aliases=["limpar"])
    @commands.has_permissions(manage_messages=True)
    async def clear(self, ctx, qtd: int):
        qtd = max(1, min(qtd, 500))
        apagadas = await ctx.channel.purge(limit=qtd + 1)
        await ctx.send(f"🧹 {len(apagadas) - 1} mensagens apagadas.", delete_after=4)

    # ---------- blacklist ----------
    async def _add_bl(self, ctx, alvo, motivo, expira):
        db.run("INSERT OR REPLACE INTO bl VALUES(?,?,?,?)", (ctx.guild.id, alvo.id, motivo, expira))
        membro = ctx.guild.get_member(alvo.id)
        if membro and not pode(ctx, membro):
            return await ctx.send("❌ Não posso banir esse usuário (hierarquia).")
        try:
            await ctx.guild.ban(alvo, reason=f"Blacklist: {motivo}")
        except discord.HTTPException:
            pass
        await ctx.send(f"⛔ {alvo} adicionado à blacklist. Motivo: {motivo}")
        await enviar_log(ctx.guild, "Blacklist", f"{alvo} por {ctx.author.mention}\n{motivo}", discord.Color.dark_red())

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def blacklist(self, ctx, alvo: discord.User, *, motivo="Sem motivo"):
        await self._add_bl(ctx, alvo, motivo, None)

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def bl(self, ctx, alvo: discord.User, tempo: str, *, motivo="Sem motivo"):
        seg = parse_tempo(tempo)
        if not seg:
            return await ctx.send("❌ Tempo inválido. Ex: `1d`, `12h`.")
        await self._add_bl(ctx, alvo, motivo, time.time() + seg)

    @commands.command(aliases=["removebl"])
    @commands.has_permissions(ban_members=True)
    async def removeblacklist(self, ctx, alvo: discord.User):
        db.run("DELETE FROM bl WHERE guild_id=? AND user_id=?", (ctx.guild.id, alvo.id))
        try:
            await ctx.guild.unban(alvo)
        except discord.HTTPException:
            pass
        await ctx.send(f"✅ {alvo} removido da blacklist.")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    async def listblack(self, ctx):
        rows = db.all_("SELECT * FROM bl WHERE guild_id=?", (ctx.guild.id,))
        if not rows:
            return await ctx.send("A blacklist está vazia.")
        txt = "\n".join(
            f"<@{r['user_id']}> — {r['motivo']}" + (f" (até <t:{int(r['expira'])}:R>)" if r["expira"] else "")
            for r in rows[:40])
        await ctx.send(embed=discord.Embed(title="Blacklist", description=txt))

    @commands.Cog.listener()
    async def on_member_join(self, m):
        r = db.one("SELECT * FROM bl WHERE guild_id=? AND user_id=?", (m.guild.id, m.id))
        if not r:
            return
        if r["expira"] and r["expira"] < time.time():
            db.run("DELETE FROM bl WHERE guild_id=? AND user_id=?", (m.guild.id, m.id))
            return
        try:
            await m.ban(reason=f"Blacklist: {r['motivo']}")
        except discord.HTTPException:
            pass

    @tasks.loop(minutes=1)
    async def expirar(self):
        for r in db.all_("SELECT * FROM bl WHERE expira IS NOT NULL AND expira<?", (time.time(),)):
            g = self.bot.get_guild(r["guild_id"])
            if g:
                try:
                    await g.unban(discord.Object(r["user_id"]), reason="BL temporária expirou")
                except discord.HTTPException:
                    pass
            db.run("DELETE FROM bl WHERE guild_id=? AND user_id=?", (r["guild_id"], r["user_id"]))

    @expirar.before_loop
    async def _antes(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Moderacao(bot))

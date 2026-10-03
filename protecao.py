import discord
from discord.ext import commands
import db
from utils import enviar_log


def perigosa(p: discord.Permissions):
    return p.administrator or p.manage_guild or p.manage_roles


def so_dono():
    async def check(ctx):
        return ctx.author.id == ctx.guild.owner_id
    return commands.check(check)


class Protecao(commands.Cog):
    """Protege cargos: reverte quem não é dono/whitelist."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- helpers ----------
    def _protegido(self, g, role_id):
        return db.one("SELECT 1 FROM cargos_prot WHERE guild_id=? AND role_id=?", (g, role_id)) is not None

    def _autorizado(self, guild, user):
        if user.id in (guild.owner_id, self.bot.user.id):
            return True
        return db.one("SELECT 1 FROM whitelist WHERE guild_id=? AND user_id=?", (guild.id, user.id)) is not None

    async def _executor(self, guild, acao, alvo_id):
        if not guild.me.guild_permissions.view_audit_log:
            return None
        try:
            async for e in guild.audit_logs(limit=6, action=acao):
                if e.target and e.target.id == alvo_id and (discord.utils.utcnow() - e.created_at).total_seconds() < 15:
                    return e.user
        except discord.HTTPException:
            pass
        return None

    async def _punir(self, guild, user, motivo):
        if db.cfg_get(guild.id, "prot_punir", "0") != "1":
            return
        m = guild.get_member(user.id)
        if m:
            try:
                from datetime import timedelta
                await m.timeout(timedelta(minutes=10), reason=f"Proteção: {motivo}")
            except discord.HTTPException:
                pass

    # ---------- eventos ----------
    @commands.Cog.listener()
    async def on_member_update(self, antes, depois):
        novos = set(depois.roles) - set(antes.roles)
        for role in novos:
            if not (self._protegido(depois.guild.id, role.id) or perigosa(role.permissions)):
                continue
            ex = await self._executor(depois.guild, discord.AuditLogAction.member_role_update, depois.id)
            if ex and not self._autorizado(depois.guild, ex):
                try:
                    await depois.remove_roles(role, reason="Proteção de cargos")
                except discord.HTTPException:
                    continue
                await enviar_log(depois.guild, "🛡️ Cargo revertido",
                                 f"{ex.mention} tentou dar **{role.name}** a {depois.mention}.", discord.Color.red())
                await self._punir(depois.guild, ex, "dar cargo protegido")

    @commands.Cog.listener()
    async def on_guild_role_update(self, antes, depois):
        g = depois.guild
        virou_perigosa = perigosa(depois.permissions) and not perigosa(antes.permissions)
        mexeu = self._protegido(g.id, depois.id) and (
            antes.permissions != depois.permissions or antes.name != depois.name or antes.color != depois.color)
        if not (virou_perigosa or mexeu):
            return
        ex = await self._executor(g, discord.AuditLogAction.role_update, depois.id)
        if ex and not self._autorizado(g, ex):
            try:
                await depois.edit(name=antes.name, permissions=antes.permissions, colour=antes.color,
                                  reason="Proteção de cargos")
            except discord.HTTPException:
                return
            await enviar_log(g, "🛡️ Edição de cargo revertida",
                             f"{ex.mention} tentou editar **{antes.name}**.", discord.Color.red())
            await self._punir(g, ex, "editar cargo protegido")

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        g = role.guild
        if not self._protegido(g.id, role.id):
            return
        ex = await self._executor(g, discord.AuditLogAction.role_delete, role.id)
        if ex and not self._autorizado(g, ex):
            try:
                novo = await g.create_role(name=role.name, permissions=role.permissions, colour=role.color,
                                           hoist=role.hoist, mentionable=role.mentionable,
                                           reason="Proteção de cargos: recriado")
            except discord.HTTPException:
                return
            db.run("DELETE FROM cargos_prot WHERE guild_id=? AND role_id=?", (g.id, role.id))
            db.run("INSERT OR IGNORE INTO cargos_prot VALUES(?,?)", (g.id, novo.id))
            await enviar_log(g, "🛡️ Cargo recriado",
                             f"{ex.mention} apagou **{role.name}**. Recriei (posição e membros não voltam sozinhos).",
                             discord.Color.red())
            await self._punir(g, ex, "apagar cargo protegido")

    # ---------- comandos ----------
    @commands.command()
    @commands.has_permissions(administrator=True)
    async def protegercargo(self, ctx, cargo: discord.Role):
        db.run("INSERT OR IGNORE INTO cargos_prot VALUES(?,?)", (ctx.guild.id, cargo.id))
        await ctx.send(f"🛡️ {cargo.mention} protegido.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def desprotegercargo(self, ctx, cargo: discord.Role):
        db.run("DELETE FROM cargos_prot WHERE guild_id=? AND role_id=?", (ctx.guild.id, cargo.id))
        await ctx.send(f"✅ {cargo.mention} não está mais protegido.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def cargosprotegidos(self, ctx):
        rows = db.all_("SELECT role_id FROM cargos_prot WHERE guild_id=?", (ctx.guild.id,))
        await ctx.send(", ".join(f"<@&{r['role_id']}>" for r in rows) or "Nenhum cargo protegido.",
                       allowed_mentions=discord.AllowedMentions.none())

    @commands.command()
    @so_dono()
    async def wl(self, ctx, acao: str, membro: discord.Member = None):
        """!wl add @user | !wl remove @user | !wl list (só o dono do servidor)"""
        g = ctx.guild.id
        acao = acao.lower()
        if acao == "list":
            rows = db.all_("SELECT user_id FROM whitelist WHERE guild_id=?", (g,))
            return await ctx.send(", ".join(f"<@{r['user_id']}>" for r in rows) or "Whitelist vazia.",
                                  allowed_mentions=discord.AllowedMentions.none())
        if not membro or acao not in ("add", "remove"):
            return await ctx.send("Use: `wl add @user`, `wl remove @user` ou `wl list`.")
        if acao == "add":
            db.run("INSERT OR IGNORE INTO whitelist VALUES(?,?)", (g, membro.id))
        else:
            db.run("DELETE FROM whitelist WHERE guild_id=? AND user_id=?", (g, membro.id))
        await ctx.send("✅ Whitelist atualizada.")

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def protecaopunir(self, ctx, estado: str):
        """Dá 10min de castigo a quem tentar burlar a proteção."""
        db.cfg_set(ctx.guild.id, "prot_punir", "1" if estado.lower() == "on" else "0")
        await ctx.send(f"✅ Punição {'ativada' if estado.lower() == 'on' else 'desativada'}.")


async def setup(bot):
    await bot.add_cog(Protecao(bot))

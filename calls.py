import discord
from discord.ext import commands
import db
from utils import enviar_log


class Calls(commands.Cog):
    """Calls temporárias: entra no canal 'criar call' e ganha uma call só sua."""

    def __init__(self, bot):
        self.bot = bot

    # ---------- helpers ----------
    async def _pegar(self, ctx, exigir_dono=True):
        v = ctx.author.voice
        if not v or not v.channel:
            await ctx.send("❌ Entre em uma call primeiro.")
            return None
        row = db.one("SELECT * FROM calls WHERE channel_id=?", (v.channel.id,))
        if not row:
            await ctx.send("❌ Essa não é uma call temporária.")
            return None
        if exigir_dono and row["owner_id"] != ctx.author.id and not ctx.author.guild_permissions.administrator:
            await ctx.send(f"❌ Só o dono (<@{row['owner_id']}>) pode fazer isso.")
            return None
        return v.channel, row

    # ---------- configuração ----------
    @commands.command()
    @commands.has_permissions(manage_guild=True)
    async def configcall(self, ctx, canal: discord.VoiceChannel):
        """Define o canal de voz 'criar call'."""
        db.cfg_set(ctx.guild.id, "hub_call", canal.id)
        await ctx.send(f"✅ Quem entrar em {canal.mention} ganha uma call própria.")

    # ---------- comandos do dono ----------
    @commands.command(aliases=["trancar"])
    async def tranca(self, ctx):
        r = await self._pegar(ctx)
        if r:
            await r[0].set_permissions(ctx.guild.default_role, connect=False)
            await ctx.send("🔒 Call trancada. Ninguém novo entra.")

    @commands.command(aliases=["destrancar"])
    async def destranca(self, ctx):
        r = await self._pegar(ctx)
        if r:
            await r[0].set_permissions(ctx.guild.default_role, connect=True)
            await ctx.send("🔓 Call destrancada.")

    @commands.command(aliases=["virardono", "assumir"])
    async def viradono(self, ctx):
        """Assume a call se o dono saiu."""
        r = await self._pegar(ctx, exigir_dono=False)
        if not r:
            return
        canal, row = r
        if row["owner_id"] == ctx.author.id:
            return await ctx.send("Você já é o dono.")
        if any(m.id == row["owner_id"] for m in canal.members):
            return await ctx.send("❌ O dono ainda está na call.")
        await self._trocar_dono(canal, row["owner_id"], ctx.author)
        await ctx.send(f"👑 {ctx.author.mention} agora é o dono da call.")

    @commands.command()
    async def transferir(self, ctx, membro: discord.Member):
        r = await self._pegar(ctx)
        if not r:
            return
        canal, row = r
        if membro not in canal.members or membro.bot:
            return await ctx.send("❌ O membro precisa estar na call.")
        await self._trocar_dono(canal, row["owner_id"], membro)
        await ctx.send(f"👑 {membro.mention} agora é o dono da call.")

    async def _trocar_dono(self, canal, antigo_id, novo):
        antigo = canal.guild.get_member(antigo_id)
        if antigo:
            await canal.set_permissions(antigo, overwrite=None)
        await canal.set_permissions(novo, connect=True, view_channel=True, move_members=True,
                                    mute_members=True, deafen_members=True)
        db.run("UPDATE calls SET owner_id=? WHERE channel_id=?", (novo.id, canal.id))

    @commands.command()
    async def bancall(self, ctx, membro: discord.Member):
        """Bane alguém da sua call."""
        r = await self._pegar(ctx)
        if not r:
            return
        canal, row = r
        if membro.id == row["owner_id"] or membro.guild_permissions.administrator or membro.bot:
            return await ctx.send("❌ Não dá para banir esse usuário da call.")
        await canal.set_permissions(membro, connect=False)
        db.run("INSERT OR IGNORE INTO call_bans VALUES(?,?)", (canal.id, membro.id))
        if membro in canal.members:
            await membro.move_to(None)
        await ctx.send(f"🚫 {membro.mention} banido da call.")

    @commands.command(aliases=["unbancall", "rmbancall"])
    async def removerbancall(self, ctx, membro: discord.Member):
        """Remove o ban de alguém da sua call."""
        r = await self._pegar(ctx)
        if not r:
            return
        canal, _ = r
        await canal.set_permissions(membro, overwrite=None)
        db.run("DELETE FROM call_bans WHERE channel_id=? AND user_id=?", (canal.id, membro.id))
        await ctx.send(f"✅ {membro.mention} pode entrar na call de novo.")

    @commands.command()
    async def limite(self, ctx, n: int):
        r = await self._pegar(ctx)
        if r:
            await r[0].edit(user_limit=max(0, min(n, 99)))
            await ctx.send(f"👥 Limite: {n if n else 'sem limite'}.")

    @commands.command()
    async def renomear(self, ctx, *, nome: str):
        r = await self._pegar(ctx)
        if r:
            await r[0].edit(name=nome[:90])
            await ctx.send("✏️ Nome alterado.")

    @commands.command()
    async def ocultarcall(self, ctx):
        r = await self._pegar(ctx)
        if r:
            await r[0].set_permissions(ctx.guild.default_role, view_channel=False)
            await ctx.send("🙈 Call oculta.")

    @commands.command()
    async def mostrarcall(self, ctx):
        r = await self._pegar(ctx)
        if r:
            await r[0].set_permissions(ctx.guild.default_role, view_channel=True)
            await ctx.send("👁️ Call visível.")

    @commands.command()
    async def callinfo(self, ctx):
        r = await self._pegar(ctx, exigir_dono=False)
        if not r:
            return
        canal, row = r
        trancada = canal.overwrites_for(ctx.guild.default_role).connect is False
        bans = db.all_("SELECT user_id FROM call_bans WHERE channel_id=?", (canal.id,))
        e = discord.Embed(title=canal.name, color=discord.Color.blurple())
        e.add_field(name="Dono", value=f"<@{row['owner_id']}>")
        e.add_field(name="Trancada", value="Sim" if trancada else "Não")
        e.add_field(name="Banidos", value=", ".join(f"<@{b['user_id']}>" for b in bans) or "Ninguém", inline=False)
        await ctx.send(embed=e)

    # ---------- eventos ----------
    @commands.Cog.listener()
    async def on_voice_state_update(self, m, antes, depois):
        if m.bot:
            return
        hub = db.cfg_get(m.guild.id, "hub_call")
        if depois.channel and hub and depois.channel.id == int(hub):
            await self._criar(m, depois.channel)
        if antes.channel and antes.channel != depois.channel:
            if db.one("SELECT 1 FROM calls WHERE channel_id=?", (antes.channel.id,)) and not antes.channel.members:
                await self._apagar(antes.channel)

    async def _criar(self, m, hub):
        ja = db.one("SELECT channel_id FROM calls WHERE owner_id=? AND guild_id=?", (m.id, m.guild.id))
        if ja:
            existente = m.guild.get_channel(ja["channel_id"])
            if existente:
                return await m.move_to(existente)
        over = {
            m.guild.default_role: discord.PermissionOverwrite(connect=True),
            m: discord.PermissionOverwrite(connect=True, view_channel=True, move_members=True,
                                           mute_members=True, deafen_members=True),
        }
        try:
            ch = await m.guild.create_voice_channel(f"📞 {m.display_name}", category=hub.category, overwrites=over)
            db.run("INSERT INTO calls VALUES(?,?,?)", (ch.id, m.guild.id, m.id))
            await m.move_to(ch)
        except discord.HTTPException:
            await enviar_log(m.guild, "Erro nas calls", "Não consegui criar a call. Verifique minhas permissões.")

    async def _apagar(self, canal):
        db.run("DELETE FROM calls WHERE channel_id=?", (canal.id,))
        db.run("DELETE FROM call_bans WHERE channel_id=?", (canal.id,))
        try:
            await canal.delete(reason="Call temporária vazia")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        # limpa calls que ficaram vazias/apagadas enquanto o bot estava off
        for row in db.all_("SELECT * FROM calls"):
            g = self.bot.get_guild(row["guild_id"])
            ch = g.get_channel(row["channel_id"]) if g else None
            if not ch:
                db.run("DELETE FROM calls WHERE channel_id=?", (row["channel_id"],))
            elif not ch.members:
                await self._apagar(ch)


async def setup(bot):
    await bot.add_cog(Calls(bot))

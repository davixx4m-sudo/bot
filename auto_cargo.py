"""
auto_cargo.py — Painel de Auto Cargo (discord.py >= 2.4)
Dá cargos automaticamente quando um membro (ou bot) entra no servidor.

Comando: !autocargo (ou /autocargo) — somente administradores.
Permissão do bot: Gerenciar Cargos. O cargo do bot precisa ficar ACIMA dos cargos automáticos.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

import discord
from discord.ext import commands

DB_PATH = os.getenv("CONTROLE_CARGOS_DB", "controle_cargos.db")
COR = discord.Color.from_rgb(88, 101, 242)
TIPOS = {"humano": "👤 Membros", "bot": "🤖 Bots"}


# ════════════════════════════════════════════════════════════════
#  BANCO
# ════════════════════════════════════════════════════════════════
class Banco:
    def __init__(self, caminho: str):
        self.conn = sqlite3.connect(caminho, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ac_config (
                guild_id INTEGER PRIMARY KEY,
                ativo INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS ac_cargos (
                guild_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,          -- 'humano' ou 'bot'
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, tipo, role_id)
            );
            """
        )
        self.conn.commit()

    def ativo(self, guild_id: int) -> bool:
        row = self.conn.execute("SELECT ativo FROM ac_config WHERE guild_id = ?", (guild_id,)).fetchone()
        return True if row is None else bool(row["ativo"])

    def set_ativo(self, guild_id: int, valor: bool):
        self.conn.execute(
            "INSERT INTO ac_config (guild_id, ativo) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET ativo = excluded.ativo",
            (guild_id, int(valor)),
        )
        self.conn.commit()

    def cargos(self, guild_id: int, tipo: str) -> set[int]:
        rows = self.conn.execute(
            "SELECT role_id FROM ac_cargos WHERE guild_id = ? AND tipo = ?", (guild_id, tipo)
        )
        return {r["role_id"] for r in rows}

    def set_cargos(self, guild_id: int, tipo: str, ids: list[int]):
        self.conn.execute("DELETE FROM ac_cargos WHERE guild_id = ? AND tipo = ?", (guild_id, tipo))
        self.conn.executemany(
            "INSERT OR IGNORE INTO ac_cargos (guild_id, tipo, role_id) VALUES (?, ?, ?)",
            [(guild_id, tipo, i) for i in ids],
        )
        self.conn.commit()


# ════════════════════════════════════════════════════════════════
#  EMBED
# ════════════════════════════════════════════════════════════════
def montar_embed(guild: discord.Guild, banco: Banco) -> discord.Embed:
    ativo = banco.ativo(guild.id)
    emb = discord.Embed(
        title="🎁 Auto Cargo",
        description="Quem entrar no servidor recebe os cargos abaixo **automaticamente**.",
        color=discord.Color.green() if ativo else discord.Color.red(),
    )
    emb.add_field(name="📌 Status", value="🟢 Ativado" if ativo else "🔴 Desativado", inline=False)
    for tipo, titulo in TIPOS.items():
        mencoes = [f"<@&{i}>" for i in sorted(banco.cargos(guild.id, tipo)) if guild.get_role(i)]
        texto = " ".join(mencoes) or "*Nenhum*"
        if len(texto) > 450:
            texto = texto[:450].rsplit(" ", 1)[0] + " …"
        emb.add_field(name=f"{titulo} — cargos automáticos", value=texto, inline=False)
    if not guild.me.guild_permissions.manage_roles:
        emb.add_field(name="⚠️ Permissão faltando", value="Gerenciar Cargos — sem isso não funciona.", inline=False)
    if guild.icon:
        emb.set_thumbnail(url=guild.icon.url)
    emb.set_footer(text=guild.name)
    return emb


# ════════════════════════════════════════════════════════════════
#  SELECT (mensagem efêmera)
# ════════════════════════════════════════════════════════════════
class SelectAutoCargos(discord.ui.RoleSelect):
    def __init__(self, banco: Banco, guild: discord.Guild, tipo: str, painel: Optional[discord.Message]):
        self.banco, self.tipo, self.painel = banco, tipo, painel
        defaults = [
            discord.SelectDefaultValue(id=i, type=discord.SelectDefaultValueType.role)
            for i in banco.cargos(guild.id, tipo) if guild.get_role(i)
        ][:25]
        super().__init__(
            placeholder="Selecione os cargos automáticos…",
            min_values=0, max_values=25, default_values=defaults,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        validos, avisos = [], []
        for r in self.values:
            if r.is_default() or r.managed:
                avisos.append(f"{r.mention} ignorado (cargo do sistema/integração).")
                continue
            validos.append(r)
            if r >= guild.me.top_role:
                avisos.append(f"⚠️ {r.mention} está acima do meu cargo — não consigo entregar esse.")
        self.banco.set_cargos(guild.id, self.tipo, [r.id for r in validos])
        lista = " ".join(r.mention for r in validos) or "*nenhum*"
        texto = f"✅ Auto cargo de {TIPOS[self.tipo]} atualizado.\nCargos: {lista}"
        if avisos:
            texto += "\n\n" + "\n".join(avisos)
        await interaction.response.edit_message(content=texto, view=None)
        if self.painel:
            try:
                await self.painel.edit(embed=montar_embed(guild, self.banco))
            except discord.HTTPException:
                pass


class ViewTemp(discord.ui.View):
    def __init__(self, item: discord.ui.Item):
        super().__init__(timeout=180)
        self.add_item(item)


# ════════════════════════════════════════════════════════════════
#  PAINEL (persistente)
# ════════════════════════════════════════════════════════════════
class PainelView(discord.ui.View):
    def __init__(self, banco: Banco):
        super().__init__(timeout=None)
        self.banco = banco

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        u = interaction.user
        if interaction.guild and (u.id == interaction.guild.owner_id or u.guild_permissions.administrator):
            return True
        await interaction.response.send_message("❌ Apenas administradores podem usar este painel.", ephemeral=True)
        return False

    async def _abrir(self, interaction: discord.Interaction, tipo: str):
        view = ViewTemp(SelectAutoCargos(self.banco, interaction.guild, tipo, interaction.message))
        await interaction.response.send_message(
            f"🎁 Selecione os cargos que {TIPOS[tipo]} recebem ao entrar:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Cargos de Membros", emoji="👤", style=discord.ButtonStyle.primary,
                       custom_id="ac:humano", row=0)
    async def membros(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._abrir(interaction, "humano")

    @discord.ui.button(label="Cargos de Bots", emoji="🤖", style=discord.ButtonStyle.primary,
                       custom_id="ac:bot", row=0)
    async def bots(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._abrir(interaction, "bot")

    @discord.ui.button(label="Ligar / Desligar", emoji="🔌", style=discord.ButtonStyle.secondary,
                       custom_id="ac:toggle", row=1)
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = interaction.guild
        self.banco.set_ativo(g.id, not self.banco.ativo(g.id))
        await interaction.response.edit_message(embed=montar_embed(g, self.banco))

    @discord.ui.button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="ac:refresh", row=1)
    async def atualizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=montar_embed(interaction.guild, self.banco))


# ════════════════════════════════════════════════════════════════
#  COG
# ════════════════════════════════════════════════════════════════
class AutoCargo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.banco = Banco(DB_PATH)

    async def cog_load(self):
        self.bot.add_view(PainelView(self.banco))

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Apenas administradores podem usar este comando.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Use este comando dentro de um servidor.")

    @commands.hybrid_command(name="autocargo", description="Abre o painel de cargo automático.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def autocargo(self, ctx: commands.Context):
        await ctx.send(embed=montar_embed(ctx.guild, self.banco), view=PainelView(self.banco))

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        if not self.banco.ativo(guild.id):
            return
        ids = self.banco.cargos(guild.id, "bot" if member.bot else "humano")
        roles = []
        for i in ids:
            r = guild.get_role(i)
            if r and not r.managed and not r.is_default() and r < guild.me.top_role:
                roles.append(r)
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Auto Cargo: entrada no servidor")
        except discord.HTTPException as e:
            print(f"[auto_cargo] Falha ao dar cargos para {member} em {guild.name}: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoCargo(bot))

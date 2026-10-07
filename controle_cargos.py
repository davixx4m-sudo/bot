"""
controle_cargos.py  —  Sistema de controle de cargos (discord.py >= 2.4)

COMO USAR
1) Suba este arquivo na raiz do projeto (mesma pasta do bot.py).
2) No bot.py, carregue a extensão (uma linha, dentro do setup_hook):
       await bot.load_extension("controle_cargos")
3) Intents obrigatórias: members (e message_content se usar o prefixo "!").
   Ative "Server Members Intent" no Developer Portal.
4) Permissões do bot: Gerenciar Cargos, Ver Registro de Auditoria,
   Enviar Mensagens, Inserir Links. O cargo do bot precisa ficar ACIMA
   dos cargos controlados.
5) Comando: !painelcargos (ou /painelcargos) — somente administradores.

Variável opcional: CONTROLE_CARGOS_DB (caminho do SQLite, padrão controle_cargos.db).
No Railway, use um Volume para o banco não ser apagado a cada deploy.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
from collections import deque
from typing import Optional

import discord
from discord.ext import commands

DB_PATH = os.getenv("CONTROLE_CARGOS_DB", "controle_cargos.db")
GRUPOS = (1, 2)
COR = discord.Color.from_rgb(88, 101, 242)
VERDE = discord.Color.green()
VERMELHO = discord.Color.red()


# ════════════════════════════════════════════════════════════════
#  BANCO DE DADOS (SQLite, tudo separado por servidor)
# ════════════════════════════════════════════════════════════════
class Banco:
    def __init__(self, caminho: str):
        self.conn = sqlite3.connect(caminho, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cc_config (
                guild_id INTEGER PRIMARY KEY,
                canal_logs INTEGER
            );
            CREATE TABLE IF NOT EXISTS cc_cargos (
                guild_id INTEGER NOT NULL,
                grupo INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, grupo, role_id)
            );
            CREATE TABLE IF NOT EXISTS cc_autorizados (
                guild_id INTEGER NOT NULL,
                grupo INTEGER NOT NULL,
                tipo TEXT NOT NULL,          -- 'role' ou 'user'
                alvo_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, grupo, tipo, alvo_id)
            );
            """
        )
        self.conn.commit()

    # ---- canal de logs
    def set_canal_logs(self, guild_id: int, canal_id: Optional[int]):
        self.conn.execute(
            "INSERT INTO cc_config (guild_id, canal_logs) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET canal_logs = excluded.canal_logs",
            (guild_id, canal_id),
        )
        self.conn.commit()

    # ---- cargos permitidos de um grupo
    def set_cargos(self, guild_id: int, grupo: int, ids: list[int]):
        self.conn.execute("DELETE FROM cc_cargos WHERE guild_id = ? AND grupo = ?", (guild_id, grupo))
        self.conn.executemany(
            "INSERT OR IGNORE INTO cc_cargos (guild_id, grupo, role_id) VALUES (?, ?, ?)",
            [(guild_id, grupo, i) for i in ids],
        )
        self.conn.commit()

    # ---- autorizados (cargos ou membros) de um grupo
    def set_autorizados(self, guild_id: int, grupo: int, tipo: str, ids: list[int]):
        self.conn.execute(
            "DELETE FROM cc_autorizados WHERE guild_id = ? AND grupo = ? AND tipo = ?",
            (guild_id, grupo, tipo),
        )
        self.conn.executemany(
            "INSERT OR IGNORE INTO cc_autorizados (guild_id, grupo, tipo, alvo_id) VALUES (?, ?, ?, ?)",
            [(guild_id, grupo, tipo, i) for i in ids],
        )
        self.conn.commit()

    def carregar(self, guild_id: int) -> dict:
        dados: dict = {"log": None}
        row = self.conn.execute(
            "SELECT canal_logs FROM cc_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row:
            dados["log"] = row["canal_logs"]
        for g in GRUPOS:
            dados[g] = {"cargos": set(), "roles": set(), "users": set()}
        for r in self.conn.execute(
            "SELECT grupo, role_id FROM cc_cargos WHERE guild_id = ?", (guild_id,)
        ):
            if r["grupo"] in dados:
                dados[r["grupo"]]["cargos"].add(r["role_id"])
        for r in self.conn.execute(
            "SELECT grupo, tipo, alvo_id FROM cc_autorizados WHERE guild_id = ?", (guild_id,)
        ):
            if r["grupo"] in dados:
                dados[r["grupo"]]["roles" if r["tipo"] == "role" else "users"].add(r["alvo_id"])
        return dados


# ════════════════════════════════════════════════════════════════
#  EMBED DO PAINEL
# ════════════════════════════════════════════════════════════════
def _juntar(mencoes: list[str], vazio: str = "*Nenhum*", limite: int = 450) -> str:
    if not mencoes:
        return vazio
    texto = " ".join(mencoes)
    if len(texto) > limite:
        texto = texto[:limite].rsplit(" ", 1)[0] + " …"
    return texto


def montar_embed(guild: discord.Guild, banco: Banco) -> discord.Embed:
    dados = banco.carregar(guild.id)
    emb = discord.Embed(
        title="🎖️ Controle de Cargos",
        description=(
            "Defina **quais cargos** cada pessoa autorizada pode setar nos membros.\n"
            "Qualquer cargo fora da lista é **revertido automaticamente** e a tentativa "
            "é registrada no canal de logs."
        ),
        color=COR,
    )
    for g in GRUPOS:
        d = dados[g]
        cargos = [f"<@&{i}>" for i in sorted(d["cargos"]) if guild.get_role(i)]
        aut = [f"<@&{i}>" for i in sorted(d["roles"]) if guild.get_role(i)]
        aut += [f"<@{i}>" for i in sorted(d["users"])]
        titulo = f"🎖️ Função {g} — Cargos Permitidos" + ("" if g == 1 else " 2")
        emb.add_field(
            name=titulo,
            value=(
                f"**Cargos que podem ser setados:**\n{_juntar(cargos)}\n\n"
                f"**Autorizados a gerenciar:**\n{_juntar(aut)}"
            ),
            inline=False,
        )
    canal = f"<#{dados['log']}>" if dados["log"] and guild.get_channel(dados["log"]) else "*Não configurado*"
    emb.add_field(name="📜 Canal de logs", value=canal, inline=False)

    perms = guild.me.guild_permissions
    faltando = []
    if not perms.manage_roles:
        faltando.append("Gerenciar Cargos")
    if not perms.view_audit_log:
        faltando.append("Ver Registro de Auditoria")
    if faltando:
        emb.add_field(
            name="⚠️ Permissões faltando no bot",
            value=", ".join(faltando) + " — sem isso o sistema não funciona.",
            inline=False,
        )
    if guild.icon:
        emb.set_thumbnail(url=guild.icon.url)
    emb.set_footer(text=guild.name)
    return emb


async def _atualizar_painel(msg: Optional[discord.Message], guild: discord.Guild, banco: Banco):
    if msg is None:
        return
    try:
        await msg.edit(embed=montar_embed(guild, banco))
    except discord.HTTPException:
        pass


def _filtrar_cargos(guild: discord.Guild, roles: list[discord.Role]) -> tuple[list[discord.Role], list[str]]:
    """Remove @everyone e cargos gerenciados (bots/boost). Retorna (válidos, avisos)."""
    validos, avisos = [], []
    for r in roles:
        if r.is_default() or r.managed:
            avisos.append(f"{r.mention} ignorado (cargo do sistema/integração).")
            continue
        validos.append(r)
        if r >= guild.me.top_role:
            avisos.append(f"⚠️ {r.mention} está acima do meu cargo — não consigo reverter esse.")
    return validos, avisos


# ════════════════════════════════════════════════════════════════
#  SELECTS (abrem em mensagem efêmera)
# ════════════════════════════════════════════════════════════════
class SelectCargos(discord.ui.RoleSelect):
    def __init__(self, banco: Banco, guild: discord.Guild, grupo: int, painel: Optional[discord.Message]):
        self.banco, self.grupo, self.painel = banco, grupo, painel
        atuais = banco.carregar(guild.id)[grupo]["cargos"]
        defaults = [
            discord.SelectDefaultValue(id=i, type=discord.SelectDefaultValueType.role)
            for i in atuais if guild.get_role(i)
        ][:25]
        super().__init__(
            placeholder="Selecione os cargos permitidos…",
            min_values=0, max_values=25, default_values=defaults,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        validos, avisos = _filtrar_cargos(guild, list(self.values))
        self.banco.set_cargos(guild.id, self.grupo, [r.id for r in validos])
        lista = _juntar([r.mention for r in validos], vazio="*nenhum*")
        texto = f"✅ **Função {self.grupo}** atualizada.\nCargos permitidos: {lista}"
        if avisos:
            texto += "\n\n" + "\n".join(avisos)
        await interaction.response.edit_message(content=texto, view=None)
        await _atualizar_painel(self.painel, guild, self.banco)


class SelectAutorizadosCargos(discord.ui.RoleSelect):
    def __init__(self, banco: Banco, guild: discord.Guild, grupo: int, painel: Optional[discord.Message]):
        self.banco, self.grupo, self.painel = banco, grupo, painel
        atuais = banco.carregar(guild.id)[grupo]["roles"]
        defaults = [
            discord.SelectDefaultValue(id=i, type=discord.SelectDefaultValueType.role)
            for i in atuais if guild.get_role(i)
        ][:25]
        super().__init__(
            placeholder="Cargos autorizados a gerenciar…",
            min_values=0, max_values=25, default_values=defaults, row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        ids = [r.id for r in self.values if not r.is_default()]
        self.banco.set_autorizados(interaction.guild.id, self.grupo, "role", ids)
        await interaction.response.edit_message(content=f"✅ Cargos autorizados da **Função {self.grupo}** salvos.")
        await _atualizar_painel(self.painel, interaction.guild, self.banco)


class SelectAutorizadosMembros(discord.ui.UserSelect):
    def __init__(self, banco: Banco, guild: discord.Guild, grupo: int, painel: Optional[discord.Message]):
        self.banco, self.grupo, self.painel = banco, grupo, painel
        atuais = banco.carregar(guild.id)[grupo]["users"]
        defaults = [
            discord.SelectDefaultValue(id=i, type=discord.SelectDefaultValueType.user)
            for i in atuais
        ][:25]
        super().__init__(
            placeholder="Membros autorizados a gerenciar…",
            min_values=0, max_values=25, default_values=defaults, row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        ids = [u.id for u in self.values]
        self.banco.set_autorizados(interaction.guild.id, self.grupo, "user", ids)
        await interaction.response.edit_message(content=f"✅ Membros autorizados da **Função {self.grupo}** salvos.")
        await _atualizar_painel(self.painel, interaction.guild, self.banco)


class SelectCanalLogs(discord.ui.ChannelSelect):
    def __init__(self, banco: Banco, guild: discord.Guild, painel: Optional[discord.Message]):
        self.banco, self.painel = banco, painel
        atual = banco.carregar(guild.id)["log"]
        defaults = (
            [discord.SelectDefaultValue(id=atual, type=discord.SelectDefaultValueType.channel)]
            if atual and guild.get_channel(atual) else []
        )
        super().__init__(
            placeholder="Escolha o canal de logs…",
            channel_types=[discord.ChannelType.text],
            min_values=0, max_values=1, default_values=defaults,
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        if not self.values:
            self.banco.set_canal_logs(guild.id, None)
            texto = "✅ Canal de logs removido."
        else:
            canal = guild.get_channel(self.values[0].id)
            if canal is None:
                return await interaction.response.edit_message(content="❌ Canal não encontrado.", view=None)
            p = canal.permissions_for(guild.me)
            if not (p.view_channel and p.send_messages and p.embed_links):
                return await interaction.response.edit_message(
                    content=f"❌ Não consigo enviar embeds em {canal.mention}. Ajuste minhas permissões.",
                    view=None,
                )
            self.banco.set_canal_logs(guild.id, canal.id)
            texto = f"✅ Logs serão enviados em {canal.mention}."
        await interaction.response.edit_message(content=texto, view=None)
        await _atualizar_painel(self.painel, guild, self.banco)


class ViewTemp(discord.ui.View):
    def __init__(self, *itens: discord.ui.Item):
        super().__init__(timeout=180)
        for i in itens:
            self.add_item(i)


# ════════════════════════════════════════════════════════════════
#  PAINEL PRINCIPAL (persistente)
# ════════════════════════════════════════════════════════════════
class PainelView(discord.ui.View):
    def __init__(self, banco: Banco):
        super().__init__(timeout=None)
        self.banco = banco

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        u = interaction.user
        if interaction.guild and (
            u.id == interaction.guild.owner_id or u.guild_permissions.administrator
        ):
            return True
        await interaction.response.send_message(
            "❌ Apenas administradores podem usar este painel.", ephemeral=True
        )
        return False

    async def _cargos(self, interaction: discord.Interaction, grupo: int):
        view = ViewTemp(SelectCargos(self.banco, interaction.guild, grupo, interaction.message))
        await interaction.response.send_message(
            f"🎖️ **Função {grupo}** — selecione os cargos que podem ser setados:",
            view=view, ephemeral=True,
        )

    async def _autorizados(self, interaction: discord.Interaction, grupo: int):
        view = ViewTemp(
            SelectAutorizadosCargos(self.banco, interaction.guild, grupo, interaction.message),
            SelectAutorizadosMembros(self.banco, interaction.guild, grupo, interaction.message),
        )
        await interaction.response.send_message(
            f"👥 **Função {grupo}** — quem pode gerenciar esses cargos? (cargos e/ou membros)",
            view=view, ephemeral=True,
        )

    @discord.ui.button(label="Configurar Cargos (1)", emoji="🎖️", style=discord.ButtonStyle.primary,
                       custom_id="cc:cargos:1", row=0)
    async def cargos_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cargos(interaction, 1)

    @discord.ui.button(label="Autorizados (1)", emoji="👥", style=discord.ButtonStyle.secondary,
                       custom_id="cc:aut:1", row=0)
    async def aut_1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._autorizados(interaction, 1)

    @discord.ui.button(label="Configurar Cargos (2)", emoji="🎖️", style=discord.ButtonStyle.primary,
                       custom_id="cc:cargos:2", row=1)
    async def cargos_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._cargos(interaction, 2)

    @discord.ui.button(label="Autorizados (2)", emoji="👥", style=discord.ButtonStyle.secondary,
                       custom_id="cc:aut:2", row=1)
    async def aut_2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._autorizados(interaction, 2)

    @discord.ui.button(label="Canal de Logs", emoji="📜", style=discord.ButtonStyle.success,
                       custom_id="cc:logs", row=2)
    async def logs(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = ViewTemp(SelectCanalLogs(self.banco, interaction.guild, interaction.message))
        await interaction.response.send_message(
            "📜 Selecione o canal onde as tentativas serão registradas:", view=view, ephemeral=True
        )

    @discord.ui.button(label="Atualizar", emoji="🔄", style=discord.ButtonStyle.secondary,
                       custom_id="cc:refresh", row=2)
    async def atualizar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=montar_embed(interaction.guild, self.banco))


# ════════════════════════════════════════════════════════════════
#  COG: comando + monitoramento de cargos
# ════════════════════════════════════════════════════════════════
class ControleCargos(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.banco = Banco(DB_PATH)
        self._processadas: deque[int] = deque(maxlen=300)  # ids de entradas de auditoria já tratadas

    async def cog_load(self):
        self.bot.add_view(PainelView(self.banco))

    async def cog_command_error(self, ctx: commands.Context, error: Exception):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ Apenas administradores podem usar este comando.", ephemeral=True)
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send("❌ Use este comando dentro de um servidor.")

    @commands.hybrid_command(name="painelcargos", description="Abre o painel de controle de cargos.")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def painelcargos(self, ctx: commands.Context):
        await ctx.send(embed=montar_embed(ctx.guild, self.banco), view=PainelView(self.banco))

    # ---------------- utilidades ----------------
    @staticmethod
    def _eh_autorizado(membro: discord.Member, grupo: dict) -> bool:
        if membro.id in grupo["users"]:
            return True
        return any(r.id in grupo["roles"] for r in membro.roles)

    async def _achar_entrada(self, guild: discord.Guild, membro_id: int,
                             add_ids: set[int], rem_ids: set[int]) -> Optional[discord.AuditLogEntry]:
        """Procura no registro de auditoria quem fez a alteração (pode demorar ~1s para aparecer)."""
        for espera in (1.0, 1.5, 2.0):
            await asyncio.sleep(espera)
            try:
                async for entry in guild.audit_logs(limit=15, action=discord.AuditLogAction.member_role_update):
                    if entry.id in self._processadas:
                        continue
                    if entry.target is None or entry.target.id != membro_id:
                        continue
                    if (discord.utils.utcnow() - entry.created_at).total_seconds() > 30:
                        break
                    adicionados = {r.id for r in (getattr(entry.after, "roles", None) or [])}
                    removidos = {r.id for r in (getattr(entry.before, "roles", None) or [])}
                    if (add_ids & adicionados) or (rem_ids & removidos):
                        self._processadas.append(entry.id)
                        return entry
            except discord.Forbidden:
                print(f"[controle_cargos] Sem permissão 'Ver Registro de Auditoria' em {guild.name}.")
                return None
            except discord.HTTPException:
                continue
        return None

    async def _enviar_log(self, guild: discord.Guild, canal_id: Optional[int], emb: discord.Embed):
        if not canal_id:
            return
        canal = guild.get_channel(canal_id)
        if canal is None:
            return
        try:
            await canal.send(embed=emb, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    # ---------------- monitoramento ----------------
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.roles == after.roles:
            return
        guild = after.guild
        dados = self.banco.carregar(guild.id)
        if not any(dados[g]["cargos"] or dados[g]["roles"] or dados[g]["users"] for g in GRUPOS):
            return  # servidor sem configuração

        ids_antes = {r.id for r in before.roles}
        ids_depois = {r.id for r in after.roles}
        add_ids, rem_ids = ids_depois - ids_antes, ids_antes - ids_depois
        if not add_ids and not rem_ids:
            return

        entry = await self._achar_entrada(guild, after.id, add_ids, rem_ids)
        if entry is None or entry.user is None:
            return
        autor = entry.user
        # ignora o próprio bot (reversões, painel de tags etc.), outros bots e o dono do servidor
        if autor.id == self.bot.user.id or autor.bot or autor.id == guild.owner_id:
            return

        executor = guild.get_member(autor.id)
        if executor is None:
            try:
                executor = await guild.fetch_member(autor.id)
            except discord.HTTPException:
                return

        meus_grupos = [g for g in GRUPOS if self._eh_autorizado(executor, dados[g])]
        permitidos: set[int] = set().union(*(dados[g]["cargos"] for g in meus_grupos)) if meus_grupos else set()
        todos_controlados: set[int] = set().union(*(dados[g]["cargos"] for g in GRUPOS))
        eh_admin = executor.guild_permissions.administrator

        registros = []  # (role, acao, permitido, motivo)
        for rid, acao in [(i, "adicionado") for i in add_ids] + [(i, "removido") for i in rem_ids]:
            role = guild.get_role(rid)
            if role is None:
                continue
            if meus_grupos:
                ok = rid in permitidos
                motivo = "—" if ok else "Cargo fora da lista de cargos permitidos para essa pessoa."
            elif rid in todos_controlados and not eh_admin:
                ok, motivo = False, "Pessoa sem autorização para gerenciar este cargo."
            else:
                continue  # não é assunto do sistema
            registros.append((role, acao, ok, motivo))

        if not registros:
            return

        # ---- reverte o que foi bloqueado
        reverter_remover = [r for r, a, ok, _ in registros if not ok and a == "adicionado"]
        reverter_adicionar = [r for r, a, ok, _ in registros if not ok and a == "removido"]
        falhou: set[int] = set()
        motivo_rev = f"Controle de Cargos: alteração não autorizada de {executor} ({executor.id})"
        if reverter_remover:
            try:
                await after.remove_roles(*reverter_remover, reason=motivo_rev)
            except discord.HTTPException:
                falhou.update(r.id for r in reverter_remover)
        if reverter_adicionar:
            try:
                await after.add_roles(*reverter_adicionar, reason=motivo_rev)
            except discord.HTTPException:
                falhou.update(r.id for r in reverter_adicionar)

        # ---- avisa a pessoa (DM) com todos os cargos bloqueados
        bloqueados = [r for r, _, ok, _ in registros if not ok]
        if bloqueados:
            nomes = ", ".join(f"**{r.name}**" for r in bloqueados)
            try:
                await executor.send(
                    f"🚫 Você não tem permissão para alterar o(s) cargo(s) {nomes} em **{guild.name}**. "
                    f"A alteração em {after.mention} foi revertida e registrada nos logs."
                )
            except discord.HTTPException:
                pass  # DM fechada — o log no canal continua valendo

        # ---- logs
        for role, acao, ok, motivo in registros:
            emb = discord.Embed(
                title="✅ Cargo permitido" if ok else "🚫 Cargo bloqueado",
                color=VERDE if ok else VERMELHO,
                timestamp=discord.utils.utcnow(),
            )
            emb.add_field(name="👤 Quem tentou", value=f"{executor.mention}\n`{executor.id}`", inline=True)
            emb.add_field(name="🧑 Em qual membro", value=f"{after.mention}\n`{after.id}`", inline=True)
            emb.add_field(name="🎖️ Cargo", value=f"{role.mention}\n`{role.id}`", inline=True)
            emb.add_field(name="🔧 Ação", value=acao.capitalize(), inline=True)
            emb.add_field(name="📌 Resultado", value="Permitido" if ok else "Bloqueado", inline=True)
            if not ok:
                emb.add_field(name="📝 Motivo", value=motivo, inline=False)
                if role.id in falhou:
                    emb.add_field(
                        name="⚠️ Atenção",
                        value="Não consegui reverter. Coloque meu cargo acima deste cargo.",
                        inline=False,
                    )
                else:
                    emb.add_field(name="↩️ Reversão", value="Alteração desfeita automaticamente.", inline=False)
            await self._enviar_log(guild, dados["log"], emb)


async def setup(bot: commands.Bot):
    await bot.add_cog(ControleCargos(bot))

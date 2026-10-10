"""
clear_automatico.py
Sistema de CLEAR AUTOMÁTICO de mensagens para discord.py 2.x

- Apenas apaga mensagens (channel.purge). Não cria, apaga ou altera canais,
  cargos, permissões ou membros.
- Cada canal configurado tem o seu próprio temporizador (asyncio.Task).
- Configuração persistida em SQLite (database.py).
- Painel: /clearconfig
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from database import Database

log = logging.getLogger("clear_automatico")

# ============================== CONFIGURAÇÕES ===============================
# Se a lista estiver vazia: qualquer membro com permissão de Administrador pode
# configurar. Se tiver IDs: só administradores QUE ESTEJAM nesta lista.
USUARIOS_AUTORIZADOS: set[int] = set()

INTERVALO_PADRAO = 10 * 60          # 10 min (usado ao adicionar canais)
INTERVALO_MINIMO = 5                # segundos (evita abuso de rate limit)
INTERVALO_MAXIMO = 365 * 86400      # 365 dias
PRESERVAR_FIXADAS = False           # True = não apaga mensagens fixadas
ARQUIVO_DB = Path(os.getenv("DB_PATH", Path(__file__).with_name("clear_automatico.db")))
ARQUIVO_DB.parent.mkdir(parents=True, exist_ok=True)
# ============================================================================

TIPOS_CANAL = (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)
TIPOS_SELECT = [
    discord.ChannelType.text,
    discord.ChannelType.voice,   # chat de texto vinculado à call
    discord.ChannelType.stage_voice,
]


# ------------------------------------------------------------------ utilidades
def formatar_intervalo(segundos: int) -> str:
    partes = []
    for nome, valor in (("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        qtd, segundos = divmod(segundos, valor)
        if qtd:
            partes.append(f"{qtd}{nome}")
    return " ".join(partes) or "0s"


def permissoes_faltando(canal: discord.abc.GuildChannel) -> list[str]:
    perms = canal.permissions_for(canal.guild.me)
    faltando = []
    if not perms.view_channel:
        faltando.append("Ver canal")
    if not perms.read_message_history:
        faltando.append("Ler histórico")
    if not perms.manage_messages:
        faltando.append("Gerenciar mensagens")
    return faltando


# ===================================================================== COG
class ClearAutomatico(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = Database(str(ARQUIVO_DB))
        self._tarefas: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._iniciado = False

    # -------------------------------------------------------------- ciclo de vida
    async def cog_load(self):
        if self.bot.is_ready():
            self._iniciar_tudo()

    async def cog_unload(self):
        for t in self._tarefas.values():
            t.cancel()
        self._tarefas.clear()
        self.db.close()

    @commands.Cog.listener()
    async def on_ready(self):
        self._iniciar_tudo()

    def _iniciar_tudo(self):
        if self._iniciado:
            return
        self._iniciado = True
        for row in self.db.list_enabled_channels():
            self.agendar(row["channel_id"])
        log.info("Clear automático: %d temporizador(es) retomado(s).", len(self._tarefas))

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        # Apenas limpa a configuração salva (não mexe em nenhum canal).
        if self.db.remove_channel(channel.id):
            self.parar(channel.id)

    # ----------------------------------------------------------------- permissões
    async def checar(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        ok = (
            interaction.guild is not None
            and isinstance(user, discord.Member)
            and user.guild_permissions.administrator
            and (not USUARIOS_AUTORIZADOS or user.id in USUARIOS_AUTORIZADOS)
        )
        if not ok:
            msg = "❌ Apenas administradores autorizados podem usar este painel."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        return ok

    # --------------------------------------------------------------- temporizadores
    def agendar(self, canal_id: int):
        """(Re)inicia o temporizador individual do canal."""
        self.parar(canal_id)
        self._tarefas[canal_id] = asyncio.create_task(
            self._loop_canal(canal_id), name=f"clear-{canal_id}"
        )

    def parar(self, canal_id: int):
        tarefa = self._tarefas.pop(canal_id, None)
        if tarefa:
            tarefa.cancel()

    async def _loop_canal(self, canal_id: int):
        while True:
            try:
                cfg = self.db.get_channel(canal_id)
                if cfg is None or not self.db.is_enabled(cfg["guild_id"]):
                    return

                espera = cfg["last_run"] + cfg["interval_seconds"] - time.time()
                if espera > 0:
                    await asyncio.sleep(espera)

                # Reconfere após a espera (config pode ter mudado)
                cfg = self.db.get_channel(canal_id)
                if cfg is None or not self.db.is_enabled(cfg["guild_id"]):
                    return

                canal = await self._obter_canal(canal_id)
                if canal is None:
                    self.db.remove_channel(canal_id)
                    return

                qtd, erro = await self.limpar_canal(canal)
                self.db.set_last_run(canal_id, time.time())
                if erro:
                    log.warning("Clear #%s (%s): %s", canal.name, canal_id, erro)
                else:
                    log.info("Clear #%s (%s): %d mensagem(ns) apagada(s).", canal.name, canal_id, qtd)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Erro no temporizador do canal %s", canal_id)
                self.db.set_last_run(canal_id, time.time())
                await asyncio.sleep(5)

    async def _obter_canal(self, canal_id: int):
        canal = self.bot.get_channel(canal_id)
        if canal is not None:
            return canal if isinstance(canal, TIPOS_CANAL) else None
        try:
            canal = await self.bot.fetch_channel(canal_id)
        except discord.NotFound:
            return None
        return canal if isinstance(canal, TIPOS_CANAL) else None

    # ------------------------------------------------------------------ o clear
    async def limpar_canal(self, canal) -> tuple[int, str | None]:
        """
        Apaga as mensagens do canal usando o purge normal da API.
        O discord.py apaga em massa as mensagens com menos de 14 dias e, ao
        chegar nas mais antigas, passa a excluí-las uma a uma (mais lento,
        respeitando o rate limit).
        Retorna (quantidade_apagada, erro | None).
        """
        lock = self._locks.setdefault(canal.id, asyncio.Lock())
        async with lock:
            faltando = permissoes_faltando(canal)
            if faltando:
                return 0, "permissões ausentes: " + ", ".join(faltando)
            check = (lambda m: not m.pinned) if PRESERVAR_FIXADAS else (lambda m: True)
            try:
                apagadas = await canal.purge(
                    limit=None,
                    check=check,
                    bulk=True,
                    reason="Clear automático",
                )
                return len(apagadas), None
            except discord.Forbidden:
                return 0, "sem permissão (Forbidden)"
            except discord.HTTPException as e:
                return 0, f"erro HTTP {e.status}: {e.text}"
            except Exception as e:  # nunca deixa falhar em silêncio
                log.exception("Falha inesperada no clear de %s", canal.id)
                return 0, f"{type(e).__name__}: {e}"

    # ------------------------------------------------- operações usadas pelo painel
    def _resolver(self, guild: discord.Guild, ids: list[int]) -> list:
        canais = []
        for cid in ids:
            ch = guild.get_channel(cid)
            if isinstance(ch, TIPOS_CANAL):
                canais.append(ch)
        return canais

    def _aviso(self, canal) -> str:
        f = permissoes_faltando(canal)
        return f" ⚠️ (sem: {', '.join(f)})" if f else ""

    def op_adicionar(self, guild: discord.Guild, ids: list[int]) -> str:
        novos, existentes = [], []
        for ch in self._resolver(guild, ids):
            if self.db.add_channel(guild.id, ch.id, INTERVALO_PADRAO):
                novos.append(ch)
                self.agendar(ch.id)
            else:
                existentes.append(ch)
        linhas = []
        if novos:
            linhas.append(
                f"✅ **Adicionados** (intervalo padrão {formatar_intervalo(INTERVALO_PADRAO)}):\n"
                + "\n".join(f"• {c.mention}{self._aviso(c)}" for c in novos)
            )
        if existentes:
            linhas.append(
                "ℹ️ **Já estavam configurados:**\n" + "\n".join(f"• {c.mention}" for c in existentes)
            )
        return "\n\n".join(linhas) or "Nenhum canal válido selecionado."

    def op_remover(self, guild: discord.Guild, ids: list[int]) -> str:
        removidos, ausentes = [], []
        for cid in ids:
            ch = guild.get_channel(cid)
            nome = ch.mention if ch else f"`{cid}`"
            if self.db.remove_channel(cid):
                self.parar(cid)
                removidos.append(nome)
            else:
                ausentes.append(nome)
        linhas = []
        if removidos:
            linhas.append("🗑️ **Removidos do clear automático:**\n" + "\n".join(f"• {n}" for n in removidos))
        if ausentes:
            linhas.append("ℹ️ **Não estavam configurados:**\n" + "\n".join(f"• {n}" for n in ausentes))
        return "\n\n".join(linhas)

    def op_selecionar(self, guild: discord.Guild, ids: list[int]) -> str:
        """Define EXATAMENTE quais canais ficam configurados (mantém intervalos existentes)."""
        validos = {c.id for c in self._resolver(guild, ids)}
        atuais = {r["channel_id"] for r in self.db.list_channels(guild.id)}
        for cid in atuais - validos:
            self.db.remove_channel(cid)
            self.parar(cid)
        for cid in validos - atuais:
            self.db.add_channel(guild.id, cid, INTERVALO_PADRAO)
            self.agendar(cid)
        if not validos:
            return "🗑️ Nenhum canal selecionado. Todos foram removidos do clear automático."
        canais = self._resolver(guild, list(validos))
        return (
            "✅ **Canais selecionados:**\n"
            + "\n".join(f"• {c.mention}{self._aviso(c)}" for c in canais)
            + f"\n\nNovos canais usam {formatar_intervalo(INTERVALO_PADRAO)} por padrão; "
            "ajuste em **Configurar intervalo**."
        )

    async def executar_agora(self, guild: discord.Guild) -> str:
        linhas = []
        total = 0
        for row in self.db.list_channels(guild.id):
            ch = guild.get_channel(row["channel_id"])
            if not isinstance(ch, TIPOS_CANAL):
                continue
            qtd, erro = await self.limpar_canal(ch)
            self.db.set_last_run(ch.id, time.time())
            self.agendar(ch.id)  # reinicia o temporizador a partir de agora
            if erro:
                linhas.append(f"• {ch.mention}: ❌ {erro}")
            else:
                total += qtd
                linhas.append(f"• {ch.mention}: ✅ {qtd} mensagem(ns)")
        return f"🧹 **Clear concluído** — {total} mensagem(ns) apagada(s).\n" + "\n".join(linhas)

    # ----------------------------------------------------------------- /clearconfig
    @app_commands.command(name="clearconfig", description="Abre o painel do clear automático.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def clearconfig(self, interaction: discord.Interaction):
        if not await self.checar(interaction):
            return
        ativo = self.db.is_enabled(interaction.guild.id)
        embed = discord.Embed(
            title="🧹 Clear Automático — Painel",
            description=(
                f"Status: **{'🟢 Ativado' if ativo else '🔴 Desativado'}**\n"
                f"Canais configurados: **{len(self.db.list_channels(interaction.guild.id))}**\n\n"
                "Use os botões abaixo para configurar. O sistema **somente apaga mensagens** — "
                "não altera canais, cargos, permissões nem membros."
            ),
            color=discord.Color.green() if ativo else discord.Color.red(),
        )
        await interaction.response.send_message(embed=embed, view=PainelView(self), ephemeral=True)

    async def mostrar_config(self, interaction: discord.Interaction):
        g = interaction.guild
        ativo = self.db.is_enabled(g.id)
        linhas = []
        for r in self.db.list_channels(g.id):
            ch = g.get_channel(r["channel_id"])
            nome = ch.mention if ch else f"`{r['channel_id']}` (não encontrado)"
            prox = (
                f"<t:{int(r['last_run'] + r['interval_seconds'])}:R>" if ativo else "pausado"
            )
            aviso = self._aviso(ch) if ch else ""
            linhas.append(
                f"• {nome} — a cada **{formatar_intervalo(r['interval_seconds'])}** — próximo: {prox}{aviso}"
            )
        texto = "\n".join(linhas) or "_Nenhum canal configurado._"
        if len(texto) > 3900:
            texto = texto[:3900] + "\n…"
        embed = discord.Embed(
            title="⚙️ Configurações do clear automático",
            description=f"Status: **{'🟢 Ativado' if ativo else '🔴 Desativado'}**\n\n{texto}",
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ===================================================================== UI
class PainelView(discord.ui.View):
    def __init__(self, cog: ClearAutomatico):
        super().__init__(timeout=600)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.checar(interaction)

    async def _abrir_seletor(self, interaction: discord.Interaction, modo: str, texto: str):
        await interaction.response.send_message(
            texto, view=SeletorCanaisView(self.cog, modo), ephemeral=True
        )

    # ---- linha 1
    @discord.ui.button(label="Selecionar canais", style=discord.ButtonStyle.primary, emoji="📋", row=0)
    async def btn_selecionar(self, interaction: discord.Interaction, button: discord.ui.Button):
        atuais = self.cog.db.list_channels(interaction.guild.id)
        lista = ", ".join(f"<#{r['channel_id']}>" for r in atuais) or "nenhum"
        await self._abrir_seletor(
            interaction,
            "selecionar",
            "Escolha **exatamente** os canais que receberão o clear automático "
            "(canais não escolhidos serão removidos).\n"
            f"Atualmente: {lista}",
        )

    @discord.ui.button(label="Adicionar canal", style=discord.ButtonStyle.success, emoji="➕", row=0)
    async def btn_adicionar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._abrir_seletor(interaction, "adicionar", "Escolha os canais que deseja **adicionar**:")

    @discord.ui.button(label="Remover canal", style=discord.ButtonStyle.danger, emoji="➖", row=0)
    async def btn_remover(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._abrir_seletor(interaction, "remover", "Escolha os canais que deseja **remover**:")

    # ---- linha 2
    @discord.ui.button(label="Configurar intervalo", style=discord.ButtonStyle.secondary, emoji="⏱️", row=1)
    async def btn_intervalo(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._abrir_seletor(
            interaction,
            "intervalo",
            "Escolha os canais cujo **intervalo** você quer definir "
            "(cada canal mantém seu próprio intervalo):",
        )

    @discord.ui.button(label="Ativar clear automático", style=discord.ButtonStyle.success, emoji="🟢", row=1)
    async def btn_ativar(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = interaction.guild
        canais = self.cog.db.list_channels(g.id)
        if not canais:
            await interaction.response.send_message(
                "⚠️ Nenhum canal configurado. Adicione canais antes de ativar.", ephemeral=True
            )
            return
        self.cog.db.set_enabled(g.id, True)
        self.cog.db.reset_timers(g.id)  # evita limpar imediatamente por atraso acumulado
        for r in canais:
            self.cog.agendar(r["channel_id"])
        await interaction.response.send_message(
            f"🟢 Clear automático **ativado** para {len(canais)} canal(is).", ephemeral=True
        )

    @discord.ui.button(label="Desativar clear automático", style=discord.ButtonStyle.danger, emoji="🔴", row=1)
    async def btn_desativar(self, interaction: discord.Interaction, button: discord.ui.Button):
        g = interaction.guild
        self.cog.db.set_enabled(g.id, False)
        for r in self.cog.db.list_channels(g.id):
            self.cog.parar(r["channel_id"])
        await interaction.response.send_message(
            "🔴 Clear automático **desativado**. As configurações foram mantidas.", ephemeral=True
        )

    # ---- linha 3
    @discord.ui.button(label="Executar clear agora", style=discord.ButtonStyle.danger, emoji="🧹", row=2)
    async def btn_executar(self, interaction: discord.Interaction, button: discord.ui.Button):
        canais = self.cog.db.list_channels(interaction.guild.id)
        if not canais:
            await interaction.response.send_message("⚠️ Nenhum canal configurado.", ephemeral=True)
            return
        lista = "\n".join(f"• <#{r['channel_id']}>" for r in canais)
        await interaction.response.send_message(
            f"⚠️ Isso vai **apagar as mensagens** destes canais agora:\n{lista}\n\nConfirmar?",
            view=ConfirmarView(self.cog),
            ephemeral=True,
        )

    @discord.ui.button(label="Ver configurações", style=discord.ButtonStyle.secondary, emoji="🔎", row=2)
    async def btn_ver(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.mostrar_config(interaction)


class SeletorCanaisView(discord.ui.View):
    """Menu de seleção de canais usado por Selecionar / Adicionar / Remover / Intervalo."""

    def __init__(self, cog: ClearAutomatico, modo: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.modo = modo
        self.select = discord.ui.ChannelSelect(
            placeholder="Escolha os canais…",
            channel_types=TIPOS_SELECT,
            min_values=0 if modo == "selecionar" else 1,
            max_values=25,
        )
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.checar(interaction)

    async def on_select(self, interaction: discord.Interaction):
        ids = [c.id for c in self.select.values]
        guild = interaction.guild

        if self.modo == "intervalo":
            validos = [c.id for c in self.cog._resolver(guild, ids)]
            if not validos:
                await interaction.response.send_message("Nenhum canal válido.", ephemeral=True)
                return
            await interaction.response.send_modal(IntervaloModal(self.cog, validos))
            return

        if self.modo == "selecionar":
            texto = self.cog.op_selecionar(guild, ids)
        elif self.modo == "adicionar":
            texto = self.cog.op_adicionar(guild, ids)
        else:
            texto = self.cog.op_remover(guild, ids)

        await interaction.response.edit_message(content=texto, view=None)


class IntervaloModal(discord.ui.Modal, title="Configurar intervalo"):
    dias = discord.ui.TextInput(label="Dias", default="0", required=False, max_length=4)
    horas = discord.ui.TextInput(label="Horas", default="0", required=False, max_length=4)
    minutos = discord.ui.TextInput(label="Minutos", default="10", required=False, max_length=4)
    segundos = discord.ui.TextInput(label="Segundos", default="0", required=False, max_length=5)

    def __init__(self, cog: ClearAutomatico, canais_ids: list[int]):
        super().__init__()
        self.cog = cog
        self.canais_ids = canais_ids

    async def on_submit(self, interaction: discord.Interaction):
        try:
            d = int(self.dias.value or 0)
            h = int(self.horas.value or 0)
            m = int(self.minutos.value or 0)
            s = int(self.segundos.value or 0)
            if min(d, h, m, s) < 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Use apenas números inteiros positivos.", ephemeral=True
            )
            return

        total = d * 86400 + h * 3600 + m * 60 + s
        if total < INTERVALO_MINIMO:
            await interaction.response.send_message(
                f"❌ O intervalo mínimo é de {INTERVALO_MINIMO} segundos.", ephemeral=True
            )
            return
        if total > INTERVALO_MAXIMO:
            await interaction.response.send_message("❌ O intervalo máximo é de 365 dias.", ephemeral=True)
            return

        guild = interaction.guild
        canais = self.cog._resolver(guild, self.canais_ids)
        for ch in canais:
            self.cog.db.set_interval(guild.id, ch.id, total)  # também reinicia o timer
            self.cog.agendar(ch.id)

        await interaction.response.send_message(
            f"⏱️ Intervalo de **{formatar_intervalo(total)}** definido para:\n"
            + "\n".join(f"• {c.mention}{self.cog._aviso(c)}" for c in canais)
            + ("" if self.cog.db.is_enabled(guild.id)
               else "\n\n⚠️ O clear automático está **desativado** — ative no painel."),
            ephemeral=True,
        )


class ConfirmarView(discord.ui.View):
    def __init__(self, cog: ClearAutomatico):
        super().__init__(timeout=60)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.checar(interaction)

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger, emoji="🧹")
    async def confirmar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="🧹 Clear iniciado em segundo plano. Canais com mensagens antigas "
                    "(+14 dias) demoram mais, pois são apagadas uma a uma.",
            view=None,
        )
        guild = interaction.guild

        async def trabalho():
            resultado = await self.cog.executar_agora(guild)
            try:
                await interaction.edit_original_response(content=resultado)
            except discord.HTTPException:
                pass  # token expirado (15 min) — o clear já foi concluído

        asyncio.create_task(trabalho())

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancelar(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelado.", view=None)


# ===================================================================== setup
async def setup(bot: commands.Bot):
    await bot.add_cog(ClearAutomatico(bot))

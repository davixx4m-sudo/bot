"""
cogs/moderacao_imagens.py

Moderação automática de imagens (discord.py 2.6+, Components V2).

- Analisa imagens enviadas pelos membros usando a API do Sightengine.
- Apaga a mensagem quando detecta conteúdo impróprio e envia um log (sem repostar a imagem).
- Painel /moderacao-imagens: SOMENTE o dono do servidor (guild.owner_id) pode abrir/alterar.
- Configuração salva por servidor em SQLite (arquivo próprio, não mexe no seu database.py).

Variáveis de ambiente (.env / Variables do Railway):
    SIGHTENGINE_API_USER=...
    SIGHTENGINE_API_SECRET=...
    # opcionais
    MODERACAO_DB_PATH=moderacao_imagens.db
    MOD_LIMITE_NUDEZ=0.60
    MOD_LIMITE_GORE=0.70
    MOD_LIMITE_CADAVER=0.50
    MOD_LIMITE_AUTOMUTILACAO=0.70
    MOD_LIMITE_MENOR=0.70
    MOD_LIMITE_SUGESTIVO_MENOR=0.40
    MOD_MAX_IMAGENS=4
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("moderacao_imagens")

# --------------------------------------------------------------------------- #
# Configuração (.env)
# --------------------------------------------------------------------------- #


def _float_env(nome: str, padrao: float) -> float:
    try:
        return float(os.getenv(nome, padrao))
    except (TypeError, ValueError):
        return padrao


def _int_env(nome: str, padrao: int) -> int:
    try:
        return int(os.getenv(nome, padrao))
    except (TypeError, ValueError):
        return padrao


API_USER = os.getenv("SIGHTENGINE_API_USER", "").strip()
API_SECRET = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
API_URL = "https://api.sightengine.com/1.0/check.json"
DB_PATH = Path(os.getenv("MODERACAO_DB_PATH", "moderacao_imagens.db"))

LIMITE_NUDEZ = _float_env("MOD_LIMITE_NUDEZ", 0.60)
LIMITE_GORE = _float_env("MOD_LIMITE_GORE", 0.70)
LIMITE_CADAVER = _float_env("MOD_LIMITE_CADAVER", 0.50)
LIMITE_AUTOMUTILACAO = _float_env("MOD_LIMITE_AUTOMUTILACAO", 0.70)
LIMITE_MENOR = _float_env("MOD_LIMITE_MENOR", 0.70)
LIMITE_SUGESTIVO_MENOR = _float_env("MOD_LIMITE_SUGESTIVO_MENOR", 0.40)
MAX_IMAGENS = _int_env("MOD_MAX_IMAGENS", 4)

EXTENSOES_IMAGEM = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MSG_SOMENTE_DONO = "🚫 Somente o dono do servidor pode gerenciar esta função."

# --------------------------------------------------------------------------- #
# Banco de dados (SQLite, uma linha por servidor)
# --------------------------------------------------------------------------- #

CAMPOS_BOOL = {"enabled", "nudity", "gore", "selfharm"}


@dataclass(frozen=True)
class GuildConfig:
    guild_id: int
    enabled: bool = False
    nudity: bool = True
    gore: bool = True
    selfharm: bool = True
    log_channel_id: Optional[int] = None


class ConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._cache: dict[int, GuildConfig] = {}

    def _run(self, fn):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def _init(conn: sqlite3.Connection):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moderacao_imagens (
                    guild_id       INTEGER PRIMARY KEY,
                    enabled        INTEGER NOT NULL DEFAULT 0,
                    nudity         INTEGER NOT NULL DEFAULT 1,
                    gore           INTEGER NOT NULL DEFAULT 1,
                    selfharm       INTEGER NOT NULL DEFAULT 1,
                    log_channel_id INTEGER
                )
                """
            )

        await asyncio.to_thread(self._run, _init)

    async def get(self, guild_id: int) -> GuildConfig:
        if guild_id in self._cache:
            return self._cache[guild_id]

        def _get(conn: sqlite3.Connection):
            return conn.execute(
                "SELECT enabled, nudity, gore, selfharm, log_channel_id "
                "FROM moderacao_imagens WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()

        row = await asyncio.to_thread(self._run, _get)
        if row is None:
            cfg = GuildConfig(guild_id=guild_id)
        else:
            cfg = GuildConfig(
                guild_id=guild_id,
                enabled=bool(row[0]),
                nudity=bool(row[1]),
                gore=bool(row[2]),
                selfharm=bool(row[3]),
                log_channel_id=row[4],
            )
        self._cache[guild_id] = cfg
        return cfg

    async def _set(self, guild_id: int, coluna: str, valor) -> None:
        # coluna vem sempre de uma lista fixa (nunca do usuário)
        if coluna not in CAMPOS_BOOL and coluna != "log_channel_id":
            raise ValueError(f"Coluna inválida: {coluna}")

        def _upd(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO moderacao_imagens (guild_id) VALUES (?) "
                "ON CONFLICT(guild_id) DO NOTHING",
                (guild_id,),
            )
            conn.execute(
                f"UPDATE moderacao_imagens SET {coluna} = ? WHERE guild_id = ?",
                (valor, guild_id),
            )

        await asyncio.to_thread(self._run, _upd)
        self._cache.pop(guild_id, None)

    async def set_bool(self, guild_id: int, campo: str, valor: bool) -> None:
        if campo not in CAMPOS_BOOL:
            raise ValueError(f"Campo inválido: {campo}")
        await self._set(guild_id, campo, int(valor))

    async def set_log_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self._set(guild_id, "log_channel_id", channel_id)


# --------------------------------------------------------------------------- #
# Painel (Components V2) – somente o dono do servidor
# --------------------------------------------------------------------------- #


def _status(ativo: bool) -> str:
    return "🟢 Ativada" if ativo else "🔴 Desativada"


class ToggleButton(discord.ui.Button):
    def __init__(self, cog: "ModeracaoImagens", campo: str, ativo: bool) -> None:
        super().__init__(
            label="Desativar" if ativo else "Ativar",
            style=discord.ButtonStyle.danger if ativo else discord.ButtonStyle.success,
        )
        self.cog = cog
        self.campo = campo

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.checar_dono(interaction):
            return
        cfg = await self.cog.store.get(interaction.guild.id)
        novo = not getattr(cfg, self.campo)
        await self.cog.store.set_bool(interaction.guild.id, self.campo, novo)
        await self.cog.atualizar_painel(interaction)


class CanalLogSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: "ModeracaoImagens") -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Selecione o canal de logs",
            min_values=1,
            max_values=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.checar_dono(interaction):
            return
        guild = interaction.guild
        canal = guild.get_channel(self.values[0].id)
        if canal is None:
            await interaction.response.send_message(
                "❌ Não consegui encontrar esse canal.", ephemeral=True
            )
            return
        perms = canal.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages):
            await interaction.response.send_message(
                f"❌ Eu não tenho permissão para ver/enviar mensagens em {canal.mention}.",
                ephemeral=True,
            )
            return
        await self.cog.store.set_log_channel(guild.id, canal.id)
        await self.cog.atualizar_painel(interaction)


class RemoverCanalButton(discord.ui.Button):
    def __init__(self, cog: "ModeracaoImagens", desabilitado: bool) -> None:
        super().__init__(
            label="Remover canal de logs",
            style=discord.ButtonStyle.secondary,
            disabled=desabilitado,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.cog.checar_dono(interaction):
            return
        await self.cog.store.set_log_channel(interaction.guild.id, None)
        await self.cog.atualizar_painel(interaction)


class PainelView(discord.ui.LayoutView):
    def __init__(self, cog: "ModeracaoImagens", cfg: GuildConfig) -> None:
        super().__init__(timeout=600)
        self.cog = cog

        container = discord.ui.Container(
            accent_colour=discord.Colour.green() if cfg.enabled else discord.Colour.dark_grey()
        )
        container.add_item(
            discord.ui.TextDisplay(
                "## 🛡️ Moderação Automática de Imagens\n"
                "Somente o **dono do servidor** pode alterar estas configurações."
            )
        )
        if not (API_USER and API_SECRET):
            container.add_item(
                discord.ui.TextDisplay(
                    "⚠️ **Credenciais da API não configuradas** "
                    "(`SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET`). "
                    "A análise não funcionará até configurá-las."
                )
            )
        container.add_item(discord.ui.Separator())

        itens = [
            ("enabled", "Moderação automática", "Liga/desliga todo o sistema.", cfg.enabled),
            ("nudity", "Nudez", "Nudez e conteúdo sexual explícito.", cfg.nudity),
            ("gore", "Gore / violência", "Gore, cadáveres e imagens perturbadoras.", cfg.gore),
            ("selfharm", "Automutilação", "Pessoas se cortando ou se ferindo.", cfg.selfharm),
        ]
        for campo, titulo, descricao, ativo in itens:
            container.add_item(
                discord.ui.Section(
                    discord.ui.TextDisplay(f"**{titulo}** — {_status(ativo)}\n{descricao}"),
                    accessory=ToggleButton(cog, campo, ativo),
                )
            )

        container.add_item(discord.ui.Separator())
        canal_txt = f"<#{cfg.log_channel_id}>" if cfg.log_channel_id else "*não definido*"
        container.add_item(
            discord.ui.TextDisplay(
                f"**Canal de logs:** {canal_txt}\n"
                "-# Conteúdo sexual envolvendo menores é sempre verificado quando a moderação está ativa."
            )
        )
        container.add_item(discord.ui.ActionRow(CanalLogSelect(cog)))
        container.add_item(
            discord.ui.ActionRow(RemoverCanalButton(cog, desabilitado=cfg.log_channel_id is None))
        )
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.checar_dono(interaction)

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item
    ) -> None:
        log.exception("Erro no painel de moderação de imagens", exc_info=error)
        msg = "❌ Ocorreu um erro ao processar sua ação."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class LogView(discord.ui.LayoutView):
    """Log de remoção. Não contém a imagem."""

    def __init__(
        self,
        autor: discord.abc.User,
        canal: discord.abc.GuildChannel,
        motivos: list[str],
    ) -> None:
        super().__init__(timeout=None)
        agora = int(time.time())
        texto = (
            "## 🚨 Imagem removida\n"
            f"**Usuário:** {autor.mention} (`{autor.id}`)\n"
            f"**Canal:** {canal.mention}\n"
            f"**Motivo:** {'; '.join(motivos)}\n"
            f"**Horário:** <t:{agora}:F> (<t:{agora}:R>)"
        )
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(texto), accent_colour=discord.Colour.red()
            )
        )


# --------------------------------------------------------------------------- #
# Cog
# --------------------------------------------------------------------------- #


class ModeracaoImagens(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = ConfigStore(DB_PATH)
        self.session: Optional[aiohttp.ClientSession] = None
        self._sem = asyncio.Semaphore(5)

    async def cog_load(self) -> None:
        await self.store.init()
        self.session = aiohttp.ClientSession()
        if not (API_USER and API_SECRET):
            log.warning(
                "SIGHTENGINE_API_USER/SIGHTENGINE_API_SECRET não definidos: "
                "a moderação de imagens não vai analisar nada."
            )

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # ----------------------------- permissões ------------------------------ #

    async def checar_dono(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is not None and interaction.user.id == guild.owner_id:
            return True
        try:
            if interaction.response.is_done():
                await interaction.followup.send(MSG_SOMENTE_DONO, ephemeral=True)
            else:
                await interaction.response.send_message(MSG_SOMENTE_DONO, ephemeral=True)
        except discord.HTTPException:
            pass
        return False

    # ------------------------------- painel -------------------------------- #

    async def atualizar_painel(self, interaction: discord.Interaction) -> None:
        cfg = await self.store.get(interaction.guild.id)
        await interaction.response.edit_message(view=PainelView(self, cfg))

    @app_commands.command(
        name="moderacao-imagens",
        description="Painel da moderação automática de imagens (somente o dono do servidor).",
    )
    @app_commands.guild_only()
    async def moderacao_imagens(self, interaction: discord.Interaction) -> None:
        if not await self.checar_dono(interaction):
            return
        try:
            cfg = await self.store.get(interaction.guild.id)
            await interaction.response.send_message(view=PainelView(self, cfg), ephemeral=True)
        except Exception:
            log.exception("Falha ao abrir o painel de moderação de imagens")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ Não foi possível abrir o painel agora.", ephemeral=True
                )

    # ------------------------------ listeners ------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        await self._processar(message)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Links de imagem só ganham embed depois do envio
        if len(after.embeds) > len(before.embeds):
            await self._processar(after)

    # ------------------------------ análise -------------------------------- #

    @staticmethod
    def _extrair_urls(message: discord.Message) -> list[str]:
        urls: list[str] = []
        for a in message.attachments:
            tipo = (a.content_type or "").lower()
            if tipo.startswith("image/") or Path(a.filename.lower()).suffix in EXTENSOES_IMAGEM:
                urls.append(a.url)
        for e in message.embeds:
            if e.image and e.image.url:
                urls.append(e.image.url)
            elif e.type == "image" and e.url:
                urls.append(e.url)
            elif e.type == "gifv" and e.thumbnail and e.thumbnail.url:
                urls.append(e.thumbnail.url)
        return list(dict.fromkeys(urls))

    async def _processar(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not (API_USER and API_SECRET) or self.session is None:
            return

        cfg = await self.store.get(message.guild.id)
        if not cfg.enabled:
            return

        urls = self._extrair_urls(message)[:MAX_IMAGENS]
        if not urls:
            return

        for url in urls:
            try:
                motivos = await self._analisar(url, cfg)
            except Exception:
                # Falha da API: não apaga nada por engano
                log.exception("Falha ao analisar imagem (servidor %s)", message.guild.id)
                continue
            if motivos:
                await self._remover_e_logar(message, cfg, motivos)
                return

    async def _analisar(self, url: str, cfg: GuildConfig) -> list[str]:
        # Nudez + atributos de rosto sempre são pedidos (necessários p/ detectar menores)
        modelos = ["nudity-2.1", "face-attributes"]
        if cfg.gore:
            modelos.append("gore-2.0")
        if cfg.selfharm:
            modelos.append("self-harm")

        params = {
            "url": url,
            "models": ",".join(modelos),
            "api_user": API_USER,
            "api_secret": API_SECRET,
        }
        async with self._sem:
            async with self.session.get(
                API_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)
            ) as resp:
                dados = await resp.json(content_type=None)

        if dados.get("status") != "success":
            raise RuntimeError(f"Resposta inesperada da API: {dados.get('error', dados)}")

        motivos: list[str] = []

        # --- Nudez e menores -------------------------------------------------
        nud = dados.get("nudity") or {}
        explicito = max(
            float(nud.get(k, 0) or 0) for k in ("sexual_activity", "sexual_display", "erotica")
        )
        sugestivo = max(
            explicito,
            float(nud.get("very_suggestive", 0) or 0),
            float(nud.get("suggestive", 0) or 0),
        )
        menor = max(
            (
                float(((f or {}).get("attributes") or {}).get("minor", 0) or 0)
                for f in dados.get("faces", [])
            ),
            default=0.0,
        )
        if menor >= LIMITE_MENOR and sugestivo >= LIMITE_SUGESTIVO_MENOR:
            motivos.append("Conteúdo sexual/sugestivo envolvendo possível menor de idade")
        if cfg.nudity and explicito >= LIMITE_NUDEZ:
            motivos.append("Nudez ou conteúdo sexual explícito")

        # --- Gore / cadáveres / perturbador ---------------------------------
        if cfg.gore:
            gore = dados.get("gore") or {}
            classes = gore.get("classes") or {}
            if float(classes.get("corpse", 0) or 0) >= LIMITE_CADAVER:
                motivos.append("Pessoa morta / cadáver")
            elif float(gore.get("prob", 0) or 0) >= LIMITE_GORE:
                motivos.append("Gore, violência gráfica ou imagem extremamente perturbadora")

        # --- Automutilação ---------------------------------------------------
        if cfg.selfharm:
            auto = dados.get("self-harm") or {}
            if float(auto.get("prob", 0) or 0) >= LIMITE_AUTOMUTILACAO:
                motivos.append("Automutilação")

        return motivos

    # ------------------------- remoção + log ------------------------------- #

    async def _remover_e_logar(
        self, message: discord.Message, cfg: GuildConfig, motivos: list[str]
    ) -> None:
        guild = message.guild
        try:
            await message.delete()
        except discord.NotFound:
            return  # já foi apagada
        except discord.Forbidden:
            log.warning(
                "Sem permissão 'Gerenciar Mensagens' em #%s (servidor %s)",
                message.channel,
                guild.id,
            )
            return
        except discord.HTTPException:
            log.exception("Erro ao apagar mensagem")
            return

        if not cfg.log_channel_id:
            return
        canal_log = guild.get_channel(cfg.log_channel_id)
        if canal_log is None:
            return
        try:
            await canal_log.send(
                view=LogView(message.author, message.channel, motivos),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            log.warning("Não foi possível enviar log no canal %s", cfg.log_channel_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ModeracaoImagens(bot))

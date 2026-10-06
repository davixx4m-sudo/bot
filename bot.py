"""
Bot de Tags por Botão + Moderação de Imagens — Components V2 (discord.py 2.6+)

Fluxo:
  /gerenciar-tags -> seleciona o canal -> configura embed e botões/cargos
  -> confirma -> o bot envia a mensagem (Container V2) e salva tudo no SQLite.

Persistência: os botões têm custom_id "tagrole:<id>" e, ao iniciar, o bot
registra novamente a view de cada mensagem salva (add_view com message_id),
então os botões antigos continuam funcionando depois de reiniciar.

Moderação de imagens: /moderacao-imagens (somente o dono do servidor) liga/desliga a análise
automática (Sightengine). Requer SIGHTENGINE_API_USER e SIGHTENGINE_API_SECRET nas variáveis.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, Optional

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # opcional: sincroniza comandos instantaneamente nesse servidor
DB_PATH = os.getenv("DB_PATH", "tags.db")

MAX_BUTTONS = 25
DEFAULT_COLOR = 0x5865F2
STYLE_LABELS = {1: "Azul", 2: "Cinza", 3: "Verde", 4: "Vermelho"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("tagbot")


class UserError(Exception):
    """Erro com mensagem amigável para mostrar ao usuário."""


# ======================================================================
# Modelos
# ======================================================================
@dataclass
class ButtonDraft:
    label: str
    emoji: Optional[str]
    role_id: int
    style: int = 2
    db_id: Optional[int] = None


@dataclass
class PanelDraft:
    guild_id: int
    channel_id: int
    title: str = "🎮 Escolha suas tags"
    description: str = "Clique nos botões abaixo para receber ou remover os cargos."
    color: int = DEFAULT_COLOR
    buttons: list[ButtonDraft] = field(default_factory=list)
    panel_id: Optional[int] = None
    message_id: Optional[int] = None


@dataclass
class Session:
    owner_id: int
    guild: discord.Guild
    draft: Optional[PanelDraft] = None


# ======================================================================
# Banco de dados (SQLite)
# ======================================================================
class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS panels (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id    INTEGER NOT NULL,
                    channel_id  INTEGER NOT NULL,
                    message_id  INTEGER,
                    title       TEXT    NOT NULL,
                    description TEXT    NOT NULL DEFAULT '',
                    color       INTEGER NOT NULL DEFAULT 5793266,
                    created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (guild_id, channel_id)
                );
                CREATE TABLE IF NOT EXISTS panel_buttons (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    panel_id  INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
                    label     TEXT    NOT NULL DEFAULT '',
                    emoji     TEXT,
                    role_id   INTEGER NOT NULL,
                    style     INTEGER NOT NULL DEFAULT 2,
                    position  INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_buttons_panel ON panel_buttons(panel_id);
                CREATE INDEX IF NOT EXISTS idx_panels_message ON panels(message_id);
                """
            )

    def _load_panel(self, row: sqlite3.Row) -> PanelDraft:
        rows = self.conn.execute(
            "SELECT * FROM panel_buttons WHERE panel_id = ? ORDER BY position, id", (row["id"],)
        ).fetchall()
        return PanelDraft(
            guild_id=row["guild_id"],
            channel_id=row["channel_id"],
            title=row["title"],
            description=row["description"],
            color=row["color"],
            panel_id=row["id"],
            message_id=row["message_id"],
            buttons=[
                ButtonDraft(
                    label=b["label"] or "",
                    emoji=b["emoji"],
                    role_id=b["role_id"],
                    style=b["style"],
                    db_id=b["id"],
                )
                for b in rows
            ],
        )

    def get_panel(self, guild_id: int, channel_id: int) -> Optional[PanelDraft]:
        row = self.conn.execute(
            "SELECT * FROM panels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        ).fetchone()
        return self._load_panel(row) if row else None

    def get_published_panels(self) -> list[PanelDraft]:
        rows = self.conn.execute("SELECT * FROM panels WHERE message_id IS NOT NULL").fetchall()
        return [self._load_panel(r) for r in rows]

    def save_panel(self, d: PanelDraft) -> None:
        """Cria ou atualiza o painel e sincroniza os botões (mantém IDs dos existentes)."""
        with self.conn:
            if d.panel_id is None:
                cur = self.conn.execute(
                    "INSERT INTO panels (guild_id, channel_id, message_id, title, description, color) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (d.guild_id, d.channel_id, d.message_id, d.title, d.description, d.color),
                )
                d.panel_id = cur.lastrowid
            else:
                self.conn.execute(
                    "UPDATE panels SET title = ?, description = ?, color = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (d.title, d.description, d.color, d.panel_id),
                )

            existing = {
                r["id"]
                for r in self.conn.execute("SELECT id FROM panel_buttons WHERE panel_id = ?", (d.panel_id,))
            }
            keep = {b.db_id for b in d.buttons if b.db_id in existing}
            for rid in existing - keep:
                self.conn.execute("DELETE FROM panel_buttons WHERE id = ?", (rid,))

            for pos, b in enumerate(d.buttons):
                if b.db_id in existing:
                    self.conn.execute(
                        "UPDATE panel_buttons SET label = ?, emoji = ?, role_id = ?, style = ?, position = ? "
                        "WHERE id = ?",
                        (b.label, b.emoji, b.role_id, b.style, pos, b.db_id),
                    )
                else:
                    cur = self.conn.execute(
                        "INSERT INTO panel_buttons (panel_id, label, emoji, role_id, style, position) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (d.panel_id, b.label, b.emoji, b.role_id, b.style, pos),
                    )
                    b.db_id = cur.lastrowid

    def set_message(self, panel_id: int, message_id: Optional[int]) -> None:
        with self.conn:
            self.conn.execute("UPDATE panels SET message_id = ? WHERE id = ?", (message_id, panel_id))

    def clear_message(self, message_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE panels SET message_id = NULL WHERE message_id = ?", (message_id,))

    def delete_panel(self, panel_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM panels WHERE id = ?", (panel_id,))

    def delete_panel_by_channel(self, guild_id: int, channel_id: int) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM panels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
            )

    def get_button(self, button_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM panel_buttons WHERE id = ?", (button_id,)).fetchone()


db = Database(DB_PATH)


# ======================================================================
# Utilidades
# ======================================================================
CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")
NO_MENTIONS = discord.AllowedMentions.none()


def normalize_emoji(raw: Optional[str]) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("<"):
        if not CUSTOM_EMOJI_RE.match(raw):
            raise UserError("Emoji customizado inválido. Use o formato `<:nome:id>`.")
        return raw
    if raw.isascii():
        raise UserError(
            "Emoji inválido. Cole o emoji em si (ex.: 🎮) ou um emoji customizado no formato "
            "`<:nome:id>` (digite `\\:emoji:` no Discord para obter o código)."
        )
    return raw


def is_admin(user: discord.abc.User) -> bool:
    return isinstance(user, discord.Member) and user.guild_permissions.administrator


def check_role_assignable(guild: discord.Guild, role: discord.Role) -> None:
    me = guild.me
    if role.is_default():
        raise UserError("Não é possível usar o cargo @everyone.")
    if role.managed:
        raise UserError(f"O cargo **{role.name}** é gerenciado por uma integração e não pode ser atribuído.")
    if role.permissions.administrator:
        raise UserError(f"Por segurança, cargos com permissão de **Administrador** ({role.name}) não são permitidos.")
    if not me.guild_permissions.manage_roles:
        raise UserError("Eu preciso da permissão **Gerenciar Cargos** no servidor.")
    if role >= me.top_role:
        raise UserError(
            f"O cargo **{role.name}** está acima (ou igual) ao meu cargo mais alto. "
            "Mova meu cargo para cima dele nas configurações do servidor."
        )


def check_channel_perms(channel: discord.abc.GuildChannel, guild: discord.Guild) -> None:
    perms = channel.permissions_for(guild.me)
    checks = (
        ("Ver Canal", perms.view_channel),
        ("Enviar Mensagens", perms.send_messages),
    )
    missing = [name for name, ok in checks if not ok]
    if missing:
        raise UserError(f"Faltam permissões em {channel.mention}: " + ", ".join(f"**{m}**" for m in missing) + ".")


def format_button_line(b: ButtonDraft) -> str:
    parts = []
    if b.emoji:
        parts.append(b.emoji)
    if b.label:
        parts.append(f"**{b.label}**")
    return f"{' '.join(parts)} → <@&{b.role_id}>"


def panel_markdown(d: PanelDraft, *, max_desc: Optional[int] = None) -> str:
    """Texto da mensagem (título, descrição e lista de cargos) em markdown."""
    parts: list[str] = []
    if d.title:
        parts.append(f"## {d.title}")
    desc = d.description
    if desc:
        if max_desc and len(desc) > max_desc:
            desc = desc[: max_desc - 1] + "…"
        parts.append(desc)
    if d.buttons:
        lines = "\n".join(format_button_line(b) for b in d.buttons)
        if len(lines) > 1500:
            lines = lines[:1499] + "…"
        parts.append("**Cargos disponíveis**\n" + lines)
    return "\n\n".join(parts) or "\u200b"


async def reply_ephemeral(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def handle_interaction_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original
    if isinstance(error, UserError):
        text = f"❌ {error}"
    elif isinstance(error, app_commands.MissingPermissions):
        text = "❌ Apenas administradores podem usar este comando."
    elif isinstance(error, app_commands.CheckFailure):
        text = "❌ Você não pode usar este comando."
    elif isinstance(error, discord.Forbidden):
        text = "❌ Não tenho permissão para fazer isso. Verifique minhas permissões e a posição do meu cargo."
    else:
        log.error("Erro inesperado em interação", exc_info=error)
        text = "❌ Ocorreu um erro inesperado. Tente novamente."
    try:
        await reply_ephemeral(interaction, text)
    except discord.HTTPException:
        pass


# ======================================================================
# Mensagem pública (Components V2) — o que os membros veem
# ======================================================================
class RoleButton(discord.ui.Button):
    """Botão persistente: o cargo vem do banco, então editar a embed não quebra botões antigos."""

    def __init__(self, b: ButtonDraft):
        super().__init__(
            style=discord.ButtonStyle(b.style),
            label=b.label or None,
            emoji=b.emoji or None,
            custom_id=f"tagrole:{b.db_id}",
        )
        self.button_id = b.db_id

    async def callback(self, interaction: discord.Interaction) -> None:
        row = db.get_button(self.button_id)
        if row is None:
            await interaction.response.send_message("❌ Este botão não está mais configurado.", ephemeral=True)
            return
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message("❌ Use este botão dentro de um servidor.", ephemeral=True)
            return
        role = guild.get_role(row["role_id"])
        if role is None:
            await interaction.response.send_message("❌ O cargo deste botão não existe mais.", ephemeral=True)
            return
        try:
            check_role_assignable(guild, role)
        except UserError:
            await interaction.response.send_message(
                "❌ Não consigo gerenciar esse cargo agora. Avise um administrador.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        if role in member.roles:
            await member.remove_roles(role, reason="Tag removida via botão")
            text = f"➖ Cargo **{role.name}** removido."
        else:
            await member.add_roles(role, reason="Tag adicionada via botão")
            text = f"✅ Cargo **{role.name}** adicionado!"
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)


class PanelLayout(discord.ui.LayoutView):
    def __init__(self, d: PanelDraft):
        super().__init__(timeout=None)
        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(panel_markdown(d)),
            discord.ui.Separator(),
        ]
        for i in range(0, len(d.buttons), 5):
            children.append(discord.ui.ActionRow(*[RoleButton(b) for b in d.buttons[i : i + 5]]))
        children.append(
            discord.ui.TextDisplay("-# Clique em um botão para receber o cargo • clique novamente para remover")
        )
        self.add_item(discord.ui.Container(*children, accent_colour=discord.Colour(d.color)))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_interaction_error(interaction, error)


async def publish_panel(guild: discord.Guild, d: PanelDraft) -> discord.Message:
    channel = guild.get_channel(d.channel_id)
    if channel is None:
        raise UserError("O canal selecionado não existe mais.")
    if not d.buttons:
        raise UserError("Adicione pelo menos um botão antes de enviar.")
    check_channel_perms(channel, guild)
    for b in d.buttons:
        role = guild.get_role(b.role_id)
        if role is None:
            raise UserError(f"O cargo do botão **{b.label or b.emoji}** não existe mais. Edite o botão.")
        check_role_assignable(guild, role)

    was_new = d.panel_id is None
    db.save_panel(d)  # precisa salvar antes: os custom_ids dos botões usam os IDs do banco

    view = PanelLayout(d)
    try:
        message: Optional[discord.Message] = None
        if d.message_id:
            try:
                message = await channel.fetch_message(d.message_id)
                await message.edit(view=view, allowed_mentions=NO_MENTIONS)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(view=view, allowed_mentions=NO_MENTIONS)
            d.message_id = message.id
            db.set_message(d.panel_id, message.id)
        return message
    except discord.HTTPException as exc:
        if was_new:
            db.delete_panel(d.panel_id)
            d.panel_id = None
            for b in d.buttons:
                b.db_id = None
        raise UserError(
            f"O Discord recusou a mensagem: {exc.text}. "
            "Verifique se os emojis são válidos e acessíveis ao bot."
        ) from exc


# ======================================================================
# Interface de gerenciamento (Components V2, efêmera, somente administradores)
# ======================================================================
Callback = Callable[[discord.Interaction], Awaitable[None]]


def make_button(
    label: str,
    on_click: Callback,
    *,
    emoji: Optional[str] = None,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    disabled: bool = False,
) -> discord.ui.Button:
    btn = discord.ui.Button(label=label, emoji=emoji, style=style, disabled=disabled)
    btn.callback = on_click
    return btn


def card(*children: discord.ui.Item, color: int = DEFAULT_COLOR) -> discord.ui.Container:
    return discord.ui.Container(*children, accent_colour=discord.Colour(color))


class BaseLayout(discord.ui.LayoutView):
    def __init__(self, session: Session, *, timeout: float = 900.0):
        super().__init__(timeout=timeout)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message("❌ Este painel pertence a outro usuário.", ephemeral=True)
            return False
        if not is_admin(interaction.user):
            await interaction.response.send_message("❌ Apenas administradores podem usar isto.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_interaction_error(interaction, error)


class SimpleLayout(discord.ui.LayoutView):
    """Mensagem estática, sem componentes interativos."""

    def __init__(self, content: str):
        super().__init__(timeout=None)
        self.add_item(card(discord.ui.TextDisplay(content), color=0x2B2D31))


# ---------- Tela 1: seleção de canal ----------
class ChannelPickLayout(BaseLayout):
    def __init__(self, session: Session, notice: Optional[str] = None):
        super().__init__(session)
        text = (
            "## 📢 Canal da Embed\n"
            "Selecione abaixo o canal onde a mensagem de tags será enviada.\n"
            "Se já existir uma configurada nesse canal, ela será carregada para edição."
        )
        if notice:
            text += f"\n\n{notice}"
        select = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Selecione o canal da embed",
            min_values=1,
            max_values=1,
        )

        async def on_pick(interaction: discord.Interaction) -> None:
            guild = self.session.guild
            channel = guild.get_channel(select.values[0].id)
            if channel is None:
                raise UserError("Não consegui acessar esse canal.")
            check_channel_perms(channel, guild)
            self.session.draft = db.get_panel(guild.id, channel.id) or PanelDraft(guild.id, channel.id)
            await interaction.response.edit_message(view=EditorLayout(self.session))

        select.callback = on_pick
        self.add_item(card(discord.ui.TextDisplay(text), discord.ui.Separator(), discord.ui.ActionRow(select)))


# ---------- Tela 2: editor ----------
class EditorLayout(BaseLayout):
    def __init__(self, session: Session, notice: Optional[str] = None):
        super().__init__(session)
        d = session.draft
        sent = d.message_id is not None
        if sent:
            link = f"https://discord.com/channels/{d.guild_id}/{d.channel_id}/{d.message_id}"
            status = f"✅ Enviada — [ver mensagem]({link})"
        elif d.panel_id is not None:
            status = "⚠️ Configurada, mas a mensagem não existe (use **Confirmar e enviar**)"
        else:
            status = "📝 Rascunho (ainda não enviada)"

        info = (
            "## ⚙️ Gerenciar Tags\n"
            f"📢 **Canal:** <#{d.channel_id}>\n"
            f"🔘 **Botões:** {len(d.buttons)}/{MAX_BUTTONS}\n"
            f"📌 **Status:** {status}"
        )
        if notice:
            info += f"\n\n{notice}"

        children: list[discord.ui.Item] = [discord.ui.TextDisplay(info), discord.ui.Separator()]
        if d.buttons:
            children.append(discord.ui.ActionRow(self._picker()))
        children.append(
            discord.ui.ActionRow(
                make_button("Editar embed", self.on_edit_embed, emoji="✏️", style=discord.ButtonStyle.primary),
                make_button("Adicionar botão", self.on_add_button, emoji="➕", style=discord.ButtonStyle.success),
            )
        )
        children.append(
            discord.ui.ActionRow(
                make_button(
                    "Confirmar e enviar", self.on_confirm, emoji="✅",
                    style=discord.ButtonStyle.success, disabled=sent,
                ),
                make_button(
                    "Atualizar Embed", self.on_update, emoji="🔄",
                    style=discord.ButtonStyle.primary, disabled=not sent,
                ),
                make_button(
                    "Remover Embed", self.on_remove, emoji="🗑️",
                    style=discord.ButtonStyle.danger, disabled=d.panel_id is None,
                ),
            )
        )
        children.append(
            discord.ui.ActionRow(
                make_button("Trocar canal", self.on_change_channel, emoji="⬅️"),
                make_button("Fechar", self.on_close, emoji="❌"),
            )
        )
        self.add_item(card(*children, color=0x2B2D31))
        # Pré-visualização do texto da mensagem final (a soma dos textos da mensagem é limitada a 4000 caracteres)
        self.add_item(
            card(
                discord.ui.TextDisplay("-# Pré-visualização\n" + panel_markdown(d, max_desc=600)),
                color=d.color,
            )
        )

    def _picker(self) -> discord.ui.Select:
        guild = self.session.guild
        options = []
        for i, b in enumerate(self.session.draft.buttons):
            role = guild.get_role(b.role_id)
            options.append(
                discord.SelectOption(
                    label=(b.label or "(somente emoji)")[:100],
                    value=str(i),
                    description=(f"Cargo: @{role.name}" if role else "Cargo inexistente")[:100],
                    emoji=b.emoji or None,
                )
            )
        select = discord.ui.Select(placeholder="Selecione um botão para editar/remover", options=options)

        async def on_pick(interaction: discord.Interaction) -> None:
            idx = int(select.values[0])
            btn = replace(self.session.draft.buttons[idx])
            await interaction.response.edit_message(view=ButtonEditLayout(self.session, btn, idx))

        select.callback = on_pick
        return select

    async def on_edit_embed(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PanelModal(self.session))

    async def on_add_button(self, interaction: discord.Interaction) -> None:
        if len(self.session.draft.buttons) >= MAX_BUTTONS:
            raise UserError(f"Limite de {MAX_BUTTONS} botões por mensagem.")
        new = ButtonDraft(label="", emoji=None, role_id=0, style=discord.ButtonStyle.secondary.value)
        await interaction.response.edit_message(view=ButtonEditLayout(self.session, new, None))

    async def _publish(self, interaction: discord.Interaction, success_text: str) -> None:
        await interaction.response.defer()
        try:
            await publish_panel(self.session.guild, self.session.draft)
            notice = success_text
        except UserError as exc:
            notice = f"❌ {exc}"
        await interaction.edit_original_response(view=EditorLayout(self.session, notice))

    async def on_confirm(self, interaction: discord.Interaction) -> None:
        await self._publish(interaction, "✅ Mensagem enviada e configuração salva!")

    async def on_update(self, interaction: discord.Interaction) -> None:
        await self._publish(interaction, "🔄 Mensagem atualizada com sucesso!")

    async def on_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=ConfirmRemoveLayout(self.session))

    async def on_change_channel(self, interaction: discord.Interaction) -> None:
        self.session.draft = None
        await interaction.response.edit_message(view=ChannelPickLayout(self.session))

    async def on_close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=SimpleLayout("Gerenciamento fechado."))
        self.stop()


# ---------- Tela 3: confirmar remoção ----------
class ConfirmRemoveLayout(BaseLayout):
    def __init__(self, session: Session):
        super().__init__(session)
        text = (
            "## 🗑️ Remover embed?\n"
            f"Isso apagará a mensagem em <#{session.draft.channel_id}> e toda a configuração de "
            "botões/cargos salva. Essa ação não pode ser desfeita."
        )
        self.add_item(
            card(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(
                    make_button("Sim, remover", self.on_yes, emoji="🗑️", style=discord.ButtonStyle.danger),
                    make_button("Cancelar", self.on_no, emoji="↩️"),
                ),
                color=0xED4245,
            )
        )

    async def on_yes(self, interaction: discord.Interaction) -> None:
        d = self.session.draft
        await interaction.response.defer()
        if d.message_id:
            channel = self.session.guild.get_channel(d.channel_id)
            if channel is not None:
                try:
                    await channel.get_partial_message(d.message_id).delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    raise UserError("Não tenho permissão para apagar a mensagem neste canal.")
        if d.panel_id is not None:
            db.delete_panel(d.panel_id)
        self.session.draft = None
        await interaction.edit_original_response(
            view=ChannelPickLayout(self.session, "🗑️ Mensagem removida e configuração apagada.")
        )

    async def on_no(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=EditorLayout(self.session))


# ---------- Tela 4: editar botão ----------
class ButtonEditLayout(BaseLayout):
    def __init__(self, session: Session, btn: ButtonDraft, index: Optional[int], notice: Optional[str] = None):
        super().__init__(session)
        self.btn = btn
        self.index = index
        role = session.guild.get_role(btn.role_id) if btn.role_id else None
        text = (
            ("## ➕ Novo botão\n" if index is None else f"## ✏️ Editando botão {index + 1}\n")
            + f"**Nome:** {btn.label or '—'}\n"
            + f"**Emoji:** {btn.emoji or '—'}\n"
            + f"**Cargo:** {role.mention if role else '⚠️ não selecionado'}\n"
            + f"**Cor:** {STYLE_LABELS.get(btn.style, 'Cinza')}"
        )
        if notice:
            text += f"\n\n{notice}"

        role_select = discord.ui.RoleSelect(
            placeholder="Selecione o cargo deste botão", min_values=1, max_values=1
        )

        async def on_role(interaction: discord.Interaction) -> None:
            guild = self.session.guild
            picked = guild.get_role(role_select.values[0].id)
            if picked is None:
                raise UserError("Não consegui acessar esse cargo.")
            check_role_assignable(guild, picked)
            for i, other in enumerate(self.session.draft.buttons):
                if i != self.index and other.role_id == picked.id:
                    raise UserError(f"Já existe um botão para o cargo **{picked.name}** nesta embed.")
            self.btn.role_id = picked.id
            await interaction.response.edit_message(view=ButtonEditLayout(self.session, self.btn, self.index))

        role_select.callback = on_role

        style_select = discord.ui.Select(
            placeholder="Cor do botão",
            options=[
                discord.SelectOption(label="Azul", value="1", emoji="🔵", default=btn.style == 1),
                discord.SelectOption(label="Cinza", value="2", emoji="⚪", default=btn.style == 2),
                discord.SelectOption(label="Verde", value="3", emoji="🟢", default=btn.style == 3),
                discord.SelectOption(label="Vermelho", value="4", emoji="🔴", default=btn.style == 4),
            ],
        )

        async def on_style(interaction: discord.Interaction) -> None:
            self.btn.style = int(style_select.values[0])
            await interaction.response.edit_message(view=ButtonEditLayout(self.session, self.btn, self.index))

        style_select.callback = on_style

        self.add_item(
            card(
                discord.ui.TextDisplay(text),
                discord.ui.Separator(),
                discord.ui.ActionRow(role_select),
                discord.ui.ActionRow(style_select),
                discord.ui.ActionRow(
                    make_button("Nome e emoji", self.on_text, emoji="✏️", style=discord.ButtonStyle.primary),
                    make_button("Salvar botão", self.on_save, emoji="💾", style=discord.ButtonStyle.success),
                ),
                discord.ui.ActionRow(
                    make_button(
                        "Remover botão", self.on_remove, emoji="➖",
                        style=discord.ButtonStyle.danger, disabled=index is None,
                    ),
                    make_button("Voltar", self.on_back, emoji="↩️"),
                ),
            )
        )

    async def on_text(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ButtonTextModal(self))

    async def on_save(self, interaction: discord.Interaction) -> None:
        if not self.btn.role_id:
            raise UserError("Selecione um cargo para o botão.")
        if not (self.btn.label or self.btn.emoji):
            raise UserError("Defina um nome ou um emoji para o botão.")
        buttons = self.session.draft.buttons
        if self.index is None:
            if len(buttons) >= MAX_BUTTONS:
                raise UserError(f"Limite de {MAX_BUTTONS} botões por mensagem.")
            buttons.append(self.btn)
        else:
            buttons[self.index] = self.btn
        await interaction.response.edit_message(
            view=EditorLayout(
                self.session,
                "💾 Botão salvo no rascunho. Use **Confirmar e enviar** / **Atualizar Embed** para aplicar no canal.",
            )
        )

    async def on_remove(self, interaction: discord.Interaction) -> None:
        del self.session.draft.buttons[self.index]
        await interaction.response.edit_message(
            view=EditorLayout(self.session, "➖ Botão removido do rascunho. Use **Atualizar Embed** para aplicar.")
        )

    async def on_back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=EditorLayout(self.session))


# ======================================================================
# Modais
# ======================================================================
class PanelModal(discord.ui.Modal, title="Configurar embed"):
    def __init__(self, session: Session):
        super().__init__()
        self.session = session
        d = session.draft
        self.title_in = discord.ui.TextInput(label="Título", max_length=256, default=d.title)
        self.desc_in = discord.ui.TextInput(
            label="Descrição",
            style=discord.TextStyle.paragraph,
            max_length=1500,
            required=False,
            default=d.description,
        )
        self.color_in = discord.ui.TextInput(
            label="Cor (hexadecimal)",
            placeholder="#5865F2",
            max_length=7,
            required=False,
            default=f"#{d.color:06X}",
        )
        for item in (self.title_in, self.desc_in, self.color_in):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.color_in.value.strip().lstrip("#")
        if raw:
            if not re.fullmatch(r"[0-9a-fA-F]{6}", raw):
                raise UserError("Cor inválida. Use hexadecimal, por exemplo `#5865F2`.")
            color = int(raw, 16)
        else:
            color = DEFAULT_COLOR
        d = self.session.draft
        d.title = self.title_in.value.strip()
        d.description = self.desc_in.value.strip()
        d.color = color
        await interaction.response.edit_message(view=EditorLayout(self.session))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_interaction_error(interaction, error)


class ButtonTextModal(discord.ui.Modal, title="Nome e emoji do botão"):
    def __init__(self, parent: ButtonEditLayout):
        super().__init__()
        self.parent = parent
        self.label_in = discord.ui.TextInput(
            label="Nome do botão", max_length=80, required=False, default=parent.btn.label or None,
            placeholder="Ex.: FREE FIRE",
        )
        self.emoji_in = discord.ui.TextInput(
            label="Emoji", max_length=64, required=False, default=parent.btn.emoji or None,
            placeholder="Ex.: 🎮 ou <:nome:123456789012345678>",
        )
        self.add_item(self.label_in)
        self.add_item(self.emoji_in)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        emoji = normalize_emoji(self.emoji_in.value)
        label = self.label_in.value.strip()
        if not label and not emoji:
            raise UserError("Informe ao menos um nome ou um emoji.")
        btn = self.parent.btn
        btn.label = label
        btn.emoji = emoji
        await interaction.response.edit_message(
            view=ButtonEditLayout(self.parent.session, btn, self.parent.index)
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await handle_interaction_error(interaction, error)


# ======================================================================
# Moderação automática de imagens (Sightengine) — painel só do dono
# ======================================================================
modlog = logging.getLogger("tagbot.imagens")

SE_USER = os.getenv("SIGHTENGINE_API_USER", "").strip()
SE_SECRET = os.getenv("SIGHTENGINE_API_SECRET", "").strip()
SE_URL = "https://api.sightengine.com/1.0/check.json"


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


LIMITE_NUDEZ = _float_env("MOD_LIMITE_NUDEZ", 0.60)
LIMITE_GORE = _float_env("MOD_LIMITE_GORE", 0.70)
LIMITE_CADAVER = _float_env("MOD_LIMITE_CADAVER", 0.50)
LIMITE_AUTOMUTILACAO = _float_env("MOD_LIMITE_AUTOMUTILACAO", 0.70)
LIMITE_MENOR = _float_env("MOD_LIMITE_MENOR", 0.70)
LIMITE_SUGESTIVO_MENOR = _float_env("MOD_LIMITE_SUGESTIVO_MENOR", 0.40)
MAX_IMAGENS = _int_env("MOD_MAX_IMAGENS", 4)

EXTENSOES_IMAGEM = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MSG_SOMENTE_DONO = "🚫 Somente o dono do servidor pode gerenciar esta função."
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
    """Configuração por servidor (uma linha por guild) no mesmo arquivo SQLite do bot."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._cache: dict[int, GuildConfig] = {}

    def _run(self, fn):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:
                return fn(conn)
        finally:
            conn.close()

    async def init(self) -> None:
        def _init(conn: sqlite3.Connection):
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_moderation (
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
                "FROM image_moderation WHERE guild_id = ?",
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
        if coluna not in CAMPOS_BOOL and coluna != "log_channel_id":
            raise ValueError(f"Coluna inválida: {coluna}")  # nunca vem do usuário

        def _upd(conn: sqlite3.Connection):
            conn.execute(
                "INSERT INTO image_moderation (guild_id) VALUES (?) ON CONFLICT(guild_id) DO NOTHING",
                (guild_id,),
            )
            conn.execute(f"UPDATE image_moderation SET {coluna} = ? WHERE guild_id = ?", (valor, guild_id))

        await asyncio.to_thread(self._run, _upd)
        self._cache.pop(guild_id, None)

    async def set_bool(self, guild_id: int, campo: str, valor: bool) -> None:
        if campo not in CAMPOS_BOOL:
            raise ValueError(f"Campo inválido: {campo}")
        await self._set(guild_id, campo, int(valor))

    async def set_log_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        await self._set(guild_id, "log_channel_id", channel_id)


# ---------- Painel (Components V2, efêmero) ----------
def _status(ativo: bool) -> str:
    return "🟢 Ativada" if ativo else "🔴 Desativada"


class ToggleButton(discord.ui.Button):
    def __init__(self, mod: "ImageModerator", campo: str, ativo: bool) -> None:
        super().__init__(
            label="Desativar" if ativo else "Ativar",
            style=discord.ButtonStyle.danger if ativo else discord.ButtonStyle.success,
        )
        self.mod = mod
        self.campo = campo

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.mod.checar_dono(interaction):
            return
        cfg = await self.mod.store.get(interaction.guild.id)
        await self.mod.store.set_bool(interaction.guild.id, self.campo, not getattr(cfg, self.campo))
        await self.mod.atualizar_painel(interaction)


class CanalLogSelect(discord.ui.ChannelSelect):
    def __init__(self, mod: "ImageModerator") -> None:
        super().__init__(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Selecione o canal de logs",
            min_values=1,
            max_values=1,
        )
        self.mod = mod

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.mod.checar_dono(interaction):
            return
        guild = interaction.guild
        canal = guild.get_channel(self.values[0].id)
        if canal is None:
            raise UserError("Não consegui acessar esse canal.")
        check_channel_perms(canal, guild)
        await self.mod.store.set_log_channel(guild.id, canal.id)
        await self.mod.atualizar_painel(interaction)


class RemoverCanalButton(discord.ui.Button):
    def __init__(self, mod: "ImageModerator", desabilitado: bool) -> None:
        super().__init__(label="Remover canal de logs", style=discord.ButtonStyle.secondary, disabled=desabilitado)
        self.mod = mod

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.mod.checar_dono(interaction):
            return
        await self.mod.store.set_log_channel(interaction.guild.id, None)
        await self.mod.atualizar_painel(interaction)


class PainelView(discord.ui.LayoutView):
    def __init__(self, mod: "ImageModerator", cfg: GuildConfig) -> None:
        super().__init__(timeout=600)
        self.mod = mod

        container = discord.ui.Container(
            accent_colour=discord.Colour.green() if cfg.enabled else discord.Colour.dark_grey()
        )
        container.add_item(
            discord.ui.TextDisplay(
                "## 🛡️ Moderação Automática de Imagens\n"
                "Somente o **dono do servidor** pode alterar estas configurações."
            )
        )
        if not (SE_USER and SE_SECRET):
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
                    accessory=ToggleButton(mod, campo, ativo),
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
        container.add_item(discord.ui.ActionRow(CanalLogSelect(mod)))
        container.add_item(discord.ui.ActionRow(RemoverCanalButton(mod, desabilitado=cfg.log_channel_id is None)))
        self.add_item(container)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.mod.checar_dono(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await handle_interaction_error(interaction, error)


class LogView(discord.ui.LayoutView):
    """Log de remoção. Não contém a imagem."""

    def __init__(self, autor: discord.abc.User, canal: discord.abc.GuildChannel, motivos: list[str]) -> None:
        super().__init__(timeout=None)
        agora = int(time.time())
        texto = (
            "## 🚨 Imagem removida\n"
            f"**Usuário:** {autor.mention} (`{autor.id}`)\n"
            f"**Canal:** {canal.mention}\n"
            f"**Motivo:** {'; '.join(motivos)}\n"
            f"**Horário:** <t:{agora}:F> (<t:{agora}:R>)"
        )
        self.add_item(discord.ui.Container(discord.ui.TextDisplay(texto), accent_colour=discord.Colour.red()))


# ---------- Lógica de análise ----------
class ImageModerator:
    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self.store = ConfigStore(DB_PATH)
        self.session: Optional[aiohttp.ClientSession] = None
        self._sem = asyncio.Semaphore(5)
        self._sem_perm: set[int] = set()

    async def start(self) -> None:
        await self.store.init()
        self.session = aiohttp.ClientSession()
        if not (SE_USER and SE_SECRET):
            modlog.warning(
                "SIGHTENGINE_API_USER/SIGHTENGINE_API_SECRET não definidos: "
                "a moderação de imagens não vai analisar nada."
            )

    async def stop(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    # -- permissões --
    async def checar_dono(self, interaction: discord.Interaction) -> bool:
        guild = interaction.guild
        if guild is not None and interaction.user.id == guild.owner_id:
            return True
        try:
            await reply_ephemeral(interaction, MSG_SOMENTE_DONO)
        except discord.HTTPException:
            pass
        return False

    async def atualizar_painel(self, interaction: discord.Interaction) -> None:
        cfg = await self.store.get(interaction.guild.id)
        await interaction.response.edit_message(view=PainelView(self, cfg))

    # -- extração --
    @staticmethod
    def extrair_urls(message: discord.Message) -> list[str]:
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

    # -- fluxo principal --
    async def processar(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not (SE_USER and SE_SECRET) or self.session is None:
            return

        cfg = await self.store.get(guild.id)
        if not cfg.enabled:
            return

        urls = self.extrair_urls(message)[:MAX_IMAGENS]
        if not urls:
            return

        # Sem "Gerenciar Mensagens" não adianta gastar análises da API
        if not message.channel.permissions_for(guild.me).manage_messages:
            if message.channel.id not in self._sem_perm:
                self._sem_perm.add(message.channel.id)
                modlog.warning("Sem 'Gerenciar Mensagens' em #%s (servidor %s)", message.channel, guild.id)
            return

        for url in urls:
            try:
                motivos = await self.analisar(url, cfg)
            except Exception:
                modlog.exception("Falha ao analisar imagem (servidor %s)", guild.id)
                continue  # falha da API: não apaga nada por engano
            if motivos:
                await self.remover_e_logar(message, cfg, motivos)
                return

    async def analisar(self, url: str, cfg: GuildConfig) -> list[str]:
        # Nudez + atributos de rosto sempre são pedidos (necessários para detectar menores)
        modelos = ["nudity-2.1", "face-attributes"]
        if cfg.gore:
            modelos.append("gore-2.0")
        if cfg.selfharm:
            modelos.append("self-harm")

        params = {"url": url, "models": ",".join(modelos), "api_user": SE_USER, "api_secret": SE_SECRET}
        async with self._sem:
            async with self.session.get(SE_URL, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                dados = await resp.json(content_type=None)

        if dados.get("status") != "success":
            raise RuntimeError(f"Resposta inesperada da API: {dados.get('error', dados)}")

        motivos: list[str] = []

        nud = dados.get("nudity") or {}
        explicito = max(float(nud.get(k, 0) or 0) for k in ("sexual_activity", "sexual_display", "erotica"))
        sugestivo = max(
            explicito,
            float(nud.get("very_suggestive", 0) or 0),
            float(nud.get("suggestive", 0) or 0),
        )
        menor = max(
            (float(((f or {}).get("attributes") or {}).get("minor", 0) or 0) for f in dados.get("faces", [])),
            default=0.0,
        )
        if menor >= LIMITE_MENOR and sugestivo >= LIMITE_SUGESTIVO_MENOR:
            motivos.append("Conteúdo sexual/sugestivo envolvendo possível menor de idade")
        if cfg.nudity and explicito >= LIMITE_NUDEZ:
            motivos.append("Nudez ou conteúdo sexual explícito")

        if cfg.gore:
            gore = dados.get("gore") or {}
            classes = gore.get("classes") or {}
            if float(classes.get("corpse", 0) or 0) >= LIMITE_CADAVER:
                motivos.append("Pessoa morta / cadáver")
            elif float(gore.get("prob", 0) or 0) >= LIMITE_GORE:
                motivos.append("Gore, violência gráfica ou imagem extremamente perturbadora")

        if cfg.selfharm:
            auto = dados.get("self-harm") or {}
            if float(auto.get("prob", 0) or 0) >= LIMITE_AUTOMUTILACAO:
                motivos.append("Automutilação")

        return motivos

    async def remover_e_logar(self, message: discord.Message, cfg: GuildConfig, motivos: list[str]) -> None:
        guild = message.guild
        try:
            await message.delete()
        except discord.NotFound:
            return  # já foi apagada
        except discord.Forbidden:
            modlog.warning("Sem permissão para apagar em #%s (servidor %s)", message.channel, guild.id)
            return
        except discord.HTTPException:
            modlog.exception("Erro ao apagar mensagem")
            return

        if not cfg.log_channel_id:
            return
        canal_log = guild.get_channel(cfg.log_channel_id)
        if canal_log is None:
            return
        try:
            await canal_log.send(
                view=LogView(message.author, message.channel, motivos), allowed_mentions=NO_MENTIONS
            )
        except discord.HTTPException:
            modlog.warning("Não foi possível enviar log no canal %s", cfg.log_channel_id)


# ======================================================================
# Bot e comando
# ======================================================================
class TagBot(discord.Client):
    def __init__(self):
        # AllowedMentions.none(): em Components V2 os cargos citados no texto NÃO devem gerar ping
        super().__init__(intents=discord.Intents.default(), allowed_mentions=NO_MENTIONS)
        self.tree = app_commands.CommandTree(self)
        self.moderator = ImageModerator(self)

    async def setup_hook(self) -> None:
        await self.moderator.start()

        # Registra novamente as views persistentes das mensagens salvas
        for panel in db.get_published_panels():
            try:
                self.add_view(PanelLayout(panel), message_id=panel.message_id)
            except Exception:
                log.exception("Falha ao registrar a view do painel %s", panel.panel_id)
        log.info("Views persistentes registradas.")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("Conectado como %s (id=%s)", self.user, self.user.id)

    async def close(self) -> None:
        await self.moderator.stop()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        await self.moderator.processar(message)

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        # Links de imagem só ganham embed depois do envio
        if len(after.embeds) > len(before.embeds):
            await self.moderator.processar(after)

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        db.clear_message(payload.message_id)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        db.delete_panel_by_channel(channel.guild.id, channel.id)


bot = TagBot()


@bot.tree.command(name="gerenciar-tags", description="Gerencia as mensagens de tags (cargos por botão)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def gerenciar_tags(interaction: discord.Interaction):
    session = Session(owner_id=interaction.user.id, guild=interaction.guild)
    await interaction.response.send_message(view=ChannelPickLayout(session), ephemeral=True)


@bot.tree.command(
    name="moderacao-imagens",
    description="Painel da moderação automática de imagens (somente o dono do servidor)",
)
@app_commands.guild_only()
async def moderacao_imagens(interaction: discord.Interaction):
    mod = bot.moderator
    if not await mod.checar_dono(interaction):
        return
    cfg = await mod.store.get(interaction.guild.id)
    await interaction.response.send_message(view=PainelView(mod, cfg), ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    await handle_interaction_error(interaction, error)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env")
    bot.run(TOKEN, log_handler=None)

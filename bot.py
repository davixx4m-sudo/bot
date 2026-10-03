from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX = os.getenv("COMMAND_PREFIX", "!").strip() or "!"
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/modbot.sqlite3").strip()
MAX_CLEAR_MESSAGES = max(1, min(int(os.getenv("MAX_CLEAR_MESSAGES", "100")), 100))
DEFAULT_LOG_CHANNEL_ID = int(os.getenv("MOD_LOG_CHANNEL_ID", "0") or 0)
DEFAULT_MOD_ROLE_ID = int(os.getenv("MOD_ROLE_ID", "0") or 0)


def parse_color(value: str) -> discord.Color:
    try:
        return discord.Color(int(value.strip().lstrip("#"), 16))
    except (TypeError, ValueError):
        return discord.Color.blurple()


EMBED_COLOR = parse_color(os.getenv("EMBED_COLOR", "5865F2"))
BES_EMBED_COLOR = discord.Color.from_rgb(128, 128, 128)
LOG_EMBED_COLOR = discord.Color.from_rgb(128, 128, 128)
COMMAND_RESPONSE_DELETE_SECONDS = 10
LOG_DIR = Path("data/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("modbot")


# ---------------------------------------------------------------------------
# Banco SQLite local
# ---------------------------------------------------------------------------
class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_warnings_guild_user
                ON warnings(guild_id, user_id);

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                log_channel_id INTEGER,
                mod_role_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS mutes (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                expires_at REAL NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS admin_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (guild_id, role_id)
            );

            CREATE TABLE IF NOT EXISTS bes_settings (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                leave_channel_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS autorole_settings (
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL,
                configured_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(bes_settings)").fetchall()}
        if "entry_notice_channel_id" not in columns:
            self.connection.execute("ALTER TABLE bes_settings ADD COLUMN entry_notice_channel_id INTEGER")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def _ensure_settings(self, guild_id: int) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES (?)", (guild_id,)
        )
        self.connection.commit()

    def get_setting(self, guild_id: int, key: str) -> Optional[int]:
        if key not in {"log_channel_id", "mod_role_id"}:
            raise ValueError("Configuração desconhecida")
        row = self.connection.execute(
            f"SELECT {key} FROM guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return int(row[key]) if row and row[key] else None

    def set_setting(self, guild_id: int, key: str, value: Optional[int]) -> None:
        if key not in {"log_channel_id", "mod_role_id"}:
            raise ValueError("Configuração desconhecida")
        self._ensure_settings(guild_id)
        self.connection.execute(
            f"UPDATE guild_settings SET {key} = ? WHERE guild_id = ?",
            (value, guild_id),
        )
        self.connection.commit()

    def add_admin_role(self, guild_id: int, role_id: int, moderator_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO admin_roles(guild_id, role_id, added_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, role_id) DO UPDATE SET added_by = excluded.added_by
            """,
            (guild_id, role_id, moderator_id),
        )
        self.connection.commit()

    def get_admin_role_ids(self, guild_id: int) -> list[int]:
        rows = self.connection.execute(
            "SELECT role_id FROM admin_roles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        ).fetchall()
        return [int(row["role_id"]) for row in rows]

    def clear_admin_roles(self, guild_id: int) -> None:
        self.connection.execute("DELETE FROM admin_roles WHERE guild_id = ?", (guild_id,))
        self.connection.commit()

    def get_bes_settings(self, guild_id: int) -> tuple[Optional[int], Optional[int], Optional[int]]:
        row = self.connection.execute(
            "SELECT welcome_channel_id, leave_channel_id, entry_notice_channel_id FROM bes_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return None, None, None
        return (
            int(row["welcome_channel_id"]) if row["welcome_channel_id"] else None,
            int(row["leave_channel_id"]) if row["leave_channel_id"] else None,
            int(row["entry_notice_channel_id"]) if row["entry_notice_channel_id"] else None,
        )

    def set_bes_settings(
        self,
        guild_id: int,
        welcome_channel_id: int,
        leave_channel_id: int,
        entry_notice_channel_id: int,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO bes_settings(guild_id, welcome_channel_id, leave_channel_id, entry_notice_channel_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                welcome_channel_id = excluded.welcome_channel_id,
                leave_channel_id = excluded.leave_channel_id,
                entry_notice_channel_id = excluded.entry_notice_channel_id
            """,
            (guild_id, welcome_channel_id, leave_channel_id, entry_notice_channel_id),
        )
        self.connection.commit()

    def get_autorole(self, guild_id: int) -> Optional[int]:
        row = self.connection.execute(
            "SELECT role_id FROM autorole_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return int(row["role_id"]) if row else None

    def set_autorole(self, guild_id: int, role_id: int, moderator_id: int) -> None:
        self.connection.execute(
            """
            INSERT INTO autorole_settings(guild_id, role_id, configured_by)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                role_id = excluded.role_id,
                configured_by = excluded.configured_by
            """,
            (guild_id, role_id, moderator_id),
        )
        self.connection.commit()

    def clear_autorole(self, guild_id: int) -> None:
        self.connection.execute("DELETE FROM autorole_settings WHERE guild_id = ?", (guild_id,))
        self.connection.commit()

    def add_warning(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str
    ) -> int:
        cursor = self.connection.execute(
            """
            INSERT INTO warnings(guild_id, user_id, moderator_id, reason)
            VALUES (?, ?, ?, ?)
            """,
            (guild_id, user_id, moderator_id, reason),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def get_warnings(self, guild_id: int, user_id: int) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                """
                SELECT * FROM warnings
                WHERE guild_id = ? AND user_id = ?
                ORDER BY id DESC
                """,
                (guild_id, user_id),
            ).fetchall()
        )

    def get_warning(self, warning_id: int, guild_id: int) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM warnings WHERE id = ? AND guild_id = ?",
            (warning_id, guild_id),
        ).fetchone()

    def delete_warning(self, warning_id: int, guild_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM warnings WHERE id = ? AND guild_id = ?",
            (warning_id, guild_id),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def save_mute(
        self,
        guild_id: int,
        user_id: int,
        expires_at: float,
        moderator_id: int,
        reason: str,
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO mutes(guild_id, user_id, expires_at, moderator_id, reason)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                moderator_id = excluded.moderator_id,
                reason = excluded.reason
            """,
            (guild_id, user_id, expires_at, moderator_id, reason),
        )
        self.connection.commit()

    def get_expired_mutes(self, now: float) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM mutes WHERE expires_at <= ?", (now,)
            ).fetchall()
        )

    def delete_mute(self, guild_id: int, user_id: int) -> None:
        self.connection.execute(
            "DELETE FROM mutes WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.connection.commit()


DB = Database(DATABASE_PATH)
message_log_cache: dict[int, tuple[discord.Guild, discord.abc.User, discord.abc.GuildChannel, str, list[str]]] = {}
deleted_log_claims: set[int] = set()
bot_deleted_message_ids: set[int] = set()
MESSAGE_CACHE_LIMIT = 5000


# ---------------------------------------------------------------------------
# Utilitários
# ---------------------------------------------------------------------------
def clean_reason(reason: Optional[str]) -> str:
    reason = (reason or "").strip()
    if reason.casefold().rstrip(".") in {"não informado", "nao informado", "sem motivo", "sem motivos"}:
        return "sem motivos específicos"
    return reason[:500] or "sem motivos específicos"


def format_duration(seconds: int) -> str:
    parts: list[str] = []
    units = ((604800, "sem."), (86400, "d."), (3600, "h"), (60, "min"), (1, "s"))
    remaining = seconds
    for unit_seconds, label in units:
        amount, remaining = divmod(remaining, unit_seconds)
        if amount:
            parts.append(f"{amount}{label}")
    return " ".join(parts) or "0s"


def parse_duration(value: str) -> int:
    match = re.fullmatch(r"(?i)(\d+)\s*(s|m|h|d|w)", value.strip())
    if not match:
        raise ValueError("Use um tempo como `30m`, `2h`, `7d` ou `1w`.")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    seconds = amount * multiplier
    if seconds <= 0:
        raise ValueError("O tempo precisa ser maior que zero.")
    return seconds


def make_embed(title: str, description: str, color: discord.Color = EMBED_COLOR) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def display_user(user: discord.abc.User) -> str:
    return f"{user.mention} (`{user}` — `{user.id}`)"


async def delete_later(message: discord.Message, delay: float) -> None:
    await asyncio.sleep(delay)
    try:
        bot_deleted_message_ids.add(message.id)
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


class CleanupContext(commands.Context):
    async def send(self, content=None, *, delete_after: Optional[float] = COMMAND_RESPONSE_DELETE_SECONDS, **kwargs):
        message = await super().send(content, delete_after=None, **kwargs)
        if delete_after is not None:
            asyncio.create_task(delete_later(message, delete_after))
        return message


async def delete_invocation(ctx: commands.Context) -> None:
    if ctx.message is None:
        return
    try:
        bot_deleted_message_ids.add(ctx.message.id)
        await ctx.message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def ensure_target_hierarchy(ctx: commands.Context, member: discord.Member) -> None:
    if member.id == ctx.author.id:
        raise commands.CommandError("Você não pode aplicar esta ação em si mesmo.")
    if member.id == ctx.guild.owner_id:
        raise commands.CommandError("O proprietário do servidor não pode ser moderado.")
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        raise commands.CommandError("O usuário possui cargo igual ou superior ao seu.")
    bot_member = ctx.guild.me
    if bot_member and member.top_role >= bot_member.top_role:
        raise commands.CommandError("Meu cargo precisa estar acima do cargo desse usuário.")


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True


class ModerationBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            strip_after_prefix=True,
            context_class=CleanupContext,
        )

    async def setup_hook(self) -> None:
        self.unmute_expired.start()

    async def close(self) -> None:
        self.unmute_expired.cancel()
        DB.close()
        await super().close()

    @tasks.loop(seconds=15)
    async def unmute_expired(self) -> None:
        for row in DB.get_expired_mutes(discord.utils.utcnow().timestamp()):
            guild = self.get_guild(int(row["guild_id"]))
            if guild is None:
                DB.delete_mute(row["guild_id"], row["user_id"])
                continue
            member = guild.get_member(int(row["user_id"]))
            try:
                if member is not None and member.is_timed_out():
                    await member.timeout(None, reason="Timeout temporário encerrado")
                DB.delete_mute(row["guild_id"], row["user_id"])
                await send_log(
                    guild,
                    "Timeout encerrado",
                    f"O timeout de {member.mention if member else row['user_id']} terminou automaticamente.",
                    discord.Color.green(),
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Não foi possível remover timeout %s: %s", row["user_id"], exc)

    @unmute_expired.before_loop
    async def before_unmute_expired(self) -> None:
        await self.wait_until_ready()


bot = ModerationBot()


def moderator_check(*required_permissions: str) -> commands.Check:
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        permissions = ctx.author.guild_permissions
        if permissions.administrator:
            return True
        configured_role_id = DB.get_setting(ctx.guild.id, "mod_role_id") or DEFAULT_MOD_ROLE_ID
        admin_role_ids = set(DB.get_admin_role_ids(ctx.guild.id))
        if configured_role_id:
            admin_role_ids.add(configured_role_id)
        if any(role.id in admin_role_ids for role in ctx.author.roles):
            return True
        if required_permissions and all(getattr(permissions, name, False) for name in required_permissions):
            return True
        raise commands.CheckFailure("Você não possui a permissão necessária para este comando.")

    return commands.check(predicate)


def write_file_log(guild: discord.Guild, title: str, description: str) -> None:
    safe_guild_id = str(guild.id)
    path = LOG_DIR / f"{safe_guild_id}.log"
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    plain_description = re.sub(r"[*_`~]", "", description)
    with path.open("a", encoding="utf-8") as file:
        file.write(f"[{timestamp}] {title} | {plain_description}\n")


async def send_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: discord.Color = LOG_EMBED_COLOR,
) -> None:
    write_file_log(guild, title, description)
    channel_id = DB.get_setting(guild.id, "log_channel_id") or DEFAULT_LOG_CHANNEL_ID
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=make_embed(title, description, LOG_EMBED_COLOR))
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Falha ao enviar log no servidor %s: %s", guild.id, exc)


async def find_audit_entry(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    seconds: int = 15,
) -> Optional[discord.AuditLogEntry]:
    try:
        async for entry in guild.audit_logs(limit=10, action=action):
            if entry.target and getattr(entry.target, "id", None) == target_id:
                if (discord.utils.utcnow() - entry.created_at).total_seconds() <= seconds:
                    return entry
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


async def find_audit_actor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    seconds: int = 15,
) -> Optional[discord.User]:
    entry = await find_audit_entry(guild, action, target_id, seconds)
    return entry.user if entry else None


async def find_message_delete_actor(
    guild: discord.Guild,
    message_author_id: int,
    channel_id: int,
    seconds: int = 15,
) -> Optional[discord.User]:
    try:
        async for entry in guild.audit_logs(limit=20, action=discord.AuditLogAction.message_delete):
            target_id = getattr(entry.target, "id", None)
            entry_channel_id = getattr(getattr(entry, "extra", None), "channel", None)
            entry_channel_id = getattr(entry_channel_id, "id", None)
            recent = (discord.utils.utcnow() - entry.created_at).total_seconds() <= seconds
            if recent and target_id == message_author_id and (entry_channel_id is None or entry_channel_id == channel_id):
                return entry.user
    except (discord.Forbidden, discord.HTTPException):
        return None
    return None


def claim_deleted_log(message_id: int) -> bool:
    if message_id in deleted_log_claims:
        return False
    deleted_log_claims.add(message_id)
    while len(deleted_log_claims) > MESSAGE_CACHE_LIMIT:
        deleted_log_claims.pop()
    return True


# ---------------------------------------------------------------------------
# Painel de boas-vindas e saída
# ---------------------------------------------------------------------------
def bes_panel_embed(view: "BesConfigView") -> discord.Embed:
    welcome = view.welcome_channel.mention if view.welcome_channel else "Ainda não selecionado"
    leave = view.leave_channel.mention if view.leave_channel else "Ainda não selecionado"
    entry_notice = view.entry_notice_channel.mention if view.entry_notice_channel else "Ainda não selecionado"
    return make_embed(
        "Configuração de boas-vindas e saída",
        "Use os três menus abaixo para configurar cada destino separadamente.\n\n"
        f"**Embed de boas-vindas:** {welcome}\n"
        f"**Embed de saída:** {leave}\n"
        f"**Aviso normal de entrada:** {entry_notice}\n\n"
        "O aviso normal será uma mensagem como `<@usuário> entrou no servidor.` e será apagado após 10 segundos.\n"
        "Escolha um canal em cada menu e, quando terminar, pressione **OK**.",
    )


class BesChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent_view: "BesConfigView", channel_kind: str, placeholder: str) -> None:
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.parent_view = parent_view
        self.channel_kind = channel_kind

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "Somente quem abriu o painel pode alterar esta configuração.",
                ephemeral=True,
            )
            return
        selected_channel = self.values[0]
        if self.channel_kind == "welcome":
            self.parent_view.welcome_channel = selected_channel
        elif self.channel_kind == "leave":
            self.parent_view.leave_channel = selected_channel
        else:
            self.parent_view.entry_notice_channel = selected_channel
        await interaction.response.edit_message(
            embed=bes_panel_embed(self.parent_view),
            view=self.parent_view,
        )


class BesConfigView(discord.ui.View):
    def __init__(self, author_id: int, guild_id: int) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.guild_id = guild_id
        self.welcome_channel: Optional[discord.TextChannel] = None
        self.leave_channel: Optional[discord.TextChannel] = None
        self.entry_notice_channel: Optional[discord.TextChannel] = None
        self.message: Optional[discord.Message] = None
        self.add_item(BesChannelSelect(self, "welcome", "Escolha o canal da embed de boas-vindas"))
        self.add_item(BesChannelSelect(self, "leave", "Escolha o canal da embed de saída"))
        self.add_item(BesChannelSelect(self, "entry_notice", "Escolha o canal do aviso normal de entrada"))

    @discord.ui.button(label="OK", style=discord.ButtonStyle.success, custom_id="bes_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Somente quem abriu o painel pode confirmar esta configuração.",
                ephemeral=True,
            )
            return
        if self.welcome_channel is None or self.leave_channel is None or self.entry_notice_channel is None:
            await interaction.response.send_message(
                "Selecione os três canais: embed de boas-vindas, embed de saída e aviso normal de entrada.",
                ephemeral=True,
            )
            return
        DB.set_bes_settings(
            self.guild_id,
            self.welcome_channel.id,
            self.leave_channel.id,
            self.entry_notice_channel.id,
        )
        self.stop()
        await interaction.response.edit_message(
            embed=make_embed(
                "Tudo pronto",
                "Tudo pronto, essa embed de configuração irá sumir em 10 segundos.",
                discord.Color.green(),
            ),
            view=None,
        )
        if interaction.message:
            asyncio.create_task(delete_later(interaction.message, 10))

    async def on_timeout(self) -> None:
        self.stop()
        if self.message:
            await delete_later(self.message, 0)


def member_info_embed(member: discord.Member, title: str, description: str, color: discord.Color) -> discord.Embed:
    display_name = member.display_name
    original_name = member.name
    embed = make_embed(
        title,
        f"{description}\n\n"
        f"**Display name:** {display_name}\n"
        f"**Nome original:** {original_name}\n"
        f"**ID do usuário:** `{member.id}`\n"
        f"**Membros atuais:** `{member.guild.member_count or len(member.guild.members)}`",
        color,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Usuário: {member}")
    return embed


async def send_bes_embed(channel_id: Optional[int], embed: discord.Embed) -> None:
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(embed=embed)
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Falha ao enviar embed de entrada/saída no canal %s: %s", channel_id, exc)


async def send_entry_notice(channel_id: Optional[int], member: discord.Member) -> None:
    if not channel_id:
        return
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(
            f"{member.mention} entrou no servidor.",
            delete_after=COMMAND_RESPONSE_DELETE_SECONDS,
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Falha ao enviar aviso de entrada no canal %s: %s", channel_id, exc)


# ---------------------------------------------------------------------------
# Painel de cargo automático
# ---------------------------------------------------------------------------
def autorole_panel_embed(view: "AutoRoleConfigView") -> discord.Embed:
    role_text = view.selected_role.mention if view.selected_role else "Ainda não selecionado"
    return make_embed(
        "Configuração de cargo automático",
        "Escolha no menu abaixo o cargo que será atribuído automaticamente a cada novo membro.\n\n"
        f"**Cargo selecionado:** {role_text}\n\n"
        "Quando terminar, pressione **OK**.",
    )


class AutoRoleSelect(discord.ui.RoleSelect):
    def __init__(self, parent_view: "AutoRoleConfigView") -> None:
        super().__init__(
            placeholder="Escolha o cargo automático",
            min_values=1,
            max_values=1,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.author_id:
            await interaction.response.send_message(
                "Somente quem abriu o painel pode alterar esta configuração.",
                ephemeral=True,
            )
            return
        self.parent_view.selected_role = self.values[0]
        await interaction.response.edit_message(
            embed=autorole_panel_embed(self.parent_view),
            view=self.parent_view,
        )


class AutoRoleConfigView(discord.ui.View):
    def __init__(self, author_id: int, guild_id: int) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.guild_id = guild_id
        self.selected_role: Optional[discord.Role] = None
        self.message: Optional[discord.Message] = None
        self.add_item(AutoRoleSelect(self))

    @discord.ui.button(label="OK", style=discord.ButtonStyle.success, custom_id="autorole_confirm")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Somente quem abriu o painel pode confirmar esta configuração.",
                ephemeral=True,
            )
            return
        if self.selected_role is None:
            await interaction.response.send_message(
                "Selecione primeiro o cargo automático.",
                ephemeral=True,
            )
            return
        role = self.selected_role
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Este painel só funciona dentro de um servidor.", ephemeral=True)
            return
        if role.is_default():
            await interaction.response.send_message("O cargo @everyone não pode ser usado como autorole.", ephemeral=True)
            return
        if role.managed:
            await interaction.response.send_message("Cargos gerenciados por integrações não podem ser usados como autorole.", ephemeral=True)
            return
        if guild.me and role >= guild.me.top_role:
            await interaction.response.send_message("Meu cargo precisa estar acima do cargo escolhido.", ephemeral=True)
            return
        DB.set_autorole(self.guild_id, role.id, self.author_id)
        self.stop()
        await interaction.response.edit_message(
            embed=make_embed(
                "Tudo pronto",
                "Tudo pronto, essa embed de configuração irá sumir em 10 segundos.",
                discord.Color.green(),
            ),
            view=None,
        )
        if interaction.message:
            asyncio.create_task(delete_later(interaction.message, 10))

    async def on_timeout(self) -> None:
        self.stop()
        if self.message:
            await delete_later(self.message, 0)


# ---------------------------------------------------------------------------
# Comando de boas-vindas e saída
# ---------------------------------------------------------------------------
@bot.command(name="autorole")
@commands.has_guild_permissions(administrator=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True, manage_messages=True, manage_roles=True)
async def autorole(ctx: commands.Context) -> None:
    view = AutoRoleConfigView(ctx.author.id, ctx.guild.id)
    message = await ctx.send(embed=autorole_panel_embed(view), view=view, delete_after=None)
    view.message = message


@bot.command(name="bes")
@commands.has_guild_permissions(administrator=True)
@commands.bot_has_permissions(send_messages=True, embed_links=True, manage_messages=True)
async def bes(ctx: commands.Context) -> None:
    view = BesConfigView(ctx.author.id, ctx.guild.id)
    message = await ctx.send(embed=bes_panel_embed(view), view=view, delete_after=None)
    view.message = message


# ---------------------------------------------------------------------------
# Comandos de moderação
# ---------------------------------------------------------------------------
@bot.command(name="ban", aliases=["banir"])
@moderator_check("ban_members")
@commands.bot_has_permissions(ban_members=True)
async def ban(ctx: commands.Context, user: discord.User, *, reason: str = "Não informado.") -> None:
    member = ctx.guild.get_member(user.id)
    if member:
        await ensure_target_hierarchy(ctx, member)
    reason = clean_reason(reason)
    await ctx.guild.ban(user, reason=f"{ctx.author}: {reason}", delete_message_seconds=86400)
    await ctx.send(embed=make_embed("Usuário banido", f"{display_user(user)}\n**Motivo:** {reason}", discord.Color.red()))
    await send_log(
        ctx.guild,
        "Banimento aplicado",
        f"**Ação:** banir membro\n**Quem baniu:** {ctx.author.mention}\n**Quem foi banido:** {display_user(user)}\n**Motivo:** {reason}",
    )


@bot.command(name="unban", aliases=["desbanir"])
@moderator_check("ban_members")
@commands.bot_has_permissions(ban_members=True)
async def unban(ctx: commands.Context, user: discord.User, *, reason: str = "Não informado.") -> None:
    reason = clean_reason(reason)
    await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=make_embed("Banimento removido", f"**Usuário:** {user}\n**Motivo:** {reason}", discord.Color.green()))
    await send_log(ctx.guild, "Banimento removido", f"**Usuário:** {user} (`{user.id}`)\n**Moderador:** {ctx.author.mention}\n**Motivo:** {reason}", discord.Color.green())


@bot.command(name="kick", aliases=["expulsar"])
@moderator_check("kick_members")
@commands.bot_has_permissions(kick_members=True)
async def kick(ctx: commands.Context, member: discord.Member, *, reason: str = "Não informado.") -> None:
    await ensure_target_hierarchy(ctx, member)
    reason = clean_reason(reason)
    await member.kick(reason=f"{ctx.author}: {reason}")
    await ctx.send(embed=make_embed("Usuário expulso", f"{display_user(member)}\n**Motivo:** {reason}", discord.Color.orange()))
    await send_log(
        ctx.guild,
        "Expulsão aplicada",
        f"**Ação:** expulsar membro\n**Quem expulsou:** {ctx.author.mention}\n**Quem foi expulso:** {display_user(member)}\n**Motivo:** {reason}",
    )


@bot.command(name="mute", aliases=["timeout", "silenciar"])
@moderator_check("moderate_members")
@commands.bot_has_permissions(moderate_members=True)
async def mute(
    ctx: commands.Context,
    member: discord.Member,
    duration: str,
    *,
    reason: str = "Não informado.",
) -> None:
    await ensure_target_hierarchy(ctx, member)
    try:
        seconds = parse_duration(duration)
    except ValueError as exc:
        raise commands.CommandError(str(exc)) from exc
    if seconds > 28 * 86400:
        raise commands.CommandError("O Discord permite timeout de no máximo 28 dias.")
    reason = clean_reason(reason)
    until = discord.utils.utcnow() + timedelta(seconds=seconds)
    await member.timeout(until, reason=f"{ctx.author}: {reason}")
    DB.save_mute(ctx.guild.id, member.id, until.timestamp(), ctx.author.id, reason)
    await ctx.send(embed=make_embed("Usuário silenciado", f"{display_user(member)}\n**Duração:** {format_duration(seconds)}\n**Motivo:** {reason}", discord.Color.orange()))
    await send_log(
        ctx.guild,
        "Mute / timeout aplicado",
        f"**Ação:** mutar membro\n**Quem mutou:** {ctx.author.mention}\n**Quem foi mutado:** {display_user(member)}\n**Tempo:** {format_duration(seconds)}\n**Motivo:** {reason}",
    )


@bot.command(name="unmute", aliases=["untimeout", "dessilenciar"])
@moderator_check("moderate_members")
@commands.bot_has_permissions(moderate_members=True)
async def unmute(ctx: commands.Context, member: discord.Member, *, reason: str = "Não informado.") -> None:
    await ensure_target_hierarchy(ctx, member)
    reason = clean_reason(reason)
    await member.timeout(None, reason=f"{ctx.author}: {reason}")
    DB.delete_mute(ctx.guild.id, member.id)
    await ctx.send(embed=make_embed("Timeout removido", f"{display_user(member)}\n**Motivo:** {reason}", discord.Color.green()))
    await send_log(ctx.guild, "Timeout removido", f"**Alvo:** {display_user(member)}\n**Moderador:** {ctx.author.mention}\n**Motivo:** {reason}", discord.Color.green())


@bot.command(name="warn", aliases=["advertir", "aviso"])
@moderator_check("manage_messages")
async def warn(ctx: commands.Context, member: discord.Member, *, reason: str = "Não informado.") -> None:
    await ensure_target_hierarchy(ctx, member)
    reason = clean_reason(reason)
    warning_id = DB.add_warning(ctx.guild.id, member.id, ctx.author.id, reason)
    total = len(DB.get_warnings(ctx.guild.id, member.id))
    await ctx.send(embed=make_embed("Aviso registrado", f"**Alvo:** {display_user(member)}\n**Aviso:** `#{warning_id}`\n**Total de avisos:** `{total}`\n**Motivo:** {reason}", discord.Color.yellow()))
    await send_log(
        ctx.guild,
        "Castigo / aviso aplicado",
        f"**Ação:** registrar castigo\n**Quem castigou:** {ctx.author.mention}\n**Quem foi castigado:** {display_user(member)}\n**Tipo:** aviso (`#{warning_id}`)\n**Total de avisos:** `{total}`\n**Motivo:** {reason}",
    )


@bot.command(name="warnings", aliases=["warns", "avisos"])
@moderator_check("manage_messages")
async def warnings(ctx: commands.Context, member: discord.Member) -> None:
    rows = DB.get_warnings(ctx.guild.id, member.id)
    if not rows:
        await ctx.send(embed=make_embed("Histórico de avisos", f"{display_user(member)} não possui avisos registrados."))
        return
    lines = []
    for row in rows[:15]:
        lines.append(f"`#{row['id']}` — <t:{int(datetime.fromisoformat(row['created_at']).timestamp())}:f> — {row['reason']}")
    await ctx.send(embed=make_embed("Histórico de avisos", f"**Usuário:** {display_user(member)}\n\n" + "\n".join(lines)))


@bot.command(name="delwarn", aliases=["removewarn", "delaviso"])
@moderator_check("manage_messages")
async def delwarn(ctx: commands.Context, warning_id: int) -> None:
    warning = DB.get_warning(warning_id, ctx.guild.id)
    if warning is None:
        raise commands.CommandError("Aviso não encontrado neste servidor.")
    if not DB.delete_warning(warning_id, ctx.guild.id):
        raise commands.CommandError("Não foi possível remover o aviso.")
    await ctx.send(embed=make_embed("Aviso removido", f"O aviso `#{warning_id}` foi removido por {ctx.author.mention}.", discord.Color.green()))
    await send_log(ctx.guild, "Aviso removido", f"**ID:** `#{warning_id}`\n**Moderador:** {ctx.author.mention}", discord.Color.green())


# ---------------------------------------------------------------------------
# Comandos de atribuição e configuração administrativa
# ---------------------------------------------------------------------------
@bot.command(name="setrole", aliases=["setroles"])
@commands.has_guild_permissions(administrator=True)
@commands.bot_has_permissions(manage_roles=True)
async def setrole(ctx: commands.Context, role: discord.Role, member: discord.Member) -> None:
    if role.is_default():
        raise commands.CommandError("O cargo @everyone não pode ser usado como cargo administrativo.")
    if role.managed:
        raise commands.CommandError("Cargos gerenciados por integrações não podem ser atribuídos manualmente.")
    if ctx.guild.me and role >= ctx.guild.me.top_role:
        raise commands.CommandError("Meu cargo precisa estar acima do cargo que será atribuído.")
    await member.add_roles(role, reason=f"Cargo administrativo atribuído por {ctx.author}")
    DB.add_admin_role(ctx.guild.id, role.id, ctx.author.id)
    await ctx.send(
        embed=make_embed(
            "Cargo administrativo atribuído",
            f"O usuário {member.mention} teve o cargo {role.mention} adicionado no seu perfil.\n\n"
            "Esse cargo agora pode acessar os comandos administrativos permitidos pelo bot.",
            discord.Color.green(),
        )
    )


@bot.command(name="infroles", aliases=["adminroles", "cargosadmin"])
@commands.has_guild_permissions(administrator=True)
async def infroles(ctx: commands.Context) -> None:
    role_ids = set(DB.get_admin_role_ids(ctx.guild.id))
    configured_role_id = DB.get_setting(ctx.guild.id, "mod_role_id") or DEFAULT_MOD_ROLE_ID
    if configured_role_id:
        role_ids.add(configured_role_id)

    embed = make_embed("Acesso aos comandos administrativos", "Este painel mostra os cargos e usuários que podem acessar a moderação do bot.")
    if role_ids:
        for role_id in sorted(role_ids):
            role = ctx.guild.get_role(role_id)
            if role is None:
                embed.add_field(name=f"Cargo removido (`{role_id}`)", value="O cargo não está mais disponível no servidor.", inline=False)
                continue
            members = role.members
            member_text = ", ".join(member.mention for member in members[:20]) if members else "Nenhum membro encontrado no cache."
            if len(members) > 20:
                member_text += f" e mais {len(members) - 20} membro(s)."
            embed.add_field(name=f"{role.name} — {len(members)} membro(s)", value=f"{role.mention}\n{member_text}", inline=False)
    else:
        embed.add_field(name="Cargos administrativos do bot", value="Nenhum cargo administrativo foi configurado com `!setrole`.", inline=False)

    administrators = [member.mention for member in ctx.guild.members if member.guild_permissions.administrator]
    admin_text = ", ".join(administrators[:20]) if administrators else "Nenhum administrador encontrado no cache."
    if len(administrators) > 20:
        admin_text += f" e mais {len(administrators) - 20} administrador(es)."
    embed.add_field(name="Administradores nativos do Discord", value=admin_text, inline=False)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Comandos de canais e mensagens
# ---------------------------------------------------------------------------
@bot.command(name="clear", aliases=["purge", "limpar"])
@moderator_check("manage_messages")
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def clear(ctx: commands.Context, amount: int) -> None:
    if amount < 1 or amount > MAX_CLEAR_MESSAGES:
        raise commands.CommandError(f"Informe uma quantidade entre 1 e {MAX_CLEAR_MESSAGES}.")
    deleted = await ctx.channel.purge(limit=amount + 1)
    removed_count = max(len(deleted) - 1, 0)
    await send_log(
        ctx.guild,
        "Comando !clear executado",
        f"**Moderador:** {ctx.author.mention}\n**Canal:** {ctx.channel.mention}\n**Quantidade removida:** `{removed_count}`",
    )
    confirmation = await ctx.send(embed=make_embed("Mensagens removidas", f"Foram removidas **{removed_count}** mensagens."))
    await asyncio.sleep(5)
    try:
        await confirmation.delete()
    except discord.HTTPException:
        pass


@bot.command(name="purgeuser", aliases=["clearuser", "limparusuario"])
@moderator_check("manage_messages")
@commands.bot_has_permissions(manage_messages=True, read_message_history=True)
async def purgeuser(ctx: commands.Context, member: discord.Member, amount: int = 100) -> None:
    if amount < 1 or amount > MAX_CLEAR_MESSAGES:
        raise commands.CommandError(f"Informe uma quantidade entre 1 e {MAX_CLEAR_MESSAGES}.")
    messages = [message async for message in ctx.channel.history(limit=amount + 1) if message.author.id == member.id]
    if ctx.message not in messages:
        messages.append(ctx.message)
    deleted = await ctx.channel.delete_messages(messages[:amount + 1])
    await send_log(
        ctx.guild,
        "Comando !purgeuser executado",
        f"**Moderador:** {ctx.author.mention}\n**Canal:** {ctx.channel.mention}\n**Usuário filtrado:** {member.mention}\n**Quantidade removida:** `{len(deleted)}`",
    )
    confirmation = await ctx.send(embed=make_embed("Mensagens removidas", f"Foram removidas **{len(deleted)}** mensagens de {member.mention}."))
    await asyncio.sleep(5)
    try:
        await confirmation.delete()
    except discord.HTTPException:
        pass


@bot.command(name="lock", aliases=["travar"])
@commands.has_guild_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def lock(ctx: commands.Context) -> None:
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False, reason=f"Canal bloqueado por {ctx.author}")
    await ctx.send(embed=make_embed("Canal bloqueado", f"{ctx.channel.mention} foi bloqueado para membros comuns.", discord.Color.orange()))
    await send_log(ctx.guild, "Canal bloqueado", f"**Canal:** {ctx.channel.mention}\n**Moderador:** {ctx.author.mention}", discord.Color.orange())


@bot.command(name="unlock", aliases=["destravar"])
@commands.has_guild_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def unlock(ctx: commands.Context) -> None:
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None, reason=f"Canal desbloqueado por {ctx.author}")
    await ctx.send(embed=make_embed("Canal desbloqueado", f"{ctx.channel.mention} voltou a permitir mensagens.", discord.Color.green()))
    await send_log(ctx.guild, "Canal desbloqueado", f"**Canal:** {ctx.channel.mention}\n**Moderador:** {ctx.author.mention}", discord.Color.green())


@bot.command(name="slowmode", aliases=["lento"])
@commands.has_guild_permissions(manage_channels=True)
@commands.bot_has_permissions(manage_channels=True)
async def slowmode(ctx: commands.Context, seconds: int = 0) -> None:
    if seconds < 0 or seconds > 21600:
        raise commands.CommandError("O slowmode deve ficar entre 0 e 21600 segundos.")
    await ctx.channel.edit(slowmode_delay=seconds, reason=f"Slowmode alterado por {ctx.author}")
    state = "desativado" if seconds == 0 else f"definido para {format_duration(seconds)}"
    await send_log(
        ctx.guild,
        "Slowmode alterado",
        f"**Canal:** {ctx.channel.mention}\n**Moderador:** {ctx.author.mention}\n**Estado:** {state}",
    )
    await ctx.send(
        embed=make_embed("Slowmode atualizado", f"O slowmode de {ctx.channel.mention} foi **{state}**."),
        delete_after=COMMAND_RESPONSE_DELETE_SECONDS,
    )


@bot.command(name="setlog", aliases=["configlog"])
@commands.has_guild_permissions(administrator=True)
async def setlog(ctx: commands.Context, channel: Optional[discord.TextChannel] = None) -> None:
    DB.set_setting(ctx.guild.id, "log_channel_id", channel.id if channel else None)
    if channel:
        await ctx.send(embed=make_embed("Canal de logs definido", f"Os logs de moderação serão enviados para {channel.mention}."))
    else:
        await ctx.send(embed=make_embed("Logs desativados", "O canal de logs específico foi removido."))


@bot.command(name="setmodrole", aliases=["configcargo"])
@commands.has_guild_permissions(administrator=True)
async def setmodrole(ctx: commands.Context, role: Optional[discord.Role] = None) -> None:
    DB.set_setting(ctx.guild.id, "mod_role_id", role.id if role else None)
    if role:
        await ctx.send(embed=make_embed("Cargo de moderação definido", f"Além das permissões nativas, {role.mention} poderá usar comandos de moderação."))
    else:
        await ctx.send(embed=make_embed("Cargo de moderação removido", "O bot voltou a depender apenas das permissões nativas do Discord."))


# ---------------------------------------------------------------------------
# Comandos utilitários
# ---------------------------------------------------------------------------
@bot.command(name="avatar", aliases=["av", "foto"])
async def avatar(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    member = member or ctx.author
    embed = make_embed(f"Avatar de {member}", "")
    embed.set_image(url=member.display_avatar.url)
    embed.set_footer(text=f"ID: {member.id}")
    await ctx.send(embed=embed, delete_after=None)


@bot.command(name="userinfo", aliases=["user", "perfil"])
async def userinfo(ctx: commands.Context, member: Optional[discord.Member] = None) -> None:
    member = member or ctx.author
    roles = [role.mention for role in reversed(member.roles[1:])]
    joined = discord.utils.format_dt(member.joined_at, "F") if member.joined_at else "Desconhecido"
    created = discord.utils.format_dt(member.created_at, "F")
    description = (
        f"**Usuário:** {display_user(member)}\n"
        f"**Conta criada:** {created}\n"
        f"**Entrou no servidor:** {joined}\n"
        f"**Cargo mais alto:** {member.top_role.mention}\n"
        f"**Cargos:** {', '.join(roles) if roles else 'Nenhum'}"
    )
    embed = make_embed(f"Informações de {member}", description)
    embed.set_thumbnail(url=member.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="serverinfo", aliases=["server", "servidor"])
async def serverinfo(ctx: commands.Context) -> None:
    guild = ctx.guild
    owner = guild.owner.mention if guild.owner else f"`{guild.owner_id}`"
    description = (
        f"**Dono:** {owner}\n"
        f"**ID:** `{guild.id}`\n"
        f"**Membros:** `{guild.member_count}`\n"
        f"**Canais:** `{len(guild.channels)}`\n"
        f"**Cargos:** `{len(guild.roles)}`\n"
        f"**Criado em:** {discord.utils.format_dt(guild.created_at, 'F')}"
    )
    embed = make_embed(f"Informações de {guild.name}", description)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await ctx.send(embed=embed)


@bot.command(name="ping")
async def ping(ctx: commands.Context) -> None:
    await ctx.send(embed=make_embed("Pong", f"Latência: **{round(bot.latency * 1000)} ms**"))


@bot.command(name="help", aliases=["ajuda", "comandos"])
async def help_command(ctx: commands.Context) -> None:
    embed = make_embed(
        "Central de comandos",
        "Todos os comandos usam o prefixo `!`. Menções podem ser usadas no lugar do ID.",
    )
    embed.add_field(name="Administração — somente Administrador", value="`!setrole @cargo @usuário` — adiciona um cargo administrativo ao usuário\n`!setroles @cargo @usuário` — alias\n`!infroles` — mostra cargos e pessoas com acesso administrativo\n`!bes` — configura boas-vindas e saída\n`!autorole` — configura o cargo automático de novos membros\n`!setlog [#canal]` — ativa logs em embed cinza e arquivo\n`!setmodrole [@cargo]`", inline=False)
    embed.add_field(name="Moderação", value="`!ban`, `!unban`, `!kick`, `!mute`, `!unmute`, `!warn`, `!warnings`, `!delwarn`", inline=False)
    embed.add_field(name="Canais e mensagens", value="`!clear`, `!purgeuser`, `!lock`, `!unlock`, `!slowmode`", inline=False)
    embed.add_field(name="Logs registrados", value="Entrada/saída de membros, entrada/saída de call, apelidos, cargos, mensagens apagadas/editadas, bans, mutes, castigos, canais trancados/destrancados, slowmode e comandos de limpeza.", inline=False)
    embed.add_field(name="Utilitários", value="`!av [@usuário]` / `!avatar [@usuário]`, `!userinfo [@usuário]`, `!serverinfo`, `!ping`", inline=False)
    embed.add_field(name="Exemplos", value="`!setrole @Admin @sla`\n`!infroles`\n`!bes`\n`!autorole`\n`!ban @Joao spam`\n`!mute @Joao 30m flood`\n`!clear 20`\n`!lock`", inline=False)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# Eventos e erros
# ---------------------------------------------------------------------------
@bot.event
async def on_ready() -> None:
    log.info("Conectado como %s (%s)", bot.user, bot.user.id if bot.user else "?")
    log.info("Prefixo ativo: %s | Servidores: %s", PREFIX, len(bot.guilds))


def clip_log_text(value: Optional[str], limit: int = 900) -> str:
    value = value or "(sem conteúdo)"
    return value if len(value) <= limit else value[: limit - 3] + "..."


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
    if not isinstance(before, discord.TextChannel) or not isinstance(after, discord.TextChannel):
        return

    before_send = before.overwrites_for(before.guild.default_role).send_messages
    after_send = after.overwrites_for(after.guild.default_role).send_messages
    if before_send != after_send:
        if after_send is False:
            title = "Canal trancado"
            state = "mensagens bloqueadas para @everyone"
        else:
            title = "Canal destrancado"
            state = "mensagens liberadas para @everyone"
        await send_log(after.guild, title, f"**Canal:** {after.mention}\n**Estado:** {state}")

    if before.slowmode_delay != after.slowmode_delay:
        state = "desativado" if after.slowmode_delay == 0 else f"ativado por {format_duration(after.slowmode_delay)}"
        await send_log(
            after.guild,
            "Slowmode alterado",
            f"**Canal:** {after.mention}\n**Estado:** {state}",
        )


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if before.channel == after.channel:
        return
    if before.channel is None and after.channel is not None:
        title = "Membro entrou em call"
        description = f"**Usuário:** {display_user(member)}\n**Canal:** {after.channel.mention}"
    elif before.channel is not None and after.channel is None:
        title = "Membro saiu da call"
        description = f"**Usuário:** {display_user(member)}\n**Canal:** {before.channel.mention}"
    else:
        title = "Membro trocou de call"
        description = f"**Usuário:** {display_user(member)}\n**Saiu de:** {before.channel.mention}\n**Entrou em:** {after.channel.mention}"
    await send_log(member.guild, title, description)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild is not None and not message.author.bot:
        message_log_cache[message.id] = (
            message.guild,
            message.author,
            message.channel,
            message.content,
            [attachment.url for attachment in message.attachments],
        )
        while len(message_log_cache) > MESSAGE_CACHE_LIMIT:
            message_log_cache.pop(next(iter(message_log_cache)))
    await bot.process_commands(message)


async def log_deleted_message(
    message_id: int,
    guild: discord.Guild,
    author: discord.abc.User,
    channel: discord.abc.GuildChannel,
    content: str,
    attachments: list[str],
) -> None:
    if not claim_deleted_log(message_id):
        return
    was_bot = message_id in bot_deleted_message_ids
    bot_deleted_message_ids.discard(message_id)
    if was_bot:
        executor_text = f"{bot.user.mention if bot.user else 'O bot'} (limpeza automática)"
    else:
        audit_actor = await find_message_delete_actor(guild, author.id, channel.id)
        if audit_actor and audit_actor.id != author.id:
            executor_text = f"{audit_actor.mention} (moderador registrado no Audit Log)"
        else:
            executor_text = f"{author.mention} (o próprio autor)"
    attachment_text = "\n**Anexos:** " + ", ".join(attachments) if attachments else ""
    await send_log(
        guild,
        "Mensagem apagada",
        f"**Quem apagou:** {executor_text}\n**Autor original:** {display_user(author)}\n**Canal:** {channel.mention}\n**Conteúdo:** {clip_log_text(content)}{attachment_text}",
    )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if message.guild is None or message.author.bot:
        return
    message_log_cache.pop(message.id, None)
    await log_deleted_message(
        message.id,
        message.guild,
        message.author,
        message.channel,
        message.content,
        [attachment.url for attachment in message.attachments],
    )


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    cached = message_log_cache.pop(payload.message_id, None)
    if cached is None:
        return
    guild, author, channel, content, attachments = cached
    await log_deleted_message(payload.message_id, guild, author, channel, content, attachments)


@bot.event
async def on_bulk_message_delete(messages: list[discord.Message]) -> None:
    if not messages:
        return
    message = messages[0]
    if message.guild is None:
        return
    authors = ", ".join(str(item.author) for item in messages[:10])
    await send_log(
        message.guild,
        "Mensagens apagadas em massa",
        f"**Canal:** {message.channel.mention}\n**Quantidade:** `{len(messages)}`\n**Autores visíveis:** {clip_log_text(authors, 500)}",
    )


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if before.guild is None or before.author.bot or before.content == after.content:
        return
    await send_log(
        before.guild,
        "Mensagem editada",
        f"**Autor:** {display_user(before.author)}\n**Canal:** {before.channel.mention}\n**Antes:** {clip_log_text(before.content)}\n**Depois:** {clip_log_text(after.content)}",
    )
    if not after.author.bot:
        message_log_cache[after.id] = (
            after.guild,
            after.author,
            after.channel,
            after.content,
            [attachment.url for attachment in after.attachments],
        )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.nick != after.nick:
        actor = await find_audit_actor(after.guild, discord.AuditLogAction.member_update, after.id)
        actor_text = actor.mention if actor else "Não identificado"
        await send_log(
            after.guild,
            "Apelido alterado",
            f"**Usuário:** {display_user(after)}\n**Apelido anterior:** `{before.nick or before.name}`\n**Novo apelido:** `{after.nick or after.name}`\n**Alterado por:** {actor_text}",
        )

    before_roles = {role.id: role for role in before.roles}
    after_roles = {role.id: role for role in after.roles}
    for role_id in after_roles.keys() - before_roles.keys():
        actor = await find_audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
        await send_log(
            after.guild,
            "Cargo adicionado ao membro",
            f"**Usuário:** {display_user(after)}\n**Cargo:** {after_roles[role_id].mention}\n**Alterado por:** {actor.mention if actor else 'Não identificado'}",
        )
    for role_id in before_roles.keys() - after_roles.keys():
        actor = await find_audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
        await send_log(
            after.guild,
            "Cargo removido do membro",
            f"**Usuário:** {display_user(after)}\n**Cargo:** {before_roles[role_id].mention}\n**Alterado por:** {actor.mention if actor else 'Não identificado'}",
        )

    before_timeout = before.timed_out_until
    after_timeout = after.timed_out_until
    if before_timeout != after_timeout:
        entry = await find_audit_entry(after.guild, discord.AuditLogAction.member_update, after.id)
        actor = entry.user if entry else None
        reason = clean_reason(entry.reason if entry else None)
        if after_timeout:
            title = "Membro mutado / castigado"
            state = f"até {discord.utils.format_dt(after_timeout, 'F')}"
        else:
            title = "Mute removido"
            state = "timeout encerrado"
        await send_log(
            after.guild,
            title,
            f"**Ação:** {'aplicar timeout' if after_timeout else 'remover timeout'}\n**Quem executou:** {actor.mention if actor else 'Não identificado'}\n**Quem foi afetado:** {display_user(after)}\n**Tempo/estado:** {state}\n**Motivo:** {reason}",
        )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    entry = await find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
    actor = entry.user if entry else None
    reason = clean_reason(entry.reason if entry else None)
    await send_log(
        guild,
        "Membro banido",
        f"**Ação:** banir membro\n**Quem baniu:** {actor.mention if actor else 'Não identificado'}\n**Quem foi banido:** {display_user(user)}\n**Motivo:** {reason}",
    )


@bot.event
async def on_member_join(member: discord.Member) -> None:
    autorole_id = DB.get_autorole(member.guild.id)
    if autorole_id:
        role = member.guild.get_role(autorole_id)
        if role and not role.is_default() and not role.managed and (not member.guild.me or role < member.guild.me.top_role):
            try:
                await member.add_roles(role, reason="Autorole configurado pelo servidor")
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning("Falha ao atribuir autorole %s ao membro %s: %s", role.id, member.id, exc)

    welcome_channel_id, _, entry_notice_channel_id = DB.get_bes_settings(member.guild.id)
    await send_entry_notice(entry_notice_channel_id, member)
    embed = member_info_embed(
        member,
        "Bem-vindo ao servidor",
        f"Seja muito bem-vindo(a), {member.mention}! Esperamos que você aproveite o servidor.",
        BES_EMBED_COLOR,
    )
    await send_bes_embed(welcome_channel_id, embed)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    kick_entry = await find_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)
    kick_actor = kick_entry.user if kick_entry else None
    kick_reason = clean_reason(kick_entry.reason if kick_entry else None)
    await send_log(
        member.guild,
        "Membro expulso" if kick_actor else "Membro saiu do servidor",
        f"**Ação:** {'expulsar membro' if kick_actor else 'saída do servidor'}\n**Quem saiu/foi expulso:** {display_user(member)}\n**Quem expulsou:** {kick_actor.mention if kick_actor else 'Não identificado / saída voluntária'}\n**Motivo:** {kick_reason}",
    )
    _, leave_channel_id, _ = DB.get_bes_settings(member.guild.id)
    embed = member_info_embed(
        member,
        "Membro saiu do servidor",
        f"{member.mention} saiu do servidor. Até mais!",
        BES_EMBED_COLOR,
    )
    await send_bes_embed(leave_channel_id, embed)


@bot.before_invoke
async def remove_command_message(ctx: commands.Context) -> None:
    await delete_invocation(ctx)


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    await delete_invocation(ctx)
    if hasattr(ctx.command, "on_error"):
        return
    error = getattr(error, "original", error)

    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.NoPrivateMessage):
        await ctx.send(embed=make_embed("Comando indisponível", "Este comando só pode ser usado dentro de um servidor."))
        return
    if isinstance(error, commands.CheckFailure):
        await ctx.send(embed=make_embed("Sem permissão", "Você não possui a permissão necessária para usar este comando.", discord.Color.red()))
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=make_embed("Sem permissão", "Você não possui a permissão necessária para usar este comando.", discord.Color.red()))
        return
    if isinstance(error, commands.BotMissingPermissions):
        permissions = ", ".join(error.missing_permissions)
        await ctx.send(embed=make_embed("Permissão do bot ausente", f"Conceda ao bot: `{permissions}`.", discord.Color.red()))
        return
    if isinstance(error, commands.MissingRequiredArgument):
        usage = f"{PREFIX}{ctx.command.qualified_name} {ctx.command.signature}".strip()
        await ctx.send(embed=make_embed("Argumento ausente", f"Uso correto: `{usage}`", discord.Color.orange()))
        return
    if isinstance(error, commands.BadArgument):
        await ctx.send(embed=make_embed("Argumento inválido", "Não consegui encontrar o usuário, cargo, canal ou número informado.", discord.Color.orange()))
        return
    if isinstance(error, commands.CommandError):
        await ctx.send(embed=make_embed("Não foi possível executar", str(error), discord.Color.orange()))
        return
    if isinstance(error, discord.Forbidden):
        await ctx.send(embed=make_embed("Ação bloqueada pelo Discord", "Verifique a hierarquia de cargos e as permissões do bot.", discord.Color.red()))
        return
    if isinstance(error, discord.HTTPException):
        await ctx.send(embed=make_embed("Erro do Discord", "A API do Discord recusou a operação. Tente novamente em instantes.", discord.Color.red()))
        return

    log.exception("Erro não tratado no comando %s", getattr(ctx.command, "qualified_name", "desconhecido"), exc_info=error)
    await ctx.send(embed=make_embed("Erro interno", "Ocorreu um erro inesperado. Consulte o console do bot.", discord.Color.red()))


if __name__ == "__main__":
    if not TOKEN or TOKEN == "cole_o_token_do_seu_bot_aqui":
        raise SystemExit("Defina DISCORD_TOKEN no arquivo .env antes de iniciar o bot.")
    bot.run(TOKEN, log_handler=None)

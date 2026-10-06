"""
puxar_bot.py — arquivo ÚNICO com !puxar e !painelpuxar em Components V2 (discord.py 2.6+).

Como usar:
  1. Coloque este arquivo na raiz do repositório, ao lado do bot.py (bot de tags).
     NÃO precisa alterar nenhum outro arquivo.
  2. No Railway, em Settings > Start Command, use:  python puxar_bot.py
     Ele inicia o bot de tags (bot.py, se existir na pasta) e o !puxar juntos,
     usando o mesmo DISCORD_TOKEN.
  3. No Developer Portal, ative "Message Content Intent" e "Server Members Intent".

Se não houver bot.py na pasta, ele roda só o !puxar.
O banco (puxar.db) é salvo na mesma pasta do DB_PATH do bot de tags (ex.: /data),
ou no caminho definido em PUXAR_DB_PATH.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import threading
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
PREFIX = os.getenv("PREFIX", "!")
MAX_ROLES = 25  # limite de opções de um menu do Discord

log = logging.getLogger("puxar")


def default_db_path() -> str:
    explicit = os.getenv("PUXAR_DB_PATH")
    if explicit:
        return explicit
    folder = os.path.dirname(os.getenv("DB_PATH", ""))
    return os.path.join(folder, "puxar.db") if folder else "puxar.db"


# ======================================================================
# Mensagens em Components V2 (Container colorido + textos)
# ======================================================================
COLOR_SUCCESS = 0x57F287
COLOR_ERROR = 0xED4245
COLOR_WARNING = 0xFEE75C
COLOR_INFO = 0x5865F2


class NoticeLayout(discord.ui.LayoutView):
    """Mensagem simples: título, descrição, linha extra opcional e miniatura opcional."""

    def __init__(
        self,
        title: str,
        description: Optional[str],
        color: int,
        *,
        extra: Optional[str] = None,
        thumbnail: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=None)
        self.plain = f"**{title}**\n{description or ''}"
        head = f"## {title}"
        if description:
            head += f"\n{description}"

        first: discord.ui.Item = discord.ui.TextDisplay(head)
        if thumbnail:
            try:
                first = discord.ui.Section(discord.ui.TextDisplay(head), accessory=discord.ui.Thumbnail(thumbnail))
            except Exception:  # miniatura é opcional: se algo falhar, segue só com o texto
                log.warning("Não foi possível criar a miniatura", exc_info=True)
                first = discord.ui.TextDisplay(head)

        children: list[discord.ui.Item] = [first]
        if extra:
            children.append(discord.ui.Separator())
            children.append(discord.ui.TextDisplay(extra))
        self.add_item(discord.ui.Container(*children, accent_colour=discord.Colour(color)))


def ok_layout(title: str, description: Optional[str] = None, **kw) -> NoticeLayout:
    return NoticeLayout(f"✅ {title}", description, COLOR_SUCCESS, **kw)


def err_layout(title: str, description: Optional[str] = None, **kw) -> NoticeLayout:
    return NoticeLayout(f"❌ {title}", description, COLOR_ERROR, **kw)


def warn_layout(title: str, description: Optional[str] = None, **kw) -> NoticeLayout:
    return NoticeLayout(f"⚠️ {title}", description, COLOR_WARNING, **kw)


def info_layout(title: str, description: Optional[str] = None, **kw) -> NoticeLayout:
    return NoticeLayout(f"ℹ️ {title}", description, COLOR_INFO, **kw)


async def send_layout(ctx: commands.Context, layout: NoticeLayout) -> None:
    """Envia a mensagem; se o bot não puder, cai para texto simples."""
    try:
        await ctx.send(view=layout)
    except discord.Forbidden:
        try:
            await ctx.send(layout.plain)
        except discord.HTTPException:
            log.warning("Sem permissão para responder no canal %s", ctx.channel.id)
    except discord.HTTPException:
        log.exception("Falha ao enviar mensagem")


# ======================================================================
# Banco de dados (SQLite, executado em threads para não bloquear o bot)
# ======================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    enabled  INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS authorized_roles (
    guild_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);
CREATE INDEX IF NOT EXISTS idx_authorized_roles_role ON authorized_roles(role_id);
"""


class PuxarDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    async def setup(self) -> None:
        def _open() -> sqlite3.Connection:
            folder = os.path.dirname(self.path)
            if folder:
                os.makedirs(folder, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.commit()
            return conn

        self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            await asyncio.to_thread(conn.close)

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Banco não inicializado.")
        return self._conn

    async def _execute(self, sql: str, params: tuple = ()) -> None:
        conn = self._require()

        def _run() -> None:
            with self._lock, conn:
                conn.execute(sql, params)

        await asyncio.to_thread(_run)

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._require()

        def _run() -> list[sqlite3.Row]:
            with self._lock:
                return conn.execute(sql, params).fetchall()

        return await asyncio.to_thread(_run)

    async def is_enabled(self, guild_id: int) -> bool:
        rows = await self._fetchall("SELECT enabled FROM guild_settings WHERE guild_id = ?", (guild_id,))
        return bool(rows[0]["enabled"]) if rows else True  # sem registro = ativado

    async def set_enabled(self, guild_id: int, enabled: bool) -> None:
        await self._execute(
            "INSERT INTO guild_settings (guild_id, enabled) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled",
            (guild_id, int(enabled)),
        )

    async def get_roles(self, guild_id: int) -> list[int]:
        rows = await self._fetchall(
            "SELECT role_id FROM authorized_roles WHERE guild_id = ? ORDER BY rowid", (guild_id,)
        )
        return [int(r["role_id"]) for r in rows]

    async def add_role(self, guild_id: int, role_id: int) -> None:
        await self._execute(
            "INSERT OR IGNORE INTO authorized_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id)
        )

    async def remove_role(self, guild_id: int, role_id: int) -> None:
        await self._execute(
            "DELETE FROM authorized_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
        )

    async def remove_role_everywhere(self, role_id: int) -> None:
        await self._execute("DELETE FROM authorized_roles WHERE role_id = ?", (role_id,))


# ======================================================================
# Painel de configuração (Components V2: Container + menus + botões)
# ======================================================================
class PainelLayout(discord.ui.LayoutView):
    def __init__(self, db: PuxarDatabase, guild: discord.Guild, author_id: int, prefix: str) -> None:
        super().__init__(timeout=600)
        self.db = db
        self.guild = guild
        self.author_id = author_id
        self.prefix = prefix
        self.enabled = True
        self.role_ids: list[int] = []
        self.notice: Optional[str] = None
        self.message: Optional[discord.Message] = None
        self._add_select: Optional[discord.ui.RoleSelect] = None
        self._remove_select: Optional[discord.ui.Select] = None

    async def refresh(self) -> None:
        """Recarrega o estado do banco (limpando cargos que não existem mais) e reconstrói a mensagem."""
        self.enabled = await self.db.is_enabled(self.guild.id)
        valid: list[int] = []
        for role_id in await self.db.get_roles(self.guild.id):
            if self.guild.get_role(role_id) is None:
                await self.db.remove_role(self.guild.id, role_id)
            else:
                valid.append(role_id)
        self.role_ids = valid
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()

        status = "🟢 **Ativado**" if self.enabled else "🔴 **Desativado**"
        if self.role_ids:
            roles_text = "\n".join(f"✅ <@&{rid}> pode usar `{self.prefix}puxar`" for rid in self.role_ids)
        else:
            roles_text = f"Nenhum cargo autorizado.\nSomente administradores podem usar `{self.prefix}puxar`."

        head = (
            f"## 🎛️ Painel do {self.prefix}puxar\n"
            f"Escolha quais cargos podem usar `{self.prefix}puxar @usuário` para mover alguém para a sua call.\n\n"
            f"**Status do sistema**\n{status}\n\n"
            f"**Cargos autorizados ({len(self.role_ids)}/{MAX_ROLES})**\n{roles_text}"
        )
        if self.notice:
            head += f"\n\n**Última ação**\n{self.notice[:1000]}"

        add_select = discord.ui.RoleSelect(
            placeholder="➕ Adicionar cargos autorizados", min_values=1, max_values=25
        )
        add_select.callback = self.on_add
        self._add_select = add_select

        children: list[discord.ui.Item] = [
            discord.ui.TextDisplay(head),
            discord.ui.Separator(),
            discord.ui.ActionRow(add_select),
        ]

        self._remove_select = None
        options = []
        for role_id in self.role_ids[:MAX_ROLES]:
            role = self.guild.get_role(role_id)
            if role is not None:
                options.append(discord.SelectOption(label=role.name[:100], value=str(role.id)))
        if options:
            remove_select = discord.ui.Select(
                placeholder="➖ Remover cargos autorizados",
                min_values=1,
                max_values=len(options),
                options=options,
            )
            remove_select.callback = self.on_remove
            self._remove_select = remove_select
            children.append(discord.ui.ActionRow(remove_select))

        toggle = discord.ui.Button(
            label="Desativar sistema" if self.enabled else "Ativar sistema",
            emoji="🔴" if self.enabled else "🟢",
            style=discord.ButtonStyle.danger if self.enabled else discord.ButtonStyle.success,
        )
        toggle.callback = self.on_toggle
        list_btn = discord.ui.Button(label="Ver cargos", emoji="📋", style=discord.ButtonStyle.primary)
        list_btn.callback = self.on_list
        close_btn = discord.ui.Button(label="Fechar", emoji="❌", style=discord.ButtonStyle.secondary)
        close_btn.callback = self.on_close
        children.append(discord.ui.ActionRow(toggle, list_btn, close_btn))

        children.append(
            discord.ui.TextDisplay(
                f"-# {self.guild.name} • Somente administradores podem editar • "
                "Administradores sempre podem usar o comando"
            )
        )
        self.add_item(
            discord.ui.Container(
                *children, accent_colour=discord.Colour(COLOR_INFO if self.enabled else COLOR_ERROR)
            )
        )

    async def _update(self, interaction: discord.Interaction) -> None:
        await self.refresh()
        await interaction.edit_original_response(view=self)

    # ---------- segurança ----------
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                view=err_layout("Este painel é de outra pessoa", f"Use `{self.prefix}painelpuxar` para abrir o seu."),
                ephemeral=True,
            )
            return False
        perms = getattr(interaction.user, "guild_permissions", None)
        if perms is None or not perms.administrator:
            await interaction.response.send_message(
                view=err_layout("Sem permissão", "Apenas administradores podem usar este painel."),
                ephemeral=True,
            )
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("Erro no painel", exc_info=error)
        layout = err_layout("Algo deu errado", "Não foi possível concluir essa ação. Tente novamente.")
        try:
            if interaction.response.is_done():
                await interaction.followup.send(view=layout, ephemeral=True)
            else:
                await interaction.response.send_message(view=layout, ephemeral=True)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        for child in self.walk_children():
            if hasattr(child, "disabled"):
                child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    # ---------- callbacks ----------
    async def on_add(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        current = set(self.role_ids)
        added: list[str] = []
        skipped: list[str] = []
        for picked in self._add_select.values:
            role = self.guild.get_role(picked.id)
            if role is None:
                continue
            if role.is_default() or role.managed:
                skipped.append(f"{role.mention} (cargo especial)")
            elif role.id in current:
                skipped.append(f"{role.mention} (já autorizado)")
            elif len(current) >= MAX_ROLES:
                skipped.append(f"{role.mention} (limite de {MAX_ROLES} cargos)")
            else:
                await self.db.add_role(self.guild.id, role.id)
                current.add(role.id)
                added.append(role.mention)
        parts = []
        if added:
            parts.append("✅ Adicionado: " + ", ".join(added))
        if skipped:
            parts.append("⚠️ Ignorado: " + ", ".join(skipped))
        self.notice = "\n".join(parts) or None
        await self._update(interaction)

    async def on_remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        removed: list[str] = []
        for value in self._remove_select.values:
            role_id = int(value)
            await self.db.remove_role(self.guild.id, role_id)
            removed.append(f"<@&{role_id}>")
        self.notice = ("➖ Removido: " + ", ".join(removed)) if removed else None
        await self._update(interaction)

    async def on_toggle(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        new_state = not self.enabled
        await self.db.set_enabled(self.guild.id, new_state)
        self.notice = "🟢 Sistema ativado." if new_state else "🔴 Sistema desativado."
        await self._update(interaction)

    async def on_list(self, interaction: discord.Interaction) -> None:
        allowed = set(self.role_ids)
        lines: list[str] = []
        for role in reversed(self.guild.roles):  # do cargo mais alto para o mais baixo
            if role.is_default() or role.managed:
                continue
            if role.id in allowed:
                lines.append(f"✅ {role.mention} pode usar `{self.prefix}puxar`")
            else:
                lines.append(f"❌ {role.mention} não pode usar `{self.prefix}puxar`")

        text = ""
        shown = 0
        for line in lines:
            if len(text) + len(line) + 1 > 3500:
                break
            text += line + "\n"
            shown += 1
        if shown < len(lines):
            text += f"… e mais {len(lines) - shown} cargos."
        if not text:
            text = "Este servidor não tem cargos além de @everyone."

        layout = info_layout("Cargos e permissão", text, extra="-# Administradores sempre podem usar o comando.")
        await interaction.response.send_message(view=layout, ephemeral=True)

    async def on_close(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            view=info_layout("Painel fechado", f"Use `{self.prefix}painelpuxar` para abrir de novo.")
        )


# ======================================================================
# Cog: !puxar e !painelpuxar
# ======================================================================
class PuxarCog(commands.Cog, name="Puxar"):
    def __init__(self, bot: "PuxarBot") -> None:
        self.bot = bot
        self.db = bot.db

    async def can_use(self, member: discord.Member) -> bool:
        """Administradores sempre podem; os demais precisam de um cargo autorizado."""
        if member.guild_permissions.administrator:
            return True
        authorized = set(await self.db.get_roles(member.guild.id))
        return any(role.id in authorized for role in member.roles)

    @commands.command(name="puxar", usage="@usuário", help="Move o usuário mencionado para a sua call.")
    @commands.cooldown(1, 3, commands.BucketType.user)
    @commands.guild_only()
    async def puxar(self, ctx: commands.Context, membro: discord.Member) -> None:
        guild = ctx.guild
        autor: discord.Member = ctx.author

        # 1. Sistema ativado?
        if not await self.db.is_enabled(guild.id):
            await send_layout(
                ctx,
                warn_layout(
                    "Sistema desativado",
                    f"O comando `{ctx.clean_prefix}puxar` está desativado neste servidor. "
                    "Peça a um administrador para ativá-lo no painel.",
                ),
            )
            return

        # 2. O executor tem cargo autorizado?
        if not await self.can_use(autor):
            await send_layout(
                ctx,
                err_layout(
                    "Sem permissão",
                    f"{autor.mention}, você não possui nenhum cargo autorizado a usar `{ctx.clean_prefix}puxar`.",
                ),
            )
            return

        # 3. O executor está em uma call?
        if autor.voice is None or autor.voice.channel is None:
            await send_layout(
                ctx,
                warn_layout(
                    "Você precisa estar em uma call",
                    "Entre em um canal de voz primeiro e use o comando novamente.",
                ),
            )
            return
        destino = autor.voice.channel

        # 4. Mencionou a si mesmo?
        if membro.id == autor.id:
            await send_layout(ctx, info_layout("Você já está na call", f"Você já está em {destino.mention}."))
            return

        # 5. O alvo está conectado a alguma call?
        if membro.voice is None or membro.voice.channel is None:
            await send_layout(
                ctx,
                warn_layout(
                    "Usuário fora de call",
                    f"{membro.mention} não está em nenhum canal de voz, então não consigo puxá-lo. "
                    "O Discord só permite mover quem já está conectado a uma call.",
                ),
            )
            return
        origem = membro.voice.channel

        # 6. O alvo já está na mesma call?
        if origem.id == destino.id:
            await send_layout(ctx, info_layout("Já está na call", f"{membro.mention} já está em {destino.mention}."))
            return

        # 7. O bot tem as permissões necessárias?
        me = guild.me
        if not origem.permissions_for(me).move_members:
            await send_layout(
                ctx,
                err_layout(
                    "Eu não tenho permissão",
                    f"Preciso da permissão **Mover Membros** em {origem.mention} para puxar {membro.mention}.",
                ),
            )
            return
        destino_perms = destino.permissions_for(me)
        if not (destino_perms.view_channel and destino_perms.connect):
            await send_layout(
                ctx,
                err_layout(
                    "Eu não tenho acesso à sua call",
                    f"Preciso das permissões **Ver Canal** e **Conectar** em {destino.mention}.",
                ),
            )
            return

        # 8. Move o usuário
        try:
            await membro.move_to(destino, reason=f"!puxar usado por {autor} ({autor.id})")
        except discord.Forbidden:
            await send_layout(
                ctx,
                err_layout(
                    "Permissão negada pelo Discord",
                    "Não consegui mover esse usuário. Verifique minhas permissões de **Mover Membros** "
                    "e as permissões dos canais de voz.",
                ),
            )
            return
        except discord.HTTPException as exc:
            log.warning("Falha ao mover %s: %s", membro.id, exc)
            await send_layout(
                ctx, err_layout("Não foi possível mover o usuário", f"O Discord recusou a ação: {exc.text}")
            )
            return

        await send_layout(
            ctx,
            ok_layout(
                "Usuário puxado",
                f"{membro.mention} foi movido para {destino.mention}.",
                extra=f"**De:** {origem.mention}  •  **Para:** {destino.mention}  •  **Solicitado por:** {autor.mention}",
                thumbnail=membro.display_avatar.url,
            ),
        )

    @commands.command(name="painelpuxar", help="Abre o painel de configuração do !puxar (administradores).")
    @commands.has_permissions(administrator=True)
    @commands.guild_only()
    async def painelpuxar(self, ctx: commands.Context) -> None:
        view = PainelLayout(self.db, ctx.guild, ctx.author.id, ctx.clean_prefix)
        await view.refresh()
        try:
            view.message = await ctx.send(view=view)
        except discord.Forbidden:
            try:
                await ctx.send("❌ Não consigo abrir o painel: preciso da permissão Enviar Mensagens neste canal.")
            except discord.HTTPException:
                log.warning("Sem permissão para responder no canal %s", ctx.channel.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self.db.remove_role_everywhere(role.id)


# ======================================================================
# Bot do !puxar
# ======================================================================
class PuxarBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()  # já inclui guilds e voice_states
        intents.message_content = True       # necessário para ler comandos com prefixo
        intents.members = True               # necessário para resolver membros e cargos
        super().__init__(
            command_prefix=PREFIX,
            intents=intents,
            help_command=None,
            # Em Components V2 menções no texto podem notificar: aqui nunca notificamos ninguém
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=False, replied_user=False),
        )
        self.db = PuxarDatabase(default_db_path())

    async def setup_hook(self) -> None:
        await self.db.setup()
        await self.add_cog(PuxarCog(self))
        log.info("!puxar carregado. Banco: %s", self.db.path)

    async def close(self) -> None:
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Puxar conectado como %s | prefixo: %s", self.user, PREFIX)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Tratamento central de erros: o bot nunca cai por causa de um comando."""
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.CommandInvokeError):
            error = error.original

        if isinstance(error, commands.MissingRequiredArgument):
            usage = f"{ctx.clean_prefix}{ctx.command.name} {ctx.command.usage or ctx.command.signature}"
            layout = warn_layout("Faltou informar alguém", f"Uso correto: `{usage}`")
        elif isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
            usage = f"{ctx.clean_prefix}{ctx.command.name} {ctx.command.usage or ctx.command.signature}"
            layout = warn_layout("Usuário não encontrado", f"Mencione alguém deste servidor. Uso correto: `{usage}`")
        elif isinstance(error, commands.NoPrivateMessage):
            layout = err_layout("Só funciona em servidores", "Use este comando dentro de um servidor.")
        elif isinstance(error, commands.MissingPermissions):
            layout = err_layout("Sem permissão", "Apenas administradores podem usar este comando.")
        elif isinstance(error, commands.CommandOnCooldown):
            layout = warn_layout("Calma aí", f"Tente novamente em {error.retry_after:.0f}s.")
        elif isinstance(error, commands.CheckFailure):
            layout = err_layout("Sem permissão", "Você não pode usar este comando.")
        elif isinstance(error, discord.Forbidden):
            layout = err_layout("Eu não tenho permissão", "Verifique as minhas permissões no servidor e nos canais.")
        else:
            log.error("Erro inesperado no comando %s", ctx.command, exc_info=error)
            layout = err_layout("Erro inesperado", "Algo deu errado. Tente novamente em instantes.")

        await send_layout(ctx, layout)


# ======================================================================
# Inicialização (roda junto do bot de tags, se bot.py existir na pasta)
# ======================================================================
def load_tags_bot() -> Optional[discord.Client]:
    """Importa o bot de tags (bot.py) sem alterá-lo. Se não existir, roda só o !puxar."""
    try:
        import bot as tags_module  # bot.py do bot de tags
    except ModuleNotFoundError as exc:
        if exc.name == "bot":
            log.info("bot.py não encontrado: iniciando somente o !puxar.")
            return None
        raise
    tags_bot = getattr(tags_module, "bot", None)
    if not isinstance(tags_bot, discord.Client):
        log.warning("bot.py não expõe um objeto `bot`; iniciando somente o !puxar.")
        return None
    log.info("Bot de tags encontrado: iniciando junto com o !puxar.")
    return tags_bot


async def main() -> None:
    discord.utils.setup_logging(level=logging.INFO)
    clients: list[discord.Client] = [PuxarBot()]
    tags_bot = load_tags_bot()
    if tags_bot is not None:
        clients.append(tags_bot)
    try:
        await asyncio.gather(*(client.start(TOKEN) for client in clients))
    finally:
        for client in clients:
            if not client.is_closed():
                await client.close()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Defina DISCORD_TOKEN no .env (ou nas variáveis do Railway).")
    try:
        asyncio.run(main())
    except discord.PrivilegedIntentsRequired:
        raise SystemExit(
            "Ative 'Message Content Intent' e 'Server Members Intent' em "
            "Discord Developer Portal > seu app > Bot > Privileged Gateway Intents."
        )
    except discord.LoginFailure:
        raise SystemExit("Token inválido. Gere um novo em Developer Portal > Bot > Reset Token.")
    except KeyboardInterrupt:
        pass

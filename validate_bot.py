import os
import tempfile

import discord
from pathlib import Path

os.environ["DATABASE_PATH"] = str(Path(tempfile.gettempdir()) / "modbot-validation.sqlite3")
os.environ["DISCORD_TOKEN"] = "validation-only"

from bot import AutoRoleConfigView, BesConfigView, CleanupContext, DB, COMMAND_RESPONSE_DELETE_SECONDS, LOG_EMBED_COLOR, bot, clean_reason, message_log_cache, parse_duration  # noqa: E402

assert parse_duration("30m") == 1800
assert parse_duration("2h") == 7200
assert parse_duration("7d") == 604800
assert parse_duration("1w") == 604800
assert clean_reason("") == "sem motivos específicos"
assert clean_reason("Não informado.") == "sem motivos específicos"
assert isinstance(message_log_cache, dict)

try:
    parse_duration("10x")
except ValueError:
    pass
else:
    raise AssertionError("Duração inválida não foi rejeitada")

warning_id = DB.add_warning(1, 2, 3, "teste")
assert warning_id > 0
assert len(DB.get_warnings(1, 2)) == 1
assert DB.delete_warning(warning_id, 1)
assert not DB.get_warnings(1, 2)

expected = {
    "ban", "unban", "kick", "mute", "unmute", "warn", "warnings", "delwarn",
    "setrole", "infroles", "bes", "autorole", "clear", "purgeuser", "lock", "unlock", "slowmode", "setlog", "setmodrole",
    "avatar", "userinfo", "serverinfo", "ping", "help",
}
actual = {command.name for command in bot.commands}
missing = expected - actual
assert not missing, f"Comandos ausentes: {missing}"
assert not any(command.name.startswith("/") for command in bot.commands)
assert bot.get_command("setroles") is bot.get_command("setrole")
assert bot.get_command("av") is bot.get_command("avatar")
assert bot.get_command("bes") is not None
assert bot.get_command("autorole") is not None
autorole_view = AutoRoleConfigView(1, 2)
assert len([item for item in autorole_view.children if isinstance(item, discord.ui.RoleSelect)]) == 1
view = BesConfigView(1, 2)
channel_selects = [item for item in view.children if isinstance(item, discord.ui.ChannelSelect)]
assert len(channel_selects) == 3
assert channel_selects[0].placeholder == "Escolha o canal da embed de boas-vindas"
assert channel_selects[1].placeholder == "Escolha o canal da embed de saída"
assert channel_selects[2].placeholder == "Escolha o canal do aviso normal de entrada"
assert COMMAND_RESPONSE_DELETE_SECONDS == 10
assert LOG_EMBED_COLOR.value == 0x808080
assert "delete_after" in CleanupContext.send.__code__.co_varnames
DB.set_bes_settings(10, 20, 30, 40)
assert DB.get_bes_settings(10) == (20, 30, 40)
DB.set_autorole(10, 40, 50)
assert DB.get_autorole(10) == 40

DB.close()
print(f"OK: {len(actual)} comandos de prefixo carregados e validações concluídas.")

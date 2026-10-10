"""
database.py
Persistência em SQLite para o sistema de clear automático.
Guarda: estado (ligado/desligado) por servidor, canais selecionados,
intervalo individual de cada canal e o horário do último clear.
"""
import sqlite3
import threading
import time


class Database:
    def __init__(self, caminho: str = "clear_automatico.db"):
        self._conn = sqlite3.connect(caminho, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._criar_tabelas()

    # ------------------------------------------------------------------ infra
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _criar_tabelas(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id INTEGER PRIMARY KEY,
                    enabled  INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS channel_config (
                    channel_id       INTEGER PRIMARY KEY,
                    guild_id         INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    last_run         REAL    NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_channel_guild
                    ON channel_config (guild_id);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------------------------------------------------------------- servidor
    def is_enabled(self, guild_id: int) -> bool:
        row = self._one("SELECT enabled FROM guild_config WHERE guild_id = ?", (guild_id,))
        return bool(row["enabled"]) if row else False

    def set_enabled(self, guild_id: int, enabled: bool) -> None:
        self._exec(
            """INSERT INTO guild_config (guild_id, enabled) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET enabled = excluded.enabled""",
            (guild_id, int(enabled)),
        )

    # ------------------------------------------------------------------ canais
    def add_channel(self, guild_id: int, channel_id: int, interval_seconds: int) -> bool:
        """Adiciona o canal. Retorna False se ele já existia (nada é alterado)."""
        cur = self._exec(
            """INSERT OR IGNORE INTO channel_config
               (channel_id, guild_id, interval_seconds, last_run) VALUES (?, ?, ?, ?)""",
            (channel_id, guild_id, interval_seconds, time.time()),
        )
        return cur.rowcount > 0

    def remove_channel(self, channel_id: int) -> bool:
        cur = self._exec("DELETE FROM channel_config WHERE channel_id = ?", (channel_id,))
        return cur.rowcount > 0

    def set_interval(self, guild_id: int, channel_id: int, interval_seconds: int) -> None:
        """Define o intervalo (cria o canal se não existir) e reinicia o temporizador."""
        self._exec(
            """INSERT INTO channel_config (channel_id, guild_id, interval_seconds, last_run)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   interval_seconds = excluded.interval_seconds,
                   last_run = excluded.last_run""",
            (channel_id, guild_id, interval_seconds, time.time()),
        )

    def set_last_run(self, channel_id: int, ts: float) -> None:
        self._exec("UPDATE channel_config SET last_run = ? WHERE channel_id = ?", (ts, channel_id))

    def reset_timers(self, guild_id: int) -> None:
        self._exec(
            "UPDATE channel_config SET last_run = ? WHERE guild_id = ?",
            (time.time(), guild_id),
        )

    def get_channel(self, channel_id: int):
        return self._one("SELECT * FROM channel_config WHERE channel_id = ?", (channel_id,))

    def list_channels(self, guild_id: int) -> list[sqlite3.Row]:
        return self._all(
            "SELECT * FROM channel_config WHERE guild_id = ? ORDER BY channel_id", (guild_id,)
        )

    def list_enabled_channels(self) -> list[sqlite3.Row]:
        """Todos os canais de servidores com o clear automático ativado."""
        return self._all(
            """SELECT c.* FROM channel_config c
               JOIN guild_config g ON g.guild_id = c.guild_id
               WHERE g.enabled = 1"""
        )

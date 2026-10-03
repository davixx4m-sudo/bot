import sqlite3

con = sqlite3.connect("dados.db")
con.row_factory = sqlite3.Row
con.executescript("""
CREATE TABLE IF NOT EXISTS config(guild_id INTEGER, key TEXT, value TEXT, PRIMARY KEY(guild_id,key));
CREATE TABLE IF NOT EXISTS msgs(guild_id INTEGER, user_id INTEGER, total INTEGER DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS voz(guild_id INTEGER, user_id INTEGER, segundos INTEGER DEFAULT 0, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS rank_cargos(guild_id INTEGER, msgs INTEGER, role_id INTEGER, PRIMARY KEY(guild_id,msgs));
CREATE TABLE IF NOT EXISTS calls(channel_id INTEGER PRIMARY KEY, guild_id INTEGER, owner_id INTEGER);
CREATE TABLE IF NOT EXISTS call_bans(channel_id INTEGER, user_id INTEGER, PRIMARY KEY(channel_id,user_id));
CREATE TABLE IF NOT EXISTS palavras(guild_id INTEGER, palavra TEXT, PRIMARY KEY(guild_id,palavra));
CREATE TABLE IF NOT EXISTS cargos_prot(guild_id INTEGER, role_id INTEGER, PRIMARY KEY(guild_id,role_id));
CREATE TABLE IF NOT EXISTS whitelist(guild_id INTEGER, user_id INTEGER, PRIMARY KEY(guild_id,user_id));
CREATE TABLE IF NOT EXISTS bl(guild_id INTEGER, user_id INTEGER, motivo TEXT, expira REAL, PRIMARY KEY(guild_id,user_id));
""")


def run(q, p=()):
    cur = con.execute(q, p)
    con.commit()
    return cur


def one(q, p=()):
    return con.execute(q, p).fetchone()


def all_(q, p=()):
    return con.execute(q, p).fetchall()


def cfg_get(guild_id, key, default=None):
    r = one("SELECT value FROM config WHERE guild_id=? AND key=?", (guild_id, key))
    return r["value"] if r else default


def cfg_set(guild_id, key, value):
    run("INSERT INTO config VALUES(?,?,?) ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value",
        (guild_id, key, str(value)))

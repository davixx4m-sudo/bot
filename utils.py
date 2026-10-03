import re
import discord
import db

_UN = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_tempo(txt):
    m = re.fullmatch(r"(\d+)([smhdw])", txt.lower())
    return int(m.group(1)) * _UN[m.group(2)] if m else None


def fmt_seg(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    partes = [f"{d}d" if d else "", f"{h}h" if h else "", f"{m}m"]
    return " ".join(p for p in partes if p)


async def enviar_log(guild, titulo, desc, cor=discord.Color.blurple()):
    cid = db.cfg_get(guild.id, "logs")
    if not cid:
        return
    ch = guild.get_channel(int(cid))
    if ch:
        try:
            await ch.send(embed=discord.Embed(title=titulo, description=desc, color=cor,
                                              timestamp=discord.utils.utcnow()))
        except discord.HTTPException:
            pass

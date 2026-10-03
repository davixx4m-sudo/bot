import os
import json
import datetime
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if (not TOKEN or "COLE_SEU" in TOKEN) and os.path.exists("token.txt"):
    TOKEN = open("token.txt", encoding="utf-8").read().strip()
PREFIX = os.getenv("PREFIX", "!")
DATA_FILE = "data.json"

intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)


# ---------- armazenamento simples em JSON ----------
def load():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"blacklist": {}, "moedas": {}, "vips": []}


def save(d):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


data = load()


@bot.event
async def on_ready():
    print(f"Online como {bot.user} | prefixo: {PREFIX}")
    await bot.change_presence(activity=discord.Game(f"{PREFIX}ajuda"))


@bot.event
async def on_member_join(member):
    # quem está na blacklist é banido ao entrar
    if str(member.id) in data["blacklist"]:
        await member.ban(reason="Blacklist")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ Você não tem permissão para isso.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Faltou argumento: `{error.param.name}`")
    elif isinstance(error, (commands.MemberNotFound, commands.UserNotFound)):
        await ctx.send("❌ Usuário não encontrado.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"⚠️ Erro: `{error}`")


# ---------- ajuda ----------
CATEGORIAS = {
    "Blacklist": ["blacklist", "removeblacklist", "listblack"],
    "Bans": ["ban", "unban", "unbanall"],
    "Castigo": ["castigo", "removecastigo"],
    "VIPs": ["addvip", "removevip", "vip"],
    "Moedas": ["carteira", "addmoedas"],
    "Canal": ["lock", "unlock", "clear"],
    "Extras": ["ajuda"],
}


@bot.command()
async def ajuda(ctx):
    emb = discord.Embed(title="Lista de comandos", color=0x5865F2)
    for cat, cmds in CATEGORIAS.items():
        emb.add_field(
            name=cat,
            value=" ".join(f"`{PREFIX}{c}`" for c in cmds),
            inline=False,
        )
    await ctx.send(embed=emb)


# ---------- bans ----------
@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, user: discord.User, *, motivo="Sem motivo"):
    await ctx.guild.ban(user, reason=motivo)
    await ctx.send(f"🔨 {user} banido. Motivo: {motivo}")


@bot.command()
@commands.has_permissions(ban_members=True)
async def unban(ctx, user: discord.User):
    await ctx.guild.unban(user)
    await ctx.send(f"✅ {user} desbanido.")


@bot.command()
@commands.has_permissions(administrator=True)
async def unbanall(ctx):
    n = 0
    async for entry in ctx.guild.bans():
        await ctx.guild.unban(entry.user)
        n += 1
    await ctx.send(f"✅ {n} usuários desbanidos.")


# ---------- blacklist ----------
@bot.command()
@commands.has_permissions(ban_members=True)
async def blacklist(ctx, user: discord.User, *, motivo="Sem motivo"):
    data["blacklist"][str(user.id)] = motivo
    save(data)
    try:
        await ctx.guild.ban(user, reason=f"Blacklist: {motivo}")
    except discord.HTTPException:
        pass
    await ctx.send(f"⛔ {user} adicionado à blacklist.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def removeblacklist(ctx, user: discord.User):
    data["blacklist"].pop(str(user.id), None)
    save(data)
    await ctx.send(f"✅ {user} removido da blacklist.")


@bot.command()
@commands.has_permissions(ban_members=True)
async def listblack(ctx):
    if not data["blacklist"]:
        return await ctx.send("Blacklist vazia.")
    linhas = [f"<@{uid}> — {m}" for uid, m in data["blacklist"].items()]
    await ctx.send("\n".join(linhas)[:1900])


# ---------- castigo (timeout) ----------
@bot.command()
@commands.has_permissions(moderate_members=True)
async def castigo(ctx, membro: discord.Member, minutos: int = 10, *, motivo="Sem motivo"):
    await membro.timeout(datetime.timedelta(minutes=minutos), reason=motivo)
    await ctx.send(f"🔇 {membro.mention} de castigo por {minutos} min.")


@bot.command()
@commands.has_permissions(moderate_members=True)
async def removecastigo(ctx, membro: discord.Member):
    await membro.timeout(None)
    await ctx.send(f"✅ Castigo removido de {membro.mention}.")


# ---------- VIP (por cargo chamado "VIP") ----------
async def get_vip_role(guild):
    role = discord.utils.get(guild.roles, name="VIP")
    return role or await guild.create_role(name="VIP")


@bot.command()
@commands.has_permissions(manage_roles=True)
async def addvip(ctx, membro: discord.Member):
    await membro.add_roles(await get_vip_role(ctx.guild))
    await ctx.send(f"💎 {membro.mention} agora é VIP.")


@bot.command()
@commands.has_permissions(manage_roles=True)
async def removevip(ctx, membro: discord.Member):
    await membro.remove_roles(await get_vip_role(ctx.guild))
    await ctx.send(f"✅ VIP removido de {membro.mention}.")


@bot.command()
async def vip(ctx):
    role = await get_vip_role(ctx.guild)
    nomes = ", ".join(m.mention for m in role.members) or "Nenhum VIP."
    await ctx.send(f"💎 VIPs: {nomes}"[:1900])


# ---------- moedas ----------
@bot.command()
async def carteira(ctx, membro: discord.Member = None):
    membro = membro or ctx.author
    saldo = data["moedas"].get(str(membro.id), 0)
    await ctx.send(f"💰 {membro.mention} tem **{saldo}** moedas.")


@bot.command()
@commands.has_permissions(administrator=True)
async def addmoedas(ctx, membro: discord.Member, qtd: int):
    k = str(membro.id)
    data["moedas"][k] = data["moedas"].get(k, 0) + qtd
    save(data)
    await ctx.send(f"💰 {membro.mention} agora tem {data['moedas'][k]} moedas.")


# ---------- canal ----------
@bot.command()
@commands.has_permissions(manage_channels=True)
async def lock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=False)
    await ctx.send("🔒 Canal trancado.")


@bot.command()
@commands.has_permissions(manage_channels=True)
async def unlock(ctx):
    await ctx.channel.set_permissions(ctx.guild.default_role, send_messages=None)
    await ctx.send("🔓 Canal destrancado.")


@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, qtd: int = 10):
    apagadas = await ctx.channel.purge(limit=qtd + 1)
    await ctx.send(f"🧹 {len(apagadas) - 1} mensagens apagadas.", delete_after=4)


if not TOKEN:
    raise SystemExit("Token ausente: coloque DISCORD_TOKEN no .env ou o token no token.txt")

bot.run(TOKEN)

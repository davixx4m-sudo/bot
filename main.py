import os, io, re, json, time, asyncio, traceback, copy
import discord
from discord import app_commands
from discord.ext import commands

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BS = discord.ButtonStyle
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ───────────── DB (JSON) ─────────────
DATA_FILE = os.path.join(os.getenv("DATA_DIR", "."), "data.json")
db = {}
try:
    with open(DATA_FILE, encoding="utf8") as f:
        db = json.load(f)
except Exception:
    db = {}


def save():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


def defaults():
    return {
        "antilink": {"link": False, "invite": False, "roles": [], "channels": [], "action": "delete"},
        "antibot": {"on": False, "log": None},
        "autoroles": {"on": False, "member": None, "bot": None, "booster": None},
        "logs": {},
        "ig": {"enabled": False, "channels": {}, "clear": True,
               "emojis": {"like": "❤️", "comment": "💬"}, "message": "", "meta": 5, "posts": {}},
        "tickets": {"channel": None, "category": None, "role": None, "log": None, "open": {}},
        "perms": {"users": [], "roles": []},
    }


def G(gid):
    gid = str(gid)
    if gid not in db:
        db[gid] = defaults()
    for k, v in defaults().items():
        db[gid].setdefault(k, v)
    return db[gid]


# ───────────── Helpers V2 ─────────────
COLOR = 0x6A1B9A


def box(*items, color=COLOR):
    return discord.ui.Container(*items, accent_colour=discord.Colour(color))


def T(s):
    return discord.ui.TextDisplay(s)


def SEP():
    return discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small)


def row(*c):
    return discord.ui.ActionRow(*c)


def B(cid, label=None, style=BS.secondary, emoji=None):
    return discord.ui.Button(custom_id=cid, label=label, style=style, emoji=emoji)


def BACK(cid="menu"):
    return B(cid, "Voltar", BS.primary)


def tog(cid, on):
    return B(cid, "Desativar", BS.danger) if on else B(cid, "Ativar", BS.success)


def mk(text, *rows, color=COLOR):
    items = [T(text)]
    if rows:
        items += [SEP(), *rows]
    v = discord.ui.LayoutView(timeout=60)
    v.add_item(box(*items, color=color))
    return v


def st(v):
    return "🟢 `(Ativado)`" if v else "🔴 `(Desativado)`"


def lst(arr, pre):
    return " ".join(f"<{pre}{x}>" for x in arr) if arr else "`Nenhum`"


def one(id_, pre):
    return f"<{pre}{id_}>" if id_ else "`Não foi definido.`"


def ch_sel(cid, ph, types=None, multi=False):
    return discord.ui.ChannelSelect(
        custom_id=cid, placeholder=ph, channel_types=types or [discord.ChannelType.text],
        min_values=0 if multi else 1, max_values=10 if multi else 1)


def role_sel(cid, ph, multi=False):
    return discord.ui.RoleSelect(
        custom_id=cid, placeholder=ph, min_values=0 if multi else 1, max_values=10 if multi else 1)


class Form(discord.ui.Modal):
    def __init__(self, title, fields, cb):
        super().__init__(title=title)
        self.cb = cb
        self.inp = {}
        for f in fields:
            ti = discord.ui.TextInput(
                label=f["label"],
                default=str(f["val"]) if f.get("val") not in (None, "") else None,
                style=discord.TextStyle.paragraph if f.get("long") else discord.TextStyle.short,
                required=f.get("req", True), max_length=f.get("max"))
            self.inp[f["id"]] = ti
            self.add_item(ti)

    async def on_submit(self, i):
        await self.cb(i, {k: v.value for k, v in self.inp.items()})

    async def on_error(self, i, error):
        traceback.print_exception(type(error), error, error.__traceback__)


async def show(i, view):
    if i.type == discord.InteractionType.application_command:
        await i.response.send_message(view=view, ephemeral=True)
    else:
        await i.response.edit_message(view=view)


async def reply(i, text):
    if i.response.is_done():
        await i.followup.send(view=mk(text), ephemeral=True)
    else:
        await i.response.send_message(view=mk(text), ephemeral=True)


NOMENT = discord.AllowedMentions.none()


def is_admin(m):
    return m.guild_permissions.administrator or m.id == m.guild.owner_id


def can_menu(m, g):
    if is_admin(m):
        return True
    p = g["perms"]
    return str(m.id) in p["users"] or any(str(r.id) in p["roles"] for r in m.roles)


# ───────────── Painéis ─────────────
def p_menu():
    sel = discord.ui.Select(custom_id="menu:sel", placeholder="Selecione uma função", options=[
        discord.SelectOption(label="Cadastrar Ticket", value="tk", emoji="🛡️", description="Configurar o sistema de tickets"),
        discord.SelectOption(label="Gerenciar Tickets", value="tm", emoji="🎧", description="Ver e fechar tickets abertos"),
        discord.SelectOption(label="Instagram", value="ig", emoji="📷", description="Canais de fotos estilo Instagram"),
        discord.SelectOption(label="Anti Link", value="al", emoji="🔗", description="Bloqueio de links e convites"),
        discord.SelectOption(label="Anti Bot", value="ab", emoji="🤖", description="Impede a entrada de bots"),
        discord.SelectOption(label="Auto Cargos", value="ar", emoji="🎭", description="Cargos automáticos ao entrar"),
        discord.SelectOption(label="Logs", value="lg", emoji="📜", description="Canais de logs do servidor"),
    ])
    return mk("# ⚙️ Central de Configurações\nSelecione abaixo a função que deseja configurar.", row(sel))


def p_al(g, i):
    a = g["antilink"]
    acao = {"delete": "apagar", "kick": "kikar", "ban": "banir"}[a["action"]]

    def act(x, label):
        return B("al:act:" + x, label, BS.success if a["action"] == x else BS.secondary)

    text = (
        "# 🔗 Anti Link • Central de Configurações\n"
        f"## 🛡️ Administrador\n<@{i.user.id}> (`{i.user.id}`)\n## ℹ️ Informações\n"
        f"Status Ant Link: {st(a['link'])}\nStatus Ant Invite: {st(a['invite'])}\n"
        f"Cargos Protegidos: {lst(a['roles'], '@&')}\nCanais Permitidos: {lst(a['channels'], '#')}\n"
        f"Ação ao detectar: `{acao}`\n## 📝 Observações\n"
        "Ant link bloqueia todo tipo de link, ignora apenas administradores.\n"
        "Ant convite bloqueia apenas convites de servidores, também ignora administradores. "
        "Os cargos protegidos e canais permitidos não serão afetados pelo sistema."
    )
    return mk(
        text,
        row(BACK(), B("al:link", "Ant-Link", emoji="🟢" if a["link"] else "🔴"),
            B("al:invite", "Ant-Invite", emoji="🟢" if a["invite"] else "🔴")),
        row(B("al:roles", "Cargos Protegidos"), B("al:chans", "Canais Protegidos")),
        row(act("ban", "Banir"), act("kick", "Kikar"), act("delete", "Apagar")),
    )


def p_pick(text, sel, back):
    return mk(text, row(sel), row(BACK(back)))


def p_ab(g, i):
    a = g["antibot"]
    return mk(
        f"# 🤖 Painel de Anti Bot\n## ℹ️ Informações\nStatus: {st(a['on'])}\nCanal de Logs: {one(a['log'], '#')}\n"
        "## 📝 Observações\nEste módulo impede a entrada de qualquer bot no servidor quando ativado.",
        row(BACK(), tog("ab:tog", a["on"]), B("ab:log", "Definir Logs")))


def p_ar(g, i):
    a = g["autoroles"]

    def r(k):
        return f"<@&{a[k]}>" if a[k] else "`Nenhum`"

    return mk(
        f"# 🎭 Painel de Auto Cargos\n## ℹ️ Informações\nStatus: {st(a['on'])}\n"
        f"Cargo Membros: {r('member')}\nCargo Bots: {r('bot')}\nCargo Boosters: {r('booster')}\n## 📝 Observações\n"
        "Cargo de membros será dado apenas para **pessoas** ao entrar.\n"
        "Cargo de bots será dado apenas para **bots** ao entrar.\n"
        "Cargo de Booster será dado quando alguém impulsionar seu servidor!",
        row(BACK(), tog("ar:tog", a["on"]), B("ar:set:member", "Definir Cargo Membro")),
        row(B("ar:set:bot", "Definir Cargo Bot"), B("ar:set:booster", "Definir Cargo Booster")),
        row(B("ar:reset", "Resetar Tudo", BS.danger)))


LOGS = {"ban": "Banimentos", "role": "Cargos", "chan": "Canais", "msg": "Mensagens",
        "voice": "Tráfego de voz", "bot": "Bots Adicionados", "join": "Entrada de Membros",
        "leave": "Saída de Membros", "mute": "Membros Silenciados"}


def p_lg(g, i):
    keys = list(LOGS)
    rows = []
    for n in range(0, len(keys), 4):
        rows.append(row(*[B("lg:pick:" + k, LOGS[k], BS.success if g["logs"].get(k) else BS.secondary)
                          for k in keys[n:n + 4]]))
    rows.append(row(BACK()))
    linhas = "\n".join(f"**{LOGS[k]}:** " + (f"<#{g['logs'][k]}>" if g["logs"].get(k) else "`Nenhum`") for k in keys)
    return mk("# 📜 Painel de Logs\n" + linhas + "\n\nEscolha uma log para definir o canal.", *rows)


def p_lg_pick(g, k):
    return mk(f"# 📜 Log: {LOGS[k]}\nCanal atual: {one(g['logs'].get(k), '#')}\nSelecione o canal onde essa log será enviada.",
              row(ch_sel("lg:ch:" + k, "Selecione o canal")),
              row(BACK("lg"), B("lg:clr:" + k, "Remover", BS.danger)))


def p_ig(g, i):
    s = g["ig"]
    linhas = []
    for c, x in s["channels"].items():
        hl = f"<#{x['highlight']}>" if x.get("highlight") else "`sem destaque`"
        rl = f" <@&{x['role']}>" if x.get("role") else ""
        linhas.append(f"<#{c}> → {hl}{rl}")
    chs = "\n".join(linhas) or "`Não foi definido.`"
    msg = f"`{s['message'][:80]}`" if s["message"] else "`Não configurada`"
    sim = "Sim" if s["clear"] else "Não"
    return mk(
        f"# 📷 Central de Configurações • Instagram\n## ℹ️ Informações\nStatus: {st(s['enabled'])}\n\n"
        f"**Canais Configurados:**\n{chs}\n\n**Configurações Globais:**\n"
        f"Limpar Destaque: `{sim}`\nEmojis dos Botões: {s['emojis']['like']} {s['emojis']['comment']}\n"
        f"Meta de curtidas p/ destaque: `{s['meta']}`\nMensagem Armazenada: {msg}\n## 📝 Observações\n"
        "Cada canal de postagem pode ter seu próprio canal de destaque e cargo. Configure quantos canais precisar!\n"
        "> A mensagem configurada é enviada no canal de destaque quando um novo destaque for detectado. Variáveis: `{user}` `{likes}`",
        row(BACK(), tog("ig:tog", s["enabled"]), B("ig:setup", "Configurar Sistema")),
        row(B("ig:del", "Deletar Configuração"), B("ig:emoji", "Configurar Emojis"), B("ig:msg", "Configurar Mensagem")),
        row(B("ig:rhl", "Resetar Destaque", BS.danger), B("ig:rall", "Resetar Tudo", BS.danger)))


def p_ig_setup(d):
    role = f"<@&{d['role']}>" if d.get("role") else "`Nenhum`"
    sim = "Sim" if d.get("clear") else "Não"
    return mk(
        f"# 📷 Configure o Instagram\n**Canal de Postagem:** {one(d.get('post'), '#')}\n"
        f"**Canal de Destaque:** {one(d.get('hl'), '#')}\n**Cargo de Destaque (opcional):** {role}\n"
        f"**Limpar o Destaque anterior?** `{sim}`",
        row(ch_sel("ig:s:post", "Canal de Postagem")),
        row(ch_sel("ig:s:hl", "Canal de Destaque")),
        row(role_sel("ig:s:role", "Cargo de Destaque (opcional)")),
        row(discord.ui.Select(custom_id="ig:s:clear", placeholder="Limpar o Destaque? (opcional)", options=[
            discord.SelectOption(label="Sim", value="1"), discord.SelectOption(label="Não", value="0")])),
        row(BACK("ig"), B("ig:s:save", "Enviar", BS.success)))


def p_ig_del(g, i):
    ids = list(g["ig"]["channels"])
    if not ids:
        return mk("# 🗑️ Deletar Configuração\nNenhum canal configurado.", row(BACK("ig")))
    opts = []
    for cid in ids[:25]:
        c = i.guild.get_channel(int(cid))
        opts.append(discord.SelectOption(label="#" + (c.name if c else cid), value=cid))
    return mk("# 🗑️ Deletar Configuração\nSelecione o canal para remover.",
              row(discord.ui.Select(custom_id="ig:delsel", placeholder="Escolha o canal", options=opts)),
              row(BACK("ig")))


def p_tk_setup(g, i, note=None):
    t = g["tickets"]
    role = f"<@&{t['role']}>" if t["role"] else "`Não foi definido.`"
    n = (note + "\n\n") if note else ""
    return mk(
        f"# 🛡️ Cadastrar Ticket\n{n}**Canal do painel:** {one(t['channel'], '#')}\n"
        f"**Categoria dos tickets:** {one(t['category'], '#')}\n**Cargo de suporte:** {role}\n**Canal de logs:** {one(t['log'], '#')}",
        row(ch_sel("tk:ch", "Canal do painel")),
        row(ch_sel("tk:cat", "Categoria dos tickets", [discord.ChannelType.category])),
        row(role_sel("tk:role", "Cargo de suporte")),
        row(ch_sel("tk:log", "Canal de logs dos tickets")),
        row(BACK(), B("tk:send", "Enviar Painel", BS.success, "📨")))


def p_tk_manage(g, i):
    t = g["tickets"]
    abertos = [(c, u) for c, u in t["open"].items() if i.guild.get_channel(int(c))]
    linhas = "\n".join(f"<#{c}> — <@{u}>" for c, u in abertos) or "`Nenhum ticket aberto.`"
    return mk(f"# 🎧 Gerenciar Tickets\n**Tickets abertos:** {len(abertos)}\n{linhas}",
              row(BACK(), B("tk:closeall", "Fechar todos", BS.danger)))


def p_pm(g, i):
    p = g["perms"]
    opts = []
    for u in p["users"]:
        mem = i.guild.get_member(int(u))
        opts.append(discord.SelectOption(label="Pessoa: " + (mem.display_name if mem else u), value="u:" + u))
    for r in p["roles"]:
        role = i.guild.get_role(int(r))
        opts.append(discord.SelectOption(label="Cargo: " + (role.name if role else r), value="r:" + r))
    rows = [row(discord.ui.UserSelect(custom_id="pm:users", placeholder="Adicionar pessoas", min_values=1, max_values=10)),
            row(role_sel("pm:roles", "Adicionar cargos", True))]
    if opts:
        rows.append(row(discord.ui.Select(custom_id="pm:rem", placeholder="Remover acesso", options=opts[:25],
                                          min_values=1, max_values=min(len(opts), 25))))
        rows.append(row(B("pm:reset", "Remover todos", BS.danger)))
    return mk(
        "# 🔐 Permissões do /menu\nAdministradores e o dono do servidor sempre têm acesso.\n\n"
        f"**Pessoas com acesso:** {lst(p['users'], '@')}\n**Cargos com acesso:** {lst(p['roles'], '@&')}",
        *rows)


async def perm_handler(i, g):
    cid = i.data["custom_id"]
    v = i.data.get("values", [])
    p = g["perms"]
    if cid == "pm:users":
        p["users"] = list(dict.fromkeys(p["users"] + v))
    elif cid == "pm:roles":
        p["roles"] = list(dict.fromkeys(p["roles"] + v))
    elif cid == "pm:rem":
        for x in v:
            k, id_ = x.split(":")
            lista = p["users"] if k == "u" else p["roles"]
            if id_ in lista:
                lista.remove(id_)
    elif cid == "pm:reset":
        g["perms"] = defaults()["perms"]
    save()
    await show(i, p_pm(g, i))


PAN = {"tk": p_tk_setup, "tm": p_tk_manage, "ig": p_ig, "al": p_al, "ab": p_ab, "ar": p_ar, "lg": p_lg}


# ───────────── Logs V2 ─────────────
async def log_to(guild, ch_id, title, desc, color=COLOR):
    if not ch_id:
        return
    ch = guild.get_channel(int(ch_id))
    if not ch:
        return
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(box(T(f"### {title}\n{desc}"), SEP(), T(f"-# <t:{int(time.time())}:F>"), color=color))
    try:
        await ch.send(view=v, allowed_mentions=NOMENT)
    except discord.HTTPException:
        pass


async def L(guild, key, title, desc, color=COLOR):
    await log_to(guild, G(guild.id)["logs"].get(key), title, desc, color)


skip_log = set()


# ───────────── Instagram ─────────────
async def grab(atts, prefix):
    files, names = [], []
    for n, a in enumerate(atts):
        ext = re.sub(r"\W", "", a.filename.rsplit(".", 1)[-1]) or "png"
        name = f"{prefix}{n}.{ext}"
        files.append(discord.File(io.BytesIO(await a.read()), filename=name))
        names.append(name)
    return files, names


def build_post(p, em):
    cap = ("\n" + p["caption"]) if p.get("caption") else ""
    items = [
        T(f"<@{p['author']}>{cap}"), SEP(),
        discord.ui.MediaGallery(*[discord.MediaGalleryItem(f"attachment://{n}") for n in p["names"]]),
    ]
    if p["comments"]:
        items += [SEP(), T("\n".join(f"**<@{x['u']}>:** {x['t']}" for x in p["comments"][-3:]))]
    items += [SEP(), row(
        B("post:like", str(len(p["likes"])), BS.secondary, em["like"]),
        B("post:com", str(len(p["comments"])), BS.secondary, em["comment"]),
        B("post:prof", "Perfil", BS.secondary, "📷"),
        B("post:more", "•••"),
        B("post:del", None, BS.danger, "🗑️"))]
    v = discord.ui.LayoutView(timeout=60)
    v.add_item(box(*items, color=0xE1306C))
    return v


async def highlight(guild, ch_id, msg, p):
    g = G(guild.id)
    cfg = g["ig"]["channels"].get(str(ch_id))
    if not cfg or not cfg.get("highlight"):
        return
    hc = guild.get_channel(int(cfg["highlight"]))
    if not hc:
        return
    if g["ig"]["clear"] and cfg.get("last"):
        try:
            await hc.get_partial_message(int(cfg["last"])).delete()
        except discord.HTTPException:
            pass
    files, names = await grab(msg.attachments, "h")
    texto = (g["ig"]["message"] or "🔥 Novo destaque de {user} com {likes} curtidas!") \
        .replace("{user}", f"<@{p['author']}>").replace("{likes}", str(len(p["likes"])))
    pre = f"<@&{cfg['role']}> " if cfg.get("role") else ""
    v = discord.ui.LayoutView(timeout=None)
    v.add_item(box(T(pre + texto), SEP(),
                   discord.ui.MediaGallery(*[discord.MediaGalleryItem(f"attachment://{n}") for n in names]),
                   color=0xFFB300))
    am = discord.AllowedMentions(roles=[discord.Object(id=int(cfg["role"]))] if cfg.get("role") else False, users=False)
    try:
        sent = await hc.send(view=v, files=files, allowed_mentions=am)
        cfg["last"] = str(sent.id)
        save()
    except discord.HTTPException:
        pass


async def post_handler(i, g):
    a = i.data["custom_id"].split(":")[1]
    mid = str(i.message.id)
    p = g["ig"]["posts"].get(mid)
    if not p:
        return await reply(i, "❌ Post antigo, não encontrado no banco.")
    em = g["ig"]["emojis"]
    if a == "like":
        if i.user.id in p["likes"]:
            p["likes"].remove(i.user.id)
        else:
            p["likes"].append(i.user.id)
        save()
        await i.response.edit_message(view=build_post(p, em))
        if not p["hl"] and len(p["likes"]) >= g["ig"]["meta"]:
            p["hl"] = True
            save()
            asyncio.create_task(highlight(i.guild, i.channel_id, i.message, p))
    elif a == "com":
        async def cb(ii, vals):
            p["comments"].append({"u": ii.user.id, "t": vals["t"][:300]})
            save()
            try:
                msg = await ii.channel.fetch_message(int(mid))
                await msg.edit(view=build_post(p, em))
            except discord.HTTPException:
                pass
            await reply(ii, "💬 Comentário enviado!")
        await i.response.send_modal(Form("Comentar", [{"id": "t", "label": "Seu comentário", "long": True, "max": 300}], cb))
    elif a == "prof":
        try:
            u = await bot.fetch_user(p["author"])
        except discord.HTTPException:
            return await reply(i, "❌ Usuário não encontrado.")
        n = sum(1 for x in g["ig"]["posts"].values() if x["author"] == u.id)
        sec = discord.ui.Section(
            T(f"### 📷 {u.name}\n<@{u.id}>\nConta criada: <t:{int(u.created_at.timestamp())}:D>\nPosts neste servidor: {n}"),
            accessory=discord.ui.Thumbnail(u.display_avatar.url))
        v = discord.ui.LayoutView(timeout=60)
        v.add_item(box(sec, color=0xE1306C))
        await i.response.send_message(view=v, ephemeral=True)
    elif a == "more":
        quem = " ".join(f"<@{u}>" for u in p["likes"]) or "`Ninguém curtiu ainda.`"
        await reply(i, f"### {em['like']} Curtidas ({len(p['likes'])})\n{quem}")
    elif a == "del":
        if i.user.id != p["author"] and not i.user.guild_permissions.manage_messages:
            return await reply(i, "❌ Só o autor ou um moderador pode apagar.")
        g["ig"]["posts"].pop(mid, None)
        save()
        skip_log.add(i.message.id)
        await i.response.defer()
        try:
            await i.message.delete()
        except discord.HTTPException:
            pass


# ───────────── Tickets ─────────────
async def ticket_handler(i, g):
    t = g["tickets"]
    a = i.data["custom_id"].split(":")[1]
    if a == "open":
        cat = i.guild.get_channel(int(t["category"])) if t["category"] else None
        if not cat:
            return await reply(i, "❌ Categoria de tickets não configurada.")
        for c, u in t["open"].items():
            if u == i.user.id and i.guild.get_channel(int(c)):
                return await reply(i, f"Você já possui um ticket: <#{c}>")
        await i.response.defer(ephemeral=True)
        perm = discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True, read_message_history=True)
        ow = {
            i.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            i.user: perm,
            i.guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        role = i.guild.get_role(int(t["role"])) if t["role"] else None
        if role:
            ow[role] = perm
        ch = await i.guild.create_text_channel(f"ticket-{i.user.name}", category=cat, overwrites=ow)
        t["open"][str(ch.id)] = i.user.id
        save()
        extra = f"\n{role.mention}" if role else ""
        await ch.send(
            view=mk(f"# 🎫 Ticket\n{i.user.mention}, descreva seu problema e aguarde o atendimento.{extra}",
                    row(B("tkp:close", "Fechar", BS.danger, "🔒"), B("tkp:claim", "Assumir", BS.success, "🙋"))),
            allowed_mentions=discord.AllowedMentions(users=[i.user], roles=[role] if role else False))
        await log_to(i.guild, t["log"], "🎫 Ticket aberto", f"{ch.mention} por <@{i.user.id}>", 0x43A047)
        return await i.followup.send(view=mk(f"✅ Ticket criado: {ch.mention}"), ephemeral=True)

    owner = t["open"].get(str(i.channel_id))
    staff = i.user.guild_permissions.manage_channels or (
        t["role"] and any(str(r.id) == t["role"] for r in i.user.roles))
    if a == "claim":
        if not staff:
            return await reply(i, "❌ Apenas a equipe pode assumir.")
        return await i.response.send_message(view=mk(f"🙋 {i.user.mention} assumiu este ticket."), allowed_mentions=NOMENT)
    if a == "close":
        if not staff and i.user.id != owner:
            return await reply(i, "❌ Sem permissão.")
        await i.response.send_message(view=mk("🔒 Fechando o ticket em 5 segundos..."))
        t["open"].pop(str(i.channel_id), None)
        save()
        await log_to(i.guild, t["log"], "🔒 Ticket fechado",
                     f"#{i.channel.name} (dono: <@{owner}>) fechado por {i.user.mention}", 0xE53935)
        await asyncio.sleep(5)
        try:
            await i.channel.delete()
        except discord.HTTPException:
            pass


# ───────────── Admin (menu) ─────────────
drafts = {}


async def admin(i, g):
    cid = i.data["custom_id"]
    v = i.data.get("values", [])
    parts = cid.split(":")
    m = parts[0]
    a = parts[1] if len(parts) > 1 else ""
    x = parts[2] if len(parts) > 2 else ""

    async def go(view):
        save()
        await show(i, view)

    if cid == "menu":
        return await show(i, p_menu())
    if cid in PAN:
        return await show(i, PAN[cid](g, i))
    if cid == "menu:sel":
        return await show(i, PAN[v[0]](g, i))

    dk = f"{i.guild_id}:{i.user.id}"

    if m == "al":
        A = g["antilink"]
        if a == "link":
            A["link"] = not A["link"]
            return await go(p_al(g, i))
        if a == "invite":
            A["invite"] = not A["invite"]
            return await go(p_al(g, i))
        if a == "act":
            A["action"] = x
            return await go(p_al(g, i))
        if a == "roles":
            return await show(i, p_pick("# 🛡️ Cargos Protegidos\nSelecione os cargos ignorados pelo Anti Link.",
                                        role_sel("al:rolesel", "Cargos protegidos", True), "al"))
        if a == "chans":
            return await show(i, p_pick("# 🛡️ Canais Permitidos\nSelecione os canais onde links são permitidos.",
                                        ch_sel("al:chansel", "Canais permitidos", multi=True), "al"))
        if a == "rolesel":
            A["roles"] = v
            return await go(p_al(g, i))
        if a == "chansel":
            A["channels"] = v
            return await go(p_al(g, i))

    elif m == "ab":
        if a == "tog":
            g["antibot"]["on"] = not g["antibot"]["on"]
            return await go(p_ab(g, i))
        if a == "log":
            return await show(i, p_pick("# 🤖 Logs do Anti Bot\nSelecione o canal de logs.", ch_sel("ab:logsel", "Canal de logs"), "ab"))
        if a == "logsel":
            g["antibot"]["log"] = v[0]
            return await go(p_ab(g, i))

    elif m == "ar":
        if a == "tog":
            g["autoroles"]["on"] = not g["autoroles"]["on"]
            return await go(p_ar(g, i))
        if a == "set":
            nome = {"member": "Membro", "bot": "Bot", "booster": "Booster"}[x]
            return await show(i, p_pick(f"# 🎭 Definir Cargo {nome}\nSelecione o cargo.", role_sel("ar:sel:" + x, "Selecione o cargo"), "ar"))
        if a == "sel":
            g["autoroles"][x] = v[0]
            return await go(p_ar(g, i))
        if a == "reset":
            g["autoroles"] = defaults()["autoroles"]
            return await go(p_ar(g, i))

    elif m == "lg":
        if a == "pick":
            return await show(i, p_lg_pick(g, x))
        if a == "ch":
            g["logs"][x] = v[0]
            return await go(p_lg(g, i))
        if a == "clr":
            g["logs"].pop(x, None)
            return await go(p_lg(g, i))

    elif m == "tk":
        t = g["tickets"]
        campos = {"ch": "channel", "cat": "category", "role": "role", "log": "log"}
        if a in campos:
            t[campos[a]] = v[0]
            return await go(p_tk_setup(g, i))
        if a == "send":
            async def cb(ii, vals):
                ch = ii.guild.get_channel(int(t["channel"])) if t["channel"] else None
                if not ch:
                    return await go_modal(ii, p_tk_setup(g, ii, "❌ Defina o canal do painel primeiro."))
                await ch.send(view=mk(f"# {vals['title']}\n{vals['desc']}",
                                      row(B("tkp:open", vals["btn"], BS.success, "🎫"))))
                await go_modal(ii, p_tk_setup(g, ii, f"✅ Painel enviado em {ch.mention}!"))
            return await i.response.send_modal(Form("Painel de Ticket", [
                {"id": "title", "label": "Título", "val": "🎫 Suporte", "max": 100},
                {"id": "desc", "label": "Descrição", "long": True, "val": "Clique no botão abaixo para abrir um ticket.", "max": 1000},
                {"id": "btn", "label": "Texto do botão", "val": "Abrir Ticket", "max": 80}], cb))
        if a == "closeall":
            for c in list(t["open"]):
                ch = i.guild.get_channel(int(c))
                if ch:
                    try:
                        await ch.delete()
                    except discord.HTTPException:
                        pass
                t["open"].pop(c, None)
            return await go(p_tk_manage(g, i))

    elif m == "ig":
        S = g["ig"]
        d = drafts.setdefault(dk, {"clear": S["clear"]})
        if a == "tog":
            S["enabled"] = not S["enabled"]
            return await go(p_ig(g, i))
        if a == "setup":
            drafts[dk] = {"clear": S["clear"]}
            return await show(i, p_ig_setup(drafts[dk]))
        if a == "s":
            if x == "post":
                d["post"] = v[0]
            elif x == "hl":
                d["hl"] = v[0]
            elif x == "role":
                d["role"] = v[0]
            elif x == "clear":
                d["clear"] = v[0] == "1"
            elif x == "save":
                if not d.get("post"):
                    return await i.response.edit_message(view=p_ig_setup(d))
                old = S["channels"].get(d["post"], {})
                S["channels"][d["post"]] = {"highlight": d.get("hl"), "role": d.get("role"), "last": old.get("last")}
                S["clear"] = d.get("clear", S["clear"])
                drafts.pop(dk, None)
                return await go(p_ig(g, i))
            return await i.response.edit_message(view=p_ig_setup(d))
        if a == "del":
            return await show(i, p_ig_del(g, i))
        if a == "delsel":
            S["channels"].pop(v[0], None)
            return await go(p_ig(g, i))
        if a == "emoji":
            async def cb(ii, vals):
                S["emojis"] = {"like": vals["like"].strip() or "❤️", "comment": vals["com"].strip() or "💬"}
                save()
                await go_modal(ii, p_ig(g, ii))
            return await i.response.send_modal(Form("Emojis dos Botões", [
                {"id": "like", "label": "Emoji de curtir", "val": S["emojis"]["like"], "max": 60},
                {"id": "com", "label": "Emoji de comentar", "val": S["emojis"]["comment"], "max": 60}], cb))
        if a == "msg":
            async def cb(ii, vals):
                S["message"] = vals["m"] or ""
                try:
                    S["meta"] = max(1, int(vals["n"]))
                except ValueError:
                    S["meta"] = 5
                save()
                await go_modal(ii, p_ig(g, ii))
            return await i.response.send_modal(Form("Mensagem de Destaque", [
                {"id": "m", "label": "Mensagem ({user} e {likes})", "long": True, "req": False, "val": S["message"], "max": 500},
                {"id": "n", "label": "Meta de curtidas para destaque", "val": S["meta"], "max": 4}], cb))
        if a == "rhl":
            for c in S["channels"].values():
                c.update({"highlight": None, "role": None, "last": None})
            return await go(p_ig(g, i))
        if a == "rall":
            g["ig"] = defaults()["ig"]
            return await go(p_ig(g, i))


async def go_modal(i, view):
    save()
    await i.response.edit_message(view=view)


# ───────────── Interações ─────────────
@bot.tree.command(name="menu", description="Abre o menu de configurações do bot")
@app_commands.guild_only()
async def menu(i: discord.Interaction):
    if not can_menu(i.user, G(i.guild.id)):
        return await reply(i, "❌ Você não tem permissão para usar o /menu.")
    await show(i, p_menu())


@bot.tree.command(name="perm", description="Define quem pode acessar o /menu")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
async def perm(i: discord.Interaction):
    if not is_admin(i.user):
        return await reply(i, "❌ Apenas administradores podem usar o /perm.")
    await show(i, p_pm(G(i.guild.id), i))


@bot.event
async def on_interaction(i: discord.Interaction):
    if i.type != discord.InteractionType.component or not i.guild:
        return
    try:
        cid = i.data.get("custom_id", "")
        g = G(i.guild.id)
        if cid.startswith("post:"):
            return await post_handler(i, g)
        if cid.startswith("tkp:"):
            return await ticket_handler(i, g)
        if cid.startswith("pm:"):
            if not is_admin(i.user):
                return await reply(i, "❌ Apenas administradores.")
            return await perm_handler(i, g)
        if not can_menu(i.user, g):
            return await reply(i, "❌ Sem permissão.")
        await admin(i, g)
    except Exception:
        traceback.print_exc()
        try:
            await reply(i, "❌ Ocorreu um erro.")
        except Exception:
            pass


# ───────────── Mensagens: Anti Link + Instagram ─────────────
INV = re.compile(r"(discord\.gg|discord(app)?\.com/invite)/\S+", re.I)
LNK = re.compile(r"(https?://|www\.)\S+|discord\.gg/\S+", re.I)


@bot.event
async def on_message(m: discord.Message):
    if not m.guild or m.author.bot:
        return
    g = G(m.guild.id)
    A = g["antilink"]
    mem = m.author
    if (A["link"] or A["invite"]) and isinstance(mem, discord.Member) \
            and not mem.guild_permissions.administrator \
            and str(m.channel.id) not in A["channels"] \
            and not any(str(r.id) in A["roles"] for r in mem.roles):
        if (A["invite"] and INV.search(m.content)) or (A["link"] and LNK.search(m.content)):
            skip_log.add(m.id)
            try:
                await m.delete()
            except discord.HTTPException:
                pass
            try:
                if A["action"] == "ban":
                    await mem.ban(reason="Anti Link")
                elif A["action"] == "kick":
                    await mem.kick(reason="Anti Link")
            except discord.HTTPException:
                pass
            return
    cfg = g["ig"]["channels"].get(str(m.channel.id))
    if g["ig"]["enabled"] and cfg:
        imgs = [a for a in m.attachments if (a.content_type or "").startswith("image/")][:10]
        if not imgs:
            return
        files, names = await grab(imgs, "img")
        p = {"author": m.author.id, "caption": (m.content or "")[:500], "names": names,
             "likes": [], "comments": [], "hl": False}
        sent = await m.channel.send(view=build_post(p, g["ig"]["emojis"]), files=files, allowed_mentions=NOMENT)
        g["ig"]["posts"][str(sent.id)] = p
        save()
        skip_log.add(m.id)
        try:
            await m.delete()
        except discord.HTTPException:
            pass


# ───────────── Membros: Anti Bot, Auto Cargos, Logs ─────────────
@bot.event
async def on_member_join(mem: discord.Member):
    g = G(mem.guild.id)
    if mem.bot and g["antibot"]["on"] and mem.id != bot.user.id:
        try:
            await mem.kick(reason="Anti Bot")
        except discord.HTTPException:
            pass
        await log_to(mem.guild, g["antibot"]["log"], "🤖 Anti Bot",
                     f"Bot **{mem}** (`{mem.id}`) foi expulso.", 0xE53935)
    if g["autoroles"]["on"]:
        rid = g["autoroles"]["bot"] if mem.bot else g["autoroles"]["member"]
        role = mem.guild.get_role(int(rid)) if rid else None
        if role:
            try:
                await mem.add_roles(role)
            except discord.HTTPException:
                pass
    if mem.bot:
        await L(mem.guild, "bot", "🤖 Bot adicionado", f"{mem.mention} (`{mem.id}`)", 0x1E88E5)
    else:
        await L(mem.guild, "join", "📥 Membro entrou",
                f"{mem.mention} (`{mem.id}`)\nConta criada: <t:{int(mem.created_at.timestamp())}:R>", 0x43A047)


@bot.event
async def on_member_remove(mem: discord.Member):
    await L(mem.guild, "leave", "📤 Membro saiu", f"<@{mem.id}> (`{mem}`)", 0xE53935)


@bot.event
async def on_member_update(o: discord.Member, n: discord.Member):
    g = G(n.guild.id)
    if not o.premium_since and n.premium_since and g["autoroles"]["on"] and g["autoroles"]["booster"]:
        role = n.guild.get_role(int(g["autoroles"]["booster"]))
        if role:
            try:
                await n.add_roles(role)
            except discord.HTTPException:
                pass
    add = [r for r in n.roles if r not in o.roles]
    rem = [r for r in o.roles if r not in n.roles]
    if add or rem:
        txt = n.mention
        if add:
            txt += "\n➕ " + " ".join(r.mention for r in add)
        if rem:
            txt += "\n➖ " + " ".join(r.mention for r in rem)
        await L(n.guild, "role", "🎭 Cargos de membro alterados", txt)
    if o.timed_out_until != n.timed_out_until:
        if n.timed_out_until:
            await L(n.guild, "mute", "🔇 Membro silenciado",
                    f"{n.mention}\nAté: <t:{int(n.timed_out_until.timestamp())}:F>", 0xFB8C00)
        elif o.timed_out_until:
            await L(n.guild, "mute", "🔊 Silenciamento removido", n.mention, 0x43A047)


@bot.event
async def on_member_ban(guild, user):
    await L(guild, "ban", "🔨 Membro banido", f"<@{user.id}> (`{user}`)", 0xE53935)


@bot.event
async def on_member_unban(guild, user):
    await L(guild, "ban", "♻️ Banimento removido", f"<@{user.id}> (`{user}`)", 0x43A047)


@bot.event
async def on_guild_role_create(r):
    await L(r.guild, "role", "🎭 Cargo criado", f"{r.mention} (`{r.id}`)", 0x43A047)


@bot.event
async def on_guild_role_delete(r):
    await L(r.guild, "role", "🎭 Cargo deletado", f"**{r.name}** (`{r.id}`)", 0xE53935)


@bot.event
async def on_guild_role_update(o, n):
    ch = []
    if o.name != n.name:
        ch.append(f"Nome: `{o.name}` → `{n.name}`")
    if o.color != n.color:
        ch.append(f"Cor: `{o.color}` → `{n.color}`")
    if o.permissions != n.permissions:
        ch.append("Permissões alteradas")
    if ch:
        await L(n.guild, "role", "🎭 Cargo editado", n.mention + "\n" + "\n".join(ch))


@bot.event
async def on_guild_channel_create(c):
    await L(c.guild, "chan", "📁 Canal criado", f"{c.mention} (`{c.name}`)", 0x43A047)


@bot.event
async def on_guild_channel_delete(c):
    await L(c.guild, "chan", "📁 Canal deletado", f"**#{c.name}** (`{c.id}`)", 0xE53935)


@bot.event
async def on_guild_channel_update(o, n):
    if o.name != n.name:
        await L(n.guild, "chan", "📁 Canal renomeado", f"{n.mention}\n`{o.name}` → `{n.name}`")


@bot.event
async def on_message_delete(m: discord.Message):
    if not m.guild or m.author.bot:
        return
    if m.id in skip_log:
        skip_log.discard(m.id)
        return
    G(m.guild.id)["ig"]["posts"].pop(str(m.id), None)
    conteudo = ("> " + m.content[:900]) if m.content else "`(sem conteúdo)`"
    await L(m.guild, "msg", "🗑️ Mensagem deletada",
            f"Autor: <@{m.author.id}>\nCanal: <#{m.channel.id}>\n{conteudo}", 0xE53935)


@bot.event
async def on_message_edit(o: discord.Message, n: discord.Message):
    if not n.guild or n.author.bot or o.content == n.content or not o.content:
        return
    await L(n.guild, "msg", "✏️ Mensagem editada",
            f"Autor: <@{n.author.id}> em <#{n.channel.id}>\n**Antes:** {o.content[:800]}\n"
            f"**Depois:** {(n.content or '')[:800]}\n[Ir para mensagem]({n.jump_url})", 0xFB8C00)


@bot.event
async def on_voice_state_update(mem, o, n):
    u = mem.mention
    if not o.channel and n.channel:
        await L(mem.guild, "voice", "🔊 Entrou em call", f"{u} → {n.channel.mention}", 0x43A047)
    elif o.channel and not n.channel:
        await L(mem.guild, "voice", "🔇 Saiu da call", f"{u} ← {o.channel.mention}", 0xE53935)
    elif o.channel != n.channel:
        await L(mem.guild, "voice", "🔁 Mudou de call", f"{u}: {o.channel.mention} → {n.channel.mention}")


# ───────────── Ready ─────────────
async def sync_guild(guild):
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    except Exception:
        traceback.print_exc()


@bot.event
async def on_ready():
    print(f"✅ Online como {bot.user}")
    for guild in bot.guilds:
        await sync_guild(guild)


@bot.event
async def on_guild_join(guild):
    await sync_guild(guild)


bot.run(os.environ["TOKEN"])

# Bot Discord (Python) - Components V2

Comandos: /menu (painel de configuracao) e /perm (quem pode usar o /menu).

## Arquivos
- main.py ............ o bot inteiro (menu, tickets, instagram, anti link, anti bot, auto cargos, logs, perm)
- requirements.txt ... dependencias (discord.py 2.6+)
- Procfile ........... comando de inicio no Railway
- .env.example ....... modelo das variaveis (TOKEN)
- .gitignore

## Railway
1. Suba estes arquivos para um repositorio no GitHub.
2. Railway > New Project > Deploy from GitHub repo.
3. Variables: TOKEN = token do bot.
4. Volume montado em /data + variavel DATA_DIR=/data (para nao perder as configuracoes).

## Discord Developer Portal
- Ative Server Members Intent e Message Content Intent.
- Convide com escopos bot + applications.commands e permissao de Administrador.

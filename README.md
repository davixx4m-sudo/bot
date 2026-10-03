# Discord Mod Bot

Bot de moderação para Discord escrito em Python, com comandos exclusivamente de prefixo `!` e configuração por variáveis de ambiente. O projeto usa SQLite local para persistir avisos, configurações por servidor e timeouts temporários.

## Requisitos

É necessário ter Python 3.10 ou superior instalado. Também será preciso criar uma aplicação de bot no [Discord Developer Portal](https://discord.com/developers/applications), copiar o token e habilitar os intents privilegiados **Server Members Intent** e **Message Content Intent** na área de configurações do bot.

O bot deve ser convidado com os escopos `bot` e `applications.commands`, mesmo que este projeto não registre nem utilize comandos slash. Conceda somente as permissões necessárias; para o conjunto completo de comandos, o bot precisa conseguir visualizar canais, enviar mensagens, incorporar links, ler histórico, gerenciar mensagens, gerenciar canais, expulsar membros, banir membros e moderar membros.

## Instalação

No terminal, entre na pasta do projeto e crie um ambiente virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Copie o arquivo de exemplo e preencha o token:

```bash
cp .env.example .env
```

No `.env`, altere `DISCORD_TOKEN` para o token real do bot. Não publique esse arquivo nem compartilhe o token. Se um token vazar, gere outro imediatamente no portal do Discord.

Depois, inicie o bot:

```bash
python bot.py
```

Quando aparecer no console que o bot está conectado, use `!help` dentro do servidor.

## Variáveis de ambiente

| Variável | Obrigatória | Finalidade |
|---|---:|---|
| `DISCORD_TOKEN` | Sim | Token privado do bot. |
| `COMMAND_PREFIX` | Não | Prefixo dos comandos; o padrão é `!`. |
| `MOD_LOG_CHANNEL_ID` | Não | ID de um canal padrão para logs de moderação. |
| `MOD_ROLE_ID` | Não | ID de um cargo que poderá usar comandos de moderação. |
| `MAX_CLEAR_MESSAGES` | Não | Limite do `!clear` e `!purgeuser`, limitado a 100. |
| `DATABASE_PATH` | Não | Caminho do banco SQLite; o padrão é `data/modbot.sqlite3`. |
| `EMBED_COLOR` | Não | Cor hexadecimal das mensagens incorporadas. |

## Comandos

Todos os comandos abaixo começam com `!`. Para usuários e cargos, é possível usar menções ou IDs.

Por padrão, o bot apaga a mensagem do comando enviada pelo usuário e apaga a resposta em embed depois de 10 segundos. A exceção é a embed de `!avatar`/`!av`, que permanece no canal; somente a mensagem do comando é removida.

O comando `!setrole @cargo @usuário` exige a permissão nativa **Administrador**, adiciona o cargo ao usuário e registra esse cargo como autorizado para os comandos administrativos de moderação. O alias `!setroles` também está disponível. O comando `!infroles` mostra em embed os cargos autorizados, os membros encontrados com esses cargos e os administradores nativos do Discord.

O comando `!bes` exige a permissão nativa **Administrador**. Ao executá-lo, o bot abre três select menus independentes: o primeiro seleciona exclusivamente o canal da embed de boas-vindas, o segundo seleciona exclusivamente o canal da embed de saída e o terceiro seleciona exclusivamente o canal da mensagem normal de aviso de entrada. Depois de escolher um canal em cada menu e pressionar **OK**, o bot salva a configuração e mostra a confirmação `Tudo pronto, essa embed de configuração irá sumir em 10 segundos`. A configuração expira visualmente em 10 segundos, enquanto os canais continuam salvos.

O comando `!autorole` também exige a permissão nativa **Administrador** e a permissão do bot para **Gerenciar cargos**. Ele abre um select menu de cargos; depois de selecionar um cargo e pressionar **OK**, o bot salva a configuração e apaga a embed de confirmação após 10 segundos. Novos membros recebem automaticamente o cargo escolhido, desde que o cargo do bot esteja acima dele na hierarquia. O cargo `@everyone` e cargos gerenciados por integrações são rejeitados.

O comando `!setlog #canal` ativa o envio de todas as auditorias para o canal configurado. Cada evento também é salvo em `data/logs/<guild_id>.log`, incluindo os registros do `!clear` e do `!purgeuser`. As embeds de auditoria usam sempre a mesma cor cinza lateral. Os logs de banimento, mute/timeout e castigo informam quem executou, quem foi afetado, motivo, tempo quando aplicável e total de avisos. Quando não houver motivo, o texto usado será `sem motivos específicos`. O diretório é criado automaticamente na primeira inicialização.

A auditoria cobre entrada e saída de membros, entrada, saída e troca de canal de voz, alteração de apelido, adição e remoção de cargos, mensagens apagadas pelo próprio autor ou por moderadores, mensagens editadas, banimentos, expulsões, mutes/timeouts, punições, canais trancados e destrancados, alterações de slowmode, `!clear` e `!purgeuser`. O log de mensagem apagada informa separadamente **Quem apagou** e **Autor original**. Para identificar o moderador responsável em eventos administrativos, conceda ao bot a permissão de visualizar o **Audit Log** do servidor. O sistema evita duplicar o mesmo evento quando os eventos normal e raw do Discord são disparados juntos.

Quando um membro entrar, o bot enviará a embed no canal de boas-vindas contendo display name, nome original, ID do usuário, avatar e quantidade atual de membros no servidor. No terceiro canal configurado, enviará também uma mensagem normal como `<@usuário> entrou no servidor.`, apagada após 10 segundos. Quando um membro sair, a embed será enviada somente no canal de saída. As duas embeds usam a mesma cor cinza na lateral para manter o visual uniforme. Para esse recurso, habilite **Server Members Intent** e conceda ao bot permissão para visualizar o canal, enviar mensagens e incorporar links.

| Categoria | Comando | Exemplo | Permissão principal |
|---|---|---|---|
| Moderação | `!ban <usuário> [motivo]` | `!ban @Joao spam` | Banir membros |
| Moderação | `!unban <usuário ou ID> [motivo]` | `!unban 123456789` | Banir membros |
| Moderação | `!kick <usuário> [motivo]` | `!kick @Joao spam` | Expulsar membros |
| Moderação | `!mute <usuário> <tempo> [motivo]` | `!mute @Joao 30m flood` | Moderar membros |
| Moderação | `!unmute <usuário> [motivo]` | `!unmute @Joao` | Moderar membros |
| Moderação | `!warn <usuário> [motivo]` | `!warn @Joao linguagem` | Gerenciar mensagens |
| Moderação | `!warnings <usuário>` | `!warnings @Joao` | Gerenciar mensagens |
| Moderação | `!delwarn <id>` | `!delwarn 12` | Gerenciar mensagens |
| Mensagens | `!clear <quantidade>` | `!clear 20` | Gerenciar mensagens |
| Mensagens | `!purgeuser <usuário> [quantidade]` | `!purgeuser @Joao 50` | Gerenciar mensagens |
| Canais | `!lock` | `!lock` | Gerenciar canais |
| Canais | `!unlock` | `!unlock` | Gerenciar canais |
| Canais | `!slowmode [segundos]` | `!slowmode 10` | Gerenciar canais |
| Administração | `!setrole <@cargo> <@usuário>` | `!setrole @Admin @Joao` | Administrador |
| Administração | `!setroles <@cargo> <@usuário>` | `!setroles @Admin @Joao` | Administrador; alias |
| Administração | `!infroles` | `!infroles` | Administrador |
| Boas-vindas | `!bes` | `!bes` | Administrador |
| Administração | `!autorole` | `!autorole` | Administrador |
| Configuração | `!setlog [#canal]` | `!setlog #logs` | Administrador |
| Auditoria | Logs automáticos | `data/logs/<guild_id>.log` e canal configurado | Bot com acesso ao canal |
| Configuração | `!setmodrole [@cargo]` | `!setmodrole @Moderador` | Administrador |
| Utilitário | `!avatar [@usuário]` ou `!av [@usuário]` | `!av @Joao` | Todos |
| Utilitário | `!userinfo [@usuário]` | `!userinfo @Joao` | Todos |
| Utilitário | `!serverinfo` | `!serverinfo` | Todos |
| Utilitário | `!ping` | `!ping` | Todos |
| Ajuda | `!help` | `!help` | Todos |

Os comandos `!setrole`, `!setroles`, `!infroles`, `!bes`, `!autorole`, `!setlog` e `!setmodrole` ficam restritos a usuários com a permissão nativa **Administrador**. Os demais comandos continuam usando as permissões específicas descritas na tabela.

Os tempos do `!mute` aceitam os formatos `s` para segundos, `m` para minutos, `h` para horas, `d` para dias e `w` para semanas. Por exemplo: `30m`, `2h`, `7d` ou `1w`. O limite de timeout do Discord é de 28 dias.

## Segurança e operação

O bot verifica a hierarquia de cargos antes de expulsar, banir ou aplicar timeout. Assim, ele não tenta moderar o dono do servidor, o próprio moderador ou membros que estejam acima do moderador ou do cargo do bot. O registro de avisos e os ajustes por servidor permanecem no SQLite local.

O comando `!setlog #canal` configura o canal de logs para o servidor atual. Para desativar um canal configurado, execute `!setlog` sem mencionar um canal. O mesmo padrão vale para `!setmodrole`: `!setmodrole` remove o cargo específico e retorna à validação pelas permissões nativas do Discord.

Para manter o bot on-line continuamente, execute-o em uma máquina ou servidor que permaneça ligado. O projeto não armazena nem envia o token para nenhum serviço externo; ele é lido somente do `.env` no processo local.

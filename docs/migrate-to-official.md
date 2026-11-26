# 从 NapCat 版迁移

你已经按旧仓库 Priestess-bot 的 NapCat 版部署过，现在想换成官方机器人，数据还想留着，就看这一页。
第一次部署的，看 [部署](deploy-official.md)。

数据库和数据目录带过来就能用，不用导出导入，只是要先用旧代码删掉关键词回复的表。真正要花时间的是认人：官方平台不给 QQ 号，
原来按 QQ 号存的绑定，要重新对上本人。NapCat 还能登录的话，让两个机器人一起跑一段时间，大部分人会被自动认出来。
NapCat 已经用不了，就只能靠成员私聊机器人扫一次码。

- [要带过来的](#要带过来的)
- [换代码](#换代码)
- [接入官方机器人](#接入官方机器人)
- [和 NapCat 并存](#和-napcat-并存)
- [NapCat 已经用不了](#napcat-已经用不了)
- [迁移完成后](#迁移完成后)
- [常见问题](#常见问题)

## 要带过来的

只有不在 git 里的这几样：

- `.env`，有的话还有 `.env.prod`；
- `data/`：数据库 `data/nonebot_plugin_orm/db.sqlite3` 和各插件的数据都在这里；
- 想留着的话，`backups/` 里账号体检的备份，以及你自己放进 `local_plugins/` 的插件。

`cache/` 会自己重新生成。

## 换代码

这个仓库和旧仓库的提交历史不相连，要装到一个新目录。旧目录的代码先不动，出了问题还能切回去。
下面以旧目录 `/opt/priestess-bot`、新目录 `/opt/priestess-qqbot` 为例。

先停掉机器人，备份数据库，再用旧代码删掉关键词回复的表。旧版的关键词回复（nonebot-plugin-pErithacus）这里已经去掉了，
设过的关键词会跟着表一起删掉，备份的数据库里还有一份。
不先删的话，新代码升级数据库时会报 `Can't locate revision identified by '4184a6a150e9'`，机器人也起不来。

```bash
sudo systemctl stop nonebot
cp -a /opt/priestess-bot/data/nonebot_plugin_orm/db.sqlite3 ~/db-before-qqbot.sqlite3   # 先备份数据库
cd /opt/priestess-bot
.venv/bin/python deploy/orm_cli.py downgrade 4184a6a150e9@base   # 用旧代码删掉关键词回复的表
```

然后装新目录。旧目录里有 `.env.prod`、`backups/`、`local_plugins/` 的，加在 `cp -a` 那一行里一起复制。
`local_plugins/` 里的插件有自己的数据库表的话，要先复制过来再升级数据库，不然也会报 `Can't locate revision`。

```bash
sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /opt/priestess-qqbot
git clone https://github.com/Billtranscend/Priestess-QQbot.git /opt/priestess-qqbot
cp -a /opt/priestess-bot/.env /opt/priestess-bot/data /opt/priestess-qqbot/
cd /opt/priestess-qqbot
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-official.txt -c requirements.lock
sudo .venv/bin/playwright install-deps chromium
.venv/bin/playwright install chromium
.venv/bin/python deploy/orm_cli.py upgrade
```

所有命令都用原来运行机器人的那个用户执行，浏览器要装在这个用户下面。

然后改 systemd 单元，把 `WorkingDirectory`、`ExecStart`、`ReadWritePaths` 改到新目录，设过 `PLAYWRIGHT_BROWSERS_PATH` 的也改掉，
再 `sudo systemctl daemon-reload`。先别启动，接着做下一节。

## 接入官方机器人

1. 在 QQ 开放平台注册机器人，做好平台上的设置，见 [部署](deploy-official.md#部署步骤) 第 1 步。
2. `.env` 里把 `DRIVER` 改成 `~fastapi+~httpx+~websockets`，再加一行 `QQ_API_BASE=https://api.bot.qq.com/`。
3. 在 `.env.prod` 里填 `QQ_BOTS`，写法照 `.env.example` 里的那一行。没有这个文件就先建一个：`touch .env.prod && chmod 600 .env.prod`。
4. 原来给 NapCat 用的 `HOST`、`PORT`、`ONEBOT_ACCESS_TOKEN` 留着不动。两个机器人一起跑的时候，NapCat 还要从这里连进来。

接下来看 NapCat 还能不能用，二选一。

## 和 NapCat 并存

NapCat 还能登录的话，先让两个机器人在同一个进程里一起跑一段时间。NapCat 照旧连这个机器人，它连机器人的设置不用改。

在 `.env` 里加上这几项：

- `OFFICIAL_BRIDGE_PRIMARY=true`：一定要设。并存期间 NapCat 账号不再主动发任何消息，只回复它收到的指令。
  不设这一项，定时推送还会交给它，活动通知、资料库更新通知就全都发不出去。
- `OFFICIAL_BRIDGE_UINS=["<官方机器人资料卡上的 QQ 号>"]`：建议填，原机器人靠它认出进群的是官方机器人。
- `HELP_MIGRATION_NOTE=true`：帮助图上提示老用户加机器人为好友、私聊发 `/skl绑定` 扫一次码。
- 用 nonebot-bison 的，设 `OFFICIAL_BRIDGE_ONEBOT_CLIENTS`，让桥接层去连 Bison，见 [搭配 nonebot-bison](deploy-official.md#搭配-nonebot-bison)。
  然后在 NapCat WebUI 的「网络配置」里停用原来连 Bison 的那个 WebSocket 客户端，免得 Bison 同时连着两个机器人。

`sudo systemctl start nonebot` 启动，把官方机器人拉进原来的群，@它发 `/skl帮助`，看看能不能正常回复。
群主要打开的两项设置、@ 的规则和平台的限制，见 [部署](deploy-official.md#群主需要做的)。

### 群怎么对上

官方机器人被拉进原机器人所在的群时，原机器人会自动在群里发一句带编码的「迁移握手」，官方机器人据此把这个群对上原来的群号。
按群号存的东西随之沿用，比如 Bison 的订阅。对上原群号的群里，指令由官方机器人回答。

没对上的群当作新群。原机器人还在线时，这种群里官方机器人只回答真正 @ 它的指令，群主设了「获取群内全部消息」也一样。
这样同一条指令不会被两个机器人各回一次。原机器人下线以后就没有这个区别了。
想让这种群里不带 @ 的指令也由官方机器人回答，设 `OFFICIAL_BRIDGE_SHADOW_UNLINKED=false`。

### 人怎么认出

两个机器人都在的群里，成员发的消息两边都收到，就会被自动认出来。两边显示的名字一样，第一条就能认出；名字不一样，要两条。
官方机器人平时只收到真正 @ 它的消息。群主没设「获取群内全部消息」的群里，只有 @ 它的消息能用来认人。
认出之后，成员在这之前用临时编号做过的绑定，也会一起带过来。

成员也可以自己动手：私聊官方机器人发 `/迁移` 领一个迁移码，10 分钟内用自己的 QQ 把「/迁移 迁移码」发给原机器人，私聊或群里都行，两边就对上了。
对上原群号的群里发 `/迁移`，官方机器人不会回。
原机器人下线以后，`/迁移` 会改成提示私聊发 `/skl绑定`。

你自己也一样。在两个机器人都在的群里真正 @ 官方机器人发一条指令，或者用 `/迁移`，就会被认出是原来的 QQ 号，`SUPERUSERS` 不用改。

### 原机器人退群之后

原账号退出最后一个群以后，NapCat 先别停，机器人也别重启，等日志里出现 `official bridge: the old bot is in no group any more`。
至少要十分钟，这期间要有人在原来的群里 @ 机器人发指令，隔几分钟一条，直到出现这一行。设了「获取群内全部消息」的群，平时聊天也算。
没等到这一行就停了 NapCat 的，下面说的都不会发生：没认出的成员照常签到、绑定一直保留，「不在群里」的解绑也一直不做。
每周一体检后你还会收到一条私聊，说没能查清谁还在群里或是好友；每次启动后，定时推送和 Bison 也要过 3 分钟才接上。
这时把 NapCat 重新连上，再等这一行。

出现这一行以后，还没被认出的成员不再参加每晚签到，绑定保留 45 天，到期由每周体检解除，删之前照常备份。
这期间他们私聊发 `/skl绑定` 扫码就能恢复，当晚起照常签到。

从这时起，「本人既不在机器人任何群里、也不是机器人好友」的成员也开始算：一发现就停掉为他跑的定时任务，
周一体检时还是这样就解除他的绑定，备份留 14 天；在那之前他发言、进群或加好友，任务就恢复。

## NapCat 已经用不了

NapCat 上游已经停了，QQ 一更新它就可能登录不上。这时没法靠两个机器人一起听来认人，只能直接换。这条路我自己没有走过。

- 所有群都当作新群。按原群号存的东西对不上了，比如 Bison 的订阅，要在群里重新添加。
- 用 nonebot-bison 的，在 `.env` 设 `OFFICIAL_BRIDGE_ONEBOT_CLIENTS`，让桥接层去连 Bison，见 [搭配 nonebot-bison](deploy-official.md#搭配-nonebot-bison)。
  NapCat 停了以后，没有别的程序去连 Bison。不设这一项，Bison 的推送就发不出去。
- 成员的绑定都还在，只是机器人还不知道谁是谁。成员加机器人为好友，私聊发 `/skl绑定` 扫一次码，原来的绑定和抽卡记录就认回本人了。
  设 `HELP_MIGRATION_NOTE=true`，帮助图上会这样提示。
- 还没认回来的成员照常每晚签到，每周体检也不会按「不在群里」解绑他们，其他检查照常。认回来以后才按这一条算。
- 和从零部署的一样，每次启动后要过 3 分钟，定时推送、给你的私聊提醒、绑定后抽卡记录拉完的那条回复和 Bison 才接上，见 [部署](deploy-official.md#部署步骤) 第 5 步。
- 你自己：超级用户名单里的号不能靠扫码认领。原来绑过森空岛的，先把 `SUPERUSERS` 清空再启动，私聊扫码认回原来的 QQ 号，
  再把 `SUPERUSERS` 改回原来的 QQ 号，重启。没绑过的，按 [部署](deploy-official.md#部署步骤) 第 6 步把 `SUPERUSERS` 换成你的编号。

`.env` 改好以后 `sudo systemctl start nonebot` 启动，把官方机器人拉进原来的群，@它发 `/skl帮助`，看看能不能正常回复。
群主要打开的两项设置、@ 的规则和平台的限制，见 [部署](deploy-official.md#群主需要做的)。

## 迁移完成后

- 和 NapCat 并存过的，日志里出现 [上面那一行](#原机器人退群之后) 之后，NapCat 的容器就可以停掉，在旧目录里 `docker compose down`。
  Bison 已经由桥接层去连，不受影响。
- 只在并存时用的代码在 `plugins/official_bridge/coexist/`，没有原机器人时什么都不做，不用管。
- `HELP_MIGRATION_NOTE` 留到老用户基本回来以后，再删掉这一项并重启。和 NapCat 并存过的，没认出的成员在日志里出现上面那一行 45 天后会被解绑。
- 换过来之后第一次周一体检之前，私聊机器人发一次不带「清理」的 `/森空岛体检`，看看它准备解绑的人数正不正常。
- 换过来以后少了两样：关键词回复，和理智回满提醒 `/理智提醒`。`/理智` 还在。
- 平时的使用和 [部署](deploy-official.md) 里写的一样，包括「平台的限制」那一节。

## 常见问题

**换成官方机器人之后，成员说自己的绑定不见了。** 绑定没有删，只是机器人还没认出他。让他加机器人为好友，私聊发 `/skl绑定` 扫一次码。

**想退回旧版本。** 先停掉机器人，把迁移前备份的 `~/db-before-qqbot.sqlite3` 放回旧目录的 `data/nonebot_plugin_orm/db.sqlite3`。
旧版还要用关键词回复的表，不换回备份它起不来。
再把 systemd 单元的路径改回旧目录，`daemon-reload` 后启动。并存时停用过 NapCat 连 Bison 的那个 WebSocket 客户端的，在 WebUI 的「网络配置」里把它重新启用，不然 Bison 的推送发不出去。迁移之后新产生的数据不会跟着回去。

更多问题见首页的 [常见问题](../README.md#常见问题)。

[返回首页](../README.md)

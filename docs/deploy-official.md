# 部署

这一页讲怎么从零把机器人部署成 QQ 官方机器人。我自己的机器人就是这样跑的，不过它是从 NapCat 迁移过来的，
从零部署这条路我没有完整走过一遍。照着做遇到问题，到 [Issues](https://github.com/Billtranscend/Priestess-QQbot/issues) 告诉我。

已经按旧仓库的 NapCat 版部署过、想把数据带过来的，先看 [从 NapCat 版迁移](migrate-to-official.md)。
硬件和系统要求见首页的 [运行环境](../README.md#运行环境)。

- [工作方式](#工作方式)
- [部署步骤](#部署步骤)
- [群主需要做的](#群主需要做的)
- [在群里发指令](#在群里发指令)
- [快捷按钮](#快捷按钮)
- [平台的限制](#平台的限制)
- [群名与机器人已不在的群](#群名与机器人已不在的群)
- [帮助图](#帮助图)
- [配置](#配置)
- [搭配 nonebot-bison](#搭配-nonebot-bison)
- [常见问题](#常见问题)

## 工作方式

机器人是 NoneBot2 加 nonebot-adapter-qq，连的是 QQ 开放平台，不用登录任何 QQ 号。

插件都是按 OneBot v11 写的。`plugins/official_bridge` 夹在中间：把平台发来的消息转成 OneBot v11 事件交给插件，
再把插件调用的 OneBot 接口换成平台的接口。所以插件不用为官方机器人改写。

```
QQ  <-->  QQ 开放平台  <-- WebSocket / HTTP -->  plugins/official_bridge  -->  NoneBot2 (bot.py) 与各插件
```

桥接层随机器人一起加载，所以 `QQ_BOTS` 必须设置。

## 部署步骤

1. 在 [QQ 开放平台](https://q.qq.com/) 注册机器人，拿到 AppID、Token 和 AppSecret，然后在平台上：
   - 把服务器的公网 IP 加进 IP 白名单，不然调用接口会被拒绝；
   - 机器人审核上线之前，只能进沙箱配置里指定的测试群；上线之后就可以把它拉进别的群。

   机器人用 WebSocket 连平台，收群聊和单聊消息的订阅已经写在第 4 步 `QQ_BOTS` 的 `intent` 里。
   平台的界面和规则会变，以 QQ 开放平台的说明为准。

2. 装系统依赖，建好目录

   ```bash
   sudo apt update
   sudo apt install -y python3.12-venv fonts-wqy-zenhei git
   sudo install -d -m 700 -o "$(id -un)" -g "$(id -gn)" /opt/priestess-qqbot
   ```

   后面每一步都用同一个普通用户执行，不要用 root。systemd 服务也用这个用户运行。

3. 取代码，装 Python 依赖和出图用的浏览器

   ```bash
   git clone https://github.com/Billtranscend/Priestess-QQbot.git /opt/priestess-qqbot
   cd /opt/priestess-qqbot
   python3.12 -m venv .venv
   .venv/bin/pip install -r requirements.txt -r requirements-official.txt -c requirements.lock
   sudo .venv/bin/playwright install-deps chromium   # Chromium 需要的系统库
   .venv/bin/playwright install chromium             # 浏览器本身，不要加 sudo
   ```

   `requirements.lock` 是我的线上机器人正在用的全部包版本。安装时一直带着 `-c requirements.lock`，不要单独升级其中的包。

   浏览器一定要像上面这样自己装。交给 nonebot-plugin-htmlrender 在第一次启动时去装是会失败的，机器人随即退出，
   日志里是 `Playwright environment is not set up correctly`。
   浏览器装在执行最后一条命令的那个用户的 `~/.cache/ms-playwright` 下，手动启动和 systemd 服务都要用这个用户。

4. 写配置

   ```bash
   cp .env.example .env
   touch .env.prod
   chmod 600 .env .env.prod
   ```

   - 在 `.env.prod` 里填 `QQ_BOTS`，写法照 `.env.example` 里的那一行，把 AppID、Token、AppSecret 换成你的。这个文件不要提交。
   - `.env` 里把 `ONEBOT_ACCESS_TOKEN` 换成一串随机字符。
   - `SUPERUSERS` 先空着，第 6 步再填。
   - 其他项保持 `.env.example` 的写法就行，每项的意思见下面的 [配置](#配置)。

   列表类的配置里，号码要加引号写成字符串，例如 `["123456789"]`。写成 `[123456789]` 启动时可能报错。

5. 初始化数据库，第一次启动

   ```bash
   .venv/bin/python deploy/orm_cli.py upgrade
   .venv/bin/python bot.py
   ```

   第一次启动要下载森空岛资源和 AKEData 的数据表，要等几分钟。日志里出现 `official bridge: <AppID> connected`，就是连上平台了。
   把机器人拉进一个群，上线前只能是沙箱里的测试群，然后 @机器人 发 `/skl帮助` 试一下。

   连上以后还要等 3 分钟，日志里出现 `official bridge: scheduled pushes now go through the official bot`，
   定时推送、发给超级用户的私聊提醒，还有绑定后抽卡记录拉完的那条回复才发得出去。以后每次启动、和平台断线重连之后都是这样。
   这 3 分钟里指令照常能用，只是这时绑定的话，那条回复多半收不到。想试绑定，等这一行出来再发 `/skl绑定`。

6. 把自己设成超级用户

   平台不给 QQ 号。桥接层给每个成员分一个从 90 亿起的编号，你自己也一样，所以 `SUPERUSERS` 里填 QQ 号没有用。

   第 5 步你在群里发 `/skl帮助` 的时候，运行 `bot.py` 的终端里有这样一行：

   ```
   [message.group.normal]: Message … from 9…@[群:8…]
   ```

   `from` 后面、`@` 前面那个 9 开头的数字就是你的编号。把它填进 `.env`，例如 `SUPERUSERS=["<你的编号>"]`，按 Ctrl+C 停掉机器人再启动，管理指令就能用了。
   注册成服务之后，这样的日志用 `journalctl -u nonebot` 看。

   机器人上线以后，也可以加它为好友，私聊发一句话，日志里是 `[message.private.friend]: Message … from 9…`，`from` 后面的数字一样是你的编号。
   超级用户本来就要加机器人为好友，私聊的提醒才收得到。

   编号存在 `data/official_bridge/idmap.sqlite3` 里。这个文件还在、机器人的 AppID 没换，同一个人的编号就不会变，
   所以它要和数据库一起备份。要重建这个文件或者换一个机器人，先把 `SUPERUSERS` 清空：编号会重新分配，
   第一个说话的人可能拿到你原来的编号，变成超级用户。

7. 注册成系统服务

   终端里手动启动的机器人按 Ctrl+C 停掉，先把示例复制过去：

   ```bash
   sudo cp deploy/nonebot.service.example /etc/systemd/system/nonebot.service
   sudo nano /etc/systemd/system/nonebot.service
   ```

   在复制出来的那份里按注释改：User 和 Group 填装浏览器的那个用户，没装在 `/opt/priestess-qqbot` 的把路径也换掉。
   仓库里的示例不要直接改，改了以后 `git pull` 可能会被拒绝。改好之后：

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now nonebot
   ```

以后怎么更新、怎么重启，见首页的 [更新与重启](../README.md#更新与重启)。

## 群主需要做的

把机器人拉进群，然后在群成员列表里点开机器人，进「设置」打开两项：

- 打开「机器人主动在群聊内发言」：活动通知、资料库更新通知这些推送要靠它。
- 「机器人可获取的群聊消息范围」选「获取群内全部消息」：不 @ 机器人发指令、在指令里 @ 别人，都要靠它。
  另外两档「获取@机器人的最近10条消息」「仅获取@机器人的消息」下，机器人只收到 @ 它的消息。

不打开也能 @ 机器人用指令，只是收不到推送。机器人被拉进群时，会在群里把这两项说明一次，下面带「查看帮助」「绑定账号」两个按钮。
之后不再重复，已经在的群也不会补发。

## 在群里发指令

在群里发指令前，要真正 @ 机器人：输入 @ 之后从列表里点选。复制来的或者手打的「@机器人名」不算。
群主把「机器人可获取的群聊消息范围」设成「获取群内全部消息」的群，可以不 @。

## 快捷按钮

菜单类的回复下面带一排按钮，点一下就等于你自己 @ 机器人发了那条指令。
按钮不能和图片放在同一条消息里，出图的回复会在图后面另发一条按钮消息。
按钮是挂在 markdown 消息下面发的。平台不让发的时候，比如机器人没有 markdown 或按钮的权限，菜单改成文字；
`/zmd抽卡记录` 直接出第一类卡池的图，其他几类写在图后面。

按钮消息由 `plugins/quick_buttons.py` 统一撤回。提问的人点了其中一个，回答发出后就撤回这个菜单，分几条发的一起撤。
群里没人点的，90 秒后撤回，太久的消息平台不让撤。进群说明这种不是回答指令的按钮不撤。

带按钮的地方：

- `/zmd抽卡记录`：先只给出选项，也就是有记录的各类卡池和「拉取最新记录」，点了哪一类才出哪一类的图。
  `/zmd抽卡记录更新` 拉完也是先给选项，并说明新增了几条。
- `/mrfz抽卡记录`：一次一张图，按卡池开放的年份分页，一年里卡池太多时再按月份切开，用按钮翻页。
  `/mrfz抽卡记录更新` 拉完从最新的一张开始，并说明新增了几条。
- `/卫`：查自己时，图下面是全部记录、收藏、单局详情这些选项。`/卫 全部` 一次一页，用按钮翻页。
- `/竞速榜`、不带名称的 `/丰碑竞速`、`/影拓丰碑`、`/战争回响`：菜单里的各项。竞速榜下面还有「更新我的记录」和竞速榜目录。
- `/干员统计`：换统计范围。
- `/活动`、`/日历`：不指明游戏时，先给出明日方舟：终末地、明日方舟、一起看三个选项，点了才出图。单款游戏的日历下面不带按钮。
- 集成战略战绩：其他主题。`/方舟干员`：各职业和六星。
- `/skl帮助`：不直接发图，先给出各分类、「使用须知」和「完整帮助图」的按钮，自动任务只在帮助图里。
  点分类得到这一类指令的文字版，点「完整帮助图」才发图，上传要几秒。`/帮助 分类名` 直接回复文字版。
- `/skl角色`：有多个角色的游戏，图下面给出其他角色，点一下设为默认。两款游戏都有多个角色时，先选游戏。
  `/skl切换终末地角色`、`/skl切换方舟角色` 不带序号时，也直接给出角色按钮。
- 叫你接着发 `/skl绑定`、`/skl添加账号`、`/skl角色更新`、`/zmd抽卡记录更新`、`/mrfz抽卡记录更新` 的回复，
  比如没绑定、登录失效、还没有抽卡记录，下面带这些指令的按钮。

有人加机器人为好友时，机器人发一句欢迎语，下面带 `/skl绑定`、`/抽卡记录`、`/帮助` 三个点一下就能发送的指令。

## 平台的限制

插件都做了兼容，但有些地方和普通 QQ 号不一样：

- 平台不给 QQ 号和群号，只给加密的标识。桥接层给每个成员和群分一个编号，成员从 90 亿起，群从 80 亿起。
  编号不是 QQ 号，也不会重复分配。
- 没有群成员列表接口。榜单上的「本群」标记和群里的统计，按机器人在这个群里见过的成员算。
- 每周体检里「本人既不在机器人任何群里、也不是机器人好友，就解绑」这一项，要等原来的 NapCat 账号退出所有群才开始，
  所以只在和 NapCat 并存过的部署上有。从零部署的机器人上这一项会跳过，其他检查照常。
  跳过时，体检报告里会有一行『群成员名单或好友列表没能完整读取：本次没有按「已退群」解除任何账号』，
  超级用户因此每周一都会私聊收到一份体检报告，发 `/森空岛体检` 也会看到这一行。
  日志里还有一条 `official bridge: get_group_list failed` 的警告，下面跟着报错堆栈。这些都是正常的，不用处理。
- 一条消息只能带一张图片或一个文件，没有合并转发卡片、表情回应和精华消息。好几页的结果里，文字页合成尽量少的几条消息，
  每条最多 2000 字，每页前面写上这一页的名字；图片逐张发。
- 主动消息有上限。我的机器人是每个群每天大约 1000 条，别的机器人以平台的说明为准。
- @全体成员 要用平台的标签单独发：机器人得是这个群的管理员，每个群每天一次。不满足时消息照常发出，只是不 @全体。

## 群名与机器人已不在的群

- 平台不给群号，但给群名。机器人进群时和每天北京时间 0 点，桥接层向平台查一遍各群的群名，Bison 管理页这些地方列出的群就用这个名字，
  群改名后第二天跟上。超级用户发 `/群检查` 可以马上查一遍。
- 机器人被移出的群，以及平台说已经不存在的群，桥接层马上删掉它的全部记录，并像被移出时一样通知各插件。
  之后再被拉进来，就是一个新群。

## 帮助图

`/skl帮助` 的帮助图由 `plugins/skl_help/help.html` 生成，机器人启动时按你的配置自动出图。图里有「使用须知」，讲怎样 @ 机器人。
图里「@机器人」处显示的名字由 `.env` 的 `HELP_BOT_NAME` 决定，默认是「机器人」。搭配了 nonebot-bison 的，设 `HELP_BISON=true`，图里会列出 Bison 的订阅指令。
`/skl帮助` 先回复按钮和文字版，文字也是从同一份 `help.html` 读出来的，见 [快捷按钮](#快捷按钮)。

改了 `.env` 里的这些设置要重启才生效，启动时会自动重新出图。

只改了 `help.html` 的话，也可以不重启：用装浏览器的那个用户运行 `.venv/bin/python deploy/render_skl_help.py`，马上生效。
这个脚本代替不了改设置之后的重启。运行中的机器人还按启动时的设置，下次发帮助图时会按旧设置重新出图，把新图盖掉。

## 配置

除了首页 [配置](../README.md#配置) 里说的时区，用到的项如下。写法见 `.env.example`。

| 配置项 | 说明 |
|---|---|
| `QQ_BOTS` | AppID、Token、AppSecret 和事件订阅。写在 `.env.prod`，不要提交 |
| `DRIVER` | 保持 `~fastapi+~httpx+~websockets` |
| `QQ_API_BASE` | `https://api.bot.qq.com/` |
| `SUPERUSERS` | 管理员的编号，见部署步骤第 6 步 |
| `ONEBOT_ACCESS_TOKEN` | 一串随机字符。机器人总会在 `HOST:PORT` 上开一个 OneBot v11 的入口，设了它别人就连不上 |
| `HELP_BOT_NAME` | 帮助图里「@机器人」处显示的名字 |
| `HELP_BISON` | 搭配了 nonebot-bison 时设为 `true`，帮助图里列出 Bison 的订阅指令 |
| `OFFICIAL_BRIDGE_MENTION_MARKUP` | 设为 `true` 时，群里的 @ 是真正的 @，能点，对方也有提醒。带 @ 的文字改用 markdown 发，图片上的 @ 挪到后面的按钮消息或者单独一条。机器人要有 markdown 权限。不设时 @ 只是文字 |
| `OFFICIAL_BRIDGE_ONEBOT_CLIENTS` | 经官方机器人服务的其他 OneBot v11 程序，见下一节 |
| `ENVIRONMENT`、`LOG_LEVEL`、`COMMAND_START`、`LOCALSTORE_USE_CWD`、`HOST`、`PORT`、`APSCHEDULER_CONFIG` | 和 `.env.example` 保持一致。`ENVIRONMENT=prod` 让机器人读 `.env.prod`，不要删。`LOG_LEVEL` 不要调高，调高了部署步骤里要找的那几行日志就没了，`deploy/restart_when_idle.py` 也看不出有没有指令在处理 |

## 搭配 nonebot-bison

[nonebot-bison](https://github.com/MountainDash/nonebot-bison) 单独运行，自己监听一个 OneBot v11 的反向 WebSocket 地址。
在 `.env` 设置 `OFFICIAL_BRIDGE_ONEBOT_CLIENTS`，填上这个地址和 Bison 那边的 access token，桥接层就会连过去，Bison 的推送经官方机器人发出。
没和 NapCat 并存过的机器人启动后过 3 分钟才去连，到时日志里有一行 `official bridge relay: connecting the other OneBot programs to the official bot`。

Bison 那边不用为官方机器人改别的，只有它自己的 `SUPERUSERS` 要注意：Bison 看到的成员也是桥接层的编号，
要填和这个机器人 `.env` 里 `SUPERUSERS` 一样的号码。从零部署的就是第 6 步查到的编号，不是 QQ 号。
设 `HELP_BISON=true` 后，帮助图里会列出 Bison 的订阅指令。

各群的订阅由群主和管理员用 Bison 自带的指令管理，比如 `@机器人 /添加订阅`。Bison 默认不给新群添加订阅。
Bison 自己不会在机器人退群时删订阅，退群后去 Bison 的后台删。

## 常见问题

**群里发指令没有反应。** 要真正 @ 机器人：输入 @ 之后从列表里点选，复制来的或者手打的「@机器人名」不算。
群主把「机器人可获取的群聊消息范围」设成「获取群内全部消息」之后，可以不 @。

**收不到活动通知、资料库更新通知。** 群主要在群的机器人设置里打开「机器人主动在群聊内发言」。

**管理指令没有反应。** `SUPERUSERS` 里要填桥接层分配的编号，不是 QQ 号，见部署步骤第 6 步。

**日志里一直没有出现 `connected`。** 先查平台上的 IP 白名单，再查 `.env.prod` 里的 `QQ_BOTS` 和 `.env` 里的 `DRIVER`。

**第一次启动就退出，日志里有 `Playwright environment is not set up correctly`。** 浏览器没装，或者运行机器人的用户
不是装浏览器的那个用户。用运行机器人的用户，重做部署步骤第 3 步的最后两条命令。

更多问题见首页的 [常见问题](../README.md#常见问题)。

[返回首页](../README.md)

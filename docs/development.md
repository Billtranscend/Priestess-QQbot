# 开发说明

想改代码、或者想知道代码怎么组织的，看这一页。部署看 [部署](deploy-official.md)。

- [架构](#架构)
- [目录结构](#目录结构)
- [上游插件的校验](#上游插件的校验)
- [修改帮助图](#修改帮助图)
- [图片清晰度与体积](#图片清晰度与体积)
- [测试](#测试)

## 架构

```
QQ  <-->  QQ 开放平台  <-- WebSocket / HTTP -->  nonebot-adapter-qq  -->  plugins/official_bridge
                                                                              |
                                                       OneBot v11 事件与接口  |
                                                                              v
                                                         NoneBot2 (bot.py, systemd)
                                                           +-- 上游插件: nonebot-plugin-skland ...
                                                           +-- 本地插件: plugins/
                                                           +-- SQLite (data/)，无头 Chromium 出图
```

插件都是按 OneBot v11 写的。`plugins/official_bridge` 把官方平台的事件转成 OneBot v11 事件交给插件，
再把插件调用的 OneBot 接口换成平台的接口，插件不用知道自己跑在官方机器人上。
桥接层还能替其他 OneBot v11 程序连官方机器人，我用它接 [nonebot-bison](https://github.com/MountainDash/nonebot-bison)，见 `relay.py`。
桥接层各文件的分工写在 `official_bridge/__init__.py` 开头。

`bot.py` 注册官方适配器、加载桥接层，同时注册 OneBot v11 适配器：插件和桥接层的机器人都按它写，`relay.py` 也用它。
所以 `QQ_BOTS` 必须设置。

插件里和桥接层打交道的地方不多：

- `plugins/quick_buttons.py`：按钮放不下或被平台拒收时，改发调用方给的文字（`fallback`）。
  按钮菜单和带 `recall_text` 的文字菜单的撤回也在这里。
- 少数插件用 `sys.modules` 查找桥接层，从不直接导入它：`skland_bind_sign` 私聊扫码认回绑定，`skland_health` 迁移期间的等待名单，
  以及 `endfield_guide` 收集战绩和配装时，一个人绑着几个号只算一次，记在官方机器人认的那个号下。
- `plugins/skl_help/help.html` 里用注释标出只在某种情况下出现的内容，见 [修改帮助图](#修改帮助图)。

## 目录结构

```
bot.py                         入口，按顺序加载插件
requirements.txt               直接依赖，固定版本
requirements-official.txt      官方机器人要的依赖：nonebot-adapter-qq 和客户端驱动
requirements.lock              线上机器人正在用的全部包版本，安装时用 -c 约束
.env.example                   配置示例
docs/                          部署、迁移和开发说明，README 用到的截图
deploy/
  nonebot.service.example      systemd 单元示例
  render_skl_help.py           马上重新生成帮助图（机器人启动时也会按需生成）
  orm_cli.py                   数据库迁移命令行
  restart_when_idle.py         等到没有指令在处理、也没有战绩收集、卫戍榜统计、账号体检或夜间签到在进行时，再重启服务
  restore_health_backup.py     把账号体检删掉的账号、角色和抽卡记录从备份写回数据库（先核对，加 --apply 才写）
plugins/
  official_bridge/             QQ 官方机器人桥接层；coexist/ 是和 NapCat 账号并存时才用的部分
  quick_buttons.py             快捷按钮；按钮发不出时的文字；按钮菜单和带 recall_text 的文字菜单的撤回
  friend_welcome/              加好友时的欢迎语和可点的指令，机器人进群时的设置说明；/抽卡记录 问看哪一款游戏
  ef_theme/                    共用的终末地风格样式和字体
  ark_theme/                   共用的明日方舟风格样式
  endfield_wiki/               终末地资料库（AKEData 同步、检索、资料卡）
  endfield_guide/              高难关卡资料、竞速榜、干员统计
  endfield_roster/             终末地角色卡（/zmd开盒）
  arknights_roster/            明日方舟角色卡（/mrfz开盒）
  arknights_garrison/          卫戍协议战绩（/卫，数据用上游的 GarrisonView，图自己画）和卫戍榜（每天 10:30 统计，结果在 data/arknights_garrison/board.json）
  skland_gacha_rank/           两款游戏的欧非榜（全部绑定用户一起排名，统计结果缓存在 data/skland_gacha_rank/ 下的 snapshot.json 和 arknights_snapshot.json）
  heybox_import/               从小黑盒导入早期抽卡记录
  weekly_report/               本周任务（/本周任务，发指令时生成）
  game_calendar/               两款游戏的活动日历和活动开启通知
  skland_health/               账号体检；每晚签到和森空岛请求的超时重试；backup.py 是删除前的备份（backups/skland-health/ 下只追加的 .jsonl）；
                               dormant.py 是迁移期间未认出成员的暂停和 45 天后解绑
  skland_sanity/               /理智 查询
  skl_help/                    帮助图：help.html 按配置生成图片（render.py）；分类按钮和 /帮助 分类名 的文字版（guide.py）
  skland_ef_theme/             抽卡记录等页面的终末地风格模板，上游图片模板的小修正
  skland_compact_images/       上游插件渲染的图片统一转成 WebP，超长页面适当缩小；同时出图的数量有上限
  render_image_cache/          渲染页面里的游戏图片（头像、图标、横幅）存到本地，下次直接读，出图更快
  skland_ark_assets/           上游明日方舟页面的外部图片改用本地缓存，缺的先补齐，免得外站慢时出图超时
  skland_auto_gacha/           终末地抽卡记录每天自动同步
  skland_ef_pools/             用 AKEData 的数据生成终末地卡池表；全新部署第一次启动、还没有表时，先用上游从 GitHub 下载的表
  skland_ark_gacha/            明日方舟抽卡记录：按年份分页看已保存的记录、手动更新、每天自动同步
  skland_bind_sign/            绑定规则和保护、/skl添加账号、绑定后的签到和抽卡同步通知；私聊扫码认回原有绑定
  skland_buttons/              给上游的肉鸽战绩、/方舟干员、/skl角色 加快捷按钮
  skland_index_tolerance/      选账号序号时接受「01」和全角数字
  skland_roles.py              取成员默认角色的公共查询
  skland_pools.py              上游没有收录的卡池类型（重构寻访）、游戏卡池页的内容接口地址
  skland_limited.py            限定池的 UP 判定和 UP 平均（抽卡记录和欧非榜共用）
  sqlite_foreign_keys/         给 SQLite 打开外键约束，解绑时角色和抽卡记录跟着账号一起删
  log_privacy/                 日志不记聊天内容，只留消息编号、发送者和群号
  skland_shortcuts/            指令别名，上游提示语改成这里的指令名；没绑定、没有默认角色时说明接着发什么
  skland_resource_resilience/  上游资源下载的超时和重试；启动时先读本地数据，资源检查放到启动以后
  command_tolerance/           指令进来前统一写法：去掉开头的文字版「@机器人」、「角色2」换成 -r 2、去掉末尾的空白
  temp_session_forward/        和 NapCat 账号并存时，群临时会话里触发的合并转发只发给本人（随 coexist/ 一起删）
  forward_reply_owner/         群里不发上游「…过多，将以多张图片形式发送」这类预告
  strict_command.py            指令严格匹配的规则
  shared.py                    共用的小工具：原子写 JSON、取当前的 OneBot 机器人、私聊通知超级用户、
                               机器人离开群的判断、群里没开主动发言的拒收判断
local_plugins/                 只在本机用的插件：放进来的包会被 bot.py 自动加载，目录不进仓库（可选）
```

运行时生成、不进仓库的目录：`data/`（数据库和各插件的数据）、`cache/`、`config/`、`backups/`（账号体检的备份）。

## 上游插件的校验

好几个本地插件是包着上游函数工作的，启动时会核对上游文件的 SHA-256，所以 `requirements.txt` 固定了 nonebot-plugin-skland 的版本。
升级上游后要是对不上，对应的插件会拒绝启动，或者退回上游原来的行为。这时要重新看过上游的改动，再更新代码里的校验值。

间接依赖也要固定。`skland_shortcuts`、`forward_reply_owner`、`temp_session_forward` 这些依赖 nonebot-plugin-alconna 的内部行为，
版本对不上时可能启动就报错，也可能悄悄失效。`requirements.lock` 是线上机器人正在用的完整版本表，安装时带上 `-c requirements.lock`。
nonebot-plugin-htmlrender 固定在 0.6.7，不要升级。

## 修改帮助图

帮助图来自 `plugins/skl_help/help.html`。只在某种情况下出现的内容用注释包起来，生成时按配置保留或去掉：

| 标记 | 保留的条件 |
|---|---|
| `<!--migration-->…<!--/migration-->` | `HELP_MIGRATION_NOTE=true` |
| `<!--bison-->…<!--/bison-->` | `HELP_BISON=true`。Bison 的指令不属于这个项目，没装 Bison 时不应该出现 |

文中的 `BOT_NAME` 会换成 `.env` 的 `HELP_BOT_NAME`，默认「机器人」。机器人启动时比较这次生成的页面和上次的是否一样，
不一样才重新出图，所以改完重启就行。

只改了 `help.html` 时，可以不重启，用装浏览器的那个用户运行 `.venv/bin/python deploy/render_skl_help.py` 马上生成。
加一个文件路径参数，就只输出到那个文件，不影响机器人在用的图。改了 `.env` 里的设置必须重启：
运行中的机器人按启动时的设置判断，会把脚本按新设置生成的图再盖回去。

`/skl帮助` 的分类按钮和 `/帮助 分类名` 的文字版，都由 `guide.py` 从生成后的页面结构读出来，不另写一份。
分区是 `ef-sec`（`auto`、`warn`、`sec`），标题是其中的 `h3` 和 `small`，指令表的单元格类名是 `cmd`、`fn`、`note`，
说明文字在 `div.colh`、`div.dim`、`div.alias` 里。`div.alias` 里的 `div.legend` 是表格各列的说明，只留在图片里。
改版式时保留这些类名，不然文字版会缺内容。

## 图片清晰度与体积

所有图片都用 WebP 发送，同样清晰时大约是 JPEG 的四成大小。上传速度随时段变化很大，图越大等得越久。
清晰度和体积在这几处调：

- 自己画的页面：`plugins/ef_theme/__init__.py` 的 `PAGE_SCALE` 和 `PAGE_QUALITY`
- `/zmd开盒`、`/mrfz开盒`：`plugins/endfield_roster/__init__.py`、`plugins/arknights_roster/__init__.py` 顶部的常量
- 上游渲染的图片：`plugins/skland_compact_images/__init__.py` 的 `QUALITY`

## 测试

这个仓库不带测试，也没有 CI，没有任何自动检查。

改了代码要自己验证：先在测试群里把改动涉及的指令真的发一遍。

[返回首页](../README.md)

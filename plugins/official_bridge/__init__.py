"""不改写现有插件，让机器人跑在 QQ 官方机器人平台上。

官方机器人（`nonebot.adapters.qq`，AppID + AppSecret，不用登录 QQ 客户端，也就不会被风控）用的是
另一套 API，看到的是 OpenID 而不是 QQ 号和群号。本插件给它套上一层 OneBot v11 外壳：每个官方事件
都转成对应的 OneBot 事件交给现有的处理函数，它们发出的每个 OneBot API 调用都通过官方 HTTP API 执行
（见 bridge.py）。编号来自编号映射表（idmap.py）。

bot.py 总是加载本插件。各文件分工：

- 本文件：接收官方事件，决定定时推送走哪个机器人，`/群检查`（群名、清除已解散的群）
- bridge.py：OneBot v11 外壳本身，一头转事件，一头转 API 调用
- idmap.py：OpenID 与编号的对应、群的关联、保存的成员名单
- relay.py：通过官方机器人为其他 OneBot v11 程序（nonebot-bison）服务
- identity.py：后来才被认出的成员保留之前的绑定；成员证明自己是谁（认领）
- state.py：以上各文件共用的东西
- coexist/：只因原 OneBot 机器人（一个登录着的 QQ 账号）和官方机器人在同一进程里并行才存在的
  一切；两者怎么配合在那里说明。与原机器人无关的新代码不放进去，只在原机器人在场时才有意义的
  代码也不放到别处。

运行方式：

- 原机器人的客户端连着的其他 OneBot v11 程序（nonebot-bison），在桥接机器人接手发推送后改由桥接
  去连接：地址和 token 不变（OFFICIAL_BRIDGE_ONEBOT_CLIENTS），见 relay.py。原机器人下线后自动
  改连。
- 谁已经退出了所有群（每周森空岛检查会给他们解绑）按最新名单判断：用 no_cache 查桥接机器人的
  群列表，得到两个机器人所在的全部群；用 no_cache 查成员名单，原机器人在的群得到原机器人看到的
  名单；只有官方机器人待过的群，用官方机器人见过的成员；原机器人已退出的群，用见过的成员加上
  最终保存的名单。原机器人离线而仍有只归它的群时，或某个群有原群号却没有最终名单时，拒绝回答
  这个问题，也不给任何人解绑。例外只有一种：原机器人已被确认不在任何群，而它在那个群从没留下过
  名单，这时按只有官方机器人待过的群回答。
- 为原机器人离开群之后准备成员名单：每天把原机器人看到的每个已关联群的完整成员名单存进编号映射表
  （`roster`）。在原机器人已离开的群里，成员名单（排行榜、周报）是官方机器人见过的成员加上最后
  保存的那份名单。看到原机器人退群（或被移出）时，保存的名单标记为最终名单，此后它也用来回答每周
  检查的问题：既不在这份名单上、也从没在任何地方被官方机器人见过的人，不在它的任何群里。
- 合并转发卡片（多页的回复）没有对应物：各页依次发送，每条消息最多带一张图。设置了
  OFFICIAL_BRIDGE_FORWARD_STITCH_HEIGHT 时，只含图片的卡片改为拼成不超过该像素高度的长图
  （默认关闭）。
- 占位编号不会重复分配；成员在自己的占位编号下绑定的东西（桥接还不知道其 QQ 号时绑的森空岛账号），
  在他被认出的那一刻就改为指向用户表里其 QQ 号的身份。
- 原机器人离开前没能被认出的成员可以自己证明身份：见认领（claim_number），成员在私聊里登录自己的
  森空岛账号时由 plugins/skland_bind_sign 调用。
- 回复只挂在被回复成员自己的消息上（作为“被动”消息）；平台不会替插件 @ 这位成员，
  插件写的 @ 照样发。推送不挂在任何消息上。
- 只要有 OneBot 机器人连着，定时推送就仍由它发。没有 OneBot 机器人连着几分钟之后，桥接机器人
  出现在 `get_bots()` 里，推送改由它发出。
  OFFICIAL_BRIDGE_PRIMARY=true 让桥接机器人在原机器人还在时就排到前面：定时任务先找到它，它的
  群列表是两个机器人所在的全部群，每个调用都交给能执行它的一方。发往官方机器人所在群的内容由官方
  机器人发出；官方机器人不在的群、没加它好友的人的私聊，以及只有 QQ 客户端才能回答的一切（成员名单、
  群名、精华消息、历史消息）都交给原机器人。

配置（.env）：QQ_BOTS 供适配器使用，另有可选的 OFFICIAL_BRIDGE_PRIMARY、
OFFICIAL_BRIDGE_SELF_ID（默认为 AppID）、OFFICIAL_BRIDGE_SHADOW_UNLINKED（默认 true）、
OFFICIAL_BRIDGE_AUTO_LINK（默认 true）、OFFICIAL_BRIDGE_TRUSTED_ADDERS（SUPERUSERS 和原机器人之外
的 QQ 号）、OFFICIAL_BRIDGE_MENTION_MARKUP、OFFICIAL_BRIDGE_MEMBERS_AUTHORITATIVE、
OFFICIAL_BRIDGE_ONEBOT_CLIENTS（[{"url": "ws://...", "access_token": "..."}]）、
OFFICIAL_BRIDGE_RELAY_ALWAYS（默认 true：原机器人也连着时，其他 OneBot 程序仍由官方机器人服务）、
OFFICIAL_BRIDGE_UINS（成员看到的官方机器人 QQ 号，例如它资料卡上的号码）。
"""

from __future__ import annotations

import asyncio
import calendar
import sys
import time

from nonebot import get_bots, logger, on_command
from nonebot.adapters import Bot as AnyBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.adapters.qq import Adapter as QQAdapter
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import event as qe
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from .bridge import AUTHOR_EXTRAS, Bridge, BridgeBot, remember_author
from .idmap import GROUP_BASE
from .relay import wire
from .state import background, bridges, official_uins, online, config, driver, idmap, legacy_bots, relay, strict
# 供其他地方调用：claim_number、claimable、served_as_placeholder 由 plugins/skland_bind_sign 调用，
# recognised 由 plugins/skland_health 调用；carry_plan 只在 identity.py 里用到。
from .identity import carry_plan, claim_number, claimable, recognised, served_as_placeholder  # noqa: F401  （见上）

# 本文件从两个机器人并行运行时期的代码里用到的东西（见 coexist/）。
from .coexist.ears import ears_for
from .coexist.handshake import is_first_handshake, look_for_groups, read_handshake, recent_adds
from .coexist.hearing import hear, identify, note_said
from .coexist.presence import legacy_gone_at as old_bot_gone_at  # noqa: F401  （plugins/skland_health 会查询）
from .coexist.presence import legacy_group_cache, legacy_in_group, legacy_left_for_good, in_shadow
from .coexist.sends import legacy_census, legacy_group_list, legacy_role, legacy_say, route

__plugin_meta__ = PluginMetadata(
    name="Official bot bridge",
    description="不改写现有插件，让机器人跑在 QQ 官方机器人平台上。",
    usage="/群检查",
)

def bridge_for(bot: QQBot) -> Bridge:
    bridge = bridges.get(bot.self_id)
    if bridge is None:
        bridge = bridges[bot.self_id] = Bridge(
            driver, idmap, bot, config.official_bridge_self_id or bot.self_id,
            mention_markup=config.official_bridge_mention_markup,
            members_authoritative=config.official_bridge_members_authoritative,
        )
    bridge.qq = bot
    bridge.on_sent = note_said
    bridge.route = route
    bridge.other_groups = legacy_group_list if config.official_bridge_legacy_sends else None
    bridge.census_groups = legacy_census
    bridge.old_bot_gone_at = old_bot_gone_at
    bridge.other_role = legacy_role
    bridge.stitch_height = config.official_bridge_forward_stitch_height
    bridge.speak_for = legacy_say if config.official_bridge_legacy_sends else None
    return bridge

@driver.on_bot_connect
async def _official_connected(bot: AnyBot) -> None:
    if not isinstance(bot, QQBot):
        return
    bridge = bridge_for(bot)
    logger.info(f"official bridge: {bot.self_id} connected, serving as OneBot {bridge.bot.self_id}")
    # 这里没有任何代码是针对官方适配器的 bot 写的，取“那个”机器人的代码（get_bot()、不限类型的
    # 连接钩子）也不能拿到它：只登记桥接机器人。
    if driver._bots.get(bot.self_id) is bot:
        del driver._bots[bot.self_id]
    online.add(bot.self_id)
    _settle_primary()
    look_for_groups()


@driver.on_bot_disconnect
async def _official_disconnected(bot: AnyBot) -> None:
    if not isinstance(bot, QQBot):
        return
    online.discard(bot.self_id)
    bridge = bridges.get(bot.self_id)
    if bridge is not None:
        _unlist(bridge)


def _unlist(bridge: Bridge) -> None:
    if bridge.bot.self_id not in bridge.adapter.bots:
        return
    # 桥接机器人以 AppID 为号时，它登记的键正是驱动器为官方机器人清除的那个键；无论哪种情况，
    # 都把它从列表中移除并运行它的断开连接钩子。
    bridge.adapter.bots.pop(bridge.bot.self_id, None)
    if get_bots().get(bridge.bot.self_id) not in (None, bridge.bot):
        return
    driver._bot_disconnect(bridge.bot)

# ---------------------------------------------------------------- 定时推送由谁发

PRIMARY_GRACE = 180.0  # 没有 OneBot 机器人连着多少秒后，由桥接机器人接手推送


def _wants_primary() -> bool:
    if config.official_bridge_primary is not None:
        return config.official_bridge_primary
    return not legacy_bots()


def _sync_primary() -> None:
    """把桥接机器人登记进 get_bots()（定时任务随后经它发送），或者把它移除。"""
    want = _wants_primary()
    for qq_id, bridge in bridges.items():
        listed = get_bots().get(bridge.bot.self_id) is bridge.bot
        if want and qq_id in online and not listed and bridge.bot.self_id not in get_bots():
            covered = ", the old bot covering where it is absent" if legacy_bots() and config.official_bridge_legacy_sends else ""
            logger.info("official bridge: scheduled pushes now go through the official bot" + covered)
            bridge.adapter.bot_connect(bridge.bot)
        elif listed and not (want and qq_id in online):
            logger.info("official bridge: scheduled pushes are back with the OneBot bot")
            _unlist(bridge)
    _front()
    _sync_relay()


def _front() -> None:
    """让桥接机器人排在 get_bots() 第一位：取“那个”OneBot 机器人的代码取的都是第一个。"""
    bots = driver._bots
    mine = {key: bot for key, bot in bots.items() if isinstance(bot, BridgeBot)}
    if mine and next(iter(bots)) not in mine:
        rest = {key: bot for key, bot in bots.items() if key not in mine}
        bots.clear()
        bots.update(mine)
        bots.update(rest)

def _settle_primary() -> None:
    """接手前先等过原机器人的重启和重连；交还则立即进行。

    已知原机器人不在任何群里时不等：没有要等的对象，否则每次重启都会让推送和其他 OneBot 程序
    在这段时间里没有机器人可用。
    """
    if legacy_bots() or config.official_bridge_primary is False or legacy_left_for_good():
        _sync_primary()
    else:
        asyncio.get_running_loop().call_later(PRIMARY_GRACE, _sync_primary)

@driver.on_bot_connect
async def _legacy_connected(bot: AnyBot) -> None:
    if isinstance(bot, OneBot) and not isinstance(bot, BridgeBot):
        legacy_group_cache.clear()
        _sync_primary()
        look_for_groups()


@driver.on_bot_disconnect
async def _legacy_disconnected(bot: AnyBot) -> None:
    if isinstance(bot, OneBot) and not isinstance(bot, BridgeBot):
        legacy_group_cache.clear()
        _settle_primary()

_relay_forced = False


def _sync_relay() -> None:
    """其他 OneBot 程序由负责发推送的那个机器人服务：原机器人的客户端，或本桥接。"""
    listed = any(get_bots().get(b.bot.self_id) is b.bot for b in bridges.values())
    want = _relay_forced or (listed and (config.official_bridge_relay_always or not legacy_bots()))
    if want and not relay.active and relay.clients:
        logger.info("official bridge relay: connecting the other OneBot programs to the official bot")
        relay.start()
    elif not want and relay.active:
        logger.info("official bridge relay: the other OneBot programs are left to the old bot's client again")
        relay.stop()

@driver.on_shutdown
async def _close() -> None:
    relay.stop()
    idmap.close()

# ---------------------------------------------------------------- 接收官方事件


_hears_all: set[str] = set()  # 已知会推送全部消息的群（编号映射表里也有记录）

async def _on_official_event(bot: QQBot, event: qe.Event) -> None:
    bridge = bridge_for(bot)
    if isinstance(event, qe.QQMessageEvent) and not bridge.first_sight(event.id):
        return
    if isinstance(event, qe.GroupAddRobotEvent):
        recent_adds[event.group_openid] = time.monotonic()
        look_for_groups(fresh=True)
        background.add(task := asyncio.create_task(_name_new_group(bridge, event.group_openid)))
        task.add_done_callback(background.discard)
    if isinstance(event, (qe.GroupMsgReceiveEvent, qe.GroupMsgRejectEvent)):  # 管理员开启或关闭了机器人的主动消息
        bridge.idmap.group_id(event.group_openid)
        bridge.idmap.set_group_muted(event.group_openid, isinstance(event, qe.GroupMsgRejectEvent))
        return
    # 没有原机器人连着，就没有第二份消息可比对：不记、不挂定时器。每条消息现看，原机器人中途连上或掉线都立刻生效。
    if isinstance(event, qe.GroupMessageCreateEvent) and config.official_bridge_auto_link and legacy_bots():
        author = event.author
        heard = hear(
            True, "".join(event.get_message().extract_plain_text().split()), bridge.idmap.group_id(event.group_openid), author.member_openid,
            bridge._timestamp(event.timestamp), (author.username or "", str(AUTHOR_EXTRAS.get(event.id, {}).get("member_nickname") or "")),
        )
        if heard is not None:
            await identify(heard)  # 这样下面的事件就已经带上成员的 QQ 号
    converted = bridge.convert(event)
    if converted is None:
        logger.debug(f"official bridge: {type(event).__name__} has no OneBot counterpart")
        return
    ob_event, inbound = converted
    if isinstance(ob_event, GroupMessageEvent):
        if is_first_handshake(event):
            # 还没人告诉这个官方机器人原机器人账号的 OpenID 是哪个。第一条带着原机器人刚生成的编码、
            # 发给本机器人的消息只可能是原机器人发的：从它就能永久确定这个账号。
            idmap.link_user(event.author.member_openid, int(legacy_bots()[0].self_id))
            read_handshake(event)
            return
        if any(str(ob_event.user_id) == b.self_id for b in legacy_bots()):
            read_handshake(event)
            return  # 从另一侧看到的原机器人自己的消息：只读取握手
        # 不能用“消息以 @机器人 开头”来判断：引用别的消息的消息以引用开头；群设置为 @ 时附带最近
        # 十条消息的，也是以引用的形式开头。这样的消息曾被当成普通聊天：在只有官方机器人的群里
        # 没有回应，该群还被记成推送全部消息。
        mentioned = inbound.at_me
        if not isinstance(event, qe.GroupAtMessageCreateEvent) and (
            event.group_openid not in _hears_all or not (row := bridge.idmap.group_by_openid(event.group_openid)) or not row["hears_all"]
        ):  # 内存里记着，也要看表里：群被清掉又重新拉进来时，表里是新的一行
            _hears_all.add(event.group_openid)
            bridge.idmap.note_group(event.group_openid, hears_all=True)
        if ears_for(ob_event.group_id) is not None and await legacy_in_group(ob_event.group_id):
            return  # 原机器人能收到这个群的消息，处理的是它那份；这份只用于回复
        if not mentioned and await in_shadow(bridge, ob_event.group_id, event.author.member_openid, ob_event.time):
            return
    if isinstance(event, qe.GroupAddRobotEvent) and legacy_bots():
        background.add(task := asyncio.create_task(_announce_join(bridge, event.group_openid, ob_event, inbound)))
        task.add_done_callback(background.discard)
        return
    if relay.connected:
        relay.forward(wire(ob_event), ob_event)
    bridge.dispatch(ob_event, inbound)


JOIN_WAIT = 45.0  # 进群事件为等待握手（确定是哪个群）而暂缓的秒数
GROUP_CHECK_UTC = (16, 0)  # 每天检查各群（群名、是否已解散）的时间：北京时间 0:00，约两分钟查完，赶在夜里的签到之前
GROUP_CHECK_EVERY = 20 * 3600  # 启动时上次检查已过去这么久，就先补查一次


async def _name_new_group(bridge: Bridge, group_openid: str) -> None:
    """新进的群马上取一次群名（Bison 等列出的群就有名字），不等每天的检查。"""
    await asyncio.sleep(5)
    try:
        await bridge.check_group(group_openid)
    except Exception as e:
        logger.info(f"official bridge: new group's name not read ({type(e).__name__})")


async def _check_groups_daily() -> None:
    """每天查一遍各群：群名跟着群改名更新，已解散的群清除。"""
    last = float(idmap.meta("groups_checked_at") or 0)
    wait = 120 if time.time() - last > GROUP_CHECK_EVERY else None
    while True:
        if wait is None:
            now = time.gmtime()
            target = calendar.timegm((now.tm_year, now.tm_mon, now.tm_mday, *GROUP_CHECK_UTC, 0))
            wait = target - time.time()
            if wait <= 0:
                wait += 24 * 3600
        await asyncio.sleep(wait)
        wait = None
        for bridge in list(bridges.values()):
            try:
                await bridge.check_groups()
            except Exception:
                logger.opt(exception=True).warning("official bridge: daily group check failed")


@driver.on_startup
async def _start_group_checks() -> None:
    background.add(task := asyncio.create_task(_check_groups_daily()))
    task.add_done_callback(background.discard)


async def _announce_join(bridge: Bridge, group_openid: str, notice, inbound=None) -> None:
    """只有当这个群对整个机器人来说是新群时，才告诉各插件“机器人进群了”。

    进入原机器人已在的群是换人接手，不是新群：那里不需要任何新群处理（否则 plugins/friend_welcome
    会再发一次进群说明，别的插件也会把它当成新群处理，而且用的还是
    握手前那一两秒里该群的占位编号）。
    """
    deadline = time.monotonic() + JOIN_WAIT
    while time.monotonic() < deadline:
        row = bridge.idmap.group_by_openid(group_openid)
        if row is not None and row["linked"] and row["gid"] < GROUP_BASE:
            logger.info(f"official bridge: joined group {row['gid']}, which the old bot is in: not announced as a new group")
            return
        await asyncio.sleep(1)
    row = bridge.idmap.group_by_openid(group_openid)
    if row is None or not row["active"]:
        return
    notice.group_id = row["gid"]
    if relay.connected:
        relay.forward(wire(notice))
    bridge.dispatch(notice, inbound)


async def _handle_event(self: QQBot, event: qe.Event) -> None:
    try:
        await _on_official_event(self, event)
    except Exception:
        logger.opt(exception=True).error(f"official bridge: {type(event).__name__} not handled")


# 这里没有插件是针对官方适配器写的：它的所有事件都经过桥接。
QQBot.handle_event = _handle_event  # type: ignore[method-assign]

_payload_to_event = QQAdapter.payload_to_event


def _keep_author_extras(payload):
    data = getattr(payload, "data", None)
    if isinstance(data, dict) and isinstance(data.get("author"), dict) and data.get("id"):
        remember_author(str(data["id"]), data)
    if isinstance(data, dict) and isinstance(data.get("msg_elements"), list):
        # 被引用的聊天记录作为元素传来时缺少适配器要求引用消息必须有的两个字段，整条消息会因此
        # 被丢弃。
        for element in data["msg_elements"]:
            if isinstance(element, dict):
                element.setdefault("msg_idx", "")
                element.setdefault("message_type", 0)
    return _payload_to_event(payload)


QQAdapter.payload_to_event = staticmethod(_keep_author_extras)  # type: ignore[method-assign]


def _official_avatar(bot: AnyBot, user_id: str) -> str | None:
    """经桥接的成员在官方平台上的头像地址；不是经桥接来的则为 None。"""
    bridge = next((b for b in bridges.values() if b.bot is bot), None)
    openid = idmap.user_openid(int(user_id)) if bridge is not None and str(user_id).isdigit() else None
    return f"https://q.qlogo.cn/qqapp/{bridge.qq.bot_info.id}/{openid}/640" if openid else None


@driver.on_startup
async def _avatars_from_platform() -> None:
    """uninfo 按 QQ 号拼头像地址，而经桥接的成员多半只有占位编号，拿到的是空白头像（如 /skl绑定 二维码中间的头像）：
    改用官方平台按 OpenID 给的头像。等插件都加载完再动：提前导入 uninfo 会让它之后无法作为插件加载。"""
    if "nonebot_plugin_uninfo" not in sys.modules:
        return
    from nonebot_plugin_uninfo.adapters.onebot11.main import fetcher

    fetch = fetcher.fetch

    async def fetched(bot, event):
        session = await fetch(bot, event)
        if url := _official_avatar(bot, session.user.id):
            session.user.avatar = url
        return session

    fetcher.fetch = fetched  # type: ignore[method-assign]

# ---------------------------------------------------------------- /群检查

group_check = on_command("群检查", rule=strict, permission=SUPERUSER, priority=5, block=True)
CHECK_WORDS = {"named": "取到群名", "gone": "已解散或机器人已不在，已清除", "failed": "查询失败（下次再查）"}


def adopt_group(group_id: int) -> bool:
    """让官方机器人以该群现在使用的群号为它服务。

    给插件用的：插件从已加载的模块里找到这个函数，不直接导入桥接。
    桥接不认识该群时返回 False。
    """
    openid = idmap.group_openid(group_id)
    if openid is None:
        return False
    if not idmap.group(group_id)["linked"]:
        idmap.link_group(openid, int(group_id))
    return True


@group_check.handle()
async def _group_check() -> None:
    """立刻查一遍各群（平时每天北京时间 0:00 自动查）：更新群名，清除已解散的群。"""
    if not bridges:
        await group_check.finish("官方机器人还没连上。")
    await group_check.send(f"开始检查 {len(idmap.groups(active_only=False))} 个群，每个约 2 秒。")
    tally: dict[str, int] = {}
    for bridge in list(bridges.values()):
        for outcome, count in (await bridge.check_groups()).items():
            tally[outcome] = tally.get(outcome, 0) + count
    await group_check.finish("检查完成：" + "，".join(f"{CHECK_WORDS.get(k, k)} {v} 个" for k, v in tally.items()))

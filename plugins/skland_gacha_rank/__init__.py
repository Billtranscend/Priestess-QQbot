"""终末地和明日方舟的抽卡欧非榜，覆盖所有通过本机器人绑定了森空岛的人。

/zmd欧非榜 列出终末地限定角色池和武器池各自最欧的十人和最非的十人；/角色欧非榜 和 /武器欧非榜
各列出其中一个榜（榜很长时只列两端）。/mrfz欧非榜 列出明日方舟限定寻访最欧和最非的十人，
/mrfz限定欧非榜 是它的完整榜单。只发 /欧非榜 时先问看哪一款（用按钮问；按钮发不出时写出两个指令）。

榜不分群（小群单独一个榜意义不大）。凡是绑定了、没有选择退出、UP 不少于 MIN_UP 个的人都会上榜，
在任何群和私聊里都一样；每款游戏用成员的默认角色。在显示榜单的这个群里的成员，用他在本群的名字显示，
并标注“本群”；其他人用游戏角色名显示，这样 QQ 名字不会从一个群流到另一个群。

退出按游戏分开：/zmd欧非榜 退出 只退终末地的榜，/mrfz欧非榜 退出 只退明日方舟的，/欧非榜 退出 两款都退
（加入同理）。每款游戏一份名单，记号码，也记“这个人”（o<成员 id>）。

每款游戏的统计规则见 endfield.py 和 arknights.py，排名和在指令之间保存的数字见 ranking.py，图见 pictures.py。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent
from nonebot.exception import MatcherException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_skland")  # 要在 plugins.skland_limited 之前，它会从这里导入
require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_apscheduler")

from plugins import ef_theme
from plugins.quick_buttons import send_buttons
from plugins.strict_command import strict

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.model import Character, CharacterDefault
from sqlalchemy import select
from plugins.shared import write_json
from plugins.skland_roles import member_numbers, owner_id_of

from . import arknights, endfield
from .pictures import render_board, render_overview
from .ranking import Game, NotReady, entries_of

__plugin_meta__ = PluginMetadata(
    name="Gacha leaderboard",
    description="终末地和明日方舟欧非榜：终末地的限定池和武器池、明日方舟的限定寻访各按欧气排名（由平均每个 UP 的抽数和 UP 数算出），绑定了森空岛、没有退出且 UP 不少于 4 个的人上榜。",
    usage="/zmd欧非榜 | /角色欧非榜 | /武器欧非榜 | /mrfz欧非榜 | /mrfz限定欧非榜 | /欧非榜 | 欧非榜指令后加 退出 / 加入",
    type="application",
)

COLD_START_DELAY = 90  # 启动后多少秒做第一次计算（此时还没有保存的数字）
CHOICE = "你要看哪个游戏的榜？"
OPT = ("退出", "加入")

ENDFIELD = Game(
    app_code="endfield",
    name="终末地",
    code="Endfield",
    command="/zmd欧非榜",
    boards=(endfield.CHARACTERS, endfield.WEAPONS),
    stat=endfield.Stat,
    entry=endfield.Entry,
    count=endfield.stat_of,
    pools_key=endfield.pools_key,
    snapshot=store.get_plugin_data_file("snapshot.json"),
    version=endfield.SNAPSHOT_VERSION,
    optout=store.get_plugin_data_file("optout.json"),
    look=endfield.LOOK,
    basis=endfield.BASIS,
    log="Endfield gacha leaderboard",
    role_name=str.strip,
)
ARKNIGHTS = Game(
    app_code="arknights",
    name="明日方舟",
    code="Arknights",
    command="/mrfz欧非榜",
    boards=(arknights.LIMITED,),
    stat=arknights.Stat,
    entry=arknights.Entry,
    count=arknights.stat_of,
    pools_key=arknights.pools_key,
    snapshot=store.get_plugin_data_file("arknights_snapshot.json"),
    version=arknights.SNAPSHOT_VERSION,
    optout=store.get_plugin_data_file("arknights_optout.json"),
    look=arknights.LOOK,
    basis=arknights.BASIS,
    log="Arknights gacha leaderboard",
    role_name=arknights.role_name,
)
GAMES = (ENDFIELD, ARKNIGHTS)

driver = get_driver()

ef_rank = on_command("zmd欧非榜", aliases={"ef欧非榜", "终末地欧非榜"}, rule=strict, priority=5, block=True)
char_rank = on_command(
    "zmd角色欧非榜", aliases={"ef角色欧非榜", "终末地角色欧非榜", "角色欧非榜"}, rule=strict, priority=5, block=True
)
weapon_rank = on_command(
    "zmd武器欧非榜", aliases={"ef武器欧非榜", "终末地武器欧非榜", "武器欧非榜"}, rule=strict, priority=5, block=True
)
ark_rank = on_command("mrfz欧非榜", aliases={"方舟欧非榜", "明日方舟欧非榜"}, rule=strict, priority=5, block=True)
ark_limited_rank = on_command("mrfz限定欧非榜", aliases={"方舟限定欧非榜", "明日方舟限定欧非榜"}, rule=strict, priority=5, block=True)
either_rank = on_command("欧非榜", rule=strict, priority=5, block=True)


def _load_optout(game: Game = ENDFIELD) -> set[str]:
    """退出了这款游戏欧非榜的号码和人；不指明游戏时是终末地的（skland_bind_sign 绑定后的报告这样问）。"""
    # 文件不存在说明没人选择退出；文件读不了时，绝不能悄悄把人重新列上榜。
    if not game.optout.exists():
        return set()
    return set(json.loads(game.optout.read_text("utf-8")))


def _save_optout(game: Game, qqs: set[str]) -> None:
    write_json(game.optout, sorted(qqs))


@scheduler.scheduled_job("cron", hour=1, minute=15, timezone="Asia/Shanghai", id="skland_gacha_rank_recount", misfire_grace_time=3600)
async def _recount() -> None:
    """每晚把所有人重算一次：什么算 UP 取决于卡池表，而卡池表随游戏更新变化。"""
    for game in GAMES:
        try:
            async with game.lock:
                await game.refresh(everyone=True)
        except NotReady:  # 那就等有人查询时再算
            pass
        except Exception as e:  # 一款游戏出错不耽误另一款
            logger.warning(f"{game.log}: the nightly recount failed: {type(e).__name__}: {e}")


async def _first_count(game: Game) -> None:
    await asyncio.sleep(COLD_START_DELAY)
    try:
        await game.stats()
    except NotReady:
        logger.info(f"{game.log}: the pool tables are not loaded yet; the first count waits for a command")
    except Exception as e:
        logger.warning(f"{game.log}: the first count failed, the next command tries again: {type(e).__name__}: {e}")


_tasks: set[asyncio.Task] = set()


@driver.on_startup
async def _count_where_nothing_is_kept() -> None:
    for game in GAMES:
        if not game.kept:  # 这个版本第一次启动：在有人查询之前先把耗时的计算做完
            task = asyncio.create_task(_first_count(game))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)


# ---------------------------------------------------------------- 谁上榜，用什么名字


async def _cards(bot: Bot, event: MessageEvent) -> dict[str, str]:
    """号码 -> 群成员在本群的名字；私聊里、或拿不到成员列表时为空。"""
    if not isinstance(event, GroupMessageEvent):
        return {}
    try:
        members = await bot.get_group_member_list(group_id=event.group_id)
    except Exception as e:  # 榜单仍然值得显示：只是没人会被标为本群
        logger.info(f"Gacha leaderboard: no member list for this group ({type(e).__name__}); nobody is marked 本群")
        return {}
    return {str(m["user_id"]): str(m.get("card") or m.get("nickname") or "").strip() for m in members}


async def _collect(game: Game, bot: Bot, event: MessageEvent) -> list:
    async with get_session() as session:
        asker = await owner_id_of(session, str(event.user_id))
    stats = await game.stats(asker)
    if not stats:
        return []
    async with get_session() as session:
        ids = await member_numbers(session)
        roles = (
            await session.execute(
                select(CharacterDefault.owner_id, Character.nickname)
                .join(Character, Character.id == CharacterDefault.character_id)
                .where(CharacterDefault.app_code == game.app_code)
            )
        ).all()
    return entries_of(stats, ids, {owner: game.role_name(name or "") for owner, name in roles}, await _cards(bot, event), _load_optout(game), game.entry)


# ---------------------------------------------------------------- 指令


async def _send(matcher, game: Game, bot: Bot, event: MessageEvent, render: Callable[[list, str], str | None], empty: str) -> None:
    try:
        try:
            entries = await _collect(game, bot, event)
        except NotReady:
            await matcher.finish("卡池数据还在加载，请过一会儿再试")
        html = render(entries, str(event.user_id))
        if html is None:
            await matcher.finish(empty)
        image = await ef_theme.render_page(html, game.look.width, height=600, scale=game.look.scale, quality=game.look.quality)
        await UniMessage.image(raw=image).send()
    except MatcherException:
        raise
    except Exception as e:  # 出图或发送出错：至少告诉他没成功，不能一声不响
        logger.warning(f"{game.log} failed: {type(e).__name__}")
        await matcher.finish("欧非榜生成失败，请稍后再试")


async def _opt(matcher, event: MessageEvent, action: str, games: tuple[Game, ...]) -> None:
    """退出或重新加入这几款游戏的欧非榜。"""
    qq = str(event.user_id)
    async with get_session() as session:
        owner = await owner_id_of(session, qq)
    # 号码之外也记下“这个人”（o<所有者 id>）：他的号以后变了（占位编号换成认出的 QQ 号），照样不上榜
    marks = {qq} | ({f"o{owner}"} if owner is not None else set())
    for game in games:
        optout = _load_optout(game)
        _save_optout(game, optout | marks if action == "退出" else optout - marks)
    which = "和".join(game.name for game in games) + ("的" if len(games) > 1 else "")
    await matcher.finish(f"已退出{which}欧非榜，不会再出现在榜单上" if action == "退出" else f"已重新加入{which}欧非榜")


async def _overview(matcher, game: Game, bot: Bot, event: MessageEvent, arg: Message) -> None:
    """总览，或者后面跟着 退出 / 加入 时只对这款游戏退出、加入。"""
    action = arg.extract_plain_text().strip()
    if action in OPT:
        await _opt(matcher, event, action, (game,))
    await _send(matcher, game, bot, event, lambda entries, qq: render_overview(game, entries, qq), game.boards[0].empty)


def games_in_words() -> str:
    """按钮发不出时的问法。"""
    return CHOICE + "\n" + "\n".join(f"{game.name}：{game.command}" for game in GAMES)


@either_rank.handle()
async def ask_game(event: MessageEvent, arg: Message = CommandArg()) -> None:
    action = arg.extract_plain_text().strip()
    if action in OPT:
        await _opt(either_rank, event, action, GAMES)
    await send_buttons(CHOICE, [[(game.name, game.command) for game in GAMES]], fallback=games_in_words())
    await either_rank.finish()


@ef_rank.handle()
async def ef_overview(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _overview(ef_rank, ENDFIELD, bot, event, arg)


@ark_rank.handle()
async def ark_overview(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    await _overview(ark_rank, ARKNIGHTS, bot, event, arg)


def _board_handler(matcher, game: Game, board) -> None:
    @matcher.handle()
    async def _(bot: Bot, event: MessageEvent) -> None:
        await _send(matcher, game, bot, event, lambda entries, qq: render_board(game, board, entries, qq), board.empty)


_board_handler(char_rank, ENDFIELD, endfield.CHARACTERS)
_board_handler(weapon_rank, ENDFIELD, endfield.WEAPONS)
_board_handler(ark_limited_rank, ARKNIGHTS, arknights.LIMITED)

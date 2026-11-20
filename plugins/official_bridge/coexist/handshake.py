"""握手: the old bot tells the official bot, in a group both are in, which group that is."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time

from nonebot import logger, on_notice
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent, Message, MessageSegment
from nonebot.adapters.qq import event as qe

from ..bridge import BridgeBot
from ..state import bridges, learn_uin, official_uins, online, idmap, legacy_bots
from .hearing import group_evidence
from .presence import legacy_group_cache
from .sends import saying_migration_line

# ---------------------------------------------------------------- 握手: which group the official bot has just joined

ADD_WINDOW = 120.0  # seconds between the official bot being added somewhere and a robot joining a group of the old bot
_HANDSHAKE_CODE = re.compile(r"迁移握手 ([0-9A-F]{6})")
recent_adds: dict[str, float] = {}  # group OpenID -> when the official bot reported being added
_handshakes: dict[str, tuple[int, str | None]] = {}  # code -> (group number, the robot account addressed)

async def shake_hands(legacy: OneBot, gid: int, uin: str | None) -> None:
    code = secrets.token_hex(3).upper()
    _handshakes[code] = (gid, uin)
    line = MessageSegment.text(f"【机器人迁移握手 {code}】本群即将由官方机器人接替，原有的设置和数据保持不变。")
    token = saying_migration_line.set(True)  # said whether or not the old bot is answering anything at the moment
    try:
        await legacy.send_group_msg(group_id=gid, message=(MessageSegment.at(uin) + " " + line) if uin else Message(line))
    finally:
        saying_migration_line.reset(token)


def is_first_handshake(event: qe.GroupMessageCreateEvent) -> bool:
    old = legacy_bots()
    if not old or any(idmap.user(int(b.self_id)) is not None and idmap.user(int(b.self_id))["linked"] for b in old):
        return False
    found = _HANDSHAKE_CODE.search(event.content or "")
    if not found or found.group(1) not in _handshakes:
        return False
    return any(getattr(m, "is_you", False) for m in event.mentions or []) or isinstance(event, qe.GroupAtMessageCreateEvent)


def read_handshake(event: qe.GroupMessageCreateEvent) -> None:
    found = _HANDSHAKE_CODE.search(event.content or "")
    pending = _handshakes.pop(found.group(1), None) if found else None
    if pending is None:
        return
    gid, uin = pending
    row = idmap.group_by_openid(event.group_openid)
    holder = idmap.group(gid)
    if row is None or (holder is not None and holder["openid"] != event.group_openid):
        logger.warning(f"official bridge: handshake for group {gid} arrived in a group that cannot take that number")
        return
    idmap.link_group(event.group_openid, gid)
    group_evidence.pop(event.group_openid, None)
    legacy_group_cache.clear()
    if uin:
        learn_uin(uin)
    logger.info(f"official bridge: group {row['gid']} is {gid} (handshake)")


async def _is_group_increase(event: GroupIncreaseNoticeEvent) -> bool:
    return True


robot_joined = on_notice(rule=_is_group_increase, priority=1, block=False)  # the rule keeps other notices out of the log


@robot_joined.handle()
async def _robot_joined(bot: OneBot, event: GroupIncreaseNoticeEvent) -> None:
    if isinstance(bot, BridgeBot) or not bridges or str(event.user_id) == bot.self_id:
        return
    uin = str(event.user_id)
    holder = idmap.group(event.group_id)
    if holder is not None and holder["linked"]:
        return
    for _ in range(15):  # the two platforms report the same join in either order
        if any(time.monotonic() - at <= ADD_WINDOW for at in recent_adds.values()):
            break
        await asyncio.sleep(1)
    else:
        return
    if uin not in official_uins:
        try:
            if not (await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id, no_cache=True)).get("is_robot"):
                return
        except Exception:
            return
    await shake_hands(bot, event.group_id, uin)


SWEEP_DELAY = 6.0  # seconds after a join or a connect before the old bot looks
SWEEP_PAUSE = 8.0  # seconds between two handshakes
RELOOKS = (120.0, 300.0, 600.0, 1200.0)  # the old client learns of a new member late (about ten minutes): looks again after these
SHAKE_AGAIN = 6 * 3600.0  # a group whose handshake went unheard is tried again after this long
SHAKE_AGAIN_JOINED = 300.0  # or after this long, once the official bot reports being added somewhere
sweeping: asyncio.Task | None = None
_sweep_fresh = False
_sweep_wake = asyncio.Event()


def look_for_groups(fresh: bool = False) -> None:
    """Have the old bot find, shortly, the groups of its own that the official bot is in and that are not tied."""
    global sweeping, _sweep_fresh
    if fresh:
        _sweep_fresh = True
        _sweep_wake.set()
    if sweeping is None or sweeping.done():
        sweeping = asyncio.get_running_loop().create_task(_sweep())


def _untied() -> bool:
    return bool(legacy_bots() and online) and any(not row["linked"] for row in idmap.groups())


async def _sweep() -> None:
    global _sweep_fresh
    try:
        waits = iter(RELOOKS)
        delay: float | None = SWEEP_DELAY
        while delay is not None:
            try:
                await asyncio.wait_for(_sweep_wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            else:  # a join has just been reported: the whole round of looks starts over
                _sweep_wake.clear()
                waits = iter(RELOOKS)
                await asyncio.sleep(SWEEP_DELAY)
            fresh, _sweep_fresh = _sweep_fresh, False
            await _shake_untied(SHAKE_AGAIN_JOINED if fresh else SHAKE_AGAIN)
            delay = SWEEP_DELAY if _sweep_fresh else (next(waits, None) if _untied() else None)
    except Exception:
        logger.opt(exception=True).warning("official bridge: could not look for the groups the official bot is in")


async def _official_member(bot: OneBot, gid: int) -> str | None:
    """The official bot's number, if that account is a member of this group of the old bot's and QQ marks it a robot."""
    for uin in sorted(official_uins):
        try:
            info = await bot.get_group_member_info(group_id=gid, user_id=int(uin), no_cache=True)
        except Exception:
            continue  # not a member
        if info and info.get("is_robot") and str(info.get("user_id")) == uin:
            return uin
    return None


async def _shake_untied(again: float) -> None:
    if not official_uins or not online:
        return
    tried: dict[str, float] = json.loads(idmap.meta("handshakes_tried") or "{}")
    for bot in legacy_bots():
        try:
            listed = [int(g["group_id"]) for g in await bot.get_group_list()]
        except Exception:
            continue
        found: list[tuple[int, str]] = []
        for gid in listed:  # all the asking first, so that the log shows the whole picture before anything is said
            holder = idmap.group(gid)
            if (holder is not None and holder["linked"]) or time.time() - tried.get(str(gid), 0) < again:
                continue
            uin = await _official_member(bot, gid)
            if uin is not None:
                found.append((gid, uin))
        if found:
            logger.info(f"official bridge: the official bot is in {len(found)} group(s) not tied yet ({', '.join(str(g) for g, _ in found)}): handshake in each")
        for gid, uin in found:
            holder = idmap.group(gid)
            if holder is not None and holder["linked"]:
                continue  # tied meanwhile
            tried[str(gid)] = time.time()
            idmap.set_meta("handshakes_tried", json.dumps(tried))
            try:
                await shake_hands(bot, gid, uin)
            except Exception as e:
                logger.warning(f"official bridge: handshake in group {gid} not sent ({type(e).__name__}: {e})")
            await asyncio.sleep(SWEEP_PAUSE)

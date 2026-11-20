"""Recognising groups and members from what both bots hear."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from ..idmap import GROUP_BASE
from ..state import learn_uin, config, driver, idmap, legacy_bots
from .presence import legacy_group_cache, legacy_in_group

# ---------------------------------------------------------------- the official bot as the old bot sees it

SAID_WINDOW = 20.0  # seconds within which the old bot hears what the official bot has just sent
_said: deque[tuple[int, str, float]] = deque(maxlen=200)


def plain(message: Message) -> str:
    return "".join(message.extract_plain_text().split())


def note_said(group_openid: str, text: str) -> None:
    row = idmap.group_by_openid(group_openid)
    text = "".join(text.split())
    if row is not None and len(text) >= 6:
        _said.append((row["gid"], text, time.monotonic()))


async def is_official_bot(bot: OneBot, event: GroupMessageEvent, text: str) -> bool:
    """Learn the official bot's number on this side: a robot account repeating what it has just sent here."""
    now = time.monotonic()
    if len(text) < 6 or not any(g == event.group_id and t == text and now - at <= SAID_WINDOW for g, t, at in _said):
        return False
    try:
        info = await bot.get_group_member_info(group_id=event.group_id, user_id=event.user_id, no_cache=True)
    except Exception:
        return False
    if not info.get("is_robot"):
        return False
    learn_uin(str(event.user_id))
    return True

# ---------------------------------------------------------------- linking members by what both bots hear

WINDOW = 10.0  # seconds between the two bots hearing the same message
SETTLE = 14.0  # wait this long before judging, so every copy of the text has arrived
NEEDED = 2  # distinct messages that must agree before an OpenID is tied to a QQ number, where one alone leaves a doubt (see _tie)
NEEDED_GROUP = 3  # and before a group is tied to the old group's number


@dataclass
class Heard:
    official: bool
    text: str
    group: int
    user: str
    at: float
    sent: int = 0  # the message's own time, the same on both sides
    names: tuple[str, ...] = ()  # the sender's nickname and name in the group, as that side shows them


_heard: deque[Heard] = deque(maxlen=4000)
_evidence: dict[str, dict[str, set[str]]] = {}
group_evidence: dict[str, dict[int, set[str]]] = {}


def trusted(openid: str) -> bool:
    """Was the bot put in the group by someone whose word on which group it is can be taken?"""
    member = idmap.user_by_openid(openid) if openid else None
    if member is None or not member["linked"]:
        return False
    uid = str(member["uid"])
    return uid in driver.config.superusers or uid in config.official_bridge_trusted_adders or any(uid == b.self_id for b in legacy_bots())


def _judge_group(entry: Heard, row) -> None:
    same = [h for h in _heard if h.text == entry.text and abs(h.at - entry.at) <= WINDOW]
    ours = [h for h in same if h.official]
    theirs = [h for h in same if not h.official]
    if len(ours) != 1 or len(theirs) != 1:
        return
    seen = group_evidence.setdefault(row["openid"], {})
    seen.setdefault(theirs[0].group, set()).add(entry.text)
    if len(seen) > 1:
        group_evidence.pop(row["openid"], None)
        return
    if len(seen[theirs[0].group]) < NEEDED_GROUP or not trusted(row["added_by"]):
        return
    holder = idmap.group(theirs[0].group)
    if holder is not None and holder["openid"] != row["openid"]:
        return  # that number already belongs to another group here: leave this group unlinked
    group_evidence.pop(row["openid"], None)
    idmap.link_group(row["openid"], theirs[0].group)
    legacy_group_cache.clear()
    logger.info(f"official bridge: group {row['gid']} recognised as {theirs[0].group} (heard by both bots)")


def hear(official: bool, text: str, group: int, user: str, sent: int = 0, names: tuple[str, ...] = ()) -> Heard | None:
    if len(text) < 3:
        return None
    entry = Heard(official, text, group, user, time.monotonic(), sent, names)
    _heard.append(entry)
    if official:
        asyncio.get_running_loop().call_later(SETTLE, _judge, entry)
    return entry


IDENTIFY_WAIT = 2.5  # seconds an untied member's message is held for the old bot's copy of it
IDENTIFY_SETTLE = 0.6  # and then for any other copy of the same text


async def identify(entry: Heard) -> None:
    """Recognise a member from the first message they send in a tied group, before it is answered.

    One message is enough only when nothing about it is in doubt: one copy on each side, sent in
    the same seconds, by senders showing the same name. Anything less is left to _judge, which
    asks for two messages.
    """
    if config.official_bridge_takeover != "all" or config.official_bridge_ears or entry.group >= GROUP_BASE or idmap.is_linked(entry.user):
        return
    row = idmap.group(entry.group)
    if row is None or not row["linked"] or not await legacy_in_group(entry.group):
        return

    def copies() -> list[Heard]:
        return [h for h in _heard if h.text == entry.text and h.group == entry.group and abs(h.sent - entry.sent) <= 3]

    deadline = time.monotonic() + IDENTIFY_WAIT
    while not any(not h.official for h in copies()):
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0.15)
    await asyncio.sleep(IDENTIFY_SETTLE)
    ours = [h for h in copies() if h.official]
    theirs = [h for h in copies() if not h.official]
    if len(ours) == 1 and len(theirs) == 1:
        _tie(entry, theirs[0])


def _tie(ours: Heard, theirs: Heard, quiet: bool = False) -> bool:
    """Tie the two senders of one message heard once on each side, unless either is somebody already or they show different names."""
    number = int(theirs.user)
    holder = idmap.user(number)
    if idmap.is_linked(ours.user) or (holder is not None and holder["linked"]):
        return False  # that member, or that number, is already someone here
    if not {n for n in ours.names if n} & {n for n in theirs.names if n}:
        if not quiet:
            logger.info(f"official bridge: one message heard by both bots in group {ours.group}, but the two senders show different names: not tied on that alone")
        return False
    before = idmap.link_user(ours.user, number)
    _evidence.pop(ours.user, None)
    logger.info(f"official bridge: member {before} recognised as {number} from one message heard by both bots in group {ours.group}")
    return True


async def recognise_first(theirs: Heard) -> bool:
    """The same from the old bot's side, for a group where its copy is the one that is handled (ears).

    Called before that copy is handed on: if the official bot's copy of the message arrives too and
    nothing about the pair is in doubt, the sender is tied there and then. Otherwise a member who
    bound an account under a placeholder number, where only the official bot is (a private chat),
    is somebody else, with nothing bound, in a group both bots are in, until two messages agree.
    """
    holder = idmap.user(int(theirs.user))
    if theirs.group >= GROUP_BASE or (holder is not None and holder["linked"]):
        return False

    def copies() -> list[Heard]:
        return [h for h in _heard if h.text == theirs.text and h.group == theirs.group and abs(h.sent - theirs.sent) <= 3]

    deadline = time.monotonic() + IDENTIFY_WAIT
    while not any(h.official for h in copies()):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.15)
    await asyncio.sleep(IDENTIFY_SETTLE)
    ours = [h for h in copies() if h.official]
    others = [h for h in copies() if not h.official]
    return len(ours) == 1 and len(others) == 1 and _tie(ours[0], theirs)


def _judge(entry: Heard) -> None:
    try:
        row = idmap.group(entry.group)
        if row is None:
            return
        if not row["linked"]:
            _judge_group(entry, row)
            return
        if idmap.is_linked(entry.user):
            return
        same = [h for h in _heard if h.text == entry.text and abs(h.at - entry.at) <= WINDOW]
        ours = [h for h in same if h.official]
        theirs = [h for h in same if not h.official and h.group == entry.group]
        if len(ours) != 1 or len(theirs) != 1:
            return
        if abs(theirs[0].sent - entry.sent) <= 3 and _tie(entry, theirs[0], quiet=True):
            return  # the first message both bots hear is enough where the two senders show the same name as well
        seen = _evidence.setdefault(entry.user, {})
        seen.setdefault(theirs[0].user, set()).add(entry.text)
        if len(seen) > 1:  # two QQ numbers for one OpenID: start over rather than guess
            _evidence.pop(entry.user, None)
            return
        if len(seen[theirs[0].user]) >= NEEDED:
            _evidence.pop(entry.user, None)
            before = idmap.link_user(entry.user, int(theirs[0].user))
            logger.info(f"official bridge: member {before} recognised as {theirs[0].user} (heard by both bots in group {entry.group})")
    except Exception:
        logger.opt(exception=True).warning("official bridge: could not judge a heard message")

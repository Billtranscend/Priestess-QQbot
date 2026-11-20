"""The old bot's hearing, handed to the bridge bot: what it hears in a group the official bot serves."""

from __future__ import annotations

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, GroupRequestEvent, Message, MessageSegment, NoticeEvent
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor

from ..bridge import Bridge, BridgeBot, without_trailing_space
from ..identity import carried
from ..idmap import GROUP_BASE
from ..relay import wire
from ..state import bridges, official_uins, online, config, idmap, relay
from .hearing import hear, is_official_bot, plain, recognise_first
from .presence import legacy_gone_from, legacy_seen_in, official_answers, served

@event_preprocessor
async def _tap_legacy(bot: OneBot, event: GroupMessageEvent) -> None:
    if isinstance(bot, BridgeBot) or not bridges:
        return
    legacy_seen_in(bot.self_id, event.group_id)  # so that the official bot's copy of this message is not answered as well
    text = plain(event.message)
    if str(event.user_id) in official_uins or await is_official_bot(bot, event, text):
        raise IgnoredException("said by the official bot")
    if ears_for(event.group_id) is None and any(seg.type == "at" and str(seg.data.get("qq")) in official_uins for seg in event.message):
        raise IgnoredException("addressed to the official bot")
    heard = None
    if config.official_bridge_auto_link:
        heard = hear(False, text, event.group_id, str(event.user_id), event.time, (event.sender.nickname or "", event.sender.card or ""))
    # What the official bot answers is not answered here. The old bot only still takes the two
    # things that have to come from its side: a migration code, and the request for a handshake.
    if text.startswith(("/迁移", "/桥接握手")):
        return
    bridge = ears_for(event.group_id)
    if bridge is not None:
        # A command from someone the two bots have not matched yet, of which the official bot gets a copy too:
        # match them on it first, so that it is handled with what they may have bound under a placeholder number.
        if heard is not None and text.startswith("/") and _copy_expected(event) and await recognise_first(heard):
            await carried(event.user_id)
        _hand_over(bridge, bot, event)
        raise IgnoredException("handled through the official bot")
    if official_answers(event.group_id, event.user_id, event.time):
        raise IgnoredException("answered by the official bot")


def _copy_expected(event: GroupMessageEvent) -> bool:
    """Is the sender not matched yet, and does the platform give the official bot this message as well (it mentions that bot, or the group sends it everything)?"""
    known = idmap.user(event.user_id)
    if known is not None and known["linked"]:
        return False
    row = idmap.group(event.group_id)
    return bool(row is not None and row["hears_all"]) or any(seg.type == "at" and str(seg.data.get("qq")) in official_uins for seg in event.message)


def ears_for(gid: int) -> Bridge | None:
    """The bridge that speaks in this group for what the old bot hears there, if that is how the group runs."""
    if not config.official_bridge_ears or not online or gid >= GROUP_BASE:
        return None
    row = idmap.group(gid)
    if row is None or not (row["linked"] and row["active"]):
        return None
    return next((b for q, b in bridges.items() if q in online), None)


def _hand_over(bridge: Bridge, legacy: OneBot, event: GroupMessageEvent) -> None:
    """Give a message the old bot heard to the bridge bot, as that bot's own event."""
    message = Message()
    for seg in event.original_message:
        mentioned = str(seg.data.get("qq")) if seg.type == "at" else ""
        if mentioned and (mentioned == legacy.self_id or mentioned in official_uins):
            message.append(MessageSegment.at(bridge.self_id))  # a mention of either bot is a mention of the bot
        else:
            message.append(MessageSegment(seg.type, dict(seg.data)))
    message = without_trailing_space(message)
    fields = event.dict(exclude={"message", "original_message", "raw_message", "reply", "to_me", "self_id"})
    handed = GroupMessageEvent(**fields, self_id=bridge.self_id, message=message, original_message=Message(message), raw_message=str(message), to_me=False)
    bridge.remember_name(event.user_id, event.sender.card or event.sender.nickname or "")
    if relay.connected:
        relay.forward(wire(handed))
    bridge.dispatch(handed, None)


@event_preprocessor
async def _tap_legacy_notice(bot: OneBot, event: NoticeEvent) -> None:
    """What happens in such a group (someone joins, leaves, is made admin) is the bridge bot's to react to as well."""
    if isinstance(bot, BridgeBot) or not bridges:
        return
    gid, uid = getattr(event, "group_id", None), str(getattr(event, "user_id", ""))
    if gid is not None and uid == bot.self_id and event.notice_type in ("group_increase", "group_decrease"):
        if event.notice_type == "group_increase":
            legacy_seen_in(bot.self_id, int(gid))
        else:
            legacy_gone_from(int(gid))
        if served(int(gid)):
            # The group stays the bot's whether the old account is in it or not: to the plugins
            # this is neither a group lost (its subscriptions would be removed) nor a new one.
            raise IgnoredException("the old bot coming or going changes nothing in a group the official bot serves")
        return
    if gid is None or uid == bot.self_id or uid in official_uins:
        return  # about one of the two bots themselves: left to the old bot's side and to the handshake
    bridge = ears_for(int(gid))
    if bridge is None:
        return
    try:
        handed = type(event)(**{**event.dict(), "self_id": bridge.self_id})
    except Exception:
        return
    bridge.dispatch(handed, None)
    raise IgnoredException("handled through the official bot")


@event_preprocessor
async def _tap_legacy_request(bot: OneBot, event: GroupRequestEvent) -> None:
    """The old bot asked back into a group the official bot serves: it goes (it is the ears there), and that is all."""
    if isinstance(bot, BridgeBot) or event.sub_type != "invite" or not served(event.group_id):
        return
    try:
        await bot.set_group_add_request(flag=event.flag, sub_type="invite", approve=True)
    except Exception as e:
        logger.warning(f"official bridge: invitation back into group {event.group_id} not accepted ({type(e).__name__})")
    raise IgnoredException("not a new group: the official bot serves it")

"""What the old bot still sends, and which of the bridge bot's calls it carries out."""

from __future__ import annotations

import json
from contextvars import ContextVar

from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message
from nonebot.adapters.onebot.v11.exception import ActionFailed as OneBotActionFailed
from nonebot.matcher import current_bot, current_event

from ..bridge import BridgeBot
from ..idmap import USER_BASE
from ..state import SENDS, bridges, config, idmap, legacy_bots
from .presence import legacy_in_group, legacy_left_for_good, note_legacy_groups

# The old bot itself no longer sends anything of its own accord either (scheduled notices and
# pushes: whatever is not an answer to the message being handled), whether or not an
# official bot is connected at the moment. Answers to commands in the groups it still serves go out.
_base_call_api = OneBot.call_api
saying_migration_line: ContextVar[bool] = ContextVar("official_bridge_handshaking", default=False)


async def _call_api_unless_unprompted(self: OneBot, api: str, **data):
    if api in SENDS and not isinstance(self, BridgeBot) and not config.official_bridge_legacy_sends:
        try:
            answering = current_bot.get() is self and current_event.get() is not None
        except LookupError:
            answering = False
        if not answering and not saying_migration_line.get():
            raise OneBotActionFailed(status="failed", retcode=1403, msg="the old bot no longer sends unprompted messages", wording="原机器人不再主动发送消息")
    return await _base_call_api(self, api, **data)


OneBot.call_api = _call_api_unless_unprompted  # type: ignore[method-assign]

async def route(api: str, data: dict) -> OneBot | None:
    """The old bot, when it and not the official bot should carry out this call of the bridge bot."""
    old = legacy_bots()
    if not old:
        return None
    if api == "get_image" and not str(data.get("file") or "").startswith("http"):
        return old[0]
    if api in SENDS and not config.official_bridge_legacy_sends:
        return None  # what the official bot cannot send is not sent
    if api == "get_group_member_list" and data.get("no_cache"):
        # Asked for by the weekly Skland check to unbind members who have left every group. The
        # old bot, while it is in the group, has the complete list; of any other group the bridge
        # answers or refuses itself (Bridge.api_get_group_member_list).
        asked = data.get("group_id")
        return old[0] if str(asked).isdigit() and await legacy_in_group(int(asked)) else None
    if api in ("delete_msg", "get_msg"):
        try:
            known = any(b.ledger.get(int(data.get("message_id"))) is not None for b in bridges.values())
        except (TypeError, ValueError):
            known = True
        return None if known else old[0]
    own = {b.bot.self_id for b in bridges.values()}
    gid, uid = data.get("group_id"), data.get("user_id")
    if str(uid) in own and api == "get_group_member_info":
        return None  # the bridge bot's own role: answered by itself, with the old bot's role taken into account
    private = api in ("send_private_msg", "send_private_forward_msg", "upload_private_file") or (
        api in ("send_msg", "send_forward_msg") and (data.get("message_type") == "private" or not gid)
    )
    if private:
        if str(uid).isdigit() and idmap.former_openid(int(uid)) is not None:
            return None  # the number a member had until a moment ago: never a QQ number the old bot could write to
        member = idmap.user(int(uid)) if str(uid).isdigit() else None
        return None if member is not None and member["friend"] else old[0]
    if gid is not None and str(gid).isdigit():
        gid = int(gid)
        row = idmap.group(gid)
        if row is not None and row["active"] and (api in SENDS or api == "get_group_at_all_remain" or str(uid) in own):
            return None  # sent by the official bot there, so its own allowance to mention everyone is the one asked about
        if str(uid).isdigit() and int(uid) >= USER_BASE:
            return None  # a member the old bot has no number for
        return old[0] if await legacy_in_group(gid) else None
    if api == "get_stranger_info" and str(uid).isdigit() and int(uid) < USER_BASE:
        return old[0]
    if not any(hasattr(b, f"api_{api}") for b in bridges.values()):
        return old[0]  # nothing the official side knows how to do, and not about a group: the old bot may
    return None


async def legacy_say(gid: int, message: Message) -> None:
    """Send through the old bot what the official bot is not allowed to send in this group."""
    for bot in legacy_bots():
        if await legacy_in_group(gid):
            await bot.send_group_msg(group_id=gid, message=message)
            return
    raise RuntimeError("no other bot in the group")


async def legacy_role(gid: int) -> str | None:
    for bot in legacy_bots():
        if await legacy_in_group(gid):
            try:
                return (await bot.get_group_member_info(group_id=gid, user_id=int(bot.self_id), no_cache=True)).get("role") or None
            except Exception:
                return None
    return None


async def legacy_census() -> list[dict]:
    """Every group the old bot is in, asked afresh. Raises when that cannot be told.

    The numbers are remembered: with no old bot connected the answer is still known if the old
    bot was in no group at all when last asked (it has left them all for good), and unknown
    otherwise (it is offline, and its groups' members cannot be told from here).
    """
    old = legacy_bots()
    if not old:
        if not legacy_left_for_good():
            raise RuntimeError("the old bot is not connected and its groups cannot be answered for")
        return []
    out: list[dict] = []
    for bot in old:
        out.extend(await bot.get_group_list(no_cache=True))
    note_legacy_groups({int(g["group_id"]) for g in out})
    numbers = json.dumps(sorted(int(g["group_id"]) for g in out))
    if numbers != idmap.meta("legacy_groups"):
        idmap.set_meta("legacy_groups", numbers)
    return out


async def legacy_group_list() -> list[dict]:
    out: list[dict] = []
    for bot in legacy_bots():
        try:
            out.extend(await bot.get_group_list())
        except Exception:
            pass
    return out

"""/迁移: a member ties their OpenID to their QQ number by hand, with a code carried from one bot to the other."""

from __future__ import annotations

import secrets
import time

from nonebot import logger, on_command
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandArg

from ..bridge import BridgeBot
from ..state import idmap, legacy_bots, strict

# ---------------------------------------------------------------- /迁移: tie an OpenID to the QQ number by hand

CODE_TTL = 600
_codes: dict[str, tuple[str, float]] = {}  # code -> (openid, expiry)
_attempts: dict[int, list[float]] = {}


def _issue(openid: str) -> str:
    now = time.time()
    for code, (owner, expiry) in list(_codes.items()):
        if owner == openid or expiry < now:
            _codes.pop(code, None)
    while True:
        code = f"{secrets.randbelow(10**6):06d}"
        if code not in _codes:
            _codes[code] = (openid, now + CODE_TTL)
            return code


def _throttled(user_id: int) -> bool:
    now = time.time()
    tries = _attempts[user_id] = [t for t in _attempts.get(user_id, []) if now - t < CODE_TTL]
    tries.append(now)
    return len(tries) > 5


migrate = on_command("迁移", rule=strict, priority=5, block=True)


@migrate.handle()
async def _migrate(bot: OneBot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    code = arg.extract_plain_text().strip()
    if isinstance(bot, BridgeBot):
        openid = idmap.user_openid(event.user_id)
        if openid is None or (code and isinstance(event, GroupMessageEvent) and legacy_bots()):
            return  # a code typed in a group is for the old bot, which is there to take it
        if idmap.is_linked(openid):
            await migrate.finish(f"这个账号已经关联 QQ {event.user_id}，原有的绑定和记录可以直接使用。")
        old = legacy_bots()
        if not old:
            await migrate.finish("原机器人已经停用，迁移码用不了了。请私聊我发送 /skl绑定 扫码，原来的绑定和记录会直接恢复。")
        await migrate.finish(
            f"请在 10 分钟内，用你的 QQ 向原机器人（QQ {old[0].self_id}）私聊或在群里发送：\n/迁移 {_issue(openid)}\n"
            "完成后，你在原机器人上的绑定和记录会接到这里。"
        )
    if not code:
        await migrate.finish("迁移到官方机器人：先向官方机器人发送 /迁移 领取迁移码，再把「/迁移 迁移码」发给我。")
    if _throttled(event.user_id):
        await migrate.finish("尝试次数过多，请 10 分钟后再试。")
    entry = _codes.get(code)
    if entry is None or entry[1] < time.time():
        await migrate.finish("迁移码不正确或已过期，请向官方机器人重新发送 /迁移 领取。")
    _codes.pop(code, None)
    idmap.link_user(entry[0], event.user_id)
    logger.info(f"official bridge: {event.user_id} linked by migration code")
    await migrate.finish("迁移完成。现在向官方机器人发送指令，使用的就是你原来的绑定和记录。")

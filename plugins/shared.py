"""几个插件共用的小工具（普通模块，不是插件）。

- write_json：原子地写 JSON 文件；
- first_bot：定时任务里取一个可用的 OneBot V11 机器人；
- tell_superusers：私聊告诉每个超级用户一件事；
- bot_left_group：通知是“机器人自己不在这个群了”（被移出，或群已解散），各插件据此清掉该群的数据；
- unprompted_refused：主动发到群里的消息被拒，因为这个群没开「机器人主动在群聊内发言」；
- heybox_hint：抽卡记录页面上提醒成员可以从小黑盒补全更早的记录；
- private_answer：群里的一条指令正把回复私聊发给本人（plugins/skland_bind_sign 设，skland_shortcuts 的发送钩子照着改发送对象）。
"""

from __future__ import annotations

import contextlib
import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from nonebot.adapters import Event


private_answer: ContextVar[Any] = ContextVar("private_answer", default=None)  # 有 route(api, data) 方法的对象


def bot_left_group(event: Event) -> bool:
    """群成员减少的通知里，离开的是机器人自己。"""
    return getattr(event, "notice_type", None) == "group_decrease" and str(getattr(event, "user_id", "")) == str(getattr(event, "self_id", None))


UNPROMPTED_REFUSED = 40_034_105  # 官方平台：群里没开「机器人主动在群聊内发言」，重试也一样


def unprompted_refused(e: Exception) -> bool:
    """主动发到群里的消息被拒，因为这个群没开「机器人主动在群聊内发言」：那个群什么也没收到。

    桥接回的是 OneBot 的 ActionFailed，错误码在 info 里（它没有 retcode 属性）。
    """
    return (getattr(e, "info", None) or {}).get("retcode") == UNPROMPTED_REFUSED


def write_json(path: Path, value: Any, **options: Any) -> None:
    """把 value 写成 JSON：先写同目录下的临时文件（权限 600），再整个替换，读的一方不会读到写了一半的文件。

    options 原样交给 json.dumps；不写 ensure_ascii 时中文按原样写入。
    """
    options.setdefault("ensure_ascii", False)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, **options), "utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def first_bot():
    """当前连着的第一个 OneBot V11 机器人（就是桥接的官方机器人），没有时为 None。"""
    from nonebot import get_bots
    from nonebot.adapters.onebot.v11 import Bot

    return next((bot for bot in get_bots().values() if isinstance(bot, Bot)), None)


async def tell_superusers(text: str, who: str) -> None:
    """私聊发给每个超级用户，绝不发到群里；who 是写日志时的插件名。"""
    from nonebot import get_driver, logger

    bot = first_bot()
    if bot is None:
        logger.warning(f"{who}: no bot online, superusers not told")
        return
    for user_id in get_driver().config.superusers:
        try:
            await bot.send_private_msg(user_id=int(user_id), message=text)
        except Exception as e:
            logger.warning(f"{who}: a superuser could not be told ({type(e).__name__})")


HEYBOX_IMPORT = "/导入小黑盒 小黑盒ID"  # 一次补两款游戏
HEYBOX_HINT = f"官方接口只提供近期记录，如果你在小黑盒有更早的抽卡记录，可发送 {HEYBOX_IMPORT} 补全"
HEYBOX_GAP_DAYS = 30  # 建号之后这么久还没有一条官方记录，才算可能缺了早期的抽卡（新玩家建号当天就会抽新手池）
HEYBOX_HINT_TIMES = 3  # 每个角色最多提醒几次：没用过小黑盒的老玩家不会一直被提醒
HEYBOX_HINTS_FILE = Path(__file__).resolve().parents[1] / "data" / "shared" / "heybox_hints.json"


def heybox_hint(earliest: int | None, imported: bool, app_code: str, created: float | None, character_id: int) -> str:
    """抽卡记录页面上的提醒：返回一句话，或空字符串。

    earliest：最早一条抽卡记录的时间；imported：有没有导入过小黑盒的行（pos 为负）。
    要提醒：没导入过小黑盒，而建号之后过了 HEYBOX_GAP_DAYS 天才有第一条官方记录
    （官方接口只给近几个月，更早的抽卡查不到了）。建号时间不知道时不提醒。有没有小黑盒数据这边看不出来，
    所以每个角色最多提醒 HEYBOX_HINT_TIMES 次。绑定后的通知可能没送到，看记录时再说。
    """
    if earliest is None or imported or not created or earliest - created < HEYBOX_GAP_DAYS * 86400:
        return ""
    try:
        shown = json.loads(HEYBOX_HINTS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        shown = {}
    key = str(character_id)
    if shown.get(key, 0) >= HEYBOX_HINT_TIMES:
        return ""
    shown[key] = shown.get(key, 0) + 1
    with contextlib.suppress(OSError):
        HEYBOX_HINTS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(HEYBOX_HINTS_FILE, shown)
    return HEYBOX_HINT

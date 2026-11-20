"""成员身份中两个机器人对照不出来的部分。

成员被认出 QQ 号后，原先在占位编号下绑定的东西仍归他；成员也可以自己证明是哪个 QQ 号（认领）。
原机器人下线后这两件事仍然需要。
"""

from __future__ import annotations

import asyncio

from nonebot import get_bots, logger
from nonebot.adapters import Bot as AnyBot

from .bridge import BridgeBot
from .idmap import USER_BASE
from .state import background, official_uins, driver, idmap

# ---------------------------------------------------------------- 成员被认出前做过的绑定跟着他走


def carry_plan(old_bind: int | None, new_bind: int | None, old_accounts: int, new_accounts: int) -> str | None:
    """占位编号被认出是某个 QQ 号时，用户表里该怎么处理。

    old_bind / new_bind：两个号在用户表里各自对应的身份（None：没有记录）。
    "rename"：占位编号的记录改成 QQ 号的。"repoint"：QQ 号的记录改指向占位编号的身份
    （QQ 号自己的身份下没有账号）。"apart"：两边都有账号，需要人工合并。None：无需迁移。
    """
    if old_bind is None or old_bind == new_bind:
        return None
    if new_bind is None:
        return "rename"
    if not old_accounts:
        return None
    return "apart" if new_accounts else "repoint"


async def _carry_records(before: int, number: int) -> None:
    """以占位编号服务的成员可能在该编号下绑了森空岛账号：迁移过去，仍归他所有。"""
    if before < USER_BASE or number >= USER_BASE:
        return
    try:
        from nonebot_plugin_orm import get_session
        from sqlalchemy import text
    except ImportError:
        return
    try:
        async with get_session() as session:
            async def bind_of(platform_id: int) -> int | None:
                return (await session.execute(text("SELECT bind_id FROM nonebot_plugin_user_bind WHERE platform_id = :p"), {"p": str(platform_id)})).scalar()

            async def accounts(owner: int | None) -> int:
                return 0 if owner is None else int((await session.execute(text("SELECT COUNT(*) FROM skland_user WHERE owner_id = :o"), {"o": owner})).scalar() or 0)

            old, new = await bind_of(before), await bind_of(number)
            plan = carry_plan(old, new, await accounts(old), await accounts(new))
            if plan == "rename":
                await session.execute(text("UPDATE nonebot_plugin_user_bind SET platform_id = :n WHERE platform_id = :p"), {"n": str(number), "p": str(before)})
            elif plan == "repoint":
                await session.execute(text("UPDATE nonebot_plugin_user_bind SET bind_id = :b WHERE platform_id = :n"), {"b": old, "n": str(number)})
            elif plan == "apart":
                logger.warning(f"official bridge: member {number} holds Skland accounts under that number and under the placeholder {before} as well: left apart, to be merged by hand")
            if plan in ("rename", "repoint"):
                await session.commit()
                logger.info(f"official bridge: what member {number} had bound as {before} before being recognised is theirs again ({plan})")
    except Exception as e:
        logger.warning(f"official bridge: records of {before} not carried over to {number} ({type(e).__name__}: {e})")


_carrying: dict[int, asyncio.Task] = {}  # 成员刚拿到的号 -> 正在迁移到该号的原有记录


def _on_link(before: int, number: int) -> None:
    try:
        background.add(task := asyncio.get_running_loop().create_task(_carry_records(before, number)))
    except RuntimeError:
        return  # 没有事件循环：没有在运行的东西，也就不可能绑定过什么
    _carrying[number] = task
    task.add_done_callback(background.discard)
    task.add_done_callback(lambda done: _carrying.pop(number, None) if _carrying.get(number) is done else None)


async def carried(number: int, wait: float = 5.0) -> None:
    """如果成员被认出前绑定的东西正在迁移到这个号下，等它迁移完。"""
    task = _carrying.get(int(number))
    if task is not None:
        try:
            await asyncio.wait_for(asyncio.shield(task), wait)
        except Exception:  # 超时或失败：照旧不带这些记录回应成员
            pass


idmap.on_link = _on_link

# ---------------------------------------------------------------- 认领：成员通过别的途径证明自己是哪个 QQ 号
#
# 平台从不告诉一个 OpenID 对应哪个 QQ 号；原机器人下线后，也没法再靠两个机器人收到同一条消息来对照。
# 有办法自行证明成员身份的插件（plugins/skland_bind_sign：成员在私聊里登录一个绑定了 QQ 号的
# 森空岛账号）把证明交到这里。


def recognised(number: str | int) -> bool:
    """这个号是否是官方机器人认识的成员：以占位编号服务的成员，或已关联 OpenID 的 QQ 号。"""
    if not str(number).isdigit():
        return False
    row = idmap.user(int(number))
    return row is not None and (int(number) >= USER_BASE or bool(row["linked"]))


def served_as_placeholder(bot: AnyBot, uid: int) -> bool:
    """本桥接以占位编号服务、尚未被认出任何 QQ 号的成员返回 True。"""
    return isinstance(bot, BridgeBot) and uid >= USER_BASE and idmap.user_openid(uid) is not None


def claimable(numbers: list[int]) -> bool:
    """用户表里同一身份的这些号能否被认领：其中没有任何一个是官方机器人已认识的人。

    特权号（超级用户、机器人）一律不行：要被当成这些号，光在别处登录一次是不够的。
    """
    barred = {str(s) for s in driver.config.superusers} | {b.self_id for b in get_bots().values()} | official_uins
    if not numbers or any(n >= USER_BASE or str(n) in barred for n in numbers):
        return False
    return not any((row := idmap.user(n)) is not None and row["linked"] for n in numbers)


def claim_number(bot: AnyBot, placeholder: int, numbers: list[int]) -> int | None:
    """把以 `placeholder` 服务的成员认定为 `numbers` 背后的人；返回他今后使用的号。"""
    if not served_as_placeholder(bot, placeholder) or not claimable(numbers):
        return None
    openid = idmap.user_openid(placeholder)
    number = numbers[0]
    idmap.link_user(openid, number)
    logger.info(f"official bridge: member {placeholder} recognised as {number} (proved by a login of their own)")
    return number

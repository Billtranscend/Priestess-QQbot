"""成员的各项功能使用哪个森空岛角色，基于 nonebot-plugin-skland 的数据模型。

一个成员（nonebot-plugin-user 的 id，即 `SkUser.owner_id`）可以绑定多个森空岛账号，每个账号下
又有多个角色。本项目的排行榜、统计和理智查询按每个成员每个游戏只用一个角色：成员的默认角色
（`skland_character_default`），由 `/skl切换终末地角色` 或 switch_default 修改。例外：持有统计统计每个账号的
每个角色（member_roles）；卫戍榜取每个成员奖杯最多的明日方舟角色。
"""

from __future__ import annotations

from nonebot_plugin_skland.account import exclusive_account_operation
from nonebot_plugin_skland.db_handler import get_default_character, get_user_characters, set_default_character
from nonebot_plugin_skland.exception import AccountOperationInProgress
from nonebot_plugin_skland.model import Character, CharacterDefault, SkUser
from nonebot_plugin_user.models import Bind
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

PLATFORM = "QQClient"


async def owner_id_of(session, qq: str | int) -> int | None:
    """某个 QQ 号对应的成员 id（前提是这个 QQ 和机器人说过话）。"""
    return (await session.scalars(select(Bind.bind_id).where(Bind.platform == PLATFORM, Bind.platform_id == str(qq)))).first()


async def member_numbers(session) -> dict[int, set[str]]:
    """成员 id -> 他被认出的号码（官方机器人上一个成员可能有占位编号和后来认出的 QQ 号）。"""
    numbers: dict[int, set[str]] = {}
    for owner_id, number in await session.execute(select(Bind.bind_id, Bind.platform_id).where(Bind.platform == PLATFORM)):
        numbers.setdefault(owner_id, set()).add(str(number))
    return numbers


def default_roles(app_code: str = "endfield"):
    """一次性 SELECT 出某个游戏里所有成员默认角色的 (owner_id, SkUser, Character)。"""
    return (
        select(CharacterDefault.owner_id, SkUser, Character)
        .join(Character, Character.id == CharacterDefault.character_id)
        .join(SkUser, SkUser.id == Character.account_id)
        .where(CharacterDefault.app_code == app_code, Character.app_code == app_code, SkUser.owner_id == CharacterDefault.owner_id)
    )


async def member_roles(session, owner_id: int, app_code: str = "endfield") -> list[tuple[SkUser, Character]]:
    """成员在某个游戏里绑定的所有（账号，角色）。"""
    rows = await session.execute(
        select(SkUser, Character).join(Character, Character.account_id == SkUser.id).where(SkUser.owner_id == owner_id, Character.app_code == app_code)
    )
    return [(row[0], row[1]) for row in rows]


async def default_role(session, owner_id: int, app_code: str = "endfield") -> tuple[SkUser, Character] | None:
    """成员的指令在某个游戏里使用的（账号，角色），没有时为 None。"""
    row = (await session.execute(default_roles(app_code).where(CharacterDefault.owner_id == owner_id))).first()
    return (row[1], row[2]) if row else None


async def other_roles(session, owner_id: int, app_code: str = "endfield") -> list[tuple[int, Character]]:
    """成员某个游戏里默认角色以外的角色和它们的序号；只有一个角色时为空。序号是角色在上游
    get_user_characters 里的位置，同 /skl角色 卡片和 /skl切换…角色。"""
    roles = await get_user_characters(owner_id, app_code, session)
    if len(roles) < 2:
        return []
    default = await get_default_character(owner_id, app_code, session)
    return [(number, role) for number, role in enumerate(roles, 1) if default is None or role.id != default.id]


async def switch_default(session, owner_id: int, character_id: int, app_code: str = "endfield") -> bool:
    """把成员某个游戏的默认角色换成这个角色并提交，同 /skl切换…角色。换了为 True；已经是默认、
    成员正在绑定解绑或更新角色、角色已不属于他时不换，为 False。"""
    try:
        async with exclusive_account_operation(owner_id):
            current = await default_role(session, owner_id, app_code)
            if current is not None and current[1].id == character_id:
                return False
            await set_default_character(owner_id, app_code, character_id, session)
            await session.commit()
    except AccountOperationInProgress:
        return False
    except (IntegrityError, ValueError):
        await session.rollback()
        return False
    return True

"""/理智：明日方舟和终末地默认角色当前的理智，以及多久后回满。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from nonebot import on_command, require
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_skland")
require("nonebot_plugin_orm")
require("nonebot_plugin_user")

from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.api import SklandAPI
from nonebot_plugin_skland.exception import LoginException, RequestException, UnauthorizedException
from nonebot_plugin_skland.model import Character, SkUser
from nonebot_plugin_skland.schemas import CRED
from nonebot_plugin_skland.schemas.arknights.card import ArkCard
from nonebot_plugin_skland.schemas.endfield.card import EndfieldCard
from nonebot_plugin_skland.services.auth import refresh_credentials

from plugins.skland_roles import default_role, owner_id_of
from sqlalchemy import select

__plugin_meta__ = PluginMetadata(
    name="Sanity",
    description="明日方舟和终末地当前的理智，以及多久后回满。",
    usage="/理智",
    type="application",
)

GAMES = {"arknights": "明日方舟", "endfield": "终末地"}

sanity = on_command("理智", rule=strict, priority=5, block=True)


@dataclass
class Sanity:
    current: int
    maximum: int
    full_ts: float

    @property
    def is_full(self) -> bool:
        return self.current >= self.maximum or time.time() >= self.full_ts


@refresh_credentials
async def _ark_card(user: SkUser, uid: str) -> ArkCard:
    return await SklandAPI.ark_card(CRED(cred=user.cred, token=user.cred_token), uid)


@refresh_credentials
async def _endfield_card(user: SkUser, char: Character) -> EndfieldCard:
    return await SklandAPI.endfield_card(
        CRED(cred=user.cred, token=user.cred_token), user_id=user.skland_user_id, role_id=char.role_id, server_id=char.channel_master_id
    )


async def _fetch(session, owner_id: int, game: str) -> Sanity | None:
    """成员在某个游戏里默认角色的理智（None：没有该角色）。"""
    selected = await default_role(session, owner_id, game)
    if selected is None:
        return None
    user, char = selected
    if game == "arknights":
        ap = (await _ark_card(user, str(char.uid))).status.ap
        return Sanity(ap.ap_now if ap.current < ap.max else ap.current, ap.max, float(ap.completeRecoveryTime))
    dungeon = (await _endfield_card(user, char)).dungeon
    cur, mx = int(dungeon.curStamina or 0), int(dungeon.maxStamina or 0)
    if mx <= 0 or not dungeon.maxTs:  # 森空岛没给上限或回满时间：说明不了什么，当作这次没读到（不能报“已回满”）
        return None
    return Sanity(cur, mx, float(dungeon.maxTs))


async def _member_for_qq(session, qq: str) -> int | None:
    """绑定了至少一个森空岛账号的 QQ 对应的成员 id。"""
    owner_id = await owner_id_of(session, qq)
    if owner_id is None or (await session.scalars(select(SkUser.id).where(SkUser.owner_id == owner_id).limit(1))).first() is None:
        return None
    return owner_id


def _remaining(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}小时{seconds % 3600 // 60}分"


@sanity.handle()
async def _(event: MessageEvent) -> None:
    lines = []
    async with get_session(autoflush=False) as session:  # 刷新的 token 留到最后才提交：查下一个游戏时不占着写锁
        user = await _member_for_qq(session, str(event.user_id))
        if user is None:
            await sanity.finish("未绑定森空岛账号，请先发送 /skl绑定", at_sender=True)
        for game, label in GAMES.items():
            try:
                state = await _fetch(session, user, game)
            except (RequestException, LoginException, UnauthorizedException) as e:
                lines.append(f"{label}：查询失败（{e.args[0] if e.args else type(e).__name__}）")
                continue
            if state is None:
                continue
            note = "已回满" if state.is_full else f"约 {_remaining(state.full_ts - time.time())}后回满"
            lines.append(f"{label}：理智 {min(state.current, state.maximum)}/{state.maximum}，{note}")
        await session.commit()
    await sanity.finish("\n".join(lines) or "没有找到已绑定的明日方舟或终末地角色", at_sender=True)

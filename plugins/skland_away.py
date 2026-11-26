"""疑似已离开的成员：不在机器人任何一个群里（有退群记录的群不算），也不是机器人好友。

发现后先停掉为他跑的定时任务（每晚签到、两款游戏的抽卡同步、卫戍榜统计、竞速和配装采集），他一被看见
（发言、进群、加好友）就恢复；每周体检时还是这样就解绑，体检当场才发现的也一样（plugins/skland_health，
删除前的备份留 14 天）。平台不给成员名单时，「不在任何群」是靠退群记录和见过谁推断的：只在私聊里用过机器人、
又删了好友的人，即使还在某个群里也会被当成离开；重新绑定就能再用。
两次查询之间露过一面也算：桥接记着每个号最后一次能证明在场的时间（last_present）。
等待重新绑定的人（官方机器人从未认出）怎么认、留多久不归这里管（见 plugins/skland_health/dormant.py），
但全员任务一样跳过他们：paused_owners 把两种人合在一起。

普通模块，不是插件：每个全员任务和体检（预览也是）开始时问一次、记下计时，所以不逐群读名单。
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from nonebot import get_driver, logger
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_user.models import Bind
from sqlalchemy import select

from plugins.shared import write_json

AWAY_DAYS = 0  # 发现后多少天才解绑：0 = 遇到的第一次每周体检（包括当场发现的那次）
AWAY_FILE = Path(__file__).resolve().parents[1] / "data" / "skland_health" / "away.json"


def away_members(binds: Iterable[tuple[str, int]], present: set[str], recognised: Callable[[str], bool],
                 kept: dict[int, float], now: float) -> dict[int, float]:
    """疑似已离开的成员（bind id）-> 第一次发现的时间。

    任何一个号码在场就算在；只算官方机器人认识的人。又被看见的人不在结果里，再离开时从头计时。
    """
    binds = list(binds)
    here = {owner for number, owner in binds if number in present}
    known = {owner for number, owner in binds if owner not in here and recognised(number)}
    return {owner: kept.get(owner, now) for owner in known}


def _load() -> dict[int, float]:
    try:
        kept = {int(owner): float(since) for owner, since in json.loads(AWAY_FILE.read_text("utf-8")).items()}
        return {owner: since for owner, since in kept.items() if math.isfinite(since) and since > 0}
    except FileNotFoundError:
        return {}
    except Exception as e:  # 读不了就从头计时：只会晚解绑
        logger.warning(f"Skland away members: the kept list could not be read ({type(e).__name__}), counting starts again")
        return {}


def _save(away: dict[int, float]) -> None:
    try:
        AWAY_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_json(AWAY_FILE, {str(owner): since for owner, since in away.items()})
    except OSError as e:  # 存不下：任务照停，计时不前进
        logger.warning(f"Skland away members: the list could not be saved ({type(e).__name__})")


async def away_owners(session) -> dict[int, float] | None:
    """现在疑似已离开的所有者 -> 第一次发现的时间；查不清时为 None（谁也不停、谁也不解绑）。"""
    bridge = sys.modules.get("plugins.official_bridge")  # 离线测试里没有它；绝不从这里导入
    if bridge is None:
        return None
    try:
        rows = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.bind_id.in_(select(SkUser.owner_id))))).all()
        present = {str(number) for number in bridge.present_numbers()}
        if not present:  # 机器人总见过一些人：空的是没读到，不是真的没有
            logger.warning("Skland away members: nobody at all is present, so it was not read; nobody counts as away")
            return None
        superusers = {str(user) for user in get_driver().config.superusers}
        binds, kept, now = [(str(number), owner) for number, owner in rows], _load(), time.time()
        away = away_members(binds, present | superusers, bridge.recognised, kept, now)
        counting = {number: owner for number, owner in binds if owner in away and owner in kept}
        if counting:  # 计时期间露过面：从最后一次露面重新算
            for number, at in bridge.last_present(list(counting)).items():
                owner = counting[str(number)]
                away[owner] = max(away[owner], min(at, now))
    except Exception as e:  # 不写 str(e)：数据库报错的文字带着 SQL 和参数
        logger.warning(f"Skland away members: who is away could not be told ({type(e).__name__}); nobody counts as away")
        return None
    if away != kept:
        _save(away)
    return away


async def superuser_owners(session) -> set[int]:
    """绑着超级用户号码的所有者：不按离开解绑，也不按 14 天规则清（plugins/skland_health）。"""
    superusers = {str(user) for user in get_driver().config.superusers}
    if not superusers:
        return set()
    return set(await session.scalars(select(Bind.bind_id).where(Bind.platform_id.in_(superusers))))


async def paused_owners(session) -> set[int]:
    """全员任务这一次要跳过的所有者：疑似已离开的，加上等官方机器人认出的；哪一种查不清，那一种就当作没有。"""
    paused = set(await away_owners(session) or ())
    health = sys.modules.get("plugins.skland_health")  # 它导入本模块，这里绝不导入它
    if health is not None:
        waiting, _ = await health.members_waiting(session)
        paused |= waiting or set()
    return paused


async def still_away(session, owner: int, since: float) -> bool:
    """删之前只对这一个人再查一次：从 since 起一直没露过面。查不清时（出错，或者他的号在映射库里
    一点痕迹都没有）为 False（不删）。"""
    bridge = sys.modules.get("plugins.official_bridge")
    if bridge is None:
        return False
    try:
        numbers = [str(number) for number, _ in (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.bind_id == owner))).all()]
        seen = bridge.last_present(numbers)
    except Exception as e:
        logger.warning(f"Skland away members: one member could not be looked at again ({type(e).__name__}); kept")
        return False
    return bool(seen) and all(at <= since for at in seen.values())

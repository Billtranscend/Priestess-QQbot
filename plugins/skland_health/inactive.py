"""角色都很久没登录游戏的森空岛账号（14 天规则）：判断用的纯函数，不涉及 nonebot、数据库和网络。

账号下每个明日方舟、终末地角色都超过 DAYS 天没登录游戏，每周体检就解绑这个账号并清除它的记录，不留备份。
最后登录时间：明日方舟看 player/info 的 status.lastOnlineTs，终末地看角色卡片的 base.lastLoginTime（都是秒）。
用机器人不算登录；每晚签到也不会改这两个时间。最近 DAYS 天里有官方抽卡记录的角色一定登录过，不用再问。
FROM 之前的体检只数人数、不删。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

DAYS = 14
DAY = 86400
FROM = datetime(2026, 12, 7, tzinfo=timezone(timedelta(hours=8))).timestamp()  # 北京时间 12-07 零点起才真的清除
INACTIVE, ACTIVE, UNCLEAR = "inactive", "active", "unclear"


def login_of(app_code: str, payload: dict) -> int | None:
    """回答里的最后登录时间（秒）；没有、不是整数、不是 10 位的秒数时为 None。"""
    raw = _dig(payload, ("data", "status", "lastOnlineTs") if app_code == "arknights" else ("data", "detail", "base", "lastLoginTime"))
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 10**9 <= value < 10**10 else None


def _dig(payload, keys: tuple[str, ...]):
    """一层层往下取；哪一层不是字典（森空岛维护时会回别的东西）就是 None。"""
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def pulled_lately(pulls: list[int | None], now: float) -> bool:
    """有角色最近 DAYS 天里有官方抽卡记录：一定登录过。"""
    return any(p is not None and now - p <= DAYS * DAY for p in pulls)


def verdict(logins: list[int | None], pulls: list[int | None], now: float) -> str:
    """一个账号的结论。logins、pulls 按角色一一对应：最后登录时间、最后一次官方抽卡的时间（没有为 None）。

    UNCLEAR：没有角色；有角色读不到；有登录时间在将来；或者登录之后一天以上还有官方抽卡（这个登录时间不对）。
    INACTIVE：每个角色都超过 DAYS 天没登录。其余是 ACTIVE。
    """
    if not logins or any(t is None or t > now + DAY for t in logins):
        return UNCLEAR
    if any(p is not None and p > t + DAY for t, p in zip(logins, pulls)):
        return UNCLEAR
    return INACTIVE if all(now - t > DAYS * DAY for t in logins) else ACTIVE

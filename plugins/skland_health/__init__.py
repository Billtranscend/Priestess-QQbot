"""每周的森空岛绑定体检（周一 04:00，Asia/Shanghai），以及签到的网络重试。

对每个已绑定的森空岛账号（一个成员可以有多个）：
  1. 验证登录（请求绑定列表；过期时刷新 cred_token / 重新授权 cred）。森空岛明确说登录不能用了
     （绑定列表说 cred 失效、或续期本身也被拒得明白，授权又说登录已过期）-> 解绑该账号；续期只回了
     说不清的错误、授权说已过期 -> 连续两个周一都这样才解绑，中间登录正常一次就从头算；换 cred 那一步
     的报错永远不算过期。其他任何回答（API 繁忙时的错误码）都不算定论：这个账号先不动，等下一次运行；
  2. 按这次读到的绑定列表更新角色：加新角色、改昵称和等级。森空岛在列表里标着已删除或已封禁的角色，
     连同抽卡记录直接清除，不写备份；列表里根本没有了的已存角色照样保留，只在报告里列出（森空岛有时
     返回的列表不完整）；读不懂列表时这个账号这次不同步；
  3. 用只读的签到状态查询探测每个明日方舟 / 终末地角色。删一个角色要两个信号：它已不在这次的绑定
     列表里，而且探测被拒（隔 RECHECK_GAP 秒问两次；讲 token、登录、签名时间的错误码、没有错误码的回答
     都不算）。两次清理之间隔 5 到 9 天都这样才删（只靠周一定时运行时就是连续两个周一）；中间哪次只有一个
     信号或探测正常，就从头算。终末地绑定下的空记录（没有角色）不算角色，照旧当场移除；
  4. 只要还有任何一个角色能用或还留着，账号本身就保留：只有登录失效，或者它所有真实角色都到了该删的时候，
     才连同角色一起解绑。每种原因一次最多删 max(CAP_MIN, CAP_SHARE)，超过的这次保留、被拒的记录照样连续，
     下次或 /森空岛体检 清理 不限 时再删；
  0. 疑似已离开的成员（不在机器人任何一个群里，也不是机器人好友，见 plugins/skland_away.py）先停掉为他们
     跑的定时任务；每周体检时还是这样（体检当场才发现的也一样），在为他发出任何森空岛请求之前连同所有账号
     一起解绑，删之前再查一次。发现之后又露过面的只查登录（过期的照样解绑），不同步、不探测角色。查不清谁离开、或查不清谁在等重新绑定
     （见 0b）时，这次谁都不按离开解绑；超级用户保留；
  0b. 官方 QQ 机器人接替了 OneBot 账号的地方（plugins/official_bridge）：通过旧账号绑定、官方机器人
     从未认出的成员，从旧账号不在任何群里那天起，不参加每晚签到，也不参加这里的任何检查，
     保留 45 天后解绑（见 dormant.py）。回来要靠他们自己：在私聊里扫码登录
     森空岛，就会把绑定交接过来；
  4b. 14 天规则（inactive.py）：账号下每个明日方舟、终末地角色都超过 14 天没登录游戏（最近 14 天有官方抽卡的
     不用问），连同角色和全部抽卡记录清除，不写备份。所有账号查完再一起处理：清之前在账号锁里重新问一遍；
     多得不正常（超过本次上限，或问不清的太多）就这次一个都不清。北京时间 12-07 之前只统计。超级用户名下的不算；
  5. 每次清理之后，某个游戏里仍有角色、却没有默认角色的成员，重新给他一个默认角色（森空岛自己有默认角色
     就用它，否则用最早的角色）。启动时也会做同样的修复。
网络失败从不被当作失效：试 3 次后，这次运行就跳过该账号。
每删一个账号或角色，提交之前先把删掉的行追加进这次运行的 backups/skland-health/<UTC 时间>.jsonl（一行一次删除，
带上这次删除的原因和登录时森空岛每一步的回答，权限 0600，见 backup.py），保留 BACKUP_DAYS 天；
写回数据库用 deploy/restore_health_backup.py。森空岛标着删除或封禁的角色直接清除，不写备份。
解绑整个账号时拿着上游的账号锁、先重读一次凭证：成员正在绑定，或检查期间重新扫码换了登录，这次不删。

/森空岛体检             （超级用户）试运行，报告会移除什么
/森空岛体检 清理        （超级用户）运行并移除
/森空岛体检 清理 不限   （超级用户）同上，不设每种原因的上限

每晚的签到任务用 httpx 默认的 5 秒超时，超时就报告为角色失败；
ark_sign / endfield_sign / refresh_token、绑定列表 / 用户 id 查询，以及图片类指令（卡片、肉鸽、抽卡页面）
背后的只读查询，都套上了网络错误重试。登录相关的调用（二维码、授权码、cred）不按指令期望的方式报告
网络错误：它们也会重试，之后以 RequestException 抛出，这样连不上森空岛的绑定会回答“绑定失败”，
而不是悄无声息地结束。

每晚签到在这里：00:05 一轮签完两款游戏（换掉了上游 00:15 明日方舟、00:20 终末地两个任务），每个账号一个会话，
处理完就保存。见文件末尾的 run_daily_sign（/全体签到 也用它，plugins/skland_shortcuts）。超级用户自己的账号在 00:05 那轮
说登录已过期时私聊告诉他一次，重新签上之前不再说；周检解绑了他自己登录过期的账号，报告里单独说一句。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from nonebot import get_driver, logger, on_command, require
from nonebot.adapters import Message
from nonebot.compat import type_validate_python
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from plugins.strict_command import strict

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")

from apscheduler.jobstores.base import JobLookupError
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_orm import get_scoped_session, get_session
from nonebot_plugin_skland import tasks
from nonebot_plugin_skland.api import SklandAPI, SklandLoginAPI
from nonebot_plugin_skland.api.login import skland_app_code
from nonebot_plugin_skland.account import (
    _persistent_role_snapshot,
    apply_planned_defaults,
    build_bound_roles_plan,
    exclusive_account_operation,
    reconcile_account_characters,
)
from nonebot_plugin_skland.db_handler import get_account_characters, select_all_accounts
from nonebot_plugin_skland.exception import AccountOperationInProgress, RequestException
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord, SkUser
from nonebot_plugin_skland.player_data import ark_card_data
from nonebot_plugin_skland.schemas import CRED, BindingAccountSnapshot, BindingApp
from nonebot_plugin_user.models import Bind
from nonebot_plugin_skland.services import sign as sign_service
from sqlalchemy import delete, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from . import backup, inactive
from .dormant import KEEP_DAYS, days_left, overdue, waiting_owners
from plugins.shared import tell_superusers, write_json
from plugins.skland_away import AWAY_DAYS, away_owners, paused_owners, still_away, superuser_owners

__plugin_meta__ = PluginMetadata(
    name="Skland health check",
    description="每周一 04:00 清理失效的森空岛绑定（含不在机器人任何群、也不是好友的成员），并为签到增加网络重试。",
    usage="/森空岛体检 [清理 [不限]]（超级用户）",
    type="application",
)

BASE = "https://zonai.skland.com/api/v1"
BINDING_URL = f"{BASE}/game/player/binding"
ARK_ATTENDANCE_URL = f"{BASE}/game/attendance"
EF_ATTENDANCE_URL = "https://zonai.skland.com/web/v1/game/endfield/attendance"
REFRESH_URL = f"{BASE}/auth/refresh"
CRED_URL = f"{BASE}/user/auth/generate_cred_by_code"
GRANT_URL = "https://as.hypergryph.com/user/oauth2/v2/grant"
PLAYER_INFO_URL = f"{BASE}/game/player/info"  # 明日方舟：status.lastOnlineTs
EF_CARD_URL = "https://zonai.skland.com/web/v1/game/endfield/card/detail"  # 终末地：base.lastLoginTime
CHECKED_APPS = ("arknights", "endfield")
GAP = 0.6  # 同一个 worker 两次请求之间的秒数
WORKERS = 5  # 同时检查或签到的账号数（签到会改森空岛那边的状态，没有拿更多 worker 试过）
RECHECK_GAP = 8  # 被拒绝的角色隔多少秒再问第二次
NO_ROLE = "终末地绑定下没有角色（仅移除这条空记录）"
EXPIRED = "登录已过期"  # 当场解绑的账号状态
DOUBTFUL = "续期被拒、授权说登录已过期"  # 续期的回答说不清：连续两个周一都这样才解绑
REQUEST_CODES = (10000, 10002, 10003)  # 讲 token、登录、签名时间的回答：说的是这次请求，不是角色
CAP_MIN, CAP_SHARE = 30, 0.05  # 每种原因一次最多删 max(CAP_MIN, 检查数 × CAP_SHARE) 个
CAPPED = {"invalid_accounts": "accounts", "empty_accounts": "accounts", "left_accounts": "accounts", "invalid_roles": "roles",
          "erased_roles": "roles", "erased_accounts": "accounts", "inactive_accounts": "accounts"}
ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "backups" / "skland-health"
BACKUP_DAYS = 14  # 删除前的备份留这么多天（两个周一），之后每次清理时删掉
AUTO_GACHA_QUEUE = ROOT / "data" / "skland_auto_gacha" / "queue.sqlite3"  # 终末地
ARK_GACHA_QUEUE = ROOT / "data" / "skland_auto_gacha" / "ark_queue.sqlite3"  # 明日方舟
EXPIRY_TOLD = ROOT / "data" / "skland_health" / "expiry_told.json"  # 已经私聊告诉过超级用户“登录已过期”的、他自己的账号 id

checkup = on_command("森空岛体检", rule=strict, permission=SUPERUSER, priority=5, block=True)
_lock = asyncio.Lock()


class NetworkError(Exception):
    """重试后仍连不上森空岛；这不说明账号有任何问题。"""


def _why(e: Exception) -> str:
    """日志里写的出错原因：异常类型，数据库报错再加驱动的说明（例如 database is locked）。
    不写 str(e)：SQLAlchemy 的报错文字带着 SQL 和参数（含凭证）；驱动的说明 e.orig 不带。"""
    if isinstance(e, DBAPIError):
        return f"{type(e).__name__}: {str(e.orig)[:80]}"
    return type(e).__name__


# ── 区分错误类型的 HTTP 请求 ────────────────────────────────────


async def _http(method: str, url: str, **kwargs) -> dict:
    last = "error"
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, url, **kwargs)
            payload = response.json()
            if isinstance(payload, dict):
                return payload
            last = "bad payload"
        except (httpx.HTTPError, ValueError) as e:
            last = type(e).__name__
        if attempt < 2:  # 最后一次失败后直接放弃，不等
            await asyncio.sleep(3 * (attempt + 1))
    raise NetworkError(last)


async def _signed_get(url: str, user: SkUser, extra: dict | None = None) -> dict:
    cred = CRED(cred=user.cred, token=user.cred_token)
    headers = await SklandAPI.get_sign_header(cred, url, method="get")
    return await _http("GET", url, headers={**headers, **(extra or {})})


async def _refresh_token(user: SkUser) -> tuple[object, str] | None:
    """续期 cred_token：成功返回 None，否则返回森空岛的（错误码，说法）。"""
    payload = await _http("GET", REFRESH_URL, headers={**SklandLoginAPI._headers, "cred": user.cred})
    token = (payload.get("data") or {}).get("token")
    if payload.get("code") == 0 and token:
        user.cred_token = token
        return None
    return payload.get("code"), str(payload.get("message") or "")


async def _regrant(user: SkUser) -> tuple[bool, str, str]:
    """用保存的登录凭证重新换 cred：（成功与否，森空岛的说法，没成功的那一步 "grant" 或 "cred"）。"""
    if not user.access_token:
        return False, "没有保存登录凭证", "grant"
    grant = await _http(
        "POST", GRANT_URL, json={"appCode": skland_app_code, "token": user.access_token, "type": 0}, headers={**SklandLoginAPI._headers}
    )
    code = (grant.get("data") or {}).get("code")
    if grant.get("status") != 0 or not code:
        return False, str(grant.get("msg") or grant.get("message") or grant.get("status")), "grant"
    payload = await _http("POST", CRED_URL, json={"code": code, "kind": 1}, headers={**SklandLoginAPI._headers})
    data = payload.get("data") or {}
    if payload.get("code") != 0 or not data.get("cred") or not data.get("token"):
        return False, str(payload.get("message") or payload.get("code")), "cred"
    user.cred, user.cred_token = data["cred"], data["token"]
    return True, "", ""


def _says_expired(said: str) -> bool:
    """森空岛明确说登录不能用了（“登录已过期，请重新登录”之类）；限流、繁忙这类回答不算。"""
    return any(word in said for word in ("过期", "失效", "重新登录", "未登录", "没有保存登录凭证"))


_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("skland_health_trace", default=None)


async def _login(user: SkUser) -> tuple[str, list | None]:
    """返回 ("ok", 绑定列表；回答里没有列表时为 None) 或 (原因, None)。森空岛要求时续期 cred_token / cred。
    体检时（_trace 里有字典）记下每一步森空岛的回答（错误码和说法的开头，不含号码），删账号时写进备份。"""
    trace = _trace.get()
    trace = {} if trace is None else trace
    code = None
    for _ in range(3):
        payload = await _signed_get(BINDING_URL, user)
        code = trace["binding"] = payload.get("code")
        if code == 0:
            return "ok", (payload.get("data") or {}).get("list")
        refused = None
        if code == 10000:
            refused = await _refresh_token(user)
            if refused is None:
                continue
            trace["refresh"] = [refused[0], refused[1][:24]]
        if code in (10000, 10002):
            granted, said, step = await _regrant(user)
            if granted:
                continue
            trace[step] = said[:24]
            # 授权成功就说明登录还在，换 cred 那一步的报错永远不算过期；限流、繁忙之类的回答说明不了什么，下次再查
            if step != "grant" or not _says_expired(said):
                return f"续期没有成功：{said[:24]}", None
            # 授权说登录已过期：绑定列表说 cred 失效（10002），或续期也被拒得明白，才是定论
            if code == 10002 or refused[0] in (10000, 10002) or _says_expired(refused[1]):
                return EXPIRED, None
            return DOUBTFUL, None
        break
    return f"接口错误 {code}", None


async def _role_state(user: SkUser, char: Character) -> tuple[str, object]:
    """探测一个角色：（"ok"、NO_ROLE 或写进报告的被拒说法，森空岛的错误码）。"""
    if char.app_code == "endfield":
        if not char.role_id:
            return NO_ROLE, None
        payload = await _signed_get(EF_ATTENDANCE_URL, user, {"sk-game-role": f"3_{char.role_id}_{char.channel_master_id}"})
    else:
        payload = await _signed_get(f"{ARK_ATTENDANCE_URL}?{urlencode({'uid': char.uid, 'gameId': char.channel_master_id})}", user)
    code = payload.get("code")
    return ("ok" if code == 0 else f"{char.app_code} {code} {str(payload.get('message') or '')[:24]}"), code


def _role_key(app_code: str, server_id: str, role_id: str) -> tuple[str, str, str]:
    return app_code, server_id, role_id


def _listed(user: SkUser, apps: list | None, stored: list[Character]) -> tuple[BindingAccountSnapshot, set[int], set[int], bool] | None:
    """这次读到的绑定列表 -> （补回了不可用的已存角色的快照，列表里根本没有的角色 id，标着删除或封禁的角色 id，
    清除标着的之后还有没有角色）；没有列表或读不懂时为 None。

    补回之后，按这个快照同步（上游的 reconcile）只会加角色、改昵称和等级，不会删任何已存角色；
    标着删除或封禁的由体检自己清除（计数、受上限约束）。
    """
    if apps is None:
        return None
    try:
        snapshot = BindingAccountSnapshot.from_apps(user.skland_user_id, type_validate_python(list[BindingApp], apps))
        available = {_role_key(r.app_code, r.server_id, r.game_role_id) for r in snapshot.roles if r.is_available}
        marked = {_role_key(r.app_code, r.server_id, r.game_role_id) for r in snapshot.roles if not r.is_available}
        gone = [c for c in stored if _role_key(c.app_code, c.channel_master_id, c.role_id) not in available]
        gone_keys = {_role_key(c.app_code, c.channel_master_id, c.role_id) for c in gone}
        snapshot.roles = [r for r in snapshot.roles if _role_key(r.app_code, r.server_id, r.game_role_id) not in gone_keys]
        snapshot.roles += [_persistent_role_snapshot(c) for c in gone]
    except Exception as e:  # 森空岛改了格式、这个账号没有记下森空岛用户 id、或已存的角色缺字段：这次不同步，也不按列表判断
        _list_errors[type(e).__name__] += 1
        return None
    unavailable = {c.id for c in gone if _role_key(c.app_code, c.channel_master_id, c.role_id) in marked}
    missing = {c.id for c in gone} - unavailable
    return snapshot, missing, unavailable, bool(available or any(c.role_id for c in gone if c.id in missing))  # 空的终末地占位不算有效角色


def _available(user: SkUser, apps: list | None) -> set[tuple[str, str, str]] | None:
    """这次绑定列表里可用的（没标删除、封禁）明日方舟、终末地角色；没有列表或读不懂时为 None。"""
    if apps is None:
        return None
    try:
        snapshot = BindingAccountSnapshot.from_apps(user.skland_user_id, type_validate_python(list[BindingApp], apps))
    except Exception:
        return None
    return {_role_key(r.app_code, r.server_id, r.game_role_id) for r in snapshot.roles if r.is_available and r.game_role_id}


async def _sync_roles(user: SkUser, snapshot: BindingAccountSnapshot, session) -> bool:
    """按快照更新这个账号的角色和默认角色（上游 sync_account 的做法，但用已经读到的列表、不删角色）。

    成员这时正在绑定或解绑（上游的同一把锁）、或者会删到任何角色时什么都不写，返回 False。
    """
    owner_id, account_id = user.owner_id, user.id
    try:
        async with exclusive_account_operation(owner_id):
            plan = await build_bound_roles_plan(owner_id, session, mode="overview", pending_snapshot=snapshot,
                                                pending_account_id=account_id, account_identity_overrides={account_id: snapshot.skland_user_id})
            await reconcile_account_characters(user, snapshot, session)
            if any(isinstance(row, (Character, GachaRecord)) for row in session.deleted):
                raise ValueError("the role sync would remove a role")
            await apply_planned_defaults(owner_id, plan.planned_defaults, session)
            await session.commit()
    except AccountOperationInProgress:
        return False
    except (IntegrityError, ValueError):
        await session.rollback()
        return False
    with contextlib.suppress(Exception):
        await ark_card_data.invalidate_account(account_id)
    return True


# ── 数据库修改 ────────────────────────────────────────────────────


def _row(obj) -> dict:
    return {column.name: getattr(obj, column.name) for column in obj.__table__.columns}


async def _lock_role(session, character_id: int) -> None:
    """删之前按抽卡同步保存时的顺序（角色、再账号，同一句 FOR UPDATE）锁住要删的行（PostgreSQL；SQLite 锁整个库，
    这句不加锁）：不会和正在保存的同步互相等死，备份读到的也是要删的全部记录，包括刚保存进来的。"""
    await session.execute(select(Character.id, SkUser.id).join(Character.account).where(Character.id == character_id).with_for_update())


async def _lock_account(session, account_id: int) -> None:
    """整个账号：先按编号锁它的每个角色，最后才锁账号。一句连表的 FOR UPDATE 会锁一个角色、账号、再下一个角色，
    这时另一个角色的保存（先锁角色再锁账号）正好能和它互相等。"""
    await session.execute(select(Character.id).where(Character.account_id == account_id).order_by(Character.id).with_for_update())
    await session.execute(select(SkUser.id).where(SkUser.id == account_id).with_for_update())


async def _remove_role(char: Character, session, export: dict) -> None:
    await _lock_role(session, char.id)
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.character_id == char.id))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"].append(_row(char))
    # 显式删除：不管数据库是否执行 ON DELETE CASCADE 都正确。
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id == char.id))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id == char.id))
    await session.execute(delete(Character).where(Character.id == char.id))
    await session.flush()


async def _remove_user(user: SkUser, session, export: dict) -> None:
    """解绑一个森空岛账号，连同它的角色及其记录。"""
    account_id = user.id
    await _lock_account(session, account_id)
    role_ids = select(Character.id).where(Character.account_id == account_id)
    records = (await session.scalars(select(GachaRecord).where(GachaRecord.character_id.in_(role_ids)))).all()
    export["skland_gacha_record"] += [_row(r) for r in records]
    export["skland_characters"] += [_row(c) for c in await get_account_characters(account_id, session)]
    export["skland_user"].append(_row(user))
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id.in_(role_ids)))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id.in_(role_ids)))
    await session.execute(delete(Character).where(Character.account_id == account_id))
    await session.execute(delete(SkUser).where(SkUser.id == account_id))
    await session.flush()
    with contextlib.suppress(Exception):
        await ark_card_data.invalidate_account(account_id)


async def _erase_role(char: Character, session) -> None:
    """删掉这个角色和它的抽卡记录、默认角色（未提交），不写备份；提交由调用方做。"""
    await _lock_role(session, char.id)
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id == char.id))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id == char.id))
    await session.execute(delete(Character).where(Character.id == char.id))
    await session.flush()


async def _erase_user(user: SkUser, session) -> None:
    """解绑这个账号，连同它的角色、默认角色和抽卡记录（未提交），不写备份；提交由调用方做。"""
    await _lock_account(session, user.id)
    role_ids = select(Character.id).where(Character.account_id == user.id)
    await session.execute(delete(GachaRecord).where(GachaRecord.character_id.in_(role_ids)))
    await session.execute(delete(CharacterDefault).where(CharacterDefault.character_id.in_(role_ids)))
    await session.execute(delete(Character).where(Character.account_id == user.id))
    await session.execute(delete(SkUser).where(SkUser.id == user.id))
    await session.flush()
    with contextlib.suppress(Exception):
        await ark_card_data.invalidate_account(user.id)


async def _drop_role(char: Character, session, export: Counter, why: dict | None = None) -> None:
    """删掉这个角色（未提交）并把删掉的追加进备份，why 是这次删除的原因；提交由调用方做。"""
    removed = backup.empty()
    await _remove_role(char, session, removed)
    if why:
        removed["why"] = why  # 备份和恢复只认三张表的键，多一个键不影响
    await _checkpoint(removed, export)


async def _drop_user(user: SkUser, session, export: Counter, why: dict | None = None) -> None:
    """解绑这个账号（未提交）并把删掉的追加进备份，why 是这次删除的原因；提交由调用方做。"""
    removed = backup.empty()
    await _remove_user(user, session, removed)
    if why:
        removed["why"] = why
    await _checkpoint(removed, export)


async def ensure_defaults(session, owner_ids: list[int] | None = None) -> int:
    """给每个成员在其有角色的每个游戏里设一个默认角色；返回设置了多少个。

    上游只在某个游戏绑定第一个角色时才自己选默认角色。默认角色随角色一起消失时（上面被移除，
    或者在 0.7.2 迁移中两个角色都标成默认而丢失），成员的指令就会要求先 `sk char set`。
    """
    owners = owner_ids if owner_ids is not None else list(await session.scalars(select(SkUser.owner_id).distinct()))
    fixed = 0
    for owner_id in owners:
        have = set(await session.scalars(select(CharacterDefault.app_code).where(CharacterDefault.owner_id == owner_id)))
        for app_code in CHECKED_APPS:
            if app_code in have:
                continue
            role_id = await session.scalar(
                select(Character.id)
                .join(Character.account)
                .where(SkUser.owner_id == owner_id, Character.app_code == app_code, Character.role_id != "")
                .order_by(Character.is_skland_default.desc(), Character.id)
                .limit(1)
            )
            if role_id is not None:
                session.add(CharacterDefault(owner_id=owner_id, app_code=app_code, character_id=role_id))
                fixed += 1
    await session.flush()
    return fixed


_export_path: Path | None = None  # 这次运行的备份文件；预览时为 None，什么都不写


async def _checkpoint(removed: dict, export: Counter) -> None:
    """删除提交之前先把这次删的追加进备份（一行）：运行中途出错或被重启，已经删掉的也都在备份里。
    写不成就抛出：调用方回滚，这个账号这次不删。export 累计这次运行备份了每张表多少行。"""
    if _export_path is None:
        return
    await asyncio.to_thread(backup.append, _export_path, removed)
    export.update({table: len(removed[table]) for table in backup.TABLES})


async def _backup_complete(path: Path, export: Counter) -> bool:
    """删完之后再读一遍备份：每行都读得出，每张表的行数等于这次删的。不抛出：核对不了也要照常报告结果。"""
    try:
        found = await asyncio.to_thread(backup.tally, path)
    except Exception as e:
        logger.warning(f"Skland health check: backup {path.name} not readable ({type(e).__name__})")
        return False
    if all(found[table] == export[table] for table in backup.TABLES):
        return True
    logger.warning(f"Skland health check: backup {path.name} holds {dict(found)} rows, {dict(export)} were removed")
    return False


SUSPECTS_FILE = ROOT / "data" / "skland_health" / "suspect_roles.json"
SECOND_STRIKE_AFTER = 5 * 86400  # 第一次被拒至少过了这么久、还被拒，才算第二次（每周一次，所以就是下一周）
STRIKE_EXPIRES = 9 * 86400  # 最近一次超过这么久（中间漏了一个周一）就不算连续，从头记
_suspects: dict[str, list] = {}  # "role:<id>" / "account:<id>" -> [第一次被拒的时间, 最近一次的时间, 自然键摘要]
_list_errors: Counter = Counter()  # 这次运行读不懂的绑定列表，按异常类型计数
_inactive_found: list[tuple[int, int, str | None]] = []  # 这次运行按 14 天规则该清的（账号, 所有者, 森空岛用户 id）：所有账号查完再一起处理
_superuser_owners: set[int] = set()  # 绑着超级用户号码的所有者：不按 14 天规则清（和按离开解绑一样免）


def _load_suspects() -> dict[str, list]:
    try:
        raw = json.loads(SUSPECTS_FILE.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:  # 读不了就当没有：最多是被拒的晚一周才删
        logger.warning(f"Skland health check: suspect roles not read ({type(e).__name__})")
        return {}
    # 旧格式（角色 id -> 时间）没有摘要，认不出是不是同一个角色：都不算
    return {str(k): [float(v[0]), float(v[1]), v[2]] for k, v in raw.items()
            if isinstance(v, list) and len(v) == 3 and all(isinstance(t, (int, float)) for t in v[:2]) and isinstance(v[2], str)}


def _save_suspects(suspects: dict[str, list]) -> None:
    SUSPECTS_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(SUSPECTS_FILE, suspects)


def _digest(*parts) -> str:
    """角色或账号的自然键摘要：数据库 id 删了会被重新用，被拒的记录只认同一个角色或账号。不存原始 UID。"""
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


def _fresh(key: str, digest: str, now: float) -> list | None:
    """同一个角色或账号、还连续着的记录（最近一次不超过 STRIKE_EXPIRES）。"""
    entry = _suspects.get(key)
    return entry if entry is not None and entry[2] == digest and now - entry[1] <= STRIKE_EXPIRES else None


def _due(key: str, digest: str, now: float) -> bool:
    """连续第二次被拒：第一次至少在 SECOND_STRIKE_AFTER 之前，之后一直连续。"""
    entry = _fresh(key, digest, now)
    return entry is not None and now - entry[0] >= SECOND_STRIKE_AFTER


def _note_strike(key: str, digest: str, now: float) -> None:
    """记下这一次（只在清理时）：连续的保留第一次的时间、更新最近一次，断了或换了角色就从头记。"""
    entry = _fresh(key, digest, now)
    if entry is None:
        _suspects[key] = [now, now, digest]
    else:
        entry[1] = now


async def _unchanged(session, user: SkUser) -> bool:
    """数据库里这个账号还在，凭证还是检查时手里的那份（绕过会话缓存直接读）。"""
    current = (await session.execute(select(SkUser.cred, SkUser.access_token).where(SkUser.id == user.id))).first()
    return current is not None and tuple(current) == (user.cred, user.access_token)


def _prune_backups(now: float) -> int:
    """删掉超过 BACKUP_DAYS 天的删除前备份，返回删了几个文件。"""
    if not BACKUP_DIR.is_dir():  # 还没有任何删除前备份
        return 0
    old = [path for path in BACKUP_DIR.iterdir() if path.suffix in (".json", ".jsonl") and path.is_file()
           and now - path.stat().st_mtime > BACKUP_DAYS * 86400]
    for path in old:
        path.unlink()
    return len(old)


def _drop_queue(user_ids: list[int]) -> None:
    """清掉已经没有账号的成员（所有者 id）在两款游戏队列里的抽卡同步。"""
    for path in (AUTO_GACHA_QUEUE, ARK_GACHA_QUEUE):
        if not user_ids or not path.exists():
            continue
        with contextlib.closing(sqlite3.connect(path, timeout=15)) as queue, queue:
            queue.execute(f"delete from pending where user_id in ({','.join('?' * len(user_ids))})", user_ids)


# ── 体检本身 ───────────────────────────────────────────────────────────


async def members_waiting(session) -> tuple[set[int] | None, float | None]:
    """通过旧机器人绑定、官方机器人从未认出的所有者，以及他们从什么时候开始等待。

    不涉及这个问题时（这里没有官方机器人，或者旧机器人还在某个群里）是空集合和 None；
    有官方机器人、却查不清楚时是 (None, None)，由调用方决定怎么办。
    """
    bridge = sys.modules.get("plugins.official_bridge")  # 离线测试里没有它；绝不从这里导入
    if bridge is None:
        return set(), None
    try:
        gone_at = bridge.old_bot_gone_at()
        if gone_at is None:
            return set(), None
        rows = (await session.execute(select(Bind.platform_id, Bind.bind_id).where(Bind.bind_id.in_(select(SkUser.owner_id))))).all()
        superusers = {str(user) for user in get_driver().config.superusers}
        return waiting_owners([(str(number), owner) for number, owner in rows], bridge.recognised, superusers), gone_at
    except Exception as e:
        logger.warning(f"Skland health check: who is waiting to be recognised could not be told ({_why(e)}); "
                       "the nightly sign leaves nobody out, the weekly check unbinds nobody for leaving")
        return None, None


# ── 14 天规则（plugins/skland_health/inactive.py）──────────────────


async def _last_official_pulls(session, roles: list[Character]) -> dict[int, int]:
    """每个角色最后一次官方抽卡的时间（不算从小黑盒导入的，那些位置是负数）。"""
    rows = await session.execute(
        select(GachaRecord.character_id, func.max(GachaRecord.gacha_ts))
        .where(GachaRecord.character_id.in_([char.id for char in roles]), GachaRecord.pos >= 0).group_by(GachaRecord.character_id))
    return {character_id: ts for character_id, ts in rows.all()}


async def _login_of(user: SkUser, char: Character) -> int | None:
    """森空岛给的这个角色最后一次登录游戏的时间（秒）；问不到、回答不对时为 None。"""
    if char.app_code == "endfield":
        url = f"{EF_CARD_URL}?" + urlencode({"roleId": char.role_id, "serverId": char.channel_master_id, "userId": user.skland_user_id})
    else:
        url = f"{PLAYER_INFO_URL}?" + urlencode({"uid": char.uid})
    payload = await _signed_get(url, user)
    return inactive.login_of(char.app_code, payload) if payload.get("code") == 0 else None


async def _inactive_state(user: SkUser, session, now: float, listed: set[tuple[str, str, str]] | None) -> tuple[str, int, list]:
    """（这个账号按 14 天规则的结论，问了森空岛没有：1 或 0，每个角色几天没登录）。最近有官方抽卡的不用问。

    listed 是这次绑定列表里可用的角色：没读到列表，或者列表里有数据库还没存的角色（新绑的、这次没同步上的），
    只看存着的角色会漏掉刚玩过的那个，所以不判。"""
    roles = [c for c in await get_account_characters(user.id, session) if c.app_code in CHECKED_APPS and c.role_id]
    if not roles or listed is None or not listed <= {_role_key(c.app_code, c.channel_master_id, c.role_id) for c in roles}:
        return inactive.UNCLEAR, 0, []
    pulls = await _last_official_pulls(session, roles)
    if inactive.pulled_lately(list(pulls.values()), now):
        return inactive.ACTIVE, 0, []
    logins = []
    for char in roles:
        logins.append(await _login_of(user, char))
        await asyncio.sleep(GAP)
    days = [f"{char.app_code} {'?' if t is None else int((now - t) // 86400)}d" for char, t in zip(roles, logins)]
    return inactive.verdict(logins, [pulls.get(char.id) for char in roles], now), 1, days


async def _settle_inactive(apply: bool, stats: Counter, touched: set[int]) -> None:
    """所有账号都查完以后处理 14 天规则找到的账号。不留备份，所以多得不正常（超过本次上限，或问不清的太多）
    就一个都不清；FROM 之前和预览只数人数。清之前在账号锁里重新问一遍。"""
    found = sorted(_inactive_found)
    stats["inactive_found"] = len(found)
    if not found:
        return
    if time.time() < inactive.FROM:
        stats["inactive_counting"] = 1  # 报告照这次说：规则还没开始，只统计
        return
    limit = _limits.get("inactive_accounts")
    if limit is not None and len(found) > limit:
        stats["inactive_braked"] = len(found)
    elif stats["inactive_unclear"] > max(3, stats["inactive_read"] // 10):
        stats["inactive_braked"], stats["inactive_braked_unclear"] = len(found), 1  # 「不限」也不解除这一条
    if stats["inactive_braked"]:
        logger.warning(f"Skland health check: {len(found)} inactive accounts found but none erased (over the cap, or too many unclear)")
        return
    if not apply:
        return
    for account_id, owner_id, skland_user_id in found:
        try:
            if await _erase_inactive(account_id, owner_id, skland_user_id, stats):
                touched.add(owner_id)
        except Exception as e:  # 一个账号出错不能停下其余的，也不能让已经清掉的不进报告
            stats["errors"] += 1
            logger.warning(f"Skland health check: inactive account {account_id} skipped after {_why(e)}")


async def _erase_inactive(account_id: int, owner_id: int, skland_user_id: str | None, stats: Counter) -> bool:
    """在上游的账号锁里重新登录、重新读绑定列表、重新问一遍，还是每个角色都超过 14 天没登录才清：
    账号连同角色、默认角色和全部抽卡记录，不留备份。扫描之后换了人（编号被别的账号用了、交给了别人、
    换了森空岛账号）或者成了超级用户的，都不清。"""
    try:
        async with exclusive_account_operation(owner_id):
            async with get_session(autoflush=False, expire_on_commit=False) as session:
                user = await session.get(SkUser, account_id)
                if user is None or user.owner_id != owner_id or user.skland_user_id != skland_user_id or owner_id in await superuser_owners(session):
                    stats["inactive_spared"] += 1
                    return False
                state, apps = await _login(user)
                found = (await _inactive_state(user, session, time.time(), _available(user, apps)))[0] if state == "ok" else inactive.UNCLEAR
                if found != inactive.INACTIVE:
                    stats["inactive_spared"] += 1  # 这期间登录过游戏，或者这次问不清
                    if session.dirty:
                        await _save_tokens(session, "Skland health check")  # 刚才续期的 token 留着
                    return False
                await _erase_user(user, session)
                await session.commit()
    except AccountOperationInProgress:
        stats["busy_skipped"] += 1
        return False
    except NetworkError:
        stats["network_skipped"] += 1
        return False
    stats["inactive_accounts"] += 1
    logger.info(f"Skland health check: erased account {account_id} (inactive_accounts, no backup)")
    return True


async def _check_account(account_id: int, session, apply: bool, away: dict[int, float], waiting: set[int], time_is_up: bool,
                         stats: Counter, reasons: Counter, export: Counter, touched: set[int]) -> None:
    """检查一个账号（run 的一个 worker 调用）；session 只给这一个账号用，不自动 flush、提交后不过期（见 run）。export 见 _checkpoint。

    写了什么就马上提交，绝不带着没提交的写入去等网络或 sleep：SQLite 同时只能有一个写事务，
    其他 worker 的写入会一直等到 database is locked。
    """
    user = await session.get(SkUser, account_id)
    if user is None:
        return
    owner_id = user.owner_id
    stats["accounts"] += 1
    trace: dict = {}  # 这次登录时森空岛每一步的回答
    _trace.set(trace)  # 每个 worker 是自己的任务，这里设的只对这个账号的检查有效

    async def remove(stat: str, reason: str, char: Character | None = None, probes: list[str] | None = None) -> bool:
        """记一次移除（char 为空时是整个账号），删了返回 True。清理时先删、追加备份、提交，都成了才记：
        备份写不成或提交失败会抛出，这次没删，也就不记。超过这种原因本次的上限时不删，记成 held_<stat>。

        解绑整个账号时拿着上游的账号锁，并先重读一次凭证：成员这时正在绑定或解绑，或者从开始检查到现在
        重新扫码换了登录，这次都不删（下次再看）。"""
        if stat in _limits and _taken[stat] >= _limits[stat]:
            stats[f"held_{stat}"] += 1
            reasons[f"{reason}（超过本次上限，先保留）"] += 1
            return False
        _taken[stat] += 1  # 先占上名额（中间没有 await，几个 worker 不会一起超过上限），没删成再退回
        why = {"stat": stat, "reason": reason, **({"login": trace} if trace else {}), **({"probes": probes} if probes else {})}
        try:
            if apply and char is not None:
                await (_erase_role(char, session) if stat == "erased_roles" else _drop_role(char, session, export, why))
                await session.commit()
                touched.add(owner_id)
            elif apply:
                async with exclusive_account_operation(owner_id):
                    if not await _unchanged(session, user):
                        _taken[stat] -= 1
                        stats["rebound_skipped"] += 1
                        return False
                    await (_erase_user(user, session) if stat == "erased_accounts" else _drop_user(user, session, export, why))
                    await session.commit()
                touched.add(owner_id)
        except AccountOperationInProgress:
            _taken[stat] -= 1
            stats["busy_skipped"] += 1
            return False
        except BaseException:
            _taken[stat] -= 1
            raise
        stats[stat] += 1
        reasons[reason] += 1
        if apply and stat == "invalid_accounts" and owner_id in _superuser_owners:
            stats["own_expired"] += 1  # 超级用户自己的账号：报告里单独说一句
        logger.info(f"Skland health check: removed {'an account' if char is None else 'a role'} ({stat})")
        return True

    if owner_id in waiting:
        # 官方机器人接手以来一直没认出的人：不为他们发任何请求，
        # 也不按他们在哪些群里来判断。
        if time_is_up:
            await remove("waiting_removed", f"账号：机器人更换后 {KEEP_DAYS} 天内没有重新绑定")
        else:
            stats["waiting_accounts"] += 1
        return
    since = away.get(owner_id)
    if since is not None and overdue(since, time.time(), AWAY_DAYS) and await still_away(session, owner_id, since):
        await remove("left_accounts", "账号：不在机器人所在的任何群，也不是机器人好友")
        return
    try:
        state, apps = await _login(user)
        if session.dirty:
            await session.commit()  # 续期后的 token 马上保存：后面还有同步和角色检查要等网络
        now, account_key = time.time(), f"account:{account_id}"
        if state == EXPIRED:
            await remove("invalid_accounts", f"账号：{state}")
            return
        if state == DOUBTFUL:
            account_digest = _digest(owner_id, user.skland_user_id)
            if _due(account_key, account_digest, now):
                if await remove("invalid_accounts", f"账号：{state}（连续两个周一）"):
                    _suspects.pop(account_key, None)
                elif apply:
                    _note_strike(account_key, account_digest, now)  # 被上限扣下：保持连续，下次或「不限」时照样解绑
                return
            stats["suspect_accounts"] += 1
            reasons[f"账号：{state}（第一次，下周一再查）"] += 1
            if apply:
                _note_strike(account_key, account_digest, now)
            return
        if state != "ok":
            # 其他任何回答（错误码、API 繁忙）都说明不了登录状态：
            # 账号和记录都保留，下次运行再看。
            stats["unclear_accounts"] += 1
            reasons[f"账号：{state}（未处理，下次再查）"] += 1
            return
        _suspects.pop(account_key, None)  # 登录正常：续期被拒的记录从头算
        if since is not None:
            return  # 疑似已离开：只查登录，别的森空岛请求都不为他发
        stored = [c for c in await get_account_characters(account_id, session) if c.app_code in CHECKED_APPS]
        listed = _listed(user, apps, stored)
        gone = None if listed is None else listed[1]
        unavailable = set() if listed is None else listed[2]
        if listed is not None and not listed[3]:
            # 一个有效角色都没有了（都标着删除或封禁、或者根本没有）：账号连同记录直接清除，不留备份
            await remove("erased_accounts", "账号：没有任何有效的明日方舟或终末地角色（直接清除，不留备份）")
            return
        if listed is None:
            stats["list_unread"] += 1
            reasons["账号：绑定列表没读到（这次不同步角色，也不按列表删）"] += 1
        elif gone:
            stats["listed_gone"] += len(gone)
            reasons["角色：森空岛绑定列表里没有了（保留，交给角色检查）"] += len(gone)
        if apply and listed is not None:
            if not await _sync_roles(user, listed[0], session):
                stats["sync_failed"] += 1
                logger.info("Skland health check: role list not refreshed for one account")
                stored = [c for c in await get_account_characters(account_id, session) if c.app_code in CHECKED_APPS]  # 回滚让读过的都过期了
            user = await session.get(SkUser, account_id)
            if user is None:
                return
        for char in (c for c in stored if c.id in unavailable):
            if await remove("erased_roles", "角色：森空岛标着已删除或已封禁（直接清除，不留备份）", char):
                _suspects.pop(f"role:{char.id}", None)
        roles = [c for c in await get_account_characters(account_id, session) if c.app_code in CHECKED_APPS and c.id not in unavailable]
        verdicts = []  # （角色, "ok" / NO_ROLE / "unclear" / "first" / "due", 探测的说法, 记录的键, 自然键摘要）
        for char in roles:
            stats["roles"] += 1
            role_state, code = await _role_state(user, char)
            if role_state not in ("ok", NO_ROLE):
                # 森空岛有时会对正常的角色回一个临时的错误码；只有回答重复出现时，才算这一周被拒。
                await asyncio.sleep(RECHECK_GAP)
                role_state, code = await _role_state(user, char)
            key, digest = f"role:{char.id}", _digest(user.skland_user_id, char.app_code, char.channel_master_id, char.role_id)
            if role_state in ("ok", NO_ROLE):
                verdict = role_state
            elif gone is None or char.id not in gone or not isinstance(code, int) or code in REQUEST_CODES:
                verdict = "unclear"  # 只有一个信号：列表里还在、列表没读到，或回答说的是请求本身（或根本没有错误码）
            else:
                verdict = "due" if _due(key, digest, now) else "first"
            verdicts.append((char, verdict, role_state, key, digest))
            await asyncio.sleep(GAP)
        due = [v for v in verdicts if v[1] == "due"]
        if due and all(v[1] in ("due", NO_ROLE) for v in verdicts):
            # 所有真实角色都到了该删的时候：连同账号一起解绑（一次备份）；超过上限就整个留着，被拒的记录照样连续
            if await remove("empty_accounts", "账号：全部角色都被接口拒绝（连续两个周一）", probes=[v[2] for v in due]):
                for v in due:
                    _suspects.pop(v[3], None)
            elif apply:
                for v in due:
                    _note_strike(v[3], v[4], now)
            return
        for char, verdict, role_state, key, digest in verdicts:
            if verdict == "ok":
                _suspects.pop(key, None)
            elif verdict == NO_ROLE:
                await remove("invalid_roles", f"角色：{role_state}", char)  # 空的终末地绑定不算被拒绝的角色
            elif verdict == "unclear":
                stats["unclear_roles"] += 1
                why = "绑定列表没读到" if gone is None else "森空岛列表里还在" if char.id not in gone else "回答说的是这次请求"
                reasons[f"角色：{role_state}（{why}，保留）"] += 1
                if apply:
                    _suspects.pop(key, None)  # 这次只有一个信号：不再算连续
            elif verdict == "due":
                if await remove("invalid_roles", f"角色：{role_state}（连续两个周一）", char):
                    _suspects.pop(key, None)
                elif apply:
                    _note_strike(key, digest, now)  # 被上限扣下：保持连续，下次或「不限」时照样删
            else:
                stats["suspect_roles"] += 1
                reasons[f"角色：{role_state}（第一次被拒，下周一再查）"] += 1
                if apply:
                    _note_strike(key, digest, now)
        if owner_id in _superuser_owners:
            stats["inactive_superusers"] += 1
            return
        found, read, days = await _inactive_state(user, session, time.time(), _available(user, apps))
        stats["inactive_read"] += read
        if found == inactive.INACTIVE:
            _inactive_found.append((account_id, owner_id, user.skland_user_id))
            logger.info(f"Skland health check: inactive account {account_id}: {', '.join(days)}")  # 只有内部编号：清除前可以抽查
        elif found == inactive.UNCLEAR and read:
            stats["inactive_unclear"] += 1
    except NetworkError:
        stats["network_skipped"] += 1
        await session.rollback()
    except Exception as e:
        stats["errors"] += 1
        logger.warning(f"Skland health check: account skipped after {_why(e)}")
        await session.rollback()


_limits: dict[str, int] = {}  # 这次运行每种原因最多删几个（不限时为空）
_taken: Counter = Counter()  # 这次运行每种原因已经删了（或正在删）几个


async def run(apply: bool, unlimited: bool = False) -> dict:
    """检查每个账号；`apply` 为真时，还会重新同步角色并移除失效的东西。`unlimited` 为真时不设每种原因的上限。

    开始记一行 started，结束记结果（"{'apply': …"）或 failed：deploy/restart_when_idle.py 看到开始了没结束就不重启。
    """
    async with _lock:
        logger.info(f"Skland health check: started ({'clean-up' if apply else 'preview'})")
        try:
            result = await _check_all(apply, unlimited)
        except Exception as e:
            logger.warning(f"Skland health check: failed ({_why(e)})")
            raise
        logger.info(f"Skland health check: {result}")
        return result


async def _check_all(apply: bool, unlimited: bool = False) -> dict:
    """run 的正文：检查一遍，返回结果。"""
    global _export_path
    stats, reasons, export = Counter(), Counter(), Counter()  # export：备份了每张表多少行
    _export_path = BACKUP_DIR / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl" if apply else None
    _suspects.clear()
    _suspects.update(_load_suspects())
    _list_errors.clear()
    _inactive_found.clear()
    _superuser_owners.clear()
    touched: set[int] = set()  # 失去了账号或角色的所有者
    removed_owners: list[int] = []  # 其中一个账号都不剩的
    started = time.time()
    session = get_scoped_session()
    try:
        away = await away_owners(session)
        if away is None:
            stats["groups_unchecked"] = 1
        _superuser_owners.update(await superuser_owners(session))
        waiting, gone_at = await members_waiting(session)
        if waiting is None:
            # 有官方机器人，却查不清谁在等：等的人多半也不在任何群里，这次谁都不按“已退群”解绑
            waiting, away = set(), None
            stats["waiting_unknown"] = 1
        time_is_up = overdue(gone_at, time.time())
        now = time.time()
        away = away or {}
        if waiting:
            stats["waiting_days_left"] = days_left(gone_at, time.time()) or 0
        account_ids = list((await session.scalars(select(SkUser.id))).all())
        outside = SkUser.owner_id.not_in(waiting)  # 上限按要检查的算：等重新绑定的不算
        size = {"accounts": len((await session.scalars(select(SkUser.id).where(outside))).all()),
                "roles": len((await session.scalars(select(Character.id).join(SkUser, Character.account_id == SkUser.id)
                                                    .where(outside, Character.app_code.in_(CHECKED_APPS)))).all())}
        _taken.clear()
        _limits.clear()
        if not unlimited:  # 等重新绑定的老账号（waiting_removed）不设上限
            _limits.update({stat: max(CAP_MIN, int(size[kind] * CAP_SHARE)) for stat, kind in CAPPED.items()})
        queue: asyncio.Queue[int] = asyncio.Queue()
        for account_id in account_ids:
            queue.put_nowait(account_id)

        async def worker() -> None:
            while not queue.empty():
                account_id = queue.get_nowait()
                try:
                    # 不自动 flush、提交后不过期：什么时候写由 _check_account 决定，提交后手里的账号和角色照样能读
                    async with get_session(autoflush=False, expire_on_commit=False) as own:
                        await _check_account(account_id, own, apply, away, waiting, time_is_up, stats, reasons, export, touched)
                except Exception as e:  # 一个账号出错不能停掉其他 worker；这个账号这次不动
                    stats["errors"] += 1
                    logger.warning(f"Skland health check: account skipped after {_why(e)}")
                await asyncio.sleep(GAP)

        await asyncio.gather(*(worker() for _ in range(min(WORKERS, len(account_ids)) or 1)))
        await _settle_inactive(apply, stats, touched)
        if _list_errors:
            logger.info(f"Skland health check: binding lists not understood: {dict(_list_errors)}")
        if apply:
            try:
                if touched:
                    left = set(await session.scalars(select(SkUser.owner_id).where(SkUser.owner_id.in_(touched)).distinct()))
                    removed_owners = sorted(touched - left)
                stats["defaults_set"] = await ensure_defaults(session)  # 所有人：别处丢了默认角色的也补上
                await session.commit()
                if touched:
                    await away_owners(session)  # 已解绑的人不再计时：恢复或重新绑定的从头算
            except Exception as e:  # 删掉的都已提交，报告更要紧：默认角色下次启动时会再补
                await session.rollback()
                stats["defaults_unchecked"] = 1
                logger.warning(f"Skland health check: default roles not checked after the run ({_why(e)})")
        pending = [since for owner, since in away.items() if owner not in removed_owners and not overdue(since, now, AWAY_DAYS)]
        if pending:  # 这次已经解绑干净的人不再报成停着任务
            stats["away_members"], stats["away_days_left"] = len(pending), days_left(min(pending), now, AWAY_DAYS)
    finally:
        await session.close()
        if apply:
            for key in [key for key, entry in _suspects.items() if time.time() - entry[1] > STRIKE_EXPIRES]:
                del _suspects[key]  # 不再连续的记录：没有用了
            try:
                await asyncio.to_thread(_save_suspects, _suspects)
            except Exception as e:  # 存不下名单就还是上次的，只记日志：结果报告更要紧
                logger.warning(f"Skland health check: suspect roles not saved ({type(e).__name__})")
    if apply:
        try:
            if pruned := await asyncio.to_thread(_prune_backups, time.time()):
                logger.info(f"Skland health check: {pruned} backup file(s) older than {BACKUP_DAYS} days removed")
        except Exception as e:  # 删不掉旧备份不影响这次结果
            logger.warning(f"Skland health check: old backups not pruned ({type(e).__name__})")
    backup_name = ""
    if apply and any(export.values()):  # 删掉的都已逐次追加进备份，这里只核对
        backup_name = _export_path.name
        if not await _backup_complete(_export_path, export):
            stats["backup_unverified"] = 1
    if apply and removed_owners:  # 直接清除的不留备份，队列照样要清
        try:
            await asyncio.to_thread(_drop_queue, removed_owners)
        except Exception as e:  # 结果报告比这个要紧：清不掉只记日志
            logger.warning(f"Skland health check: gacha sync queue not cleared ({type(e).__name__})")
    return {
        "apply": apply, "seconds": round(time.time() - started), "backup": backup_name, "reasons": dict(reasons), **dict(stats),
        "removed_gacha_records": export["skland_gacha_record"],
    }


def summary(result: dict) -> str:
    removed_accounts = result.get("invalid_accounts", 0) + result.get("empty_accounts", 0) + result.get("left_accounts", 0) + result.get("waiting_removed", 0)
    verb = "已解除" if result["apply"] else "将会解除"
    lines = [
        f"森空岛账号体检{'' if result['apply'] else '（仅检查，未改动）'}",
        f"检查 {result.get('accounts', 0)} 个账号、{result.get('roles', 0)} 个角色，用时 {result['seconds']} 秒",
        f"{verb} {removed_accounts} 个失效账号、{result.get('invalid_roles', 0)} 个失效角色"
        + (f"，清除 {result['erased_roles']} 个森空岛标着已删除或已封禁的角色（不留备份）" if result.get("erased_roles") else "")
        + (f"，清除 {result['erased_accounts']} 个没有任何有效角色的账号（不留备份）" if result.get("erased_accounts") else ""),
    ]
    lines += [f"· {reason} ×{count}" for reason, count in sorted(result["reasons"].items(), key=lambda kv: -kv[1])]
    if result.get("own_expired"):
        lines.append(f"其中有你自己的账号（登录过期），记录已备份 {BACKUP_DAYS} 天，重新扫码后我帮你找回。")
    skipped = result.get("network_skipped", 0) + result.get("errors", 0) + result.get("unclear_accounts", 0)
    if skipped:
        lines.append(f"因网络、接口报错或异常跳过 {skipped} 个账号（未改动，下次再查）")
    if result.get("groups_unchecked"):
        lines.append("没能查清谁还在群里或是好友：本次没有按「已退群」解除任何账号")
    if result.get("waiting_unknown"):
        lines.append("没能查清哪些成员还在等机器人更换后重新绑定：本次没有按「已退群」解除任何账号，这些成员的账号照常检查")
    held = sum(result.get(f"held_{stat}", 0) for stat in CAPPED)
    if held:
        lines.append(f"超过本次上限先保留 {held} 个（见上面标着「先保留」的原因）：确认是真的就发 /森空岛体检 清理 不限")
    if result.get("away_members"):
        lines.append(f"疑似已离开（不在任何群、也不是好友）{result['away_members']} 人：已停定时任务，最早 {result['away_days_left']} 天后解除，期间露面就恢复")
    found, erased = result.get("inactive_found", 0), result.get("inactive_accounts", 0)
    if erased:
        lines.append(f"清除 {erased} 个角色都超过 {inactive.DAYS} 天没登录游戏的账号（不留备份）"
                     + (f"；另有 {found - erased} 个清除前再问时登录过、问不清或正忙，这次保留" if found > erased else ""))
    elif result.get("inactive_braked_unclear"):
        lines.append(f"角色都超过 {inactive.DAYS} 天没登录游戏的账号有 {result['inactive_braked']} 个，但这次森空岛问不清的太多，一个都没清，下周再看")
    elif result.get("inactive_braked"):
        lines.append(f"角色都超过 {inactive.DAYS} 天没登录游戏的账号有 {result['inactive_braked']} 个，超过本次上限，这次一个都没清；"
                     "确认是真的就发 /森空岛体检 清理 不限")
    elif found:
        lines.append(f"角色都超过 {inactive.DAYS} 天没登录游戏的账号 {found} 个：" + (
            "12-07 起每周体检解绑并清除记录（不留备份），这次只统计" if result.get("inactive_counting")
            else "清除前再问时都登录过、问不清或正忙，这次都保留" if result["apply"] else "发送 /森空岛体检 清理 解绑并清除（不留备份）"))
    if result.get("waiting_accounts"):
        lines.append(f"机器人更换后还没重新绑定的老账号 {result['waiting_accounts']} 个：已停自动签到、未检查，{result.get('waiting_days_left', 0)} 天后解除")
    if result["apply"] and result["backup"]:
        lines.append(f"删除前已备份：backups/skland-health/{result['backup']}（含 {result['removed_gacha_records']} 条抽卡记录）")
    if result.get("backup_unverified"):
        lines.append("删完后核对这个备份没有通过（有读不出的行，或行数和删掉的对不上），请检查这个文件")
    if result.get("defaults_unchecked"):
        lines.append("删完后没能给失去角色的成员补默认角色（数据库报错），下次启动时会自动补上")
    if not result["apply"] and (removed_accounts or any(result.get(key) for key in ("invalid_roles", "erased_roles", "erased_accounts"))):
        lines.append("发送 /森空岛体检 清理 执行解除")
    return "\n".join(lines)


# 有删除、有超过上限先保留的，或者体检本身出了错，才私聊（errors 也要说：备份写不成时要删的都只记在这里）。
# 角色从绑定列表里消失按规则自己处理，不私聊。
WEEKLY_DM_KEYS = ("invalid_accounts", "empty_accounts", "left_accounts", "invalid_roles", "erased_roles", "erased_accounts", "waiting_removed",
                  "inactive_accounts", "inactive_found", "inactive_braked",
                  *(f"held_{stat}" for stat in CAPPED),
                  "network_skipped", "errors", "groups_unchecked", "waiting_unknown", "backup_unverified", "defaults_unchecked")


@scheduler.scheduled_job("cron", day_of_week="mon", hour=4, minute=0, timezone="Asia/Shanghai", id="skland_health", misfire_grace_time=3600)
async def _weekly() -> None:
    try:
        result = await run(apply=True)
    except Exception as e:  # run 已经记了日志；不发异常文字（数据库报错会带上参数）
        await tell_superusers(f"森空岛体检这次没有跑完（{type(e).__name__}），详情看日志", "Skland health check")
        return
    if not any(result.get(key) for key in WEEKLY_DM_KEYS):
        return
    await tell_superusers(summary(result), "Skland health check")


@get_driver().on_startup
async def _repair_defaults() -> None:
    try:
        async with get_session() as session:
            fixed = await ensure_defaults(session)
            await session.commit()
        if fixed:
            logger.info(f"Skland health check: default role set for {fixed} member/game pairs that had none")
    except Exception as e:
        logger.warning(f"Skland health check: default roles not verified ({type(e).__name__})")


def _mode(text: str) -> tuple[bool, bool] | None:
    """指令后面的文字 -> （清理，不设上限）；别的写法为 None。"""
    return {(): (False, False), ("清理",): (True, False), ("清理", "不限"): (True, True)}.get(tuple(text.split()))


@checkup.handle()
async def _(arg: Message = CommandArg()) -> None:
    mode = _mode(arg.extract_plain_text())
    if mode is None:
        await checkup.finish("用法：/森空岛体检（只检查）、/森空岛体检 清理、/森空岛体检 清理 不限（不设上限）")
    apply, unlimited = mode
    if _lock.locked():
        await checkup.finish("体检正在进行中，请稍后再试")
    await checkup.send("开始检查全部森空岛账号，约需几分钟…")
    await checkup.finish(summary(await run(apply, unlimited)))


# ── 签到的网络重试 ─────────────────────────────────────────────


class SklandUnreachable(RequestException):
    """在指令把错误写进回复的地方，读起来是普通文字（NoneBot 的异常打印出来是 repr）。"""

    def __str__(self) -> str:
        return str(self.args[0])


def _retry_network(bound):
    """森空岛调用因网络失败时重试（插件把这类失败报告为 RequestException）。

    httpx 默认超时是 5 秒，而超时异常不带文字，所以不包这一层的话，指令会回答“…失败:”，冒号后面什么都没有。
    试三次之后，会告诉成员发生了什么。
    """

    @functools.wraps(bound)
    async def wrapper(*args, **kwargs):
        for attempt in range(3):
            try:
                return await bound(*args, **kwargs)
            except RequestException as e:
                cause = e.__cause__ or e.__context__
                if not isinstance(cause, httpx.HTTPError):
                    raise
                if attempt == 2:
                    logger.warning(f"Skland call {bound.__name__} unreachable after 3 tries: {type(cause).__name__}")
                    raise SklandUnreachable("连接森空岛失败（网络超时或中断），请稍后重试") from cause
                await asyncio.sleep(2 * (attempt + 1))

    wrapper._network_retry = True
    return wrapper


for _owner, _name in (
    (SklandAPI, "ark_sign"), (SklandAPI, "endfield_sign"), (SklandLoginAPI, "refresh_token"),
    (SklandAPI, "get_binding"), (SklandAPI, "get_user_ID"),  # 绑定过程中和卡片都会读取
    # 图片类指令背后的只读查询
    (SklandAPI, "endfield_card"), (SklandAPI, "ark_card"), (SklandAPI, "get_rogue"), (SklandAPI, "endfield_war_echoes"),
    (SklandAPI, "get_gacha_categories"), (SklandAPI, "get_gacha_history"), (SklandAPI, "get_ef_gacha_history"), (SklandAPI, "get_ef_gacha_content"),
):
    _method = getattr(_owner, _name)
    if not getattr(_method, "_network_retry", False):
        setattr(_owner, _name, staticmethod(_retry_network(_method)))


def _report_network(bound, attempts: int):
    """登录调用遇到网络失败时，重试之后转成插件自己的 RequestException。

    登录调用会把 httpx 的错误直接放出来，而指令只处理 RequestException：成员刚扫完二维码就
    连接超时，绑定会以一段 traceback 结束，没有任何回复。
    """

    @functools.wraps(bound)
    async def wrapper(*args, **kwargs):
        for attempt in range(attempts):
            try:
                return await bound(*args, **kwargs)
            except httpx.HTTPError as e:
                if attempt == attempts - 1:
                    logger.warning(f"Skland login call {bound.__name__} unreachable after {attempts} tries: {type(e).__name__}")
                    raise SklandUnreachable("连接森空岛失败（网络超时或中断），请稍后重试") from e
                await asyncio.sleep(2 * (attempt + 1))

    wrapper._network_reported = True
    return wrapper


# 二维码循环每两秒轮询一次 get_scan_status，它把 RequestException 当作“还没扫码”。
for _name, _attempts in (("get_scan", 3), ("get_scan_status", 1), ("get_token_by_scan_code", 3), ("get_grant_code", 3),
                         ("get_cred", 3), ("get_role_token_by_uid", 3), ("get_ak_cookie", 3)):
    _method = getattr(SklandLoginAPI, _name)
    if not getattr(_method, "_network_reported", False):
        setattr(SklandLoginAPI, _name, staticmethod(_report_network(_method, _attempts)))


# ── 每晚签到 ─────────────────────────────────────────────────────



async def _save_tokens(session, label: str = "Nightly sign-in") -> None:
    """提交签到刷新的 token；保存不了（数据库忙、账号刚被解绑）就放弃，绝不致命：下次用到时会再刷新。
    提交失败后会话必须先回滚才能再用，所以不在这里重试。"""
    try:
        await session.commit()
    except Exception as e:
        await session.rollback()
        logger.warning(f"{label}: refreshed tokens of one account not saved ({type(e).__name__})")


def _sign_failures(entries: list) -> str:
    """结束日志里没签上的角色数，按登录过期和其他原因分开；没有就是空串。
    和上游 _format_sign_result 同一个判断：结果是字符串、又不是“请勿重复签到”的算失败。"""
    said = [entry.get("result") for entry in entries]
    failed = [text for text in said if isinstance(text, str) and "请勿重复签到" not in text]
    if not failed:
        return ""
    expired = sum(map(_says_expired, failed))
    counts = {"login expired": expired, "other": len(failed) - expired}
    return f", {len(failed)} failed (" + ", ".join(f"{reason} {n}" for reason, n in counts.items() if n) + ")"


SIGN_GAMES = ("arknights", "endfield")


_signing = asyncio.Lock()  # 同一时间只有一轮签到


@dataclass
class SignRound:
    """一轮签到的结果（/全体签到 据此回一条）：各游戏的签到条目（和签到缓存里的一样），没签的账号数，出错的次数。"""

    entries: dict[str, list] = field(default_factory=dict)
    left_out: int = 0  # 等官方机器人认出的成员的账号
    paused: int = 0  # 疑似已离开的成员的账号
    broken: int = 0  # 一个角色或整个账号出错没签成


async def run_daily_sign(*games: str, tell: bool = False) -> SignRound:
    """每晚签到（/全体签到 也用它）：不指定游戏时两款一轮签完。WORKERS 个账号同时签，每个账号用自己的会话，
    先方舟后终末地，第二款游戏用第一款签到时刚刷新的 token，同一个账号不会被两轮同时刷新；处理完一个账号就保存。
    等待重新绑定的、疑似已离开的成员不签。上游的钩子按一款游戏调用，那时只签那一款。

    插件在 scoped session 上运行两个任务，而在指令之外，这个会话是整个进程共用的同一个：
    先跑完的任务会在另一个任务还在用时把它关掉，后者因 DetachedInstanceError 而死，当天剩下的
    角色都签不上。它还只在最后才提交，所以一个刷新过的 token 会让数据库在整个运行期间都处于
    写锁定状态，那几分钟里其他所有写操作都会因 "database is locked" 失败。
    结果按账号原来的顺序分别写进各游戏的签到缓存（/全体签到详情 照这个顺序列出）。
    tell 为真时（00:05 的定时任务），超级用户自己的账号说登录已过期，就私聊告诉他（见 _tell_own_expiry）。
    """
    games = games or SIGN_GAMES
    async with _signing:  # /全体签到 赶上每晚那一轮：等它签完再签（都是“请勿重复签到”），同一个账号不会被两轮同时刷新
        return await _sign_round(games, tell)



async def _sign_round(games: tuple[str, ...], tell: bool) -> SignRound:
    """run_daily_sign 的正文，拿着 _signing 跑。"""
    async with get_session() as session:
        waiting = (await members_waiting(session))[0] or set()  # 查不清谁在等时照常全部签：多签几个人无害
        away = await paused_owners(session)
        own = await superuser_owners(session) if tell else set()
        accounts = [(account.id, account.owner_id) for account in await select_all_accounts(session)]
    left_out = sum(1 for _, owner_id in accounts if owner_id in waiting)  # 通过旧机器人绑定、之后一直没出现：在他们回来之前不签到
    paused = sum(1 for _, owner_id in accounts if owner_id in away and owner_id not in waiting)
    todo = [account_id for account_id, owner_id in accounts if owner_id not in waiting and owner_id not in away]
    signed: list[dict[str, list]] = [{} for _ in todo]
    broken = [0]
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(len(todo)):
        queue.put_nowait(index)

    async def worker() -> None:
        while not queue.empty():
            index = queue.get_nowait()
            signed[index] = await _sign_games(todo[index], games, broken)

    await asyncio.gather(*(worker() for _ in range(min(WORKERS, len(todo)) or 1)))
    report = SignRound(left_out=left_out, paused=paused, broken=broken[0])
    for game in games:
        entries = report.entries[game] = [entry for account in signed for entry in account.get(game, [])]
        sign_service.write_sign_cache(game, entries)
        last = game == games[-1]  # 出错的次数两款合计，只记在最后一行
        logger.info(f"Nightly sign-in ({game}): {len(entries)} roles done" + _sign_failures(entries) + (f", {broken[0]} ended with an error" if last and broken[0] else "") + (f"; {left_out} account(s) of members not recognised since the official bot took over left out" if left_out else "") + (f"; {paused} account(s) of members who seem to have left paused" if paused else ""))
    if tell:
        try:
            await _tell_own_expiry({account_id for account_id, owner_id in accounts if owner_id in own}, todo, signed, {account_id for account_id, _ in accounts})
        except Exception as e:  # 私聊不成不影响签到
            logger.warning(f"Nightly sign-in: the superuser's own expired login not told ({type(e).__name__})")
    return report


async def _tell_own_expiry(own: set[int], todo: list[int], signed: list[dict[str, list]], present: set[int]) -> None:
    """超级用户自己的账号（own）今晚签到说登录已过期：私聊告诉他一次，这个账号重新签上之前不再说。
    todo、signed 一一对应（账号 id、这个账号各游戏的签到结果）；present 是现在所有账号的 id，解绑了的不再记。"""
    try:
        loaded = {int(n) for n in json.loads(EXPIRY_TOLD.read_text("utf-8"))}
    except FileNotFoundError:
        loaded = set()
    except Exception as e:  # 读不了就当没告诉过：最多多说一次
        logger.warning(f"Nightly sign-in: expiry notes not read ({type(e).__name__})")
        loaded = set()
    told = loaded & present
    for account_id, entries in zip(todo, signed):
        if account_id not in own:
            continue
        roles = [entry for game in entries.values() for entry in game]
        said = [entry.get("result") for entry in roles]
        if any(not isinstance(text, str) or "请勿重复签到" in text for text in said):  # 有角色签上了：登录好着
            told.discard(account_id)
        elif account_id not in told and any(isinstance(text, str) and _says_expired(text) for text in said):
            names = "、".join(dict.fromkeys(entry["nickname"] for entry in roles))
            if await tell_superusers(f"你的森空岛账号（角色：{names}）登录已过期，今晚的自动签到没签上。私聊发 /skl绑定 重新扫码就好，抽卡记录都还在。",
                                     "Nightly sign-in"):
                told.add(account_id)  # 没发出去（机器人不在线、平台拒收）就不记：下一晚再说
    if told != loaded:
        await asyncio.to_thread(_save_told, told)


def _save_told(told: set[int]) -> None:
    EXPIRY_TOLD.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(EXPIRY_TOLD, sorted(told))


async def _sign_games(account_id: int, games, broken: list[int], label: str = "Nightly sign-in") -> dict[str, list]:
    """签一个账号下这几款游戏的所有角色（按角色的顺序，先方舟后终末地）：同一个会话、同一个账号对象，第一次签到
    刷新过的 token 后面接着用。一个账号出什么错（数据库忙、签到途中被解绑），别的账号照样签。label 是日志的开头。"""
    entries: dict[str, list] = {game: [] for game in games}
    try:
        async with get_session(expire_on_commit=False, autoflush=False) as session:
            account = await session.get(SkUser, account_id)
            if account is None:
                return entries
            for character in await get_account_characters(account_id, session):
                if character.app_code not in entries:
                    continue
                try:
                    entries[character.app_code].append(await sign_service.sign_character(account, character, character.app_code))
                except Exception as e:  # 一个角色出错不能让后面所有人当晚都签不了
                    broken[0] += 1
                    logger.warning(f"{label}: a {character.app_code} role failed with {type(e).__name__}")
            if session.dirty:
                await _save_tokens(session, label)
    except Exception as e:
        broken[0] += 1
        logger.warning(f"{label}: one account skipped after {type(e).__name__}")
    return entries


async def _sign_account(account_id: int, game, broken: list[int], label: str = "Nightly sign-in") -> list:
    """只签一款游戏的角色（绑定后的那次签到用它）。"""
    return (await _sign_games(account_id, (game,), broken, label))[game]


tasks._run_daily_sign = run_daily_sign
# 上游的两个签到任务（00:15 明日方舟、00:20 终末地）换成 00:05 一轮：两个任务同时跑时，同一个账号会被两边同时刷新 token
for _job in ("daily_arksign", "daily_efsign"):
    try:
        scheduler.remove_job(_job)
    except JobLookupError:  # 上游改了任务名：它的任务就还在，每晚会多签一轮
        logger.warning(f"Nightly sign-in: upstream job {_job} not found, check nonebot_plugin_skland.tasks")
scheduler.add_job(run_daily_sign, "cron", kwargs={"tell": True}, hour=0, minute=5, timezone="Asia/Shanghai", id="skland_nightly_sign",
                  misfire_grace_time=1800, replace_existing=True)

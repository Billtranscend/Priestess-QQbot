"""从小黑盒（Heybox）“抽卡分析”导入更早的终末地、明日方舟抽卡记录。

/导入小黑盒 <小黑盒ID>       一次补两款游戏：重建官方 API 已不再返回的卡池 / 时段。成员没有某款游戏的角色、
                            或小黑盒账号里没有那款游戏的「抽卡分析」数据时，那款游戏跳过（明日方舟见下面一节和 ark_plan.py）
/小黑盒登录                  超级用户私聊：扫码登录机器人读取明日方舟数据用的小黑盒账号；之后机器人每天自动续期

官方抽卡记录 API 只能往回查几个月；小黑盒保存着用户更早上传的数据。终末地的公开概览（凭小黑盒 id
即可读取，无需登录）是在无头浏览器里打开工具页面拿到的，请求由页面自己签名。明日方舟的只回答
已登录的小黑盒账号，所以机器人用一个登录了的账号去读。汇总数据怎么变成记录行、有哪些检查保护数据，
见 plan.py（终末地）和 ark_plan.py（明日方舟）：

  - 小黑盒账号绑定的游戏 UID 必须和调用者森空岛绑定的相同，而且每个 6 星的 id 都必须带这个 UID：
    谁也不能导入别的玩家的记录；
  - 官方行从不修改；卡池只会向前（更早的方向）延伸，而且只在两边数据重叠部分一致时才延伸；
  - 导入的行 `pos` 为负，所以能辨认出来，也能删除。

导入结果由（小黑盒的回答，官方行）决定。官方行也可能在导入之后才到：新绑定的第一次同步，
或者每晚同步还没拉到的抽卡。这些行覆盖的抽卡必须从导入里去掉，否则会重复计数，所以小黑盒的
回答会保存下来，每当新增官方行时就据此重建导入（`reconcile`）。

小黑盒账号绑定的游戏 UID 和成员绑定的那款游戏的角色都对不上时，那款游戏不导入，记下这次尝试（只在内存里，
RETRY_WINDOW 秒内有效）。成员在这期间用 /skl添加账号 绑上了那个角色，这款游戏的抽卡同步一结束就自动再导入一次，
导入到的角色不是默认角色时顺便切换成默认，在他发指令的地方告诉他结果（`_retry`）。他没等同步结束、自己又发了
/导入小黑盒，导入到这期间新绑的角色时也一样切换。

其余手动导入补回的记录不在默认角色上时不切换：回复里提醒一句，下面再跟一个切换到那个角色的按钮（`_role_note`）。
按钮是菜单，由 plugins/quick_buttons.py 撤回。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timedelta, timezone

import httpx
from nonebot import logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment, PrivateMessageEvent
from nonebot.matcher import current_event
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from nonebot.adapters import Event
from nonebot.rule import Rule
from nonebot.typing import T_State


require("nonebot_plugin_htmlrender")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("nonebot_plugin_user")
require("plugins.skland_auto_gacha")
require("plugins.skland_ark_gacha")
require("nonebot_plugin_apscheduler")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import UniMessage
from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_htmlrender import get_new_page
from nonebot_plugin_orm import async_scoped_session, get_session
from nonebot_plugin_skland.commands import gacha as ark_gacha_command
from nonebot_plugin_skland.data_source import ef_gacha_pool_data, gacha_table_data
from nonebot_plugin_skland.model import Character, CharacterDefault, GachaRecord, SkUser
from nonebot_plugin_user import UserSession
from sqlalchemy import delete, event, func, insert, select

from plugins import skland_ark_gacha as ark_gacha
from plugins import skland_auto_gacha as auto_gacha
from plugins.quick_buttons import Rows, send_buttons
from plugins.skland_roles import default_role, other_roles, switch_default
from plugins.strict_command import command_ends_cleanly, strict

from .ark_plan import UNRECORDED, GamePool, build_ark_plan
from .plan import Official, Plan, build_plan
from plugins.shared import first_bot, tell_superusers, write_json

__plugin_meta__ = PluginMetadata(
    name="Heybox gacha import",
    description="从小黑盒「抽卡分析」补回官方接口已无法拉取的终末地、明日方舟抽卡记录。",
    usage="/导入小黑盒 小黑盒ID",
    type="application",
)

TOOL_URL = "https://www.xiaoheihe.cn/game/endfield/card_statistic?user_id={heybox_id}"
OVERVIEW_PATH = "/game/endfield/player/overview"
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
DATA_DIR = store.get_plugin_data_dir()
COOLDOWN = 300
FETCH_TIMEOUT = 15000  # 页面本身的毫秒数
ANSWER_TIMEOUT = 25000  # 等数据到达的毫秒数（通常约 7 秒：脚本一个接一个加载）
FETCH_ATTEMPTS = 3
RECONCILE_DELAY = 10  # 官方行写入后等几秒；它的事务得先提交
RECONCILE_ATTEMPTS = 3
QUEUE_WAIT = 300  # 前面有人在导入时最多排队等多少秒（一次导入两款游戏，通常半分钟左右）
RETRY_WINDOW = 3300  # UID 对不上的那次导入记多久：官方机器人只能在 3500 秒内回复一条消息，回复里说“一小时内”
CN = timezone(timedelta(hours=8))

async def _id_may_follow(event: Event, state: T_State) -> bool:
    """和 strict_command 一样，但也接受 "/导入小黑盒12345678"（数字紧贴在指令后面）。"""
    return command_ends_cleanly(event, state, digits=True)


import_cmd = on_command("导入小黑盒", rule=Rule(_id_may_follow), priority=5, block=True)
_lock = asyncio.Lock()
_last_run: dict[tuple[int, str], float] = {}
_running: set[int] = set()  # 正在导入（含排队）的成员：重复发送时不再排第二次


class ImportBusy(Exception):
    """排队等了 QUEUE_WAIT 秒，前面的导入还没结束。"""


@contextlib.asynccontextmanager
async def _queued():
    """一次只做一个导入：后来的人排队，不用自己再发一遍。"""
    try:
        async with asyncio.timeout(QUEUE_WAIT):
            await _lock.acquire()
    except TimeoutError:
        raise ImportBusy from None
    try:
        yield
    finally:
        _lock.release()


WHERE_ID ="小黑盒 ID 是小黑盒 App 个人主页里的 ID 数字，不是游戏 UID"
COMMAND = "/导入小黑盒"
NOT_BOUND = f"你还没有在我这里绑定森空岛。请先发送 /skl绑定 扫码绑定，绑定成功后再发送 {COMMAND} 小黑盒ID"
NO_ROLE = "你绑定的森空岛账号下没有终末地和明日方舟角色，无法导入。如果刚创建角色，请先发送 /skl角色更新"
GAMES = {"endfield": "终末地", "arknights": "明日方舟"}
NO_ROLE_SKIP = "你绑定的森空岛账号下没有{game}角色，已跳过"
RECEIVED = "正在为你拉取{games}小黑盒抽卡记录"
QUEUED = "。前面还有人在导入，轮到你会自动开始"
STILL_RUNNING = "导入进行中，完成后会回复结果，请稍等"
NO_DATA_SKIP = "小黑盒里没有{game}的抽卡分析数据（没绑定{game}，或还没点过更新），已跳过"
MISMATCH = "没有导入：这个小黑盒账号绑定的{game} UID 和你在我这里绑定的{game}角色都对不上"
IMPORT_FAILED = "导入失败"
RETRY_HINT = (  # 指令单独成句，skland_shortcuts 才会在下面挂 /skl添加账号 按钮
    "如果小黑盒绑定的{games}号在你的另一个森空岛账号上，可以把那个账号也绑上来。"
    "发送 /skl添加账号 绑定，一小时内绑好的话，我会自动用这个小黑盒 ID 再导入一次{games}。"
)


def _uid(character: Character) -> str:
    """角色的游戏 UID：终末地的在 role_id，明日方舟的在 uid。"""
    return str((character.role_id if character.app_code == "endfield" else character.uid) or "")


async def _roles(owner_id: int, session, app_code: str = "endfield") -> list[Character]:
    """成员某个游戏的角色（所有账号），默认角色在前。"""
    uid = Character.role_id if app_code == "endfield" else Character.uid
    default_id = await session.scalar(select(CharacterDefault.character_id).where(CharacterDefault.owner_id == owner_id, CharacterDefault.app_code == app_code))
    roles = (
        await session.scalars(
            select(Character).join(Character.account).where(SkUser.owner_id == owner_id, Character.app_code == app_code, uid != "").order_by(Character.id)
        )
    ).all()
    return sorted(roles, key=lambda role: role.id != default_id)


async def _is_bound(owner_id: int, session) -> bool:
    return (await session.scalars(select(SkUser.id).where(SkUser.owner_id == owner_id).limit(1))).first() is not None


def _has_data(app_code: str, result: dict) -> bool:
    """小黑盒账号里有没有这款游戏的「抽卡分析」数据（没绑这款游戏、或从没点过更新时没有）。plan.py / ark_plan.py 的同一检查。"""
    if app_code == "endfield":
        return bool(result.get("is_bind") and result.get("user_info"))
    return bool(result.get("user_info") and (result.get("gacha_record") or {}).get("total_pools"))




class HeyboxRefused(Exception):
    """小黑盒有回答，但给的不是数据（它自己的消息如果有，就作为异常参数）。"""


# 小黑盒的脚本放在腾讯云只做国内加速的 CDN 上：不管从哪里查，DNS 都只给两个国内节点，其中一个可能
# 连不上（所有脚本都会卡住，Chromium 不会去试另一个地址；httpx 会试，但要先等满连接超时）。
# 所以页面从该主机加载的脚本由这里拉取：先同时试连各个地址，用连得最快的那个（记住 CDN_PICK_TTL 秒），
# 按主机名校验证书，再交给页面。都连不上时照旧按主机名去取。
CDN_HOST = "static.max-c.com"
CDN_BACKUPS = ("121.12.126.87", "61.170.82.68", "117.135.148.28")  # 同一 CDN 的其他节点，DNS 给的都连不上时备用
CDN_PICK_TTL = 600
CDN_PROBE_TIMEOUT = 3
DROPPED_HEADERS = {"content-encoding", "content-length", "transfer-encoding", "connection"}
_cdn_pick: tuple[str | None, float] = (None, 0.0)
_cdn_lock = asyncio.Lock()  # 同一页面的几个脚本同时来问时，只试连一次


async def _connect_time(address: str) -> float | None:
    """连上这个地址的 443 端口用了几秒；连不上为 None。"""
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(address, 443), CDN_PROBE_TIMEOUT)
    except (OSError, TimeoutError):
        return None
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return time.monotonic() - started


async def _cdn_candidates() -> list[str]:
    with contextlib.suppress(OSError):
        found = await asyncio.get_running_loop().getaddrinfo(CDN_HOST, 443, family=socket.AF_INET, type=socket.SOCK_STREAM)
        return list(dict.fromkeys([info[4][0] for info in found] + list(CDN_BACKUPS)))
    return list(CDN_BACKUPS)


async def _cdn_address() -> str | None:
    """连得最快的 CDN 地址（缓存一段时间）；一个都连不上时为 None。"""
    global _cdn_pick
    async with _cdn_lock:
        address, picked = _cdn_pick
        if address and time.monotonic() - picked < CDN_PICK_TTL:
            return address
        return await _pick_cdn()


async def _pick_cdn() -> str | None:
    global _cdn_pick
    candidates = await _cdn_candidates()
    times = await asyncio.gather(*(_connect_time(candidate) for candidate in candidates))
    live = sorted((spent, candidate) for spent, candidate in zip(times, candidates) if spent is not None)
    address = live[0][1] if live else None
    _cdn_pick = (address, time.monotonic())
    if len(live) < len(candidates):
        logger.info(f"Heybox CDN: {len(candidates) - len(live)} of {len(candidates)} addresses unreachable, using the fastest of the rest")
    return address


def _via(client: httpx.AsyncClient):
    """路由处理函数：每次加载用一个保持连接的客户端（脚本一个接一个加载；每次新建连接约 1.5 秒，
    总共约 15 秒，而复用一个连接约 7 秒）。"""

    async def handle(route) -> None:
        global _cdn_pick
        address = await _cdn_address()
        url = httpx.URL(route.request.url)
        headers = {"User-Agent": USER_AGENT, "Referer": "https://www.xiaoheihe.cn/"}
        try:
            if address:  # 连这个地址，但 SNI 和证书校验仍按主机名
                response = await client.get(url.copy_with(host=address), headers={**headers, "Host": CDN_HOST}, extensions={"sni_hostname": CDN_HOST})
            else:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError:
            _cdn_pick = (None, 0.0)  # 下次重新挑
            await route.continue_()  # 让浏览器自己去试
            return
        headers = {k: v for k, v in response.headers.items() if k.lower() not in DROPPED_HEADERS}
        await route.fulfill(status=response.status_code, headers=headers, body=response.content)

    return handle


async def _fetch_once(heybox_id: str) -> dict:
    async with (
        httpx.AsyncClient(timeout=FETCH_TIMEOUT / 1000, follow_redirects=True) as client,
        get_new_page(device_scale_factor=1, viewport={"width": 430, "height": 900}, user_agent=USER_AGENT) as page,
    ):
        await page.route(f"https://{CDN_HOST}/**", _via(client))
        async with page.expect_response(lambda r: OVERVIEW_PATH in r.url, timeout=ANSWER_TIMEOUT) as waiter:
            await page.goto(TOOL_URL.format(heybox_id=heybox_id), wait_until="domcontentloaded", timeout=FETCH_TIMEOUT)
        response = await waiter.value
        return await response.json()


async def fetch_overview(heybox_id: str) -> dict:
    """某个小黑盒 id 的概览；工具页面自己签名并发送请求。

    正常加载约 3 秒就有回答。页面的脚本放在小黑盒的 CDN 上，访问时不时会超时；
    这时应用根本不会去请求数据，所以换一个新页面重试。
    """
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            payload = await _fetch_once(heybox_id)
        except Exception as e:
            last = e
            logger.info(f"Heybox fetch attempt {attempt + 1} failed: {type(e).__name__}")
            continue
        if payload.get("status") != "ok":
            raise HeyboxRefused(str(payload.get("msg") or "").strip()[:60])
        return payload.get("result") or {}
    raise last or RuntimeError("heybox fetch failed")


def _imported(character_id: int):
    """某个角色下由导入生成的行：官方 seqId 为正，导入的为负。"""
    return (GachaRecord.character_id == character_id, GachaRecord.pos < 0)


def _endfield_official(*columns):
    """对所有成员的官方终末地记录行做 SELECT。"""
    return select(*columns).join(Character, Character.id == GachaRecord.character_id).where(Character.app_code == "endfield", GachaRecord.pos >= 0)


async def _known_names(session) -> tuple[dict[tuple[str, str], str], dict[str, str]]:
    """从数据库里所有官方行学到的 (item_type, name) -> 物品 id，以及 pool_id -> 卡池名称。"""
    ids = {
        (item_type, name): item_id
        for item_type, name, item_id in await session.execute(
            _endfield_official(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
            .group_by(GachaRecord.item_type, GachaRecord.char_name, GachaRecord.char_id)
        )
        if item_id
    }
    with contextlib.suppress(Exception):  # 最近没人抽到过的名字：退回到 wiki 索引
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        index = loaded[0] if loaded else {}
        for item_type, table in (("char", index.get("operators", {})), ("weapon", index.get("weapons", {}))):
            for item_id, item in table.items():
                if item.get("name") and not item_id.startswith("chr_9000"):
                    ids.setdefault((item_type, item["name"]), item_id)
    pools = dict(
        (await session.execute(
            _endfield_official(GachaRecord.pool_id, func.max(GachaRecord.pool_name))
            .group_by(GachaRecord.pool_id)
        )).all()
    )
    return ids, pools


def _pool_name(pool_id: str, fallback: str, known: dict[str, str]) -> str:
    if pool_id in known:
        return known[pool_id]
    local = ef_gacha_pool_data.get_pool(pool_id)
    return (local.pool_name if local and local.pool_name else fallback) or pool_id


def _pool_windows() -> dict[str, tuple[int, int]]:
    """卡池 id -> (开放, 关闭) 的 unix 秒，来自 wiki 索引的日历；拿不到时为空。"""
    windows: dict[str, tuple[int, int]] = {}
    with contextlib.suppress(Exception):
        from plugins import endfield_wiki

        loaded = endfield_wiki._resolver()
        for pool in ((loaded[0] if loaded else {}).get("calendar") or {}).get("pools") or []:
            with contextlib.suppress(KeyError, ValueError):
                opened, closed = (datetime.strptime(pool[key], "%Y/%m/%d %H:%M:%S").replace(tzinfo=CN) for key in ("open", "close"))
                windows[pool["id"]] = (int(opened.timestamp()), int(closed.timestamp()))
    return windows


def _game_pools() -> list[GamePool]:
    """明日方舟的游戏卡池表（上游启动时加载）。"""
    return [GamePool(p.gachaPoolId, p.gachaPoolName, p.openTime, p.endTime) for p in gacha_table_data.gacha_table]


async def _ark_rows(session, character, plan: Plan) -> list[dict]:
    wanted = {row["pool_id"] for row in plan.rows}
    names = dict(  # 官方行里的卡池名称（如“联合行动”），游戏表里很多卡池只有一个通用名
        (await session.execute(
            select(GachaRecord.pool_id, func.max(GachaRecord.pool_name)).where(GachaRecord.pool_id.in_(wanted), GachaRecord.pos >= 0).group_by(GachaRecord.pool_id)
        )).all()
    )
    rows = [
        {
            "character_id": character.id, "item_type": "char", "pool_id": row["pool_id"], "pool_name": names.get(row["pool_id"]) or row["pool_name"],
            "char_id": row["char_id"], "char_name": row["name"], "rarity": row["rarity"], "is_new": row["is_new"], "is_free": False,
            "gacha_ts": row["ts"], "pos": row["pos"],
        }
        for row in plan.rows
    ]
    return rows


async def _plan(session, character, result: dict) -> Plan:
    """该角色的导入计划（按它现有的官方行）。"""
    official = [
        Official(pool_id, name, rarity, int(ts), pos, bool(is_free))
        for pool_id, name, rarity, ts, pos, is_free in await session.execute(
            select(GachaRecord.pool_id, GachaRecord.char_name, GachaRecord.rarity, GachaRecord.gacha_ts, GachaRecord.pos, GachaRecord.is_free)
            .where(GachaRecord.character_id == character.id, GachaRecord.pos >= 0)
        )
    ]
    if character.app_code == "arknights":
        return build_ark_plan(result, _uid(character), official, _game_pools())
    return build_plan(result, str(character.role_id), official, _pool_windows())


async def _rows(session, character, plan: Plan) -> list[dict]:
    """计划对应的数据库行：补上物品 id 和卡池名称（终末地要查全部成员的官方行，所以只在真要写入时做）。"""
    if plan.error or not plan.rows:
        return []
    if character.app_code == "arknights":
        return await _ark_rows(session, character, plan)
    ids, pool_names = await _known_names(session)
    return [
        {
            "character_id": character.id,
            "item_type": row["item_type"], "pool_id": row["pool_id"],
            "pool_name": _pool_name(row["pool_id"], row["pool_name"], pool_names),
            "char_id": ids.get((row["item_type"], row["name"]), ""), "char_name": row["name"],
            "rarity": row["rarity"], "is_new": False, "is_free": row["is_free"], "gacha_ts": row["ts"], "pos": row["pos"],
        }
        for row in plan.rows
    ]


def _compared(row: dict) -> tuple:
    """重建前后比较的内容，和 reconcile 从数据库读出的 key 一一对应（明日方舟的计划没有免费抽，按 False）。"""
    return row["pool_id"], row["ts"], row["pos"], row["name"], row.get("is_free", False)


async def _replace(session, character_id: int, rows: list[dict]) -> None:
    await session.execute(delete(GachaRecord).where(*_imported(character_id)))
    for start in range(0, len(rows), 500):
        await session.execute(insert(GachaRecord), rows[start : start + 500])


async def run_import(session, character, result: dict) -> Plan:
    """用 `result` 重新生成的导入替换该角色之前的导入；成功后提交。"""
    plan = await _plan(session, character, result)
    rows = await _rows(session, character, plan)
    if rows:
        await _replace(session, character.id, rows)
        await session.commit()
    return plan


def _snapshot_key(character: Character) -> str:
    """保存小黑盒回答的文件名里代表角色的部分：终末地是 UID，明日方舟是 ark-UID。"""
    return _uid(character) if character.app_code == "endfield" else f"ark-{_uid(character)}"


def _snapshot_path(owner_id: int, key: str) -> Path:
    return DATA_DIR / f"{owner_id}-{key}.json"


def _keep_raw(owner_id: int, key: str, heybox_id: str, result: dict) -> None:
    """来源记录：导入所依据的小黑盒回答（权限 0600，每个角色一个文件）。"""
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _snapshot_path(owner_id, key)
    write_json(path, {"heybox_id": heybox_id, "fetched": time.time(), "result": result}, ensure_ascii=False)
    uid = str((result.get("user_info") or {}).get("uid"))
    legacy = DATA_DIR / f"{owner_id}.json"  # 成员还不能有多个终末地角色时写的文件
    with contextlib.suppress(OSError, ValueError):
        if str((json.loads(legacy.read_text("utf-8")).get("result") or {}).get("user_info", {}).get("uid")) == uid:
            legacy.unlink()


def _snapshot(owner_id: int, key: str, uid: str) -> dict | None:
    for path in (_snapshot_path(owner_id, key), DATA_DIR / f"{owner_id}.json"):
        with contextlib.suppress(OSError, ValueError):
            snapshot = json.loads(path.read_text("utf-8"))
            if str((snapshot.get("result") or {}).get("user_info", {}).get("uid")) == str(uid):
                return snapshot
    return None


async def reconcile(character_id: int) -> str:
    """用导入时保存的小黑盒回答，重建某个角色的导入。

    从没导入过的角色（没有保存的回答，或者没有导入的行）什么都不会发生。计划不再接受的卡池，
    会失去导入的行：两边重叠的地方以官方行为准。
    """
    async with _lock, get_session() as session:
        character = await session.get(Character, character_id)
        if character is None or character.app_code not in GAMES or not _uid(character):
            return "no role"
        owner_id = await session.scalar(select(SkUser.owner_id).where(SkUser.id == character.account_id))
        snapshot = await asyncio.to_thread(_snapshot, owner_id, _snapshot_key(character), _uid(character))
        if not snapshot or not isinstance(snapshot.get("result"), dict):
            return "no snapshot"
        key = (GachaRecord.pool_id, GachaRecord.gacha_ts, GachaRecord.pos, GachaRecord.char_name, GachaRecord.is_free)
        before = sorted(tuple(row) for row in await session.execute(select(*key).where(*_imported(character_id))))
        if not before:
            return "nothing imported"
        plan = await _plan(session, character, snapshot["result"])
        if plan.error:
            return "snapshot rejected"
        after = sorted(_compared(row) for row in plan.rows)
        if after == before:
            return "unchanged"
        await _replace(session, character_id, await _rows(session, character, plan))
        await session.commit()
    return f"{len(before)} -> {len(after)} rows"


_due: set[int] = set()
_reconciling: set[asyncio.Task] = set()


async def _reconcile_later(character_id: int, delay: float) -> None:
    await asyncio.sleep(delay)
    _due.discard(character_id)
    for attempt in range(RECONCILE_ATTEMPTS):
        try:
            outcome = await reconcile(character_id)
        except Exception as e:  # 例如数据库正忙于触发这次重建的那次同步
            logger.warning(f"Heybox reconcile attempt {attempt + 1} failed: {type(e).__name__}")
            await asyncio.sleep(RECONCILE_DELAY)
            continue
        if outcome not in ("no role", "no snapshot", "nothing imported", "unchanged"):
            logger.info(f"Heybox import reconciled after new official rows: {outcome}")
        return


def _schedule_reconcile(character_id: int, delay: float = RECONCILE_DELAY) -> None:
    if character_id in _due:
        return
    _due.add(character_id)
    task = asyncio.get_running_loop().create_task(_reconcile_later(character_id, delay))
    _reconciling.add(task)
    task.add_done_callback(_reconciling.discard)


@event.listens_for(GachaRecord, "after_insert")
def _official_row_added(mapper, connection, target) -> None:
    """/zmd抽卡记录更新 通过 ORM 保存新行；每晚同步由下面的监听者处理。"""
    if (target.pos or 0) >= 0 and target.character_id is not None and target.character_id not in _due:
        with contextlib.suppress(Exception):
            _schedule_reconcile(target.character_id)


async def _after_official_sync(owner_id: int, status: str, records: int, final: bool, app_code: str = "endfield") -> None:
    if status in ("success", "partial") and records:
        async with get_session() as session:
            roles = [role.id for role in await _roles(owner_id, session, app_code)]
        for character_id in roles:
            _schedule_reconcile(character_id, 1)
    if final:
        await _retry_if_bound(owner_id, app_code)


async def _after_ark_sync(owner_id: int, status: str, records: int, final: bool) -> None:
    """明日方舟的每日同步用 SQL 直接写入，不经过 ORM 的 after_insert。"""
    await _after_official_sync(owner_id, status, records, final, "arknights")


if _after_official_sync not in auto_gacha.sync_listeners:
    auto_gacha.sync_listeners.append(_after_official_sync)
if _after_ark_sync not in ark_gacha.sync_listeners:
    ark_gacha.sync_listeners.append(_after_ark_sync)


def fetch_failure_text(error: Exception) -> str:
    """读不到小黑盒时告诉成员的原因。"""
    if isinstance(error, HeyboxRefused):
        said = f"：{error.args[0]}" if error.args and error.args[0] else ""
        return f"导入失败：小黑盒拒绝了这次查询{said}，没有写入任何数据。请过几分钟再试"
    if isinstance(error, (TimeoutError, httpx.TimeoutException)) or "Timeout" in type(error).__name__:
        return "导入失败：小黑盒的网页连续 3 次没加载出来（小黑盒那边响应慢），没有写入任何数据，请过几分钟再试"
    return f"导入失败：读取小黑盒数据时出错（{type(error).__name__}），没有写入任何数据。请过几分钟再试，一直失败请联系管理员"


def summary(plan: Plan, app_code: str = "endfield") -> str:
    game = GAMES[app_code]
    record = "/zmd抽卡记录" if app_code == "endfield" else "/mrfz抽卡记录"
    if plan.error:
        return f"导入失败：{plan.error}"
    lines = []
    if plan.imported:
        first = min(row["ts"] for row in plan.rows)
        lines.append(
            f"小黑盒导入完成：补回 {len(plan.imported)} 个卡池、{sum(p.pulls for p in plan.imported)} 抽、"
            f"{sum(p.six_stars for p in plan.imported)} 个六星，最早到 {datetime.fromtimestamp(first, CN):%Y-%m-%d}"
        )
        if app_code == "endfield":
            lines.append(f"六星与每个六星的抽数来自小黑盒，四星不记具体干员；发 {record} 查看")
        else:
            lines.append(f"六星、五星和它们的抽数来自小黑盒，三星、四星不记具体干员（每个卡池最后一个六星之后的五星小黑盒也没有记）；发 {record} 查看")
        if partial := [p.pool_name for p in plan.imported if p.partial]:
            lines.append(f"{'、'.join(f'「{name}」' for name in partial)}官方接口只剩后面一段记录，补的是前面查不到的那些抽")
    else:
        lines.append(
            f"小黑盒里没有比我这里已有记录更早的抽卡数据，无需导入（小黑盒 {plan.heybox_pulls} 抽，我这里已有 {plan.official_pulls} 抽）。"
            f"如果小黑盒很久没更新，先在小黑盒的{game}「抽卡分析」里点一次更新再试"
        )
    if plan.estimated:
        names = "、".join(f"「{name}」" for name in plan.estimated[:3]) + (f"等 {len(plan.estimated)} 个卡池" if len(plan.estimated) > 3 else "")
        lines.append(f"{names}的抽卡顺序小黑盒没有记全（几个卡池同时开放轮流抽过，或有些抽没记卡池），六星前的抽数按小黑盒显示的保底计数估算")
    unrecorded = Counter(name for pool in plan.skipped if pool.skipped == UNRECORDED for name in pool.pool_name.removesuffix("所在的卡池").split("、"))
    if unrecorded:
        sixes = "、".join(name + (f"×{count}" if count > 1 else "") for name, count in unrecorded.items())  # 同一个干员出了几次就写几次
        lines.append(
            f"另有{'这个' if sum(unrecorded.values()) == 1 else '这几个'}六星没法补：{sixes}。小黑盒没记下它们是在哪个卡池抽的"
            "（一般是那段时间没在小黑盒点更新），其余卡池照常导入"
        )
    for pool in [pool for pool in plan.skipped if pool.skipped != UNRECORDED][:6]:
        lines.append(f"未导入「{pool.pool_name}」：{pool.skipped}")
    return "\n".join(lines)


@dataclass(frozen=True)
class GameImport:
    """一款游戏的导入结果。"""

    outcome: str  # 见 _import_game
    text: str  # 给成员的话
    heybox_uid: str = ""  # 小黑盒账号绑定的这款游戏的 UID，读到数据时才有
    role_id: int = 0  # 导入到的角色，读到数据且 UID 对上时才有
    role_name: str = ""  # 这个角色在回复里的叫法


ARK_SERVERS = {"1": "官服", "2": "bilibili服"}  # 同 /skl角色 卡片的叫法


def _role_name(character: Character) -> str:
    """回复里怎么叫这个角色：昵称；明日方舟加上区服（同一账号可能官服、bilibili服各有一个同名角色）。"""
    if character.app_code == "endfield":
        return character.nickname
    return f"{character.nickname}（{ARK_SERVERS.get(character.channel_master_id, character.server_name)}）"


async def _import_game(app_code: str, user_id: int, heybox_id: str, session) -> GameImport:
    """导入一款游戏。不在这里结束指令。

    结果：imported 补回了记录；nothing 小黑盒没有更早的记录；no_role 成员没有这款游戏的角色；
    no_data 小黑盒账号里没有这款游戏的数据；mismatch 小黑盒账号的游戏 UID 和成员的角色都对不上；
    cooldown 刚导入过；failed 出错，没有写入。
    """
    game = GAMES[app_code]
    roles = await _roles(user_id, session, app_code)  # 每款游戏重新查：前一款回滚后旧对象已过期
    if not roles:
        return GameImport("no_role", NO_ROLE_SKIP.format(game=game))
    wait = COOLDOWN - (time.time() - _last_run.get((user_id, app_code), 0))
    if wait > 0:
        logger.info(f"Heybox import ({app_code}) skipped: imported {int(COOLDOWN - wait)} s ago")
        return GameImport("cooldown", f"刚导入过，请 {int(wait // 60) + 1} 分钟后再试")
    try:
        result = await (fetch_overview if app_code == "endfield" else fetch_ark_analysis)(heybox_id)
    except HeyboxLoginExpired:
        logger.warning("Heybox import: the saved Heybox login is missing or expired")
        await _tell_login_lost()
        return GameImport("failed", LOGIN_LOST)
    except Exception as e:
        logger.warning(f"Heybox fetch ({app_code}) failed: {type(e).__name__}: {str(e).splitlines()[0][:120] if str(e) else ''}")  # Playwright 的调用日志里会出现小黑盒 id（所以只记第一行）
        return GameImport("failed", fetch_failure_text(e))
    if not _has_data(app_code, result):
        logger.info(f"Heybox import ({app_code}) skipped: no data for this game on the Heybox account")
        return GameImport("no_data", NO_DATA_SKIP.format(game=game))
    try:
        # 小黑盒账号会指明它的角色；导入就放到那个角色下，不管它是成员的哪一个角色。
        heybox_uid = str((result.get("user_info") or {}).get("uid"))
        character = next((role for role in roles if _uid(role) == heybox_uid), None)
        if character is None:
            logger.info(f"Heybox import ({app_code}) skipped: the Heybox account's game UID matches none of the member's roles")
            return GameImport("mismatch", MISMATCH.format(game=game), heybox_uid)
        # ORM 对象在提交后会过期；先留下普通值，供之后使用
        key, role_id, role_name = _snapshot_key(character), character.id, _role_name(character)
        plan = await run_import(session, character, result)
    except Exception as e:
        await session.rollback()
        logger.opt(exception=True).error(f"Heybox import ({app_code}) failed while building or writing rows: {type(e).__name__}: {e}")
        return GameImport("failed", "导入时程序出错，没有写入任何数据。问题已记录，请稍后再试")
    if plan.rows and not plan.error:
        _last_run[(user_id, app_code)] = time.time()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(_keep_raw, user_id, key, heybox_id, result)
    logger.info(
        f"Heybox import ({app_code}): error={plan.error[:16] or None} pools={len(plan.imported)} rows={len(plan.rows)} "
        f"skipped={len(plan.skipped)} heybox_pulls={plan.heybox_pulls} official_pulls={plan.official_pulls}"
    )
    if plan.error:
        return GameImport("failed", summary(plan, app_code), heybox_uid)
    return GameImport("imported" if plan.imported else "nothing", summary(plan, app_code), heybox_uid, role_id, role_name)


async def _import(matcher, user_session: UserSession, session, arg: Message) -> None:
    """/导入小黑盒：同一个小黑盒 ID 依次导入终末地和明日方舟，一条回复写两款游戏的结果。"""
    asked_at = time.time()  # 官方机器人能回复这条消息的时间从它收到时算起
    heybox_id = arg.extract_plain_text().strip()
    if not (heybox_id.isdigit() and 4 <= len(heybox_id) <= 12):
        logger.info("Heybox import refused: usage")  # 原因只记类别，不记成员填的内容
        await matcher.finish(
            f"用法：{COMMAND} 小黑盒ID，例如 {COMMAND} 12345678\n"
            f"{WHERE_ID}。一次导入终末地和明日方舟两款游戏，小黑盒里没有哪款游戏的数据就跳过哪款。"
            "导入前请先在小黑盒的「抽卡分析」里点一次更新。\n"
            "只能导入与你已绑定的游戏 UID 相同的小黑盒账号。"
        )
    user_id = user_session.user_id
    if not await _is_bound(user_id, session):
        logger.info("Heybox import refused: not bound")
        await matcher.finish(NOT_BOUND, at_sender=True)
    games = {app_code: {role.id: _uid(role) for role in await _roles(user_id, session, app_code)} for app_code in GAMES}  # 角色 id -> 游戏 UID
    if not any(games.values()):
        logger.info("Heybox import refused: no role in either game")
        await matcher.finish(NO_ROLE, at_sender=True)
    if any(heybox_id in uids.values() for uids in games.values()):
        logger.info("Heybox import refused: the game UID was given")
        await matcher.finish(f"你填的是自己的游戏 UID。{WHERE_ID}，请换成小黑盒 ID 再试", at_sender=True)
    if user_id in _running:
        logger.info("Heybox import refused: this member's import is still running")
        await matcher.finish(STILL_RUNNING, at_sender=True)
    _running.add(user_id)
    try:
        names = "和".join(GAMES[app_code] for app_code, uids in games.items() if uids)
        await UniMessage.text(RECEIVED.format(games=names) + (QUEUED if _lock.locked() else "")).send(at_sender=True)  # 先说一句收到了
        results: dict[str, GameImport] = {}
        try:
            async with _queued():
                for app_code in GAMES:
                    results[app_code] = await _import_game(app_code, user_id, heybox_id, session)
        except ImportBusy:
            logger.info(f"Heybox import refused: waited {QUEUE_WAIT} s for other imports")
            await matcher.finish(f"现在导入的人多，排队超过 {QUEUE_WAIT // 60} 分钟了，请过一会儿再试", at_sender=True)
        # 在 _remember 之前：要看上次 UID 对不上时他有哪些角色；仍在 _running 里，自动再导入不会同时开始
        notes = {app_code: await _role_note(session, user_id, app_code, result) for app_code, result in results.items()}
    finally:
        _running.discard(user_id)
    _remember(user_id, heybox_id, results, games, asked_at)
    outcomes = {app_code: result.outcome for app_code, result in results.items()}
    lines = [f"【{GAMES[app_code]}】{result.text}{notes[app_code][0]}" for app_code, result in results.items()]
    if all(outcome in ("no_data", "no_role") for outcome in outcomes.values()):  # 有角色的游戏一个都没读到：多半填错了 ID
        lines.append("这个小黑盒 ID 下没有读到任何游戏的抽卡数据。请确认填的是小黑盒 App 个人主页里的 ID 数字（不是游戏 UID），并且已经在小黑盒的「抽卡分析」里登录游戏账号、点过更新")
    if mismatched := [app_code for app_code, outcome in outcomes.items() if outcome == "mismatch"]:
        lines.append(RETRY_HINT.format(games="和".join(GAMES[app_code] for app_code in mismatched)))
    text = "\n\n".join(lines)
    if mismatched and all(outcome in ("mismatch", "no_data", "no_role", "failed") for outcome in outcomes.values()):  # 一款也没导入
        text = f"{IMPORT_FAILED}\n{text}"
    await UniMessage.text(text).send(at_sender=True)
    if offers := {app_code: offer for app_code, (_, offer) in notes.items() if offer is not None}:
        # 无文字兜底：回复里已经写了想切换发什么
        await send_buttons(SWITCH_CARD, _switch_rows(offers))


@import_cmd.handle()
async def _(event: MessageEvent, user_session: UserSession, session: async_scoped_session, arg: Message = CommandArg()) -> None:
    await _import(import_cmd, user_session, session, arg)


# ── 导入到的不是默认角色 ───────────────────────────────────────────────

SWITCHED = "\n我已把你的{game}默认角色切换为 {name}，{record} 和 {board} 现在看的就是它；想换回原来的角色发 {switch}"
OTHER_ROLE = "\n记录导入到了你的另一个{game}角色 {name}，默认角色没变，{record} 看的还是默认角色；想切换可以发 {switch}"
NO_DEFAULT = "\n记录导入到了你的{game}角色 {name}；你还没有设置{game}默认角色，{record} 要设置后才能看，想设成它可以发 {switch}"
SWITCH_CARD = "要切换默认角色可以点这里"
COMMANDS = {  # 抽卡记录、欧非榜、切换默认角色
    "endfield": ("/zmd抽卡记录", "/zmd欧非榜", "/skl切换终末地角色"),
    "arknights": ("/mrfz抽卡记录", "/mrfz欧非榜", "/skl切换方舟角色"),
}
Offer = tuple[int, str]  # 可以切换过去的角色：/skl切换…角色 的序号，昵称


async def _switched(session, owner_id: int, app_code: str, result: GameImport) -> str:
    """把这款游戏的默认角色换成导入到的角色。换了返回告诉成员的那句，没换为空。"""
    try:
        switched = await switch_default(session, owner_id, result.role_id, app_code)
    except Exception as e:  # 导入已经写好了：换不了默认角色，也照样告诉成员导入的结果
        logger.warning(f"Heybox import ({app_code}): default role not switched ({type(e).__name__})")
        return ""
    if not switched:
        return ""
    record, board, switch = COMMANDS[app_code]
    return SWITCHED.format(game=GAMES[app_code], name=result.role_name, record=record, board=board, switch=switch)


async def _other_role(session, owner_id: int, app_code: str, role_id: int) -> tuple[Offer, bool] | None:
    """这个角色不是成员这款游戏的默认角色时：它的序号和昵称，成员有没有默认角色。"""
    try:
        others = await other_roles(session, owner_id, app_code)
        offer = next(((number, role.nickname) for number, role in others if role.id == role_id), None)
        return None if offer is None else (offer, await default_role(session, owner_id, app_code) is not None)
    except Exception as e:  # 只是提醒：查不了就不提
        logger.warning(f"Heybox import ({app_code}): roles not looked up for the default-role note ({type(e).__name__})")
        return None


async def _role_note(session, owner_id: int, app_code: str, result: GameImport) -> tuple[str, Offer | None]:
    """手动导入后接在这款游戏那段后面的话，和要给的切换按钮。UID 对不上之后新绑的角色这次对上了：
    同自动再导入，换成默认角色。其余补回了记录、却不在默认角色上的：只提醒，给切换按钮（还没有默认角色时也不替他设）。"""
    if result.outcome not in ("imported", "nothing"):
        return "", None
    entry = _waiting(owner_id, app_code)
    if entry is not None and result.role_id not in entry.games[app_code][1] and (text := await _switched(session, owner_id, app_code, result)):
        return text, None
    if result.outcome != "imported" or (other := await _other_role(session, owner_id, app_code, result.role_id)) is None:
        return "", None
    offer, has_default = other
    record, _, switch = COMMANDS[app_code]
    note = OTHER_ROLE if has_default else NO_DEFAULT
    return note.format(game=GAMES[app_code], name=result.role_name, record=record, switch=switch), offer


def _switch_rows(offers: dict[str, Offer]) -> Rows:
    """切换按钮，一款游戏一行。"""
    named = len(offers) > 1  # 两款都有时标签带上游戏名：两边的昵称常常一样
    return [
        [(f"切换到 {GAMES[app_code]} {nickname}" if named else f"切换到 {nickname}", f"{COMMANDS[app_code][2]} {number}")]
        for app_code, (number, nickname) in offers.items()
    ]


# ── UID 对不上：成员绑上另一个账号后自动再导入 ─────────────────────────────

STILL_MISMATCH = "你刚绑定的账号里也没有小黑盒绑定的那个{game}角色，{game}还是没有导入"
RETRIED = "你刚绑定的账号对上了小黑盒，我已自动重新导入：\n【{game}】{text}"
RETRY_FAILED = "我自动重新导入{game}时没成功：{text}"
RETRY_BUSY = f"自动重新导入{{game}}时排队超时，请稍后再发一次 {COMMAND}"


@dataclass
class Mismatch:
    """一次 UID 对不上的 /导入小黑盒。"""

    heybox_id: str
    games: dict[str, tuple[str, frozenset[int]]]  # 游戏 -> (小黑盒账号绑定的游戏 UID, 当时成员这款游戏的角色 id)
    event: MessageEvent  # 成员发的那条指令：自动再导入的结果作为对它的回复
    at: float


_mismatches: dict[int, Mismatch] = {}  # 成员 id -> 最近一次；只在内存里
_retries: set[asyncio.Task] = set()
_retry_turns: dict[int, asyncio.Lock] = {}  # 成员 id -> 他的自动再导入轮流用的锁
_retry_users: Counter[int] = Counter()  # 在用或在等这把锁的自动再导入个数，为 0 时删掉锁


@contextlib.asynccontextmanager
async def _retry_turn(owner_id: int):
    """同一成员的自动再导入一个接一个：两款游戏先后同步完时，后一款等前一款做完，不会被当成手动导入跳过。
    不同成员之间由 _queued 排队。"""
    _retry_users[owner_id] += 1
    try:
        async with _retry_turns.setdefault(owner_id, asyncio.Lock()):
            yield
    finally:
        _retry_users[owner_id] -= 1
        if not _retry_users[owner_id]:
            del _retry_users[owner_id], _retry_turns[owner_id]


def _remember(owner_id: int, heybox_id: str, results: dict[str, GameImport], roles: dict[str, dict[int, str]], at: float) -> None:
    """记下这次哪些游戏 UID 对不上（替换之前的记录），at 是收到指令的时间；导入成功的游戏不用再等。"""
    for app_code, result in results.items():
        if result.outcome in ("imported", "nothing"):
            _forget_game(owner_id, app_code)
    games = {app_code: (result.heybox_uid, frozenset(roles[app_code])) for app_code, result in results.items() if result.outcome == "mismatch"}
    event = current_event.get(None)
    if games and isinstance(event, MessageEvent):
        _mismatches[owner_id] = Mismatch(heybox_id, games, event, at)


def _waiting(owner_id: int, app_code: str) -> Mismatch | None:
    """成员这款游戏还在等自动再导入的那次记录；过期的记录顺手删掉。"""
    now = time.time()
    for owner, entry in list(_mismatches.items()):
        if now - entry.at > RETRY_WINDOW:
            del _mismatches[owner]
    entry = _mismatches.get(owner_id)
    return entry if entry is not None and app_code in entry.games else None


def _forget_game(owner_id: int, app_code: str, entry: Mismatch | None = None) -> None:
    """这款游戏不用再等了：从 entry（默认成员现在的记录）里删掉，删空了就删记录。"""
    entry = _mismatches.get(owner_id) if entry is None else entry
    if entry is not None:
        entry.games.pop(app_code, None)
        if not entry.games and _mismatches.get(owner_id) is entry:
            del _mismatches[owner_id]


async def _retry_if_bound(owner_id: int, app_code: str) -> None:
    """这款游戏的同步结束了：成员绑上了小黑盒的那个角色就再导入（不在这里等它），绑了别的角色就告诉他一次。"""
    if (entry := _waiting(owner_id, app_code)) is None:
        return
    heybox_uid, known = entry.games[app_code]
    async with get_session() as session:
        roles = {role.id: _uid(role) for role in await _roles(owner_id, session, app_code)}
    if _waiting(owner_id, app_code) is not entry:  # 查角色期间同时结束的另一次同步已经处理过了
        return
    if heybox_uid in roles.values():
        task = asyncio.get_running_loop().create_task(_retry(owner_id, app_code, entry))
        _retries.add(task)
        task.add_done_callback(_retries.discard)
    elif roles.keys() - known:
        _forget_game(owner_id, app_code, entry)
        await _answer(entry.event, STILL_MISMATCH.format(game=GAMES[app_code]))


async def _retry(owner_id: int, app_code: str, entry: Mismatch) -> None:
    """用 entry 记下的小黑盒 ID 再导入这款游戏，结果回复在成员发指令的地方。记录已换成新的一次、
    或手动导入正在进行时不做：手动导入成功了会清掉记录。"""
    async with _retry_turn(owner_id):
        if _waiting(owner_id, app_code) is not entry or owner_id in _running:
            return
        _running.add(owner_id)
        try:
            logger.info(f"Heybox import ({app_code}): retrying after a new binding")
            text = await _retried(owner_id, app_code, entry.heybox_id)
        except Exception as e:
            logger.opt(exception=True).error(f"Heybox import ({app_code}) retry failed: {type(e).__name__}")
            text = RETRY_FAILED.format(game=GAMES[app_code], text=f"程序出错，问题已记录，请稍后再发一次 {COMMAND}")  # 不知道出错前写没写入
        finally:
            _running.discard(owner_id)
            _forget_game(owner_id, app_code, entry)
    await _answer(entry.event, text)


async def _retried(owner_id: int, app_code: str, heybox_id: str) -> str:
    """再导入一款游戏；导入到的角色不是默认角色就换成默认。返回告诉成员的话。"""
    game = GAMES[app_code]
    async with get_session() as session:
        try:
            async with _queued():
                result = await _import_game(app_code, owner_id, heybox_id, session)
        except ImportBusy:
            return RETRY_BUSY.format(game=game)
        if result.outcome not in ("imported", "nothing"):
            return RETRY_FAILED.format(game=game, text=result.text)
        return RETRIED.format(game=game, text=result.text) + await _switched(session, owner_id, app_code, result)


async def _answer(event: MessageEvent, text: str) -> None:
    """在成员发指令的地方回复他，同 skland_bind_sign 报告同步结果：把那条消息设为当前事件，桥接就作为回复发出。
    群里发不出去（例如这个群不让机器人主动发言）时改为私聊。"""
    bot = first_bot()
    if bot is None:
        return
    token = current_event.set(event)
    try:
        if isinstance(event, GroupMessageEvent):
            try:
                await bot.send_group_msg(group_id=event.group_id, message=MessageSegment.at(event.get_user_id()) + " " + text)
                return
            except Exception as e:
                logger.info(f"Heybox import: retry reply not delivered in the group ({type(e).__name__}); trying privately")
        await bot.send_private_msg(user_id=int(event.get_user_id()), message=text)
    except Exception as e:
        logger.warning(f"Heybox import: retry reply not delivered ({type(e).__name__})")
    finally:
        current_event.reset(token)


# ── 明日方舟：机器人用一个登录了的小黑盒账号去读 ─────────────────────────
#
# 明日方舟的“抽卡分析”只回答已登录的小黑盒账号（终末地的公开概览不用登录）。机器人保存一个专门账号的
# 登录状态（LOGIN_FILE，0600，绝不打印）；成员发 /导入小黑盒 小黑盒ID 时，带着它打开工具页面，
# 取页面自己发出的请求的回答。小黑盒的网页每次打开都会调用 account/restore_login，它把登录凭证换发成
# 从那时起再有效 7 天；机器人每天早晚各打开一次（renew_login），登录就一直有效。续不上或已失效时私聊告诉超级用户，
# 由他们私聊发 /小黑盒登录 重新扫码。

ARK_TOOL_HOME = "https://web.xiaoheihe.cn/game/arknights/card_statistic"
ARK_TOOL_URL = ARK_TOOL_HOME + "?user_id={heybox_id}"
RESTORE_PATH = "/account/restore_login"
RENEW_WARN = 2 * 86400  # 自动续期失败、离到期不到这么久时提醒超级用户
ARK_TIMEOUT = 45000  # 明日方舟的页面脚本多，小黑盒的脚本服务器慢的时候要二十秒以上才加载完
RENEW_TIMEOUT = 90000  # 续期没人在等，可以等更久
RENEW_ATTEMPTS = 3
RENEW_RETRY = 600  # 续期失败后隔多少秒再试
ARK_ANALYSIS_PATH = "/game/ark/lottery_analysis"
OPEN_IN_APP = "https://web.xiaoheihe.cn/open_inapp**"
LOGIN_URL = "https://login.xiaoheihe.cn/"
LOGIN_FILE = DATA_DIR / "login" / "state.json"
LOGIN_COOKIE = "user_pkey"  # 扫码登录成功后才有的 cookie
LOGIN_WAIT = 150  # 等扫码的秒数（二维码约 2 分钟内有效）
LOGIN_LOST_REPEAT = 6 * 3600  # 登录失效的私聊提醒，隔多久才再发一次
DESKTOP_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
LOGIN_LOST = "暂时导入不了：我读取小黑盒用的登录已失效，已通知管理员重新登录，请过段时间再试"
LOGIN_LOST_NOTICE = "小黑盒登录已失效，成员现在用 /导入小黑盒 导入不了明日方舟。请私聊机器人发送 /小黑盒登录，用机器人专用的小黑盒账号扫码"
RENEW_FAILING = "小黑盒登录这几天没能自动续期，{until} 到期；到期后成员用 /导入小黑盒 导入不了明日方舟。可以私聊机器人发送 /小黑盒登录 重新扫码"
QR_DRAWN = (  # 二维码画好之前是空白的：数一数深色像素
    "c => { try { const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0;"
    " for (let i = 0; i < d.length; i += 4) if (d[i + 3] > 0 && d[i] < 100) n++; return n; } catch (e) { return 9999; } }"
)

login_cmd = on_command("小黑盒登录", rule=strict, permission=SUPERUSER, priority=5, block=True)
_login_lost_told = 0.0


class HeyboxLoginExpired(Exception):
    """机器人保存的小黑盒登录已失效，或者还没有登录过。"""


def _login_expires(state: dict) -> float:
    """登录 cookie 的过期时间（unix 秒）；没有登录时为 0。"""
    return max((float(c.get("expires") or 0) for c in state.get("cookies") or [] if c.get("name") == LOGIN_COOKIE), default=0.0)


def _save_login(state: dict) -> None:
    LOGIN_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(LOGIN_FILE, state)


async def _tell_login_lost() -> None:
    global _login_lost_told
    if time.time() - _login_lost_told >= LOGIN_LOST_REPEAT:
        _login_lost_told = time.time()
        await tell_superusers(LOGIN_LOST_NOTICE, "Heybox import")


async def _fetch_ark_once(heybox_id: str) -> dict:
    if not LOGIN_FILE.exists():
        raise HeyboxLoginExpired
    async with (
        httpx.AsyncClient(timeout=FETCH_TIMEOUT / 1000, follow_redirects=True) as client,
        get_new_page(device_scale_factor=1, viewport={"width": 430, "height": 900}, user_agent=USER_AGENT, storage_state=str(LOGIN_FILE)) as page,
    ):
        await page.route(f"https://{CDN_HOST}/**", _via(client))
        # 网页版会跳到“请在 App 中打开”，跳走会中断还没回来的请求；回 204，浏览器就留在原页面
        await page.route(OPEN_IN_APP, lambda route: route.fulfill(status=204))
        async with page.expect_response(lambda r: ARK_ANALYSIS_PATH in r.url, timeout=ARK_TIMEOUT) as waiter:
            await page.goto(ARK_TOOL_URL.format(heybox_id=heybox_id), wait_until="domcontentloaded", timeout=ARK_TIMEOUT)
        payload = await (await waiter.value).json()
        state = await page.context.storage_state()
    if payload.get("status") == "ok" and _login_expires(state):
        await asyncio.to_thread(_save_login, state)  # 小黑盒可能换发了 cookie
    return payload


async def fetch_ark_analysis(heybox_id: str) -> dict:
    """某个小黑盒 id 的明日方舟抽卡分析，用机器人保存的小黑盒登录读取；页面加载不出来时换新页面重试。"""
    last: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        try:
            payload = await _fetch_ark_once(heybox_id)
        except HeyboxLoginExpired:
            raise
        except Exception as e:
            last = e
            logger.info(f"Heybox fetch attempt {attempt + 1} failed: {type(e).__name__}")
            continue
        if payload.get("status") == "login":
            raise HeyboxLoginExpired
        if payload.get("status") != "ok":
            raise HeyboxRefused(str(payload.get("msg") or "").strip()[:60])
        return payload.get("result") or {}
    raise last or RuntimeError("heybox fetch failed")


async def renew_login() -> float:
    """打开一次小黑盒网页，让它换发登录凭证，再存下来；返回新的到期时间。登录已失效时抛 HeyboxLoginExpired。"""
    if not LOGIN_FILE.exists():
        raise HeyboxLoginExpired
    async with (
        httpx.AsyncClient(timeout=FETCH_TIMEOUT / 1000, follow_redirects=True) as client,
        get_new_page(device_scale_factor=1, viewport={"width": 430, "height": 900}, user_agent=USER_AGENT, storage_state=str(LOGIN_FILE)) as page,
    ):
        await page.route(f"https://{CDN_HOST}/**", _via(client))
        await page.route(OPEN_IN_APP, lambda route: route.fulfill(status=204))
        async with page.expect_response(lambda r: RESTORE_PATH in r.url, timeout=RENEW_TIMEOUT) as waiter:
            await page.goto(ARK_TOOL_HOME, wait_until="domcontentloaded", timeout=RENEW_TIMEOUT)
        answer = await (await waiter.value).json()
        state = await page.context.storage_state()
    if answer.get("status") != "ok" or not (answer.get("result") or {}).get("pkey") or not _login_expires(state):
        raise HeyboxLoginExpired
    await asyncio.to_thread(_save_login, state)
    return _login_expires(state)


@scheduler.scheduled_job("cron", hour="6,18", minute=10, id="heybox_login_renew", misfire_grace_time=3600, coalesce=True)
async def _renew_daily() -> None:
    if not LOGIN_FILE.exists():
        return
    for attempt in range(RENEW_ATTEMPTS):
        if attempt:
            await asyncio.sleep(RENEW_RETRY)  # 两次之间不占着锁，成员照样可以导入
        try:
            async with _lock:
                until = await renew_login()
        except HeyboxLoginExpired:
            logger.warning("Heybox login renewal: the saved login is no longer accepted")
            await _tell_login_lost()
            return
        except Exception as e:  # 小黑盒的网页没加载出来之类：过一会儿再试
            logger.warning(f"Heybox login renewal attempt {attempt + 1} failed: {type(e).__name__}")
            continue
        logger.info(f"Heybox login renewed until {datetime.fromtimestamp(until, CN):%m-%d %H:%M}")
        return
    with contextlib.suppress(Exception):  # 这一轮都没续上：十二小时后的下一轮再试，快到期了就提醒
        until = _login_expires(json.loads(LOGIN_FILE.read_text("utf-8")))
        if until - time.time() < RENEW_WARN:
            await tell_superusers(RENEW_FAILING.format(until=f"{datetime.fromtimestamp(until, CN):%m-%d %H:%M}"), "Heybox import")


async def _login_qr(page) -> bytes:
    """打开小黑盒的网页登录，换到扫码登录，等二维码画好后截图。"""
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await page.locator(".login-code-form__qr-trigger").first.click(timeout=15000)
    qr = page.locator("canvas").first
    await qr.wait_for(state="visible", timeout=15000)
    for _ in range(60):  # 页面拿到登录票据后才画二维码
        if await qr.evaluate(QR_DRAWN) > 200:
            break
        await asyncio.sleep(0.5)
    else:
        raise TimeoutError("the login QR code was never drawn")
    box = await qr.bounding_box()
    return await page.screenshot(clip={"x": box["x"] - 24, "y": box["y"] - 24, "width": box["width"] + 48, "height": box["height"] + 48})


@login_cmd.handle()
async def _(event: MessageEvent) -> None:
    if not isinstance(event, PrivateMessageEvent):
        await login_cmd.finish("请私聊机器人发送 /小黑盒登录：登录二维码不能发在群里")
    if _lock.locked():
        await login_cmd.finish("有人正在导入，请稍后再发")
    state: dict = {}
    async with (
        _lock,
        httpx.AsyncClient(timeout=FETCH_TIMEOUT / 1000, follow_redirects=True) as client,
        get_new_page(device_scale_factor=1, viewport={"width": 1280, "height": 900}, user_agent=DESKTOP_AGENT) as page,
    ):
        await page.route(f"https://{CDN_HOST}/**", _via(client))
        try:
            picture = await _login_qr(page)
        except Exception as e:
            logger.warning(f"Heybox login: no QR code ({type(e).__name__})")
            await login_cmd.finish("小黑盒的登录页面没有加载出来，请稍后再发 /小黑盒登录")
        await UniMessage.image(raw=picture).send()
        await UniMessage.text("请用机器人专用的小黑盒账号，在小黑盒 App 里扫码并确认登录（约 2 分钟内有效）").send()
        deadline = time.monotonic() + LOGIN_WAIT
        while time.monotonic() < deadline and not _login_expires(state):
            await asyncio.sleep(2)
            state = await page.context.storage_state()
        if _login_expires(state):
            await asyncio.sleep(2)  # 让登录后的跳转把 cookie 写完
            state = await page.context.storage_state()
            await asyncio.to_thread(_save_login, state)
    if not _login_expires(state):
        await login_cmd.finish("没有等到扫码，二维码已失效。需要时再发 /小黑盒登录")
    global _login_lost_told
    _login_lost_told = 0.0
    logger.info("Heybox login saved")
    until = datetime.fromtimestamp(_login_expires(state), CN)
    await login_cmd.finish(f"小黑盒登录成功（凭证 {until:%m-%d %H:%M} 到期）。机器人每天早晚自动续期，一般不用再扫码；续不上时会私聊提醒你")


# 上游按导出链接导入（skland import）：小黑盒的导出给不出链接，明日方舟按小黑盒 ID 导入。
# 上游的指令已由 plugins.skland_shortcuts 关闭；这里以防万一还有别的路走到它。


async def _ark_link_import(url, user_session, session, *, role_index=None) -> None:
    await UniMessage(f"明日方舟的早期抽卡记录从小黑盒导入：{COMMAND} 小黑盒ID").finish(at_sender=True)


ark_gacha_command.import_handler = _ark_link_import

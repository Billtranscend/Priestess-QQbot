"""在 nonebot-plugin-skland 的多账号绑定之上，加上本项目的绑定规则和后续处理。

一个成员（一个 QQ）可以绑定多个森空岛账号。上游每次绑定都会在显示角色列表后要求回复「确认」；
本项目保留成员熟悉的流程，只多加一条指令：

  /skl绑定       首次绑定：扫码 -> 绑定成功，不用确认。
                 已经绑定且登录仍有效：不发二维码。如果抽卡查询用的鹰角 token 已过期，
                 就给出二维码，成员用同一个账号重新扫码即可续期，所有记录都保留。
  /skl添加账号   已绑定的成员再加一个森空岛账号：扫码 -> 角色列表 -> 回复「确认」（上游的确认流程），
                 底层是 `skland qrcode --add`。还没绑定过的成员会被引导到 /skl绑定，不发二维码。

用凭据绑定（上游的 `skland bind`，`/sk -b`、`/sk --bind`，以及旧写法 /skltoken绑定）已关闭，而且完全不回应：
贴到聊天里的凭据等于完整的登录，只能扫码绑定。

群里发的 /skl绑定 把二维码私聊发给本人，群里只说一句；之后这条指令的回复和抽卡同步的通知也都私聊
（/skl添加账号 只有二维码私聊：角色列表和「确认」留在群里）。私聊发不出去（还不是好友）就照旧发在群里，
末尾加一句加好友的提示。

下面两条规则是因为二维码可能发在群里，森空岛分不出是谁扫的：
  - 一个森空岛账号只能属于一个 QQ；
  - 通过 /skl绑定 重新扫码时，必须是该成员自己的账号之一。

保存绑定之后的后续处理：
  - 首次绑定或新加的账号会在后台签到一次（每晚的任务只覆盖午夜之前绑定的账号），再更新一次卫戍榜成绩
    （即 /卫戍榜 更新；全员的统计每天 10:30 才做）；
  - 终末地（skland_auto_gacha）和明日方舟（skland_ark_gacha）的抽卡记录都马上排队拉取，回复里说明
    正在拉取、请成员稍等；在各自第一次尝试结束前，两款游戏的抽卡记录指令和更新指令都回答“请稍等”；
  - 两边都结束后，在成员绑定的地方用一条消息告诉他结果。

另外，撤回二维码失败时（平台只让撤约两分钟内的消息，或桥接找不到那条消息）绑定照常进行
（上游在扫码和保存之间撤回那张图片）。
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import re
import sys
import time
from collections import Counter
from contextvars import Context, ContextVar

from nonebot import get_driver, logger, on_message, require
from nonebot.adapters.onebot.v11 import ActionFailed, Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.exception import IgnoredException, MockApiException
from nonebot.matcher import Matcher, current_event, current_matcher
from nonebot.message import run_preprocessor
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_skland")
require("plugins.skland_auto_gacha")
require("plugins.skland_ark_gacha")

import nonebot_plugin_localstore as store
from nonebot_plugin_alconna import Option, UniMessage, command_manager
from nonebot_plugin_alconna.consts import ALCONNA_RESULT
from nonebot_plugin_alconna.uniseg.message import current_send_wrapper
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.api import SklandLoginAPI
from nonebot_plugin_skland.commands import bind as bind_commands
from nonebot_plugin_skland.db_handler import get_account_characters, get_accounts
from nonebot_plugin_skland.exception import (
    AccountIdentityResolutionError,
    BindingStateChangedError,
    DuplicateAccountIdentityError,
    RequestException,
)
from nonebot_plugin_skland.matcher import skland_command
from nonebot_plugin_skland.model import SkUser
from nonebot_plugin_skland.services import binding as binding_service
from nonebot_plugin_skland.services.sign import sign_character
from sqlalchemy import select

from plugins import skland_ark_gacha as ark_gacha
from plugins import skland_auto_gacha as auto_gacha
from plugins.skland_roles import owner_id_of
from plugins import shared
from plugins.shared import first_bot, write_json

__plugin_meta__ = PluginMetadata(
    name="Skland binding rules",
    description="森空岛绑定规则与绑定后的自动签到、卫戍榜更新、抽卡同步通知；/skl添加账号 绑定更多森空岛账号。",
    usage="/skl绑定　/skl添加账号",
    type="application",
)

BOUND = "绑定成功"
NOTICE = "\n正在为你签到明日方舟和终末地、更新卫戍榜成绩（仅首次绑定，之后每天自动进行）"
ADDED = "\n已添加为你的另一个森空岛账号，正在为新账号签到、更新卫戍榜成绩。发 /skl角色 查看全部角色和序号，发 /skl切换终末地角色 序号 或 /skl切换方舟角色 序号 更换默认角色"
RENEWED = "\n登录凭证已更新，之前保存的抽卡记录都还在"
SYNC_NOTICE = "\n正在拉取你的终末地和明日方舟抽卡记录，完成后会在这里通知你；收到通知前请先不要查看或更新抽卡记录"
SYNC_BUSY = "正在自动拉取你的抽卡记录，完成后我会{where}通知你，请收到通知后再查看"
HEYBOX_HINT = "\n" + shared.HEYBOX_HINT
GAMES = {"ef": ("终末地", "/zmd抽卡记录"), "ark": ("明日方舟", "/mrfz抽卡记录")}  # 绑定后排队同步的游戏：名称、查看记录的指令
SYNCED = {"first": "抽卡记录拉取完成：", "add": "新账号的抽卡记录已同步：", "renew": "抽卡记录已重新同步："}
PENDING_FILE = store.get_plugin_data_file("pending.json")
PENDING_TTL = 3 * 3600  # 队列重试约 35 分钟后放弃；比这更旧的都已失效
_tasks: set[asyncio.Task] = set()
_flow: ContextVar[str | None] = ContextVar("skland_bind_flow", default=None)  # 保存绑定期间为 "first" | "renew" | "add"


# ── 回复的附加行 ──────────────────────────────────────────────────────────


def _install_notice(notice: str) -> None:
    """一次性，只作用于当前 matcher 的上下文；在它之前安装的包装函数之后运行。"""
    previous = current_send_wrapper.get(None)
    consumed = False

    async def with_notice(bot, event, message):
        nonlocal consumed
        first_reply = not consumed and isinstance(message, UniMessage) and message.extract_plain_text() in (BOUND, "账号更新成功")
        if previous is not None:
            message = await previous(bot, event, message)
        if first_reply:
            consumed = True
            current_send_wrapper.set(previous)
            message = message.copy()
            message += notice
        return message

    current_send_wrapper.set(with_notice)


# ── 绑定后的签到 ─────────────────────────────────────────────


async def sign_once(owner_id: int) -> dict[str, int]:
    """为成员的每个明日方舟 / 终末地角色签到；返回用于日志的计数。

    今天已经签过的角色会回答“重复签到”，这不算失败。
    """
    stats = Counter()
    # 自己的会话：这是绑定指令之后在后台跑的，指令的会话（scoped）在指令结束时就会被关掉
    async with get_session(expire_on_commit=False) as session:
        for account in await get_accounts(owner_id, session):
            for character in await get_account_characters(account.id, session):
                if character.app_code not in ("arknights", "endfield") or (character.app_code == "endfield" and not character.role_id):
                    continue
                result = (await sign_character(account, character, character.app_code))["result"]
                failed = isinstance(result, str) and "重复签到" not in result
                stats[f"{'ark' if character.app_code == 'arknights' else 'endfield'}_{'failed' if failed else 'ok'}"] += 1
        await session.commit()  # 保存刷新过的 token
    return dict(stats)


async def _after_binding(owner_id: int, qq: str | None) -> None:
    """首次绑定和新加的账号：签到一次，再更新一次卫戍榜成绩。都在后台，不另外回复。"""
    try:
        logger.info(f"Skland sign-in after binding done: {await sign_once(owner_id)}")
    except Exception as e:
        logger.warning(f"Skland sign-in after binding failed: {type(e).__name__}")
    garrison = sys.modules.get("plugins.arknights_garrison")  # 加载了卫戍榜才有；绝不从这里导入
    if qq is None or garrison is None:
        return
    try:
        # 在空的上下文里运行：卫戍榜更新是独立的后台任务，不沿用绑定那条指令的上下文
        said = await asyncio.get_running_loop().create_task(garrison.refresh_one(qq), context=Context())
        logger.info(f"Stronghold board after binding: {'updated' if said.startswith('卫戍榜成绩已更新') else 'nothing to show'}")
    except Exception as e:
        logger.warning(f"Stronghold board after binding failed: {type(e).__name__}")


# ── 抽卡同步通知 ───────────────────────────────────────────────────


def _load_pending() -> dict[str, dict]:
    try:
        data = json.loads(PENDING_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {uid: entry for uid, entry in data.items() if time.time() - entry.get("ts", 0) < PENDING_TTL}


def _save_pending(pending: dict[str, dict]) -> None:
    PENDING_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json(PENDING_FILE, pending)


# 成员绑定时发的那条消息（只在内存里，重启后就没了）。同步结果在后台报告，这时没有“正在回答谁”：
# 官方机器人在不允许主动发言的群里只能发回复，而回复要挂在被回答成员自己近几分钟的消息上。
# 报告时把这条消息设为当前事件，桥接就会把通知作为对它的回复发出。
_asked: dict[int, tuple[object, float]] = {}


def _remember_chat(owner_id: int, kind: str = "first") -> None:
    """记下绑定是在哪里进行的，好在那里报告同步结果。"""
    event = current_event.get(None)
    if event is None or not hasattr(event, "get_user_id"):
        return
    now = time.time()
    for owner, (_, at) in list(_asked.items()):
        if now - at > PENDING_TTL:
            del _asked[owner]
    _asked[owner_id] = (event, now)
    pending = _load_pending()
    pending[str(owner_id)] = {
        "qq": event.get_user_id(),
        "group_id": event.group_id if isinstance(event, GroupMessageEvent) else None,
        "private": _answered_privately(),  # 群里的指令私聊回答的：先私聊报告，发不出去再回到群里
        "ts": time.time(),
        "kind": kind,
    }
    _save_pending(pending)


def _report_in_group(qq: str) -> None:
    """本该私聊的回复退回了群里：同步结果也在群里报告，和回复里说的“在这里”一致。"""
    pending = _load_pending()
    changed = False
    for entry in pending.values():
        if entry.get("qq") == qq and entry.get("private"):
            entry["private"], changed = False, True
    if changed:
        _save_pending(pending)


def _forget_chat(owner_id: int) -> None:
    _asked.pop(owner_id, None)
    pending = _load_pending()
    if pending.pop(str(owner_id), None) is not None:
        _save_pending(pending)


def _rank_opted_out(qq: str, owner_id: int) -> set[str]:
    """退出了欧非榜的游戏（GAMES 的键），按号码或按人（o<成员 id>）退出的都算；名单读不了时当作都退出了（不说加入）。"""
    try:
        from plugins import skland_gacha_rank as rank

        marks = {qq, f"o{owner_id}"}
        return {game for game, board in (("ef", rank.ENDFIELD), ("ark", rank.ARKNIGHTS)) if marks & rank._load_optout(board)}
    except Exception:
        return set(GAMES)


def _expect(owner_id: int, games: list[str]) -> None:
    """记下要等哪几个游戏的同步结果（GAMES 的键）；一个也没排上，就不会有报告。"""
    pending = _load_pending()
    if str(owner_id) not in pending:
        return
    if games:
        pending[str(owner_id)]["results"] = dict.fromkeys(games)
    else:
        pending.pop(str(owner_id))
    _save_pending(pending)


def _sync_report(kind: str, results: dict[str, list], opted_out: set[str] = frozenset()) -> str | None:
    """绑定后排的抽卡同步都结束了，在成员绑定的地方说的话；None：没什么值得说的。

    results：游戏 -> [状态, 新增条数]；opted_out：成员退出了欧非榜的游戏，不说加入了统计名单。
    """
    shown, parts, failed = [], [], []
    for game, (status, records) in results.items():
        name, command = GAMES[game]
        if status == "failed":
            failed.append(f"{command}更新")
            parts.append(f"{name}拉取失败（可稍后发送 {command}更新 重试）")
        elif status in ("success", "partial"):  # 其他状态：没有这个游戏的角色、没有 token、期间已解绑或重新绑定
            shown.append(command)
            if not records:
                part = f"{name}暂时没有查到记录" if kind == "first" else f"{name}没有新增记录"
            else:
                part = f"{name} {records} 条" if kind == "first" else f"{name}新增 {records} 条"
            notes = [f"有角色拉取失败，可稍后发送 {command}更新 重试"] if status == "partial" else []
            if kind == "first" and records and game not in opted_out:
                notes.append("现已加入欧非榜统计名单")
            parts.append(part + (f"（{'；'.join(notes)}）" if notes else ""))
    if not shown:
        return f"抽卡记录拉取失败，可稍后发送 {' 或 '.join(failed)} 重试" if failed else None
    text = SYNCED[kind] + "；".join(parts)
    if kind == "add":
        text += f"。{' 和 '.join(shown)} 看的是默认角色，看其他角色请加 角色序号，如 {shown[0]} 角色2（序号见 /skl角色）"
    elif kind == "renew":
        text += f"，现在可以发 {' 或 '.join(shown)} 查看"
    else:  # 首次绑定：告诉成员更早的记录怎么补
        text += HEYBOX_HINT
    return text


async def _on_synced(game: str, owner_id: int, status: str, records: int, final: bool) -> None:
    if not final:  # 失败但还会重试：继续等
        return
    pending = _load_pending()
    entry = pending.get(str(owner_id))
    if entry is None:
        return
    results = entry.setdefault("results", {"ef": None})  # 没有 results 的条目只等终末地
    if game not in results or results[game] is not None:  # 绑定时没有为这个游戏排同步，或者已经有了结果
        return
    results[game] = [status, records]
    waiting = None in results.values()  # 另一个游戏还没结束
    if not waiting:
        pending.pop(str(owner_id))
    _save_pending(pending)
    text = None if waiting else _sync_report(entry.get("kind") or "first", results, _rank_opted_out(entry["qq"], owner_id))
    asked = None if waiting else _asked.pop(owner_id, (None, 0.0))[0]
    bot = first_bot()
    if text is None or bot is None:
        return
    token = current_event.set(asked) if asked is not None else None  # 作为对绑定那条消息的回复发出（见 _asked）
    try:
        group_id = entry.get("group_id")
        if group_id and not entry.get("private"):
            await bot.send_group_msg(group_id=int(group_id), message=MessageSegment.at(entry["qq"]) + " " + text)
            return
        try:
            await bot.send_private_msg(user_id=int(entry["qq"]), message=text)
        except Exception as e:
            if not group_id:
                raise
            logger.info(f"Skland binding: pull report not delivered privately ({type(e).__name__}); reported in the group")
            await bot.send_group_msg(group_id=int(group_id), message=MessageSegment.at(entry["qq"]) + " " + text)
    finally:
        if token is not None:
            current_event.reset(token)


async def _on_ef_synced(owner_id: int, status: str, records: int, final: bool) -> None:
    await _on_synced("ef", owner_id, status, records, final)


async def _on_ark_synced(owner_id: int, status: str, records: int, final: bool) -> None:
    await _on_synced("ark", owner_id, status, records, final)


if _on_ef_synced not in auto_gacha.sync_listeners:
    auto_gacha.sync_listeners.append(_on_ef_synced)
if _on_ark_synced not in ark_gacha.sync_listeners:
    ark_gacha.sync_listeners.append(_on_ark_synced)


# ── 账号是谁的 ─────────────────────────────────────────────────

ALREADY_BOUND = "你已经绑定过森空岛了，不用再扫码。角色有变化发 /skl角色更新；想再绑定一个森空岛账号发 /skl添加账号；想解绑发 /skl解绑"
BIND_FIRST = "你还没有绑定过森空岛账号，请先发 /skl绑定 绑定第一个账号；/skl添加账号 是已经绑定之后再加一个账号用的"
BOUND_ELSEWHERE = "这个森空岛账号已经绑定在另一个 QQ 上，没有为你绑定。二维码只能由发指令的本人用自己的森空岛扫；如果那是你的另一个 QQ，请先在那个 QQ 上发 /skl解绑"
RESTORED = "已确认这个森空岛账号是你的，原来的绑定和抽卡记录都已恢复，不用重新绑定。发 /skl角色 可以查看；如果之后提示登录失效，再发一次 /skl绑定 续期"
RESTORE_IN_PRIVATE = "这个森空岛账号之前已经绑定过，原来的绑定和记录都还在。要恢复：第一步，加我为好友；第二步，再发一次 /skl绑定，二维码会私聊发给你，扫完就恢复"
OTHER_ACCOUNT = "扫码的森空岛账号和你已绑定的不是同一个，绑定没有改动。续期请用原来的账号扫码；想再绑定一个账号请发 /skl添加账号"
NO_ROLES = "这个森空岛账号下没有可绑定的明日方舟或终末地角色，没有保存"
CHANGED = "绑定数据在这期间发生了变化，没有保存，请重新发送指令"
IDENTITY_FAILED = "现有账号的身份校验失败，请先发送 /skl角色更新，或用 /skl解绑 移除异常的账号"


async def _gacha_login_expired(user: SkUser) -> bool:
    """抽卡查询需要的 token 已失效。凡是不能确定的都算没过期。"""
    if not user.access_token:
        return False
    try:
        await SklandLoginAPI.get_grant_code(user.access_token, 1)
    except RequestException as e:
        return "过期" in str(e)
    except Exception as e:
        logger.info(f"Skland bind guard: gacha login check inconclusive ({type(e).__name__})")
    return False


async def _login_alive(user: SkUser) -> bool:
    """保存的登录是否仍然有效（森空岛要求时会续期）。未知算作有效：不会凭猜测发二维码。"""
    try:
        from plugins import skland_health  # 在本插件之后加载

        return (await skland_health._login(user))[0] != "登录已过期"
    except Exception as e:
        logger.info(f"Skland bind guard: login check inconclusive ({type(e).__name__})")
        return True


def _arp(state: T_State):
    return getattr(state.get(ALCONNA_RESULT), "result", None)


def _add_requested() -> bool:
    matcher = current_matcher.get(None)
    result = _arp(matcher.state) if matcher else None
    return bool(result and result.find("qrcode.add"))


def _qrcode_scan() -> bool:
    matcher = current_matcher.get(None)
    result = _arp(matcher.state) if matcher else None
    return bool(result and result.find("qrcode"))


@run_preprocessor
async def _no_qrcode_when_bound(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = _arp(state)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("qrcode"):
        return
    adding = bool(arp.find("qrcode.add"))
    async with get_session() as session:
        owner_id = await owner_id_of(session, event.get_user_id())
        accounts = list(await session.scalars(select(SkUser).where(SkUser.owner_id == owner_id))) if owner_id is not None else []
        if adding:
            if accounts:
                return  # 发二维码：保存之前角色列表必须先确认
            # 还没绑定过：第一个账号用一条指令，再加账号用另一条，免得成员搞混。
            await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + BIND_FIRST if isinstance(event, GroupMessageEvent) else BIND_FIRST)
            raise IgnoredException("no Skland account bound yet")
        if not accounts:
            return
        renewal = False
        for account in accounts:
            if not await _login_alive(account) or await _gacha_login_expired(account):
                renewal = True
        await session.commit()  # 保存续期后的 token
    if renewal:
        logger.info("Skland bind guard: QR code allowed, a login needs renewing")
        return
    await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + ALREADY_BOUND if isinstance(event, GroupMessageEvent) else ALREADY_BOUND)
    raise IgnoredException("already bound to Skland")


# 用凭据绑定（上游的 `skland bind` / `-b` / `--bind`，以及旧的 /skltoken绑定）已经关闭，而且完全不回应：
# 机器人不回答，也不转给上游。排在上游的 matcher（优先级 1）之前：不带凭据的 `/sk -b` 否则会打开
# Alconna 的补全提示，提示里输入的凭据照样会出现在群里。
TOKEN_COMMAND = re.compile(r"^\s*/\s*(?:(?:森空岛|skl|skd)\s*token\s*绑定|(?:skland|sk)\s+(?:-b|--bind|bind)(?=\s|$))", re.I)


async def _is_token_command(event: MessageEvent) -> bool:
    return TOKEN_COMMAND.match(event.get_plaintext()) is not None


token_binding = on_message(rule=_is_token_command, priority=0, block=True)


@token_binding.handle()
async def _() -> None:
    logger.info("Skland binding by token is closed; the message was ignored")


@run_preprocessor
async def _no_token_binding(matcher: Matcher, state: T_State) -> None:
    """其他任何写法只要还能到达上游的 `skland bind`，也一样不处理、不回答。"""
    arp = _arp(state)
    if matcher.plugin_name != "nonebot_plugin_skland" or arp is None or not arp.find("bind"):
        return
    raise IgnoredException("token binding is closed")


# ── 群里的二维码私聊发给本人 ─────────────────────────────────────────

QR_IN_PRIVATE = "二维码已私聊发给你，请到和我的私聊里扫码"
QR_ADD_FRIEND = "\n加我为好友后，下次二维码会私聊发给你，别人看不到"


class PrivateQR:
    """一位成员在群里发的 /skl绑定 或 /skl添加账号（一次指令一个）。

    phase：waiting 二维码还没发；sending 正私聊发二维码；private 之后的回复都私聊（/skl绑定）；
    group 之后都在群里（/skl添加账号，或私聊发不出去）；over 指令已结束（后台任务也可能还拿着它）。
    """

    def __init__(self, event: GroupMessageEvent, adding: bool) -> None:
        self.event, self.adding = event, adding
        self.phase = "waiting"
        self.qr_private = False  # 二维码只有本人看到了
        self._sending: list[tuple[dict, Message, bool]] = []  # 改成私聊的调用：data、原本发到群里的消息、是不是二维码

    def route(self, api: str, data: dict) -> None:
        """由 skland_shortcuts 的发送钩子在任何 await 之前调用：回答本人这条指令、发到本群的消息改为私聊。

        只改 send_msg（指令的回复都经 bot.send 走它）：send_group_msg 的参数里改不出私聊。
        """
        if api != "send_msg" or data.get("message_type") != "group" or current_event.get(None) is not self.event:
            return
        message = data.get("message")
        if str(data.get("group_id")) != str(self.event.group_id) or not isinstance(message, Message):
            return
        qr = self.phase == "waiting" and any(seg.type == "image" for seg in message)
        if not qr and self.phase != "private":
            return
        if qr:
            self.phase = "sending"
        private = Message(seg for seg in message if seg.type not in ("at", "reply"))
        while private and private[0].type == "text" and not private[0].data.get("text", "").strip():
            del private[0]
        data.pop("group_id", None)
        data.update(message_type="private", user_id=self.event.user_id, message=private)
        self._sending.append((data, message, qr))

    def take(self, data: dict) -> tuple[Message, bool] | None:
        for entry in self._sending:
            if entry[0] is data:
                self._sending.remove(entry)
                return entry[1], entry[2]
        return None


def _answered_privately() -> bool:
    answer = shared.private_answer.get()
    return isinstance(answer, PrivateQR) and answer.phase == "private"


def _qr_seen_by_asker_only() -> bool:
    answer = shared.private_answer.get()
    return isinstance(answer, PrivateQR) and answer.qr_private


@Bot.on_called_api
async def _private_reply_sent(bot: Bot, exception: Exception | None, api: str, data: dict, result) -> None:
    """改成私聊的回复发出之后：二维码送到了就在群里说一句；没送到（不是好友等）就照原样发在群里，
    上游拿到的是群里那条的回执（撤回的也是它）。"""
    answer = shared.private_answer.get()
    taken = answer.take(data) if isinstance(answer, PrivateQR) else None
    if taken is None:
        return
    original, qr = taken
    if exception is None:
        if qr:
            answer.qr_private = True
            try:
                await bot.send(answer.event, MessageSegment.at(answer.event.user_id) + " " + QR_IN_PRIVATE)
            except Exception as e:
                logger.warning(f"Skland binding: the group was not told the QR code went privately ({type(e).__name__})")
            answer.phase = "group" if answer.adding else "private"
        return
    reason = exception.info.get("retcode") if isinstance(exception, ActionFailed) else type(exception).__name__
    logger.info(f"Skland binding: private reply not delivered ({reason}); answered in the group")
    answer.phase = "group"
    if qr:
        original = original + MessageSegment.text(QR_ADD_FRIEND)
    else:
        try:
            _report_in_group(str(answer.event.user_id))
        except Exception as e:
            logger.warning(f"Skland binding: pull report not moved to the group ({type(e).__name__})")
    try:
        result = await bot.send_msg(message_type="group", group_id=answer.event.group_id, user_id=answer.event.user_id, message=original)
    except Exception as e:
        logger.warning(f"Skland binding: the reply was not sent to the group either ({type(e).__name__})")
        return
    raise MockApiException(result)


_upstream_qrcode = bind_commands.qrcode_handler
if getattr(_upstream_qrcode, "_bind_sign", False):
    raise RuntimeError("Skland private QR code already installed")


@functools.wraps(_upstream_qrcode)
async def _qrcode_privately(user_session, session) -> None:
    """群里发的指令：二维码私聊发给本人（PrivateQR）；其他情况原样交给上游。"""
    event = current_event.get(None)
    if not isinstance(event, GroupMessageEvent):
        await _upstream_qrcode(user_session, session)
        return
    answer = PrivateQR(event, adding=_add_requested())
    token = shared.private_answer.set(answer)
    try:
        await _upstream_qrcode(user_session, session)
    finally:
        answer.phase = "over"
        shared.private_answer.reset(token)


# ── 保存绑定 ────────────────────────────────────────────────────

_upstream_confirm = bind_commands._confirm_account_binding
if getattr(_upstream_confirm, "_bind_sign", False):
    raise RuntimeError("Skland binding rules already installed")
_previous_commit = binding_service.commit_account_binding
if getattr(_previous_commit, "_bind_sign", False):
    raise RuntimeError("Skland binding follow-ups already installed")


@functools.wraps(_previous_commit)
async def _commit_with_followups(prepared, session):
    """保存绑定之后：两款游戏的抽卡记录排队同步，准备同步通知、补充回复、签到和更新卫戍榜。"""
    kind, owner_id = _flow.get(None), prepared.owner_id
    if kind:
        try:
            _remember_chat(owner_id, kind)  # 在绑定把同步排进队列之前，这样就不会错过结果
        except Exception as e:
            logger.warning(f"Skland binding: sync notice not armed: {type(e).__name__}")
    try:
        await _previous_commit(prepared, session)
    except BaseException:
        if kind:
            with contextlib.suppress(Exception):
                _forget_chat(owner_id)  # 没有排同步，所以不会有任何报告
        raise
    games = [game for game, queue in (("ef", auto_gacha), ("ark", ark_gacha)) if _queue_sync(queue, owner_id)]
    if not kind:
        return
    try:
        _expect(owner_id, games)
    except Exception as e:
        logger.warning(f"Skland binding: sync notice not armed: {type(e).__name__}")
    _install_notice({"first": NOTICE, "renew": RENEWED, "add": ADDED}[kind] + (SYNC_NOTICE if games else ""))
    if kind != "renew":
        event = current_event.get(None)
        qq = event.get_user_id() if event is not None and hasattr(event, "get_user_id") else None
        task = asyncio.get_running_loop().create_task(_after_binding(owner_id, qq))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


def _queue_sync(queue, owner_id: int) -> bool:
    """排一次抽卡同步（queue 是 skland_auto_gacha 或 skland_ark_gacha），排上了返回 True。"""
    try:
        return queue.queue_after_binding(owner_id)
    except Exception as e:  # 绑定已经提交了，绝不能把它误报成绑定失败
        logger.error(f"Skland binding: gacha sync not queued ({queue.__name__}): {type(e).__name__}")
        return False


async def _restore_identity(user_session, session, skland_user_id: str, own_accounts: set) -> str | None:
    """官方机器人不认识的成员登录了一个已绑定到某个 QQ 号的账号：他就是那个成员吗？

    官方平台不显示 QQ 号，所以通过旧机器人绑定的成员，在有东西把两边对上之前
    （plugins/official_bridge），对官方机器人来说是陌生人。登录这个森空岛账号就是这样一种对应，
    前提是只有本人才能做到：私聊里是这样（二维码不给别人看），群里的指令只有二维码私聊发给了本人时才是。只有在这个 QQ 号下
    还没有认识的其他人、且该成员自己还没绑定任何东西时，才把身份交接过去。
    返回用来回答的那一行；返回 None 则按普通情况拒绝。
    """
    try:
        from nonebot.matcher import current_bot
        from nonebot_plugin_user.models import Bind

        official_bridge = sys.modules.get("plugins.official_bridge")  # 离线测试里没有它；绝不从这里导入
        if official_bridge is None:
            return None

        requester = str(user_session.platform_user.id)
        if own_accounts or not requester.isdigit() or not official_bridge.served_as_placeholder(current_bot.get(), int(requester)):
            return None
        owner = await session.scalar(select(SkUser.owner_id).where(SkUser.skland_user_id == skland_user_id).limit(1))
        numbers = [int(p) for p in await session.scalars(select(Bind.platform_id).where(Bind.bind_id == owner)) if str(p).isdigit()]
        await session.rollback()
        if owner is None or not official_bridge.claimable(numbers):
            return None
        if not user_session.session.scene.is_private and not _qr_seen_by_asker_only():
            return RESTORE_IN_PRIVATE
        if official_bridge.claim_number(current_bot.get(), int(requester), numbers) is None:
            return None
        logger.info("Skland bind guard: a member not recognised before logged in to their own bound account; their binding is theirs again")
        return RESTORED
    except Exception as e:
        logger.warning(f"Skland bind guard: identity not restored ({type(e).__name__})")  # 不写异常文字：数据库报错会带上参数（含凭证）
        with contextlib.suppress(Exception):
            await session.rollback()
        return None


@functools.wraps(_upstream_confirm)
async def _confirm(*, owner_id, pending, snapshot, mode, user_session, session) -> None:
    """判断扫码 / 输入的凭据可以做什么，然后带或不带上游的确认提示保存它。"""
    own_accounts = {account.skland_user_id for account in await get_accounts(owner_id, session)}
    taken = None
    if pending.skland_user_id not in own_accounts:
        taken = await session.scalar(select(SkUser.id).where(SkUser.skland_user_id == pending.skland_user_id, SkUser.owner_id != owner_id).limit(1))
    await session.rollback()
    if taken is not None:
        restored = await _restore_identity(user_session, session, pending.skland_user_id, own_accounts)
        if restored is not None:
            await UniMessage(restored).send(at_sender=True)
            return
        logger.info("Skland bind guard: refused, the account belongs to another QQ")
        await UniMessage(BOUND_ELSEWHERE).send(at_sender=True)
        return
    own = pending.skland_user_id in own_accounts
    if own_accounts and not own:
        if _qrcode_scan() and not _add_requested():  # 为续期发出的二维码，却被另一个账号扫了
            logger.info("Skland bind guard: refused, re-scan with another account")
            await UniMessage(OTHER_ACCOUNT).send(at_sender=True)
            return
        token = _flow.set("add")
        try:
            await _upstream_confirm(owner_id=owner_id, pending=pending, snapshot=snapshot, mode=mode, user_session=user_session, session=session)
        finally:
            _flow.reset(token)
        return
    # 首次绑定，或者又是成员自己的账号之一：不用确认直接保存。
    try:
        prepared = await binding_service.prepare_account_binding(owner_id, pending, snapshot, session, mode="upsert")
    except AccountIdentityResolutionError:
        await UniMessage(IDENTITY_FAILED).send(at_sender=True)
        return
    except (DuplicateAccountIdentityError, ValueError) as e:
        logger.warning(f"Skland binding not prepared: {type(e).__name__}")
        await UniMessage(IDENTITY_FAILED).send(at_sender=True)
        return
    if prepared.target_account_id is None and not any(role.is_available for role in snapshot.roles):
        await UniMessage(NO_ROLES).send(at_sender=True)
        return
    token = _flow.set("renew" if own else "first")
    try:
        await binding_service.commit_account_binding(prepared, session)
    except BindingStateChangedError:
        await UniMessage(CHANGED).send(at_sender=True)
        return
    else:
        await UniMessage(BOUND).send(at_sender=True)  # 提交时装上的包装函数会补上后续的几行
    finally:
        _flow.reset(token)


_commit_with_followups._bind_sign = True
binding_service.commit_account_binding = _commit_with_followups
_confirm._bind_sign = True
bind_commands._confirm_account_binding = _confirm
_qrcode_privately._bind_sign = True
bind_commands.qrcode_handler = _qrcode_privately  # 上游在处理指令时才导入它

_qrcode = next(option for option in skland_command.options if getattr(option, "dest", "") == "qrcode")
if not any(getattr(option, "dest", "") == "add" for option in _qrcode.options):
    with command_manager.update(skland_command):
        _qrcode.options.append(Option("--add|add", help_text="为已绑定的成员再绑定一个森空岛账号"))


# ── 绑定的同步进行中时，不提供抽卡指令 ─────────────────────


@run_preprocessor
async def _wait_for_bind_sync(matcher: Matcher, bot: Bot, event: MessageEvent, state: T_State) -> None:
    arp = _arp(state)
    upstream = matcher.plugin_name == "nonebot_plugin_skland" and arp is not None
    if upstream and arp.find("efgacha"):
        syncing = auto_gacha.bind_sync_pending
    elif (upstream and arp.find("gacha")) or isinstance(matcher, ark_gacha.update_command):  # /mrfz抽卡记录、/mrfz抽卡记录更新
        syncing = ark_gacha.bind_sync_pending
    else:
        return
    async with get_session() as session:
        owner_id = await owner_id_of(session, event.get_user_id())
    # 只针对已经告诉成员要等（之后也会告诉他结果）的同步。
    entry = _load_pending().get(str(owner_id)) if owner_id else None
    if entry is None or not syncing(owner_id):
        return
    privately = entry.get("private") or not entry.get("group_id")  # 和 _on_synced 报告的地方一致
    busy = SYNC_BUSY.format(where="私聊" if privately else "在这里")
    await bot.send(event, MessageSegment.at(event.get_user_id()) + " " + busy if isinstance(event, GroupMessageEvent) else busy)
    raise IgnoredException("the binding's gacha sync is still running")


# ── 二维码撤回 ──────────────────────────────────────────────────────


@Bot.on_called_api
async def _recall_may_fail(bot: Bot, exception: Exception | None, api: str, data: dict, result) -> None:
    """森空岛指令里撤回失败时，记日志并当作已撤回。

    桥接只认它记下的消息，平台也只让撤约两分钟内的消息，所以撤回可能失败；到那时二维码已经扫过或过期了，
    所以把图片留在聊天里毫无损失，而抛出的异常却会让绑定失败。
    """
    if api != "delete_msg" or exception is None:
        return
    matcher = current_matcher.get(None)
    if matcher is None or matcher.plugin_name != "nonebot_plugin_skland":
        return
    logger.warning(f"Skland QR code recall failed, the command continues: {type(exception).__name__}")
    raise MockApiException(None)


@get_driver().on_startup
async def _verify_binding_rules() -> None:
    if (bind_commands._confirm_account_binding is not _confirm or binding_service.commit_account_binding is not _commit_with_followups
            or bind_commands.qrcode_handler is not _qrcode_privately):
        raise RuntimeError("Skland binding rules were replaced")
    if hasattr(sys.modules["nonebot_plugin_skland"], "qrcode_handler"):  # 上游改成了导入时就取走：二维码会照旧发在群里
        logger.warning("Skland binding: upstream binds qrcode_handler at import; group QR codes are not sent privately")
    prefix = next(iter(skland_command.prefixes), "")
    if not skland_command.parse(f"{prefix}skland qrcode --add").find("qrcode.add"):
        raise RuntimeError("Skland qrcode --add option is not active")

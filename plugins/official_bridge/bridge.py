"""桥接本体：官方机器人的事件进来，OneBot v11 事件出去；API 调用则反过来。

`BridgeBot` 是一个真正的 `onebot.v11.Bot`，所以为 OneBot 机器人写的处理函数、权限和辅助函数都照常
工作。它的适配器并不和 OneBot 实现通信：每个 OneBot API 都通过官方 HTTP API（经由已连接的
`nonebot.adapters.qq` 机器人）和编号映射表来应答。
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import os
import re
import sqlite3
import time
from collections import OrderedDict, deque
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from urllib.parse import unquote, urlparse

from nonebot import logger
from nonebot.adapters import Adapter as BaseAdapter
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import (
    FriendAddNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    PrivateMessageEvent,
    Sender,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.adapters.qq import Bot as QQBot
from nonebot.adapters.qq import event as qe
from nonebot.adapters.qq.exception import ActionFailed as QQActionFailed
from nonebot.adapters.qq.exception import RateLimitException as QQRateLimited
from nonebot.adapters.qq.models import Action, Button, InlineKeyboard, InlineKeyboardRow, Media, MessageKeyboard, MessageMarkdown, MessageReference, Permission, RenderData
from nonebot.adapters.qq.models import MessagePromptKeyboard, PromptAction, PromptButton, PromptContent, PromptKeyboardModel, PromptRenderData, PromptRow
from nonebot.drivers import Driver, Request
from nonebot.matcher import current_event, current_matcher

from .idmap import GROUP_BASE, SELF, USER_BASE, IdMap

GROUP, C2C = "group", "c2c"

# 被动回复：（收到的消息在多少秒内可以回复，每条消息可回复几次）。比文档写的 60 分钟 x 5 次（群）和
# 60 分钟 x 4 次（私聊）略少一点，免得上传慢时越过界限。
PASSIVE = {GROUP: (3500, 5), C2C: (3500, 4)}
INLINE_UPLOAD_MAX = 6 * 1024 * 1024  # 更大的文件走分片上传
URL_REFUSED = 40_054_010
# 在群里 @全体成员：平台自己的标记，用在文字消息里，仅限身为群管理员的机器人，每个群每天一次
# （平台文档）。
EVERYONE = "<qqbot-at-everyone />"
UNPROMPTED_REFUSED = 40_034_105  # 该群不允许机器人在没被搭话时主动发言
MUTED_RETRY = 600.0  # 某群拒收过主动消息后，隔多少秒再试
MEMBERS_NOT_GRANTED = 11253  # 成员名单权限是逐个机器人单独开通的
MEMBER_PAGES = 400  # 平台成员名单最多读这么多页（每页 30 人）；到了上限还没读完的，不算读完
MEMBER_PAGE_PACE = 1.1  # 读成员名单时任意两次请求（不分哪个群）至少隔这么多秒（文档写这个接口 60 QPM）
MEMBERS_REFRESH_EVERY = 6 * 3600  # 一个群的平台成员名单最多隔这么多秒读一次
MEMBERS_RETRY_AFTER = 30 * 60  # 读失败（不是没有权限）的群，隔这么久就可以再读，不必等满 MEMBERS_REFRESH_EVERY
MEMBERS_BACKOFF = 60.0  # 平台说请求太频繁（429）时，全机器人暂停读名单的秒数；连续再遇到就翻倍
MEMBERS_BACKOFF_MAX = 600.0
MEMBERS_DAILY_AFTER = 15 * 3600  # 距上次读完这么久的群，每小时那一轮就再读：留出轮询和一次失败后等 6 小时，一天至少一次
MEMBER_IDS_SEEN = "member_ids_seen"  # meta：平台名单上出现过在那个群发过言的人（名单和事件用的是同一套编号）
GROUP_GONE_CONFIRM = 3600.0  # 平台第一次说某群不存在之后，至少隔这么久、再说一次，才当它真没了
STITCH_GAP = 16  # 拼成一张图的两页之间的间隔像素
FORWARD_WORDS_MAX = 2000  # 合并转发卡片的文字页合成一条消息时，每条消息的字数上限
FORWARD_LINE_MAX = 80  # 这么短的文字页写成一行，跟在它的标题后面
STITCH_MAX_BYTES = 8 * 1024 * 1024  # 拼好的图存成 PNG 超过这个大小时改存 JPEG


def page_of_words(name: str, text: str) -> str:
    """合并转发卡片里只有文字的一页，按它在卡片之外读起来的样子给出。

    卡片里每一页都挂在自己的名字下面，这个名字往往是区分各页的唯一线索（这是谁的结果），所以把它
    放在文字前面。
    """
    text = text.strip()
    if "\n" in text and len(text) <= FORWARD_LINE_MAX:
        text = " ".join(line.strip() for line in text.split("\n") if line.strip())
    if not name:
        return text
    return f"{name}\n{text}\n" if "\n" in text else f"{name}：{text}"


def gathered(pages: list[str], limit: int = FORWARD_WORDS_MAX) -> list[str]:
    """把文字页合成尽量少的消息，每条不超过 `limit` 个字；不会把一页拆开。"""
    out: list[str] = []
    for page in pages:
        if out and len(out[-1]) + 1 + len(page) <= limit:
            out[-1] += "\n" + page
        else:
            out.append(page)
    return [text.strip() for text in out if text.strip()]


def stitch(pictures: list[bytes], max_height: int) -> list[bytes]:
    """把图片上下拼接，在高度允许的范围内拼成尽量少的长图（阻塞操作：放到线程里跑）。"""
    from io import BytesIO

    from PIL import Image

    pages = [Image.open(BytesIO(blob)).convert("RGB") for blob in pictures]
    stacks: list[list[int]] = []
    for index, page in enumerate(pages):
        height = sum(pages[i].height + STITCH_GAP for i in stacks[-1]) + page.height if stacks else 0
        if stacks and height <= max_height:
            stacks[-1].append(index)
        else:
            stacks.append([index])
    out: list[bytes] = []
    for stack in stacks:
        if len(stack) == 1:
            out.append(pictures[stack[0]])  # 原样
            continue
        width = max(pages[i].width for i in stack)
        canvas = Image.new("RGB", (width, sum(pages[i].height for i in stack) + STITCH_GAP * (len(stack) - 1)), pages[stack[0]].getpixel((0, 0)))
        top = 0
        for i in stack:
            canvas.paste(pages[i], ((width - pages[i].width) // 2, top))
            top += pages[i].height + STITCH_GAP
        buffer = BytesIO()
        canvas.save(buffer, "PNG")
        if buffer.tell() > STITCH_MAX_BYTES:
            buffer = BytesIO()
            canvas.save(buffer, "JPEG", quality=88)
        out.append(buffer.getvalue())
    return out

_URL = re.compile(r"https?://[^\s]+|(?<![\w@])(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s]*)?", re.I)

# 群里真正的 @（能点、有提醒）只在 markdown 消息里有效；文字消息和图片消息里平台都原样显示标记。
MENTION_WAIT = 4.0  # 秒：图片里 @ 不了人，等这么久看后面有没有按钮消息可以带上这个 @，没有就单独发一条（带一句说明）
MENTION_HOLD_MAX = 120.0  # 秒：这个群还有消息在发（多页图片的下一张还在上传）时，补发的 @ 最多再等这么久（到上限后再留一次 MENTION_WAIT）
GROUP_GONE = 11255  # 平台：请求的资源不存在（用户/群已注销）
GROUP_INFO_PACE = 2.2  # 秒：群信息接口每分钟最多 30 次


def mention_tag(openid: str) -> str:
    return f'<qqbot-at-user id="{openid}" />'


def markdown_escaped(text: str) -> str:
    """普通文字放进 markdown 后照原样显示（在群里实测过）：网址不动（QQ 不处理网址里的转义）；
    [ 用实体（反斜杠加 [ 或 ( 会被当成公式）；] ( ) 不动；其余格式符号加反斜杠，$ 也是（成对时是公式）。"""
    out, last = [], 0
    for m in re.finditer(r"https?://\S+", text):
        out += [_md_plain(text[last : m.start()]), m.group(0)]
        last = m.end()
    return "".join(out + [_md_plain(text[last:])])


def _md_plain(text: str) -> str:
    return "".join("&#91;" if c == "[" else "\\" + c if c in "\\`*_{}#+-.!>~|<$" else c for c in text)


def _without(text: str, mentions: dict[str, str]) -> str:
    for name in mentions:
        text = text.replace(name, "")
    return text


def _names_asker() -> bool:
    """插件可以在元数据里写 extra={"mention_asker": False}：它回答指令时不点名提问的成员。"""
    try:
        plugin = current_matcher.get().plugin
    except LookupError:
        return True
    meta = plugin.metadata if plugin is not None else None
    return bool((meta.extra if meta is not None else {}).get("mention_asker", True))


def with_mentions(text: str, mentions: dict[str, str]) -> str:
    """把文字里的“@名字”换成真正的 @，其余转义。"""
    pattern = "|".join(re.escape(name) for name in sorted(mentions, key=len, reverse=True))
    out, last = [], 0
    for m in re.finditer(pattern, text):
        out += [markdown_escaped(text[last : m.start()]), mention_tag(mentions[m.group(0)])]
        last = m.end()
    return "".join(out + [markdown_escaped(text[last:])])



def as_message(value: Any) -> Message:
    """调用方传进来的各种形式的 OneBot 消息：文字、消息段，或它们的 dict 形式。"""
    if isinstance(value, Message):
        return value
    if isinstance(value, MessageSegment):
        return Message(value)
    if isinstance(value, str):
        return Message(value)
    if isinstance(value, Mapping):
        return Message(MessageSegment(str(value.get("type")), dict(value.get("data") or {})))
    out = Message()
    for item in value or []:
        out += as_message(item)
    return out


_current: contextvars.ContextVar["Inbound | None"] = contextvars.ContextVar("official_bridge_inbound", default=None)


# 适配器的模型会丢掉的发送者字段（成员的群名片、头像地址），在原始数据和转换结果之间
# 按消息 id 暂存。
AUTHOR_EXTRAS: OrderedDict[str, dict[str, Any]] = OrderedDict()


def remember_author(message_id: str, data: Mapping[str, Any]) -> None:
    author = data.get("author") or {}
    AUTHOR_EXTRAS[message_id] = {
        "member_nickname": author.get("member_nickname"),
        "mentions": [m for m in data.get("mentions") or [] if isinstance(m, Mapping)],
    }
    while len(AUTHOR_EXTRAS) > 2000:
        AUTHOR_EXTRAS.popitem(last=False)


class _Muted(Exception):
    """已知该群拒收主动消息，而且没有可以回复的消息。"""


def without_trailing_space(message: Message) -> Message:
    """去掉最后一个词后面空白的消息。

    按钮发出的指令后面带一个空格，而按整句匹配的指令（"/zmd抽卡记录 限定池"）带了空格就匹配不上。
    插件要读的东西都不在这个空格里。
    """
    if message and message[-1].type == "text":
        text = str(message[-1].data.get("text", "")).rstrip()
        if text or len(message) == 1:
            message[-1] = MessageSegment.text(text)
        else:
            message.pop()
    return message


class BridgeBot(OneBot):
    """另一端是官方 API 的 OneBot v11 机器人。"""


@dataclass
class Inbound:
    kind: str
    openid: str
    msg_id: str
    at: float
    used: int = 0
    author: int = 0  # 发送者
    text: str = ""  # 去掉空白后的文字
    at_me: bool = False  # 它 @ 了机器人（这条消息是不是对机器人说的，靠它判断）
    ref_idx: str | None = None  # 引用它时所指向的引用标识
    taken_for: int | None = None  # 另一个机器人听到的那条消息，这条已被判定为它在平台上的副本
    event: bool = False  # 不是消息，而是一个可以回复的事件（成员添加了机器人）；这时 msg_id 是事件的 id
    private_refused: bool = False  # 用私聊回复这条群消息被平台拒过（多半对方不是机器人好友）：不再试

    def usable(self) -> bool:
        ttl, limit = PASSIVE[self.kind]
        return self.used < limit and time.time() - self.at < ttl

    def attachment(self) -> dict[str, Any]:
        """让一条发出的消息成为对这条消息的回复所需的参数（先把 `used` 加一）。"""
        return {"event_id": self.msg_id} if self.event else {"msg_id": self.msg_id, "msg_seq": self.used}


@dataclass
class Record:
    kind: str
    openid: str
    official_ids: list[str]
    ref_idx: str | None = None
    event: dict[str, Any] | None = None


@dataclass
class Part:
    text: str = ""
    file_type: int = 0  # 1 图片，2 视频，3 语音，4 文件
    data: bytes | None = None
    url: str | None = None
    name: str = ""
    reply_to: int | None = None
    lead: tuple[int, int] | None = None  # 最先被 @ 的成员，以及这个 @ 在文字中结束的位置
    quote_ref: str | None = None  # 要引用的消息在平台上的引用标识（消息记录里没有它时使用）
    mentions: dict[str, str] = field(default_factory=dict)  # 文字里的“@名字” -> 被 @ 成员的 openid（发出时换成真正的 @）


def _inline(segment: MessageSegment) -> bool:
    """消息段里直接带着文件内容（base64:// 或字节），而不是一个网址或路径。"""
    file = segment.data.get("file")
    return segment.type in ("image", "record", "video", "file") and (isinstance(file, bytes) or str(file or "").startswith("base64://"))


def _carries_data(summary: dict[str, Any] | None) -> bool:
    message = (summary or {}).get("message")
    return isinstance(message, Message) and any(_inline(seg) for seg in message)


def _lighten(record: "Record | None") -> None:
    """只留文字：内嵌的文件换成“[图片]”之类的说明（被引用时仍看得出那里原来有什么）。"""
    if record is None or not _carries_data(record.event):
        return
    words = {"image": "[图片]", "record": "[语音]", "video": "[视频]", "file": "[文件]"}
    light = Message(MessageSegment.text(words[seg.type]) if _inline(seg) else seg for seg in record.event["message"])
    record.event["message"], record.event["raw_message"] = light, str(light)


# 刚才那次发送是不是挂在成员消息上的回复（而不是主动消息）。每次发送各自一份：几条回复同时发送时互不覆盖，
# 不会因为别人的回复，把一个不收主动消息的群误判成又收了。
_was_reply: contextvars.ContextVar[bool] = contextvars.ContextVar("was_reply", default=False)


@dataclass
class Ledger:
    """OneBot 的消息 id 是整数，官方的是长字符串：两个方向都记下来。"""

    size: int = 20000
    heavy: int = 50  # 带着图片原始数据的记录最多留这么多条（一张图几 MB）；更早的只留文字
    _next: int = field(default_factory=lambda: int(time.time() * 10) % 10**9)
    _by_id: OrderedDict[int, Record] = field(default_factory=OrderedDict)
    _by_ref: dict[str, int] = field(default_factory=dict)
    _heavy: deque[int] = field(default_factory=deque)

    def add(self, record: Record) -> int:
        self._next = self._next % (2**31 - 2) + 1
        mid = self._next
        self._by_id[mid] = record
        if record.ref_idx:
            self._by_ref[record.ref_idx] = mid
        while len(self._by_id) > self.size:
            _, old = self._by_id.popitem(last=False)
            if old.ref_idx:
                self._by_ref.pop(old.ref_idx, None)
        if _carries_data(record.event):
            self._heavy.append(mid)
            while len(self._heavy) > self.heavy:
                _lighten(self._by_id.get(self._heavy.popleft()))
        return mid

    def by_ref(self, ref_idx: str | None) -> int | None:
        return self._by_ref.get(ref_idx) if ref_idx else None

    def get(self, mid: int) -> Record | None:
        return self._by_id.get(int(mid))



class BridgeAdapter(BaseAdapter):
    """顶替 OneBot 适配器的位置：名字相同，插件据此走 OneBot 的代码路径。"""

    def __init__(self, driver: Driver, bridge: "Bridge") -> None:
        super().__init__(driver)
        self.bridge = bridge

    @classmethod
    def get_name(cls) -> str:
        return "OneBot V11"

    async def _call_api(self, bot: OneBot, api: str, **data: Any) -> Any:
        if self.bridge.route is not None:  # 原机器人还在时，只有它能做的事交给它
            other = await self.bridge.route(api, data)
            if other is not None:
                return await other.call_api(api, **data)
        handler = getattr(self.bridge, f"api_{api}", None)
        if handler is None:
            raise ActionFailed(status="failed", retcode=1404, msg=f"{api} is not available on the official bot", wording="官方机器人不支持该操作")
        try:
            return await handler(**data)
        except ActionFailed:
            raise
        except Exception as e:  # 官方 API 出错或这里有 bug：插件期待的是 OneBot 的异常
            code = getattr(e, "code", None)
            text = getattr(e, "message", None) or f"{type(e).__name__}: {e}"
            logger.opt(exception=code is None).warning(f"official bridge: {api} failed ({code}): {text}")
            raise ActionFailed(status="failed", retcode=code or 1, msg=str(text), wording=str(text)) from e


class Bridge:
    copy_wait = 3.0  # 回复等待它所回复消息的平台副本到达的秒数（见 _await_copy）
    media_needs_text = False  # API 拒收过不带任何文字的图片后置为 True

    def __init__(self, driver: Driver, idmap: IdMap, qq_bot: QQBot, self_id: str, *, mention_markup: bool = False, members_authoritative: bool = False) -> None:
        self.idmap = idmap
        self.qq = qq_bot
        self.adapter = BridgeAdapter(driver, self)
        self.bot = BridgeBot(self.adapter, self_id)
        self.self_id = int(self_id)
        self.mention_markup = mention_markup
        self._waiting_mentions: dict[tuple[str, str], tuple[str, asyncio.Task]] = {}  # (群, 成员) -> 图片里没 @ 上的他，等着交给他的下一条按钮消息
        self._sending: dict[str, int] = {}  # 群 -> 正在发往这个群的消息（含合并转发的整组）有几处：补发的 @ 等它们发完
        self._addressed: deque[int] = deque(maxlen=500)  # 已经点过名的指令消息：同一次回复只 @ 一次
        self.members_authoritative = members_authoritative
        self.ledger = Ledger()
        self.recent: dict[tuple[str, str], deque[Inbound]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._members_denied_until = 0.0
        self._members_read_at: dict[str, float] = {}  # 群 OpenID -> 上次排上读它的成员名单的时间
        self._members_lock = asyncio.Lock()  # 一次只读一个群的成员名单
        self._members_next_at = 0.0  # time.monotonic() 到这个时刻之前不发成员名单相关的请求（全机器人共用）
        self._members_backoff = 0.0  # 上一次 429 之后的暂停秒数；成功一次就归零
        self._member_tasks: dict[str, asyncio.Task] = {}  # 群 OpenID -> 正在排队或在读的那一次（每群最多一个）
        self._info_next_at = 0.0  # time.monotonic() 到这个时刻之前不发群信息请求（查群和读名单时的核对共用）
        # 正在读成员名单的群 -> 每次读一组（读期间发言、进群的人，读期间发言、进群、退群的人，读期间进群、退群的人）
        self._reading: dict[str, list[tuple[set[str], set[str], set[str]]]] = {}
        self.on_sent: Any = None  # 每向群里发出一段文字就调用一次，参数为（群 OpenID，文字）
        self.route: Any = None  # async (api, params) -> 应该接手该调用的另一个 OneBot 机器人，或 None
        self.other_groups: Any = None  # async () -> 那个机器人的群列表
        self.stitch_height = 0  # 合并转发卡片的各页拼成不超过这个高度的图（0：每页单独发）
        self.census_groups: Any = None  # async () -> 那个机器人所在的全部群（尽可能确定）；无法确定时抛异常
        self.old_bot_gone_at: Any = None  # () -> 那个机器人被确认已不在任何群的时间；还在某个群、或没确认过时为 None
        self.other_role: Any = None  # async (群号) -> 那个机器人在该群的身份，或 None
        self.speak_for: Any = None  # async (群号, 消息) -> 由那个机器人发出，失败时抛异常
        self.names: OrderedDict[int, str] = OrderedDict()  # 只知道编号的成员的名字（原机器人听到的）
        self._unprompted: dict[tuple[str, str], deque[float]] = {}
        self._copy_awaited: OrderedDict[int, None] = OrderedDict()  # 已经等过一次平台副本的消息

    # ------------------------------------------------------------------ 接收

    def first_sight(self, official_id: str) -> bool:
        """重复投递的事件返回 False（一条 @ 消息可能同时作为两种群事件到达）。"""
        if official_id in self._seen:
            return False
        self._seen[official_id] = None
        while len(self._seen) > 5000:
            self._seen.popitem(last=False)
        return True

    def _convert_message(self, event: qe.QQMessageEvent, at_me: bool) -> Message:
        own = {m.id for m in (event.mentions or []) if getattr(m, "is_you", False) and getattr(m, "id", None)}
        own.add(self.qq.self_id)
        out = Message()
        if at_me:
            out.append(MessageSegment.at(self.self_id))
        for seg in event.get_message():
            kind = seg.type
            if kind == "text":
                out.append(MessageSegment.text(seg.data.get("text", "")))
            elif kind == "mention_user":
                target = str(seg.data.get("user_id", ""))
                if target and target not in own:
                    out.append(MessageSegment.at(self.idmap.user_id(target)))
            elif kind == "mention_everyone":
                out.append(MessageSegment.at("all"))
            elif kind == "emoji":
                out.append(MessageSegment.face(int(seg.data.get("id", 0) or 0)))
        out.extend(self._attachments(event.attachments))
        return without_trailing_space(out)

    @staticmethod
    def _attachments(attachments: Any) -> list[MessageSegment]:
        """图片、语音、视频和群文件，带上适配器自己的消息会丢掉的文件名和大小。"""
        out: list[MessageSegment] = []
        for item in attachments or []:
            url = getattr(item, "url", None)
            if not url:
                continue
            kind = str(getattr(item, "content_type", "") or "").split("/", 1)[0]
            name, size = str(getattr(item, "filename", "") or ""), str(getattr(item, "size", "") or "")
            if kind == "image":
                out.append(MessageSegment("image", {"file": url, "url": url, "file_size": size}))
            elif kind in ("voice", "audio"):
                out.append(MessageSegment("record", {"file": url, "url": url}))
            elif kind == "video":
                out.append(MessageSegment("video", {"file": url, "url": url}))
            else:  # 群文件；大图片也会以群文件的形式到达，靠文件名区分
                out.append(MessageSegment("file", {"file": name or url, "name": name, "file_id": url, "file_size": size, "url": url}))
        return out

    def _quoted(self, event: qe.QQMessageEvent, kind: str, openid: str) -> MessageSegment | None:
        """引用另一条消息的消息所对应的回复消息段。

        在见过的消息里查找被引用的消息；查不到的（比本次运行更早，或索引方式不同）就用事件里带的
        副本重建，这样无论哪种情况 `event.reply` 都有文字和图片。
        """
        scene = getattr(event, "message_scene", None)
        ref = next((item[len("ref_msg_idx=") :] for item in getattr(scene, "ext", None) or [] if item.startswith("ref_msg_idx=")), None)
        elements = getattr(event, "msg_elements", None) or []
        if not ref and not elements:
            return None
        mid = self.ledger.by_ref(ref)
        kinds = [str(getattr(a, "content_type", "")) for e in elements for a in (getattr(e, "attachments", None) or [])]
        logger.debug(f"official bridge: a message quotes another ({'seen before' if mid is not None else 'not seen before'}; {len(elements)} quoted part(s), attachments {kinds})")
        if mid is None and elements:
            element = elements[0]
            mid = self.ledger.by_ref(getattr(element, "msg_idx", None))
            if mid is None:
                author = getattr(element, "author", None)
                author_openid = getattr(author, "member_openid", None) or getattr(author, "user_openid", None) or getattr(author, "id", None)
                if getattr(author, "bot", False) or not author_openid:
                    uid = self.self_id if getattr(author, "bot", False) else 0
                else:
                    uid = self.idmap.user_id(author_openid, getattr(author, "username", None))
                message = Message()
                if getattr(element, "content", ""):
                    message.append(MessageSegment.text(element.content))
                message.extend(self._attachments(getattr(element, "attachments", None)))
                summary: dict[str, Any] = dict(
                    time=int(time.time()), message_type="group" if kind == GROUP else "private",
                    sender=dict(user_id=uid, nickname=getattr(author, "username", None) or str(uid)), message=message, raw_message=str(message),
                )
                mid = self.ledger.add(Record(kind, openid, [], getattr(element, "msg_idx", None) or ref, summary))
                summary.update(message_id=mid, real_id=mid)
        return MessageSegment.reply(mid) if mid is not None else None

    @staticmethod
    def _timestamp(value: Any) -> int:
        try:
            if isinstance(value, datetime):
                return int(value.timestamp())
            return int(datetime.fromisoformat(str(value)).timestamp())
        except Exception:
            return int(time.time())

    @staticmethod
    def _ref_idx(event: qe.QQMessageEvent) -> str | None:
        scene = getattr(event, "message_scene", None)
        for item in getattr(scene, "ext", None) or []:
            if item.startswith("msg_idx="):
                return item[len("msg_idx=") :]
        return None

    def convert(self, event: qe.Event) -> tuple[Any, Inbound | None] | None:
        """官方事件对应的 OneBot 事件（以及回复它时可以引用的内容），或 None。"""
        if isinstance(event, qe.GroupMessageCreateEvent):
            author = event.author
            group_openid = event.group_openid
            self._present(group_openid, author.member_openid)  # 先于任何写库：正在读这个群的名单时，他算在名单上
            gid = self.idmap.group_id(group_openid)
            extras = AUTHOR_EXTRAS.pop(event.id, {})
            card = str(extras.get("member_nickname") or "")
            failed = [False]  # 这条消息里写库失败过一次，就不再逐个去写（每次都可能要等锁）
            uid = self.idmap.user_id(author.member_openid, author.username)
            # 先记发言人：他刚说了话，不能因为记被 @ 的人失败而留着退群标记
            if self._note_member(gid, group_openid, author.member_openid, author.member_role, card or author.username, seen=True, at=self._timestamp(event.timestamp), failed=failed):
                logger.info(f"official bridge: a member on record as gone from group {gid} spoke there: counted as a member again")
            for mention in extras.get("mentions") or []:  # 成员身份和名字唯一会出现的另一个地方
                mentioned = mention.get("member_openid")
                if mention.get("is_you"):
                    self._note_member(gid, group_openid, SELF, mention.get("member_role"), failed=failed)
                elif mentioned and not mention.get("bot"):
                    self.idmap.user_id(mentioned, mention.get("username"))
                    self._note_member(gid, group_openid, mentioned, mention.get("member_role"), mention.get("member_nickname") or mention.get("username"), failed=failed)
            at_me = isinstance(event, qe.GroupAtMessageCreateEvent) or any(getattr(m, "is_you", False) and getattr(m, "scope", "") == "single" for m in event.mentions or [])
            message = self._convert_message(event, at_me)
            if (quoted := self._quoted(event, GROUP, group_openid)) is not None:
                message.insert(0, quoted)
            inbound = Inbound(GROUP, group_openid, event.id, time.time(), author=uid, text="".join(message.extract_plain_text().split()), at_me=at_me, ref_idx=self._ref_idx(event))
            fields = dict(
                time=self._timestamp(event.timestamp), self_id=self.self_id, post_type="message", sub_type="normal",
                user_id=uid, message_type="group", group_id=gid, font=0, to_me=False,
            )
            sender = dict(user_id=uid, nickname=author.username or card or str(uid), card=card, role=author.member_role)
            summary = {**fields, "sender": sender}
            fields["sender"] = Sender(**sender)
            mid = self.ledger.add(Record(GROUP, group_openid, [event.id], self._ref_idx(event), summary))
            summary.update(message_id=mid, real_id=mid, message=deepcopy(message), raw_message=str(message))
            self.recent.setdefault((GROUP, group_openid), deque(maxlen=20)).append(inbound)
            return GroupMessageEvent(**fields, message_id=mid, message=message, original_message=deepcopy(message), raw_message=str(message)), inbound

        if isinstance(event, qe.C2CMessageCreateEvent):
            openid = event.author.user_openid
            uid = self.idmap.user_id(openid, event.author.username, friend=True)
            message = self._convert_message(event, False)
            if (quoted := self._quoted(event, C2C, openid)) is not None:
                message.insert(0, quoted)
            inbound = Inbound(C2C, openid, event.id, time.time())
            name = event.author.username or (self.idmap.user(uid) or {"nickname": ""})["nickname"] or str(uid)
            fields = dict(
                time=self._timestamp(event.timestamp), self_id=self.self_id, post_type="message", sub_type="friend",
                user_id=uid, message_type="private", font=0, to_me=True,
            )
            summary = {**fields, "sender": dict(user_id=uid, nickname=name)}
            fields["sender"] = Sender(user_id=uid, nickname=name)
            mid = self.ledger.add(Record(C2C, openid, [event.id], self._ref_idx(event), summary))
            summary.update(message_id=mid, real_id=mid, message=deepcopy(message), raw_message=str(message))
            self.recent.setdefault((C2C, openid), deque(maxlen=20)).append(inbound)
            return PrivateMessageEvent(**fields, message_id=mid, message=message, original_message=deepcopy(message), raw_message=str(message)), inbound

        if isinstance(event, (qe.GroupAddRobotEvent, qe.GroupDelRobotEvent)):
            gid = self.idmap.group_id(event.group_openid)
            operator = self.idmap.user_id(event.op_member_openid)
            common = dict(time=self._timestamp(event.timestamp), self_id=self.self_id, post_type="notice", user_id=self.self_id, group_id=gid, operator_id=operator)
            if isinstance(event, qe.GroupAddRobotEvent):
                self.idmap.set_group_active(event.group_openid, True)
                self.idmap.note_group(event.group_openid, added_by=event.op_member_openid)
                if event.op_member_openid:  # 拉机器人进群的人此刻一定在群里：记成成员，去掉更早的退群标记；身份不知道，不覆盖已有的
                    self._present(event.group_openid, event.op_member_openid)
                    self._note_member(gid, event.group_openid, event.op_member_openid, None, None, listed=True, at=self._timestamp(event.timestamp))
                # 和成员添加机器人一样，这个事件也可以回复：进群时说的话因此不算主动消息，
                # 而群主允许之前，群里是不许主动消息的。
                joined = Inbound(GROUP, event.group_openid, event.event_id, time.time(), event=True) if getattr(event, "event_id", None) else None
                return GroupIncreaseNoticeEvent(**common, notice_type="group_increase", sub_type="invite"), joined
            self._forget_group(event.group_openid)  # 不在的群一律清掉，不留记录
            return GroupDecreaseNoticeEvent(**common, notice_type="group_decrease", sub_type="kick_me"), None

        if isinstance(event, (qe.GroupMemberAddEvent, qe.GroupMemberRemoveEvent)):
            # 群成员进群、退群（intent GROUP_MEMBER_EVENT）：只维护编号映射表里的成员和退群记录，不生成 OneBot 通知，
            # 也不转给中继。日志只写群号。
            removed = isinstance(event, qe.GroupMemberRemoveEvent)
            # 先于任何写库。没写是谁的也记一笔：正在读的名单变了
            self._present(event.group_openid, event.member_openid, left=removed, moved=True)
            row = self.idmap.group_by_openid(event.group_openid)
            if row is None:  # 不在记录里的群（比如机器人刚被移出、记录已清掉）：不为它新建记录
                logger.info(f"official bridge: a member {'left' if removed else 'joined'} a group not on record")
                return None
            gid = row["gid"]
            if not event.member_openid:
                logger.info(f"official bridge: a member {'left' if removed else 'joined'} group {gid}, but the event names nobody")
                return None
            if removed:
                left_at = self._timestamp(event.timestamp)
                row = self.idmap.member(event.group_openid, event.member_openid)
                if row is not None and left_at < (row["joined_at"] or 0) <= time.time():  # 迟到的退群：他在那之后又进过群
                    # 读名单期间来的话，名单上有他也没写他（以事件为准），所以在这里撤掉早先那一笔
                    self.idmap.clear_departed(event.group_openid, event.member_openid, left_at)
                    logger.info(f"official bridge: a departure from group {gid} older than that member's joining: not noted")
                    return None
                user = self.idmap.user_by_openid(event.member_openid)
                listed = row is not None or bool(
                    user and user["linked"] and self.idmap.roster_member(gid, user["uid"]))  # 在不在该群的成员名单上（见过的，或名单表里认出的）
                self.idmap.mark_departed(event.group_openid, event.member_openid, left_at)
                logger.info(f"official bridge: a member left group {gid}" + ("" if listed else " (matched no one on its member list)"))
            else:
                joined_at = self._timestamp(event.timestamp)
                back = self.idmap.clear_departed(event.group_openid, event.member_openid, after=joined_at, ties=True)
                row = self.idmap.member(event.group_openid, event.member_openid)
                # 进群早于记下的退群：什么都不写。否则记成成员（名字还不知道）；重新进群的只能是普通成员，原来记着的
                # 管理员身份作废，但只在这次确实去掉了退群标记、或进群晚于他最后一次发言时（晚到的旧进群不改身份）
                if not self.idmap.has_departed(event.group_openid, event.member_openid) and (row is None or back or joined_at > (row["last_seen"] or 0)):
                    self.idmap.user_id(event.member_openid)
                    self.idmap.touch_member(event.group_openid, event.member_openid, "member",
                                            joined_at=max(joined_at, row["joined_at"] if row else 0))
                elif row is not None and joined_at > (row["joined_at"] or 0) and not self.idmap.has_departed(event.group_openid, event.member_openid):
                    # 他在那之后已经发过言：身份不动，进群时间照记，迟到的退群要和它比
                    self.idmap.touch_member(event.group_openid, event.member_openid, None, joined_at=joined_at)
                logger.info(f"official bridge: a member joined group {gid}" + (" (was on record as gone)" if back else ""))
            return None

        if isinstance(event, qe.FriendAddEvent):
            uid = self.idmap.user_id(event.openid, friend=True)
            # 平台允许像回复消息一样回复这个事件：欢迎新成员的话因此不算主动消息。
            greeted = Inbound(C2C, event.openid, event.event_id, time.time(), event=True) if event.event_id else None
            return FriendAddNoticeEvent(time=self._timestamp(event.timestamp), self_id=self.self_id, post_type="notice", notice_type="friend_add", user_id=uid), greeted

        if isinstance(event, qe.FriendDelEvent):
            # OneBot 没有对应的事件；只记下来，好友列表（get_friend_list）从此不再有这个人。
            self.idmap.unfriend(event.openid)
            return None
        return None

    def _note_member(self, gid: int, *args: Any, failed: list[bool] | None = None, **kwargs: Any) -> bool:
        """记下群里的一个成员（idmap.touch_member）。这只是记录：写库失败只记一行，这条消息照常处理。
        `failed` 在一条消息里共用：失败过一次，后面的就不写了。"""
        if failed is not None and failed[0]:
            return False
        try:
            return self.idmap.touch_member(*args, **kwargs)
        except sqlite3.Error as e:
            if failed is not None:
                failed[0] = True
            logger.warning(f"official bridge: a member of group {gid} not written to the id map ({type(e).__name__}: {e})")
            return False

    def _present(self, group_openid: str, openid: str, left: bool = False, moved: bool = False) -> None:
        """正在读这个群的平台成员名单时，此人发了言、进了群（读完时算他在名单上）或退了群。
        这些人的身份、昵称和退群标记以事件为准：旧的一页里再出现，只算"在名单上"，不写他。
        `moved`：是进群或退群（没写是谁时是空串），这一次读取因此不确认任何人退群。"""
        for present, changed, moves in self._reading.get(group_openid, ()):
            changed.add(openid)
            if not left:
                present.add(openid)
            if moved:
                moves.add(openid)

    def dispatch(self, ob_event: Any, inbound: Inbound | None) -> asyncio.Task:
        """为转换后的事件运行 OneBot 处理函数，并记下它的回复可以引用的内容。"""
        token = _current.set(inbound)
        try:
            return asyncio.create_task(self.bot.handle_event(ob_event))
        finally:
            _current.reset(token)

    # ------------------------------------------------------------------ 发送

    def _name_of(self, uid: int, group_openid: str | None) -> str:
        openid = (self.idmap.user_openid(uid) or self.idmap.former_openid(uid)) if group_openid else None  # 刚被认领的成员：事件里还是旧编号
        if openid and group_openid:
            row = self.idmap.member(group_openid, openid)
            if row and row["nickname"]:
                return row["nickname"]
        row = self.idmap.user(uid)
        return (row["nickname"] if row and row["nickname"] else "") or self.names.get(uid) or str(uid)

    def remember_name(self, uid: int, name: str) -> None:
        if name:
            self.names[uid] = name
            self.names.move_to_end(uid)
            while len(self.names) > 5000:
                self.names.popitem(last=False)

    async def _pace(self, kind: str, openid: str) -> None:
        """对同一个聊天的主动消息每分钟限 20 条：宁可等一等，也不要被拒。"""
        sent = self._unprompted.setdefault((kind, openid), deque())
        while True:
            now = time.monotonic()
            while sent and now - sent[0] > 60:
                sent.popleft()
            if len(sent) < 18:
                break
            await asyncio.sleep(min(60 - (now - sent[0]) + 0.2, 30))
        sent.append(time.monotonic())

    @staticmethod
    def _read_file(value: Any) -> tuple[bytes | None, str | None]:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value), None
        text = str(value or "")
        if text.startswith("base64://"):
            return base64.b64decode(text[len("base64://") :]), None
        if text.startswith(("http://", "https://")):
            return None, text
        path = unquote(urlparse(text).path) if text.startswith("file://") else text
        if path and os.path.isfile(path):
            with open(path, "rb") as f:
                return f.read(), None
        raise ActionFailed(status="failed", retcode=1200, msg=f"file not readable: {text[:80]}", wording="文件读取失败")

    def _split(self, message: Message, group_openid: str | None) -> list[Part]:
        """一条官方消息只能带文字和最多一个媒体：把 OneBot 消息切开。"""
        parts: list[Part] = [Part()]
        mentioned: tuple[str, int, int] | None = None  # 最后一个 @：@ 了谁、在哪一段、它的文字在哪里结束
        for seg in message:
            kind, data = seg.type, seg.data
            if kind == "text":
                parts[-1].text += str(data.get("text", ""))
            elif kind == "at":
                qq = str(data.get("qq", ""))
                if mentioned is not None and mentioned[:2] == (qq, len(parts) - 1) and not parts[-1].text[mentioned[2]:].strip():
                    continue  # 同一个成员被连续 @ 两次（at_sender 的 @ 后面紧跟插件自己的 @）
                if qq == "all":
                    parts[-1].text += f"{EVERYONE} " if group_openid else "@全体成员 "
                    continue
                leading = len(parts) == 1 and parts[0].lead is None and not parts[0].text.strip()
                openid = self.idmap.user_openid(int(qq)) if qq.isdigit() else None
                name = f"@{self._name_of(int(qq), group_openid) if qq.isdigit() else qq}"
                parts[-1].text += f"{name} "
                if self.mention_markup and openid and group_openid:
                    parts[-1].mentions[name] = openid
                mentioned = (qq, len(parts) - 1, len(parts[-1].text))
                if leading and qq.isdigit():
                    parts[0].lead = (int(qq), len(parts[0].text))
            elif kind == "reply":
                try:
                    parts[0].reply_to = int(data.get("id"))
                except (TypeError, ValueError):
                    pass
            elif kind in ("image", "record", "video", "file"):
                blob, url = self._read_file(data.get("file") or data.get("url"))
                if parts[-1].file_type:
                    parts.append(Part())
                part = parts[-1]
                part.file_type = {"image": 1, "video": 2, "record": 3, "file": 4}[kind]
                part.data, part.url, part.name = blob, url, str(data.get("name") or "")
            elif kind == "share":  # 分享卡片官方没有：写成标题和网址
                words = [str(data.get(k) or "").strip() for k in ("title", "url")]
                parts[-1].text += "\n".join(w for w in words if w)
            elif kind not in ("face",):
                logger.debug(f"official bridge: segment {kind} has no official counterpart, dropped")
        out: list[Part] = []
        for part in parts:
            out.append(part)
            if part.file_type and EVERYONE in part.text:  # 这个标记只在文字消息里有效：文字先发，图片后发
                out.append(Part(file_type=part.file_type, data=part.data, url=part.url, name=part.name))
                part.file_type, part.data, part.url, part.name = 0, None, None, ""
        return [p for p in out if p.file_type or p.text.strip()]

    @staticmethod
    def _answering() -> tuple[int | None, str]:
        """当前正在处理的消息的发送者，以及那条消息去掉空白后的文字。"""
        try:
            event = current_event.get()
        except LookupError:
            return None, ""
        uid = getattr(event, "user_id", None)
        try:
            text = "".join(event.get_message().extract_plain_text().split())
        except Exception:
            text = ""
        return (int(uid) if uid is not None else None), text

    def _address(self, parts: list[Part], group_openid: str) -> None:
        """用这个平台能做到的方式，表明一条群回复是给谁的。

        插件标明回复对象的方式是引用它回复的消息（reply_to），或先 @ 该成员（at_sender）。在 QQ 客户端上
        两者都表示“这是给你的”。在这里，引用需要平台自己的那份消息副本，平台也不会替插件
        @ 人。所以：引用当前处理消息的回复，改为点出发送者的名字；
        只有那条消息指向另一个成员（有人问起别人）时才引用它，让回复看得出是谁问了谁。
        """
        try:
            event = current_event.get()
        except LookupError:
            return
        uid, mid = getattr(event, "user_id", None), getattr(event, "message_id", None)
        first = parts[0]
        if uid is None or mid is None:
            return
        if first.lead is not None and first.lead[0] == int(uid):
            self._addressed.append(mid)  # 插件自己 @ 了他
        if first.reply_to != mid:
            if first.reply_to is None and first.lead is None and self._first_answer(group_openid) is not None:
                self._name(first, int(uid), group_openid)  # 插件没说回复谁：指令的第一条回复点出提问的成员
            return
        if self._about_others(event):
            copy = _current.get()
            if copy is None:
                # 由另一个机器人听到：平台上的副本是该成员发出指令之后的那条消息（有多条时取文字相同的），
                # 未被认出的成员则取文字相同的最新一条。
                text, sent = self._answering()[1], float(getattr(event, "time", 0) or 0)
                recent = [i for i in self.recent.get((GROUP, group_openid), ()) if time.time() - i.at < PASSIVE[GROUP][0] and i.taken_for in (None, mid)]
                own = [i for i in recent if i.author == int(uid) and i.at >= sent - 5]
                copy = next((i for i in own if text and i.text == text), None) or next(iter(own), None) or next((i for i in reversed(recent) if text and i.text == text), None)
                if copy is not None:
                    copy.taken_for = mid  # 一个副本只代表一条消息：该成员的下一条指令是另一条
            if self.ledger.get(mid) is not None or (copy is not None and copy.ref_idx):
                first.quote_ref = copy.ref_idx if copy is not None else None
                return
        first.reply_to = None  # 没有可引用的（或不涉及别人）：直接点出这位成员
        if first.lead is None:
            self._name(first, int(uid), group_openid)
        self._addressed.append(mid)

    @staticmethod
    def _words_apart(parts: list[Part]) -> list[Part]:
        """图片里 @ 不了人：图片带着 @ 和别的文字（如绑定二维码的说明）时，文字连同 @ 挪到图片下面单独一条，
        那条是真正的 @。只有 @ 没有别的文字的图片照旧（@ 交给后面的按钮消息，或单独补一句）。"""
        out: list[Part] = []
        for part in parts:
            if not (part.file_type and part.mentions and _without(part.text, part.mentions).strip()):
                out.append(part)
                continue
            out.append(Part(file_type=part.file_type, data=part.data, url=part.url, name=part.name, reply_to=part.reply_to, quote_ref=part.quote_ref))
            out.append(Part(text=part.text, lead=part.lead, mentions=part.mentions))
        return out

    def _name(self, first: Part, uid: int, group_openid: str) -> None:
        name = f"@{self._name_of(uid, group_openid)} "
        first.text = name + first.text
        first.lead = (uid, len(name))
        member = self.idmap.user_openid(uid)
        if self.mention_markup and member is not None:
            first.mentions[name.strip()] = member

    def _about_others(self, event: Any) -> bool:
        """这条指令 @ 了别的成员（有人问起别人）。"""
        uid = str(getattr(event, "user_id", ""))
        return any(
            seg.type == "at" and str(seg.data.get("qq")) not in ("all", str(self.self_id), uid)
            for seg in getattr(event, "original_message", None) or ()
        )

    def _in_chat(self, group_openid: str) -> bool:
        """正在回答的消息就是在这个群里发的（而不是在别处让机器人往这个群发东西，例如私聊里的 /资料库更新）。"""
        try:
            event = current_event.get()
        except LookupError:
            return False
        gid = getattr(event, "group_id", None)
        try:
            return gid is not None and self.idmap.group_openid(int(gid)) == group_openid
        except (TypeError, ValueError):
            return False

    def _first_answer(self, group_openid: str | None = None) -> int | None:
        """正在回答成员发的 / 指令、还没点过名、也不是问起别人时，返回这位成员（并记为已点名）。
        聊天里的自动回复之类不是指令，不点名；发往另一个群的消息（group_openid 不是指令所在的群）也不点名。"""
        try:
            event = current_event.get()
        except LookupError:
            return None
        if group_openid is not None and not self._in_chat(group_openid):
            return None
        uid, mid = getattr(event, "user_id", None), getattr(event, "message_id", None)
        if uid is None or mid is None or mid in self._addressed or not self._answering()[1].startswith("/") or self._about_others(event):
            return None
        if not _names_asker():
            return None
        self._addressed.append(mid)
        return int(uid)

    def _pick_inbound(self, kind: str, openid: str) -> Inbound | None:
        """回复可以挂靠的那条收到的消息（回复不像主动消息那样有数量限制）。

        在群里只用被回复成员的消息，不借用别人的：他自己的，或原机器人
        听到的那条消息在平台上的副本。不针对任何人发的东西（推送）不挂靠任何消息。
        """
        current = _current.get()
        if current and current.kind == kind and current.openid == openid and current.usable():
            return current
        uid, text = self._answering() if kind == GROUP else (None, "")
        if kind == GROUP and uid is None:
            return None
        for item in reversed(self.recent.get((kind, openid), ())):
            if not item.usable():
                continue
            if kind != GROUP or item.author == uid or (text and item.text == text and time.time() - item.at < 30):
                return item
        if kind == C2C and current and current.kind == GROUP and not current.event and not current.private_refused and current.usable():
            if openid in (self.idmap.user_openid(current.author), self.idmap.former_openid(current.author)):
                # 正在处理这位成员的群消息：平台允许用私聊回复它（对方得是机器人好友），次数和群里的回复共用
                return current
        return None

    async def _await_copy(self, openid: str) -> None:
        """给被回复消息的平台副本一点时间到达。

        消息可能在平台把它投递到这里之前就到了插件那里：群里另一个机器人先听到了。这时立即给出的回复
        找不到可以挂靠的消息，只能作为主动消息发出；群不允许主动消息时就根本发不出去（成员的指令在
        同一秒内被回复）。每条消息只等一次，而且只在确实会有副本时才等：该群推送全部消息，
        或这条消息 @ 了机器人。
        """
        if self.copy_wait <= 0 or _current.get() is not None:
            return  # 平台自己投递的：处理的就是它的副本
        try:
            event = current_event.get()
        except LookupError:
            return  # 没有回复对象（推送）
        mid = getattr(event, "message_id", None)
        if mid is None or getattr(event, "user_id", None) is None or mid in self._copy_awaited:
            return
        self._copy_awaited[mid] = None
        while len(self._copy_awaited) > 500:
            self._copy_awaited.popitem(last=False)
        row = self.idmap.group_by_openid(openid)
        mentions_me = any(seg.type == "at" and str(seg.data.get("qq")) == str(self.self_id) for seg in getattr(event, "original_message", None) or ())
        if row is None or not (row["hears_all"] or mentions_me):
            return
        deadline = time.monotonic() + self.copy_wait
        while self._pick_inbound(GROUP, openid) is None and time.monotonic() < deadline:
            await asyncio.sleep(0.1)

    async def _download(self, url: str) -> bytes | None:
        try:
            response = await self.qq.adapter.request(Request("GET", url, timeout=60))
            if response.status_code == 200 and isinstance(response.content, bytes) and response.content:
                return response.content
        except Exception as e:
            logger.debug(f"official bridge: could not fetch media ({type(e).__name__}), passing the address on")
        return None

    async def _upload(self, kind: str, openid: str, part: Part) -> str:
        blob, url = part.data, part.url
        if blob is None and url:
            blob = await self._download(url)
        target = {"group_openid": openid} if kind == GROUP else {"openid": openid}
        if blob is not None and len(blob) > INLINE_UPLOAD_MAX:
            chunked = self.qq.post_group_upload if kind == GROUP else self.qq.post_c2c_upload
            name = part.name or {1: "image.png", 2: "video.mp4", 3: "voice.silk"}.get(part.file_type, "file.bin")
            result = await chunked(openid, part.file_type, name, blob, False)
        else:
            inline = self.qq.post_group_files if kind == GROUP else self.qq.post_c2c_files
            source = {"file_data": blob} if blob is not None else {"url": url}
            result = await inline(**target, file_type=part.file_type, srv_send_msg=False, **source)
        return result.file_info

    def _sending_more(self, kind: str, openid: str) -> None:
        if kind == GROUP:
            self._sending[openid] = self._sending.get(openid, 0) + 1

    def _sending_less(self, kind: str, openid: str) -> None:
        if kind == GROUP:
            left = self._sending.get(openid, 1) - 1
            if left > 0:
                self._sending[openid] = left
            else:
                self._sending.pop(openid, None)

    async def _post(self, kind: str, openid: str, part: Part) -> tuple[str, str | None]:
        """发一段；发往群的期间记在 _sending 里（上传图片可能要好几秒），补发的 @ 等它发完再发。"""
        self._sending_more(kind, openid)
        try:
            return await self._post_part(kind, openid, part)
        finally:
            self._sending_less(kind, openid)

    async def _post_part(self, kind: str, openid: str, part: Part) -> tuple[str, str | None]:
        post = self.qq.post_group_messages if kind == GROUP else self.qq.post_c2c_messages
        target = {"group_openid": openid} if kind == GROUP else {"openid": openid}
        text = part.text
        if part.file_type:
            for name in part.mentions:  # 图片里 @ 不了人：发完由 _mention_later 补上
                text = text.replace(name, "")
        body: dict[str, Any] = {"msg_type": 0, "content": text.strip() or None}
        if part.file_type:
            # 没有文字的图片：API 接受不带文字的图片（它自己的示例就是这样），只为填字段而留的空白
            # 会在图片下面显示成一个空行。
            body.update(msg_type=7, media=Media(file_info=await self._upload(kind, openid, part)), content=body["content"] or (" " if self.media_needs_text else None))
        quoted = self.ledger.get(part.reply_to) if part.reply_to else None
        reference = (quoted.ref_idx if quoted and quoted.ref_idx else None) or part.quote_ref
        if reference:
            body["message_reference"] = MessageReference(message_id=reference)

        async def deliver(payload: dict[str, Any], **attached: Any):
            if kind == GROUP and EVERYONE in str(payload.get("content") or ""):
                try:
                    result = await post(**target, **payload, **attached)
                    self._everyone_done(openid)
                    return result
                except Exception as e:
                    if getattr(e, "code", None) in (URL_REFUSED, UNPROMPTED_REFUSED):
                        raise
                    # 不是该群管理员，或今天已经用过一次：要说的话照样发。
                    self._everyone_done(openid)
                    logger.info(f"official bridge: a mention of everyone was refused ({getattr(e, 'code', None) or type(e).__name__}); sent without it")
                    rest = str(payload["content"]).replace(EVERYONE, "").strip()
                    if not rest:
                        return SimpleNamespace(id="", ext_info=None)
                    return await post(**target, **{**payload, "content": rest}, **attached)
            try:
                return await post(**target, **payload, **attached)
            except Exception as e:
                if payload.get("msg_type") != 7 or payload.get("content") or getattr(e, "code", None) in (URL_REFUSED, UNPROMPTED_REFUSED) or self.media_needs_text:
                    raise
                result = await post(**target, **{**payload, "content": " "}, **attached)
                Bridge.media_needs_text = True  # 带空白发出去了：这个 API 终究还是要文字
                logger.info(f"official bridge: a picture without text was refused ({getattr(e, 'code', type(e).__name__)}) and accepted with a blank; blanks are sent from now on")
                return result

        def marked(payload: dict[str, Any]) -> dict[str, Any] | None:
            """文字里还留着“@名字”时，整条改成带真正 @ 的 markdown。"""
            content = str(payload.get("content") or "")
            names = {name: member for name, member in part.mentions.items() if name in content}
            if payload.get("msg_type") != 0 or not names or EVERYONE in content:
                return None
            rest = {k: v for k, v in payload.items() if k != "content"}
            return {**rest, "msg_type": 2, "markdown": MessageMarkdown(content=with_mentions(content, names))}

        async def send_as(payload: dict[str, Any], inbound: Inbound | None):
            real = marked(payload)
            if real is not None:
                try:
                    return await deliver(real, **(inbound.attachment() if inbound else {}))
                except Exception as e:
                    if getattr(e, "code", None) in (URL_REFUSED, UNPROMPTED_REFUSED):
                        raise
                    logger.info(f"official bridge: a mention in markdown was refused ({getattr(e, 'code', None) or type(e).__name__}); sent as text")
                    if inbound is not None:
                        inbound.used += 1
            return await deliver(payload, **(inbound.attachment() if inbound else {}))

        async def attempt(payload: dict[str, Any]):
            inbound = self._pick_inbound(kind, openid)
            _was_reply.set(inbound is not None)
            if inbound is not None:
                inbound.used += 1
                # 回复 @ 了机器人的消息时，平台不会自动 @ 发消息的人（在群里实测过）：插件写的 @ 照样发
                age = int(time.time() - inbound.at)
                try:
                    sent = await send_as(payload, inbound)
                    if inbound.kind != kind:
                        logger.info("official bridge: a group message answered in private")
                    if age >= 300:
                        logger.info(f"official bridge: passive reply accepted {age}s after the message")
                    return sent
                except Exception as e:
                    if getattr(e, "code", None) == URL_REFUSED:
                        raise
                    if not isinstance(e, QQActionFailed):
                        # 网络超时、送审中：消息可能已经发出去了。再主动发一遍会重复，交给调用方当作失败
                        raise
                    if inbound.kind != kind:
                        inbound.private_refused = True  # 用私聊回复群消息被拒：这条消息不再试私聊，群里照常能回复
                    else:
                        inbound.used = PASSIVE[kind][1]  # 平台拒绝了这次回复：这条消息不能再回复了
                    logger.info(f"official bridge: passive reply refused ({getattr(e, 'code', type(e).__name__)}) {age}s after the message, sending unprompted")
            _was_reply.set(False)
            await self._pace(kind, openid)
            return await send_as(payload, None)

        try:
            result = await attempt(body)
        except Exception as e:
            retry = dict(body)
            if getattr(e, "code", None) == URL_REFUSED and retry.get("content"):
                # 网址会被直接拒收：消息改成不带链接的网址也能看懂。
                retry["content"] = _URL.sub(lambda m: re.sub(r"^https?://", "", m.group(0)).replace(".", "。"), retry["content"])
            elif "message_reference" in retry:
                logger.info(f"official bridge: an answer with a quote was refused ({getattr(e, 'code', None) or type(e).__name__}); sent without the quote")
                retry.pop("message_reference")
            else:
                raise
            result = await attempt(retry)
        if self.on_sent is not None and kind == GROUP and part.text.strip():
            self.on_sent(openid, part.text)
        if kind == GROUP and part.file_type and part.mentions:
            self._mention_later(openid, next(iter(part.mentions.values())))
        ref = getattr(getattr(result, "ext_info", None), "ref_idx", None)
        return str(result.id or ""), ref

    async def send(self, kind: str, openid: str, message: Any) -> int:
        message = as_message(message)
        parts = self._split(message, openid if kind == GROUP else None)
        if not parts:
            raise ActionFailed(status="failed", retcode=1200, msg="nothing to send", wording="消息为空")
        ids: list[str] = []
        ref: str | None = None
        if kind == GROUP and self._pick_inbound(kind, openid) is None:
            await self._await_copy(openid)
        if kind == GROUP:
            self._address(parts, openid)
            if self.mention_markup:
                parts = self._words_apart(parts)
        for index, part in enumerate(parts):
            try:
                if kind == GROUP and self._muted(openid) and self._pick_inbound(kind, openid) is None:
                    raise _Muted()
                official_id, ref_idx = await self._post(kind, openid, part)
            except Exception as e:
                # 不允许机器人主动发言的群只接受对平台投递的消息的回复。原机器人还在时，其余的经它发出，
                # 免得原机器人听到的指令没人回应。
                # 官方一侧的其他故障（机器人被下架、API 连不上）也一样：有回复比由哪个机器人回复更重要。
                row = self.idmap.group_by_openid(openid) if kind == GROUP else None
                if row is not None and getattr(e, "code", None) == UNPROMPTED_REFUSED:
                    self.idmap.set_group_muted(openid, True)
                    logger.info(f"official bridge: group {row['gid']} does not let the official bot speak unprompted" + ("; the old bot says it instead" if self.speak_for is not None else "; not sent"))
                if row is None or self.speak_for is None:
                    if isinstance(e, _Muted):
                        raise ActionFailed(status="failed", retcode=UNPROMPTED_REFUSED, msg="the group does not let the bot speak unprompted", wording="本群未允许机器人主动发言") from None
                    raise
                if getattr(e, "code", None) == UNPROMPTED_REFUSED:
                    pass
                elif not isinstance(e, _Muted):
                    logger.warning(f"official bridge: sending to group {row['gid']} failed ({getattr(e, 'code', None) or type(e).__name__}); the old bot says it instead")
                rest = Message()
                for later in parts[index:]:
                    rest += self._as_segments(later)
                try:
                    await self.speak_for(row["gid"], rest)
                except Exception:
                    raise e from None  # 那里其实没有别的机器人：维持原来的失败
                break
            else:
                if kind == GROUP and not _was_reply.get():
                    row = self.idmap.group_by_openid(openid)
                    if row is not None and row["muted_at"]:
                        self.idmap.set_group_muted(openid, False)  # 一条主动消息发出去了：又允许了
            if official_id:
                ids.append(official_id)
            ref = ref or ref_idx
        summary: dict[str, Any] = dict(
            time=int(time.time()), message_type="group" if kind == GROUP else "private",
            sender=dict(user_id=self.self_id, nickname=""), message=message, raw_message=str(message),
        )
        mid = self.ledger.add(Record(kind, openid, ids, ref, summary))
        summary.update(message_id=mid, real_id=mid)
        return mid

    def _mention_later(self, openid: str, member: str) -> None:
        """图片里 @ 不了人：交给接下来的按钮消息（send_card）；MENTION_WAIT 秒内没有，就单独发一条 @，说明以上是他要的结果。"""
        if (waiting := self._waiting_mentions.pop((openid, member), None)) is not None:
            waiting[1].cancel()
        self._waiting_mentions[(openid, member)] = (member, asyncio.create_task(self._mention_alone(openid, member, self._asked())))

    @staticmethod
    def _asked() -> str:
        """成员刚发的指令（原样，去掉 @），用来说明补发的 @ 是为了什么。"""
        try:
            text = " ".join(current_event.get().get_message().extract_plain_text().split())
        except Exception:
            return ""
        return text[:30] if text.startswith("/") else ""

    async def _mention_alone(self, openid: str, member: str, asked: str = "") -> None:
        await asyncio.sleep(MENTION_WAIT)
        # 这个群还有消息在发（多页图片的下一张还在上传、整组合并转发还没发完）：等它们都发完，再等 MENTION_WAIT，
        # 让紧跟着的按钮消息有时间把 @ 带走；@ 因此落在最后。为在发的消息最多等 MENTION_HOLD_MAX 秒。
        # 同一成员后面又有带他 @ 的图时，会重新计时并取消这一次。等的期间仍登记在 _waiting_mentions 里，按钮消息随时可以带走它。
        hold_until = time.monotonic() + MENTION_HOLD_MAX
        while self._sending.get(openid) and time.monotonic() < hold_until:
            while self._sending.get(openid) and time.monotonic() < hold_until:
                await asyncio.sleep(0.2)
            await asyncio.sleep(MENTION_WAIT)
        self._waiting_mentions.pop((openid, member), None)
        try:
            inbound = self._pick_inbound(GROUP, openid)
            attached: dict[str, Any] = {}
            if inbound is not None:
                inbound.used += 1
                attached = inbound.attachment()
            else:
                await self._pace(GROUP, openid)
            words = f"以上是 {markdown_escaped(asked)} 的结果" if asked else "以上是结果"  # 已经 @ 了他，不再说“你的”
            await self.qq.post_group_messages(group_openid=openid, msg_type=2, markdown=MessageMarkdown(content=f"{mention_tag(member)} {words}"), **attached)
        except Exception as e:
            logger.info(f"official bridge: the mention after a picture was not sent ({getattr(e, 'code', None) or type(e).__name__})")

    async def send_card(self, kind: str, openid: str, text: str, rows: list[list[tuple[str, str]]]) -> str:
        """一条下面带几排按钮的 markdown 消息；返回官方消息的 id。

        每个按钮是（按钮上的字，点击后以成员名义发出的指令）。最多五排，每排五个。机器人不允许发送时，
        抛出平台的应答（自定义按钮权限是逐个机器人单独开通的）。
        """
        keyboard = MessageKeyboard(content=InlineKeyboard(rows=[
            InlineKeyboardRow(buttons=[
                Button(
                    id=f"{r}-{c}",
                    render_data=RenderData(label=label, visited_label=label, style=1),
                    action=Action(type=2, permission=Permission(type=2), data=command, enter=True, unsupport_tips=f"请手动发送：{command}"),
                )
                for c, (label, command) in enumerate(row[:5])
            ])
            for r, row in enumerate(rows[:5])
        ]))
        post = self.qq.post_group_messages if kind == GROUP else self.qq.post_c2c_messages
        target = {"group_openid": openid} if kind == GROUP else {"openid": openid}
        inbound = self._pick_inbound(kind, openid)
        attached: dict[str, Any] = {}
        if inbound is not None:
            inbound.used += 1
            attached = inbound.attachment()
        asker = self._answering()[0] if kind == GROUP else None
        asker_openid = self.idmap.user_openid(asker) if asker is not None else None
        waiting = self._waiting_mentions.pop((openid, asker_openid), None) if asker_openid else None
        if waiting is not None:  # 前面那张给他的图片没 @ 上他：这条按钮消息带上（别人的不动）
            waiting[1].cancel()
            if not text.startswith(mention_tag(waiting[0])):
                text = f"{mention_tag(waiting[0])} {text}"
        elif kind == GROUP and self.mention_markup and not text.startswith("<qqbot-at-user") and (asker := self._first_answer(openid)) is not None:
            # 和文字、图片回复一样，指令的第一条回复点出提问的成员
            if (member := self.idmap.user_openid(asker)) is not None:
                text = f"{mention_tag(member)} {text}"
        try:
            result = await post(**target, msg_type=2, markdown=MessageMarkdown(content=text), keyboard=keyboard, **attached)
        except Exception:
            if waiting is not None:
                self._mention_later(openid, waiting[0])
            raise
        return str(result.id or "")

    async def send_hints(self, openid: str, text: str, commands: list[str]) -> str:
        """一条下面带指令的私聊消息，点一下即发送对应指令；返回官方消息的 id。

        即平台的“提示键盘”：每行一个建议，点击建议会原样发出它显示的文字。它出现在适配器的模型里，
        平台文档里却没有；本机器人可以发送。有可回复的对象（一条消息，或成员添加机器人
        的事件）时作为回复发出；被拒或没有可回复的对象时作为主动消息发出。
        """
        hints = MessagePromptKeyboard(keyboard=PromptKeyboardModel(content=PromptContent(rows=[
            # 每个字段都要给：适配器会把保持默认值的字段从请求里省掉
            PromptRow(buttons=[PromptButton(render_data=PromptRenderData(label=command, style=2), action=PromptAction(type=2))])
            for command in commands
        ])))
        body: dict[str, Any] = dict(openid=openid, msg_type=2, markdown=MessageMarkdown(content=text), prompt_keyboard=hints)
        inbound = self._pick_inbound(C2C, openid)
        if inbound is not None:
            inbound.used += 1
            try:
                return str((await self.qq.post_c2c_messages(**body, **inbound.attachment())).id or "")
            except Exception as e:
                if inbound.kind != C2C:
                    inbound.private_refused = True  # 挂的是群消息：只是不再用私聊回复它
                else:
                    inbound.used = PASSIVE[C2C][1]  # 这条不能再回复了
                logger.info(f"official bridge: suggested commands were refused as an answer ({getattr(e, 'code', None) or type(e).__name__}); sent unprompted")
        await self._pace(C2C, openid)
        return str((await self.qq.post_c2c_messages(**body)).id or "")

    def _note_own(self, kind: str, openid: str, official_id: str, text: str) -> int:
        """像保存任何发出的消息一样，保存一条桥接自己生成的消息（按钮、建议指令）；返回它的编号。"""
        summary: dict[str, Any] = dict(time=int(time.time()), message_type="group" if kind == GROUP else "private", sender=dict(user_id=self.self_id, nickname=""), message=Message(text), raw_message=text)
        mid = self.ledger.add(Record(kind, openid, [official_id] if official_id else [], None, summary))
        summary.update(message_id=mid, real_id=mid)
        return mid

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))  # 平台按北京时间计日

    def _everyone_done(self, group_openid: str) -> None:
        self.idmap.set_meta(f"everyone:{group_openid}", self._today())

    async def api_get_group_at_all_remain(self, group_id: Any, **_: Any) -> dict:
        """今天在该群是否还能 @全体成员。

        每个群每天一次，而且只限身为群管理员的机器人。这里不查询机器人的身份（编号映射表里记的可能
        已经过时）：不是管理员时平台会拒绝，消息改为不带 @全体 发出，当天的次数也算用掉。
        """
        left = 0 if self.idmap.meta(f"everyone:{self._group(int(group_id))}") == self._today() else 1
        return {"can_at_all": True, "remain_at_all_count_for_group": left, "remain_at_all_count_for_uin": left}

    def _muted(self, group_openid: str) -> bool:
        row = self.idmap.group_by_openid(group_openid)
        return bool(row and row["muted_at"] and time.time() - row["muted_at"] < MUTED_RETRY)

    @staticmethod
    def _as_segments(part: Part) -> Message:
        """为官方 API 切开的一段消息，重新表示成 OneBot 消息。"""
        out = Message()
        if part.text.strip():
            out.append(MessageSegment.text(part.text))
        if part.file_type:
            kind = {1: "image", 2: "video", 3: "record", 4: "file"}[part.file_type]
            source = "base64://" + base64.b64encode(part.data).decode() if part.data is not None else part.url
            out.append(MessageSegment(kind, {"file": source, **({"name": part.name} if part.name else {})}))
        return out

    def _group(self, group_id: Any) -> str:
        openid = self.idmap.group_openid(int(group_id))
        if openid is None:
            raise ActionFailed(status="failed", retcode=1200, msg=f"group {group_id} is unknown to the official bot", wording="机器人不在该群")
        return openid

    def _user(self, user_id: Any) -> str:
        openid = self.idmap.user_openid(int(user_id)) or self.idmap.former_openid(int(user_id))
        if openid is None:
            raise ActionFailed(status="failed", retcode=1200, msg=f"user {user_id} has never talked to the official bot", wording="该用户尚未使用过官方机器人")
        return openid

    # ------------------------------------------------------------------ OneBot API

    async def api_send_msg(self, message: Any = None, message_type: str | None = None, group_id: Any = None, user_id: Any = None, **_: Any) -> dict:
        if message_type == "private" or (message_type is None and not group_id):
            return await self.api_send_private_msg(user_id=user_id, message=message)
        return await self.api_send_group_msg(group_id=group_id, message=message)

    async def api_send_group_msg(self, group_id: Any, message: Any, **_: Any) -> dict:
        message = as_message(message)
        if any(seg.type == "node" for seg in message):
            return await self.api_send_group_forward_msg(group_id=group_id, messages=list(message))
        return {"message_id": await self.send(GROUP, self._group(group_id), message)}

    async def api_send_card(self, text: Any = "", rows: Any = None, group_id: Any = None, user_id: Any = None, mention: Any = None, **_: Any) -> dict:
        """不是 OneBot 调用：一行文字，下面带几排指令按钮（见 send_card）。

        rows：由 [按钮上的字, 点击后发出的指令] 组成的列表，插件通过 plugins/quick_buttons.py 请求。
        mention：在群里开头 @ 这位成员（QQ 号或编号）。
        """
        kind, openid = (GROUP, self._group(group_id)) if group_id else (C2C, self._user(user_id))
        buttons = [[(str(label), str(command)) for label, command in row] for row in rows or [] if row]
        if not buttons:
            raise ActionFailed(status="failed", retcode=1200, msg="no buttons to send", wording="没有按钮")
        if kind == GROUP and self._pick_inbound(kind, openid) is None:
            await self._await_copy(openid)
        text = str(text or "").strip() or "请选择"
        if kind == GROUP and str(mention or "").isdigit():
            member = self.idmap.user_openid(int(mention))
            real = self.mention_markup and member is not None
            text = f"{mention_tag(member) if real else markdown_escaped('@' + self._name_of(int(mention), openid))} {text}"
        official_id = await self.send_card(kind, openid, text, buttons)
        return {"message_id": self._note_own(kind, openid, official_id, str(text or ""))}

    async def api_send_hints(self, text: Any = "", commands: Any = None, user_id: Any = None, group_id: Any = None, **_: Any) -> dict:
        """不是 OneBot 调用：一条下面带指令、点一下即发送的私聊消息（见 send_hints）。

        插件通过 plugins/quick_buttons.py 请求。
        """
        wanted = [str(command).strip() for command in commands or [] if str(command).strip()]
        if group_id or user_id is None or not wanted:
            raise ActionFailed(status="failed", retcode=1200, msg="suggested commands need a private chat and a command", wording="没有可发送的指令")
        openid = self._user(user_id)
        words = str(text or "").strip() or "可以直接点下面的指令"
        return {"message_id": self._note_own(C2C, openid, await self.send_hints(openid, words, wanted), words)}

    async def api_send_private_msg(self, user_id: Any, message: Any, **_: Any) -> dict:
        message = as_message(message)
        if any(seg.type == "node" for seg in message):
            return await self.api_send_private_forward_msg(user_id=user_id, messages=list(message))
        return {"message_id": await self.send(C2C, self._user(user_id), message)}

    async def _forward(self, kind: str, openid: str, messages: Any) -> dict:
        """官方一侧没有合并转发卡片：它的各页依次发出（见 _forward_pages）。整组发完之前都算在发，补发的 @ 落在最后一页之后。"""
        self._sending_more(kind, openid)
        try:
            return await self._forward_pages(kind, openid, messages)
        finally:
            self._sending_less(kind, openid)

    async def _forward_pages(self, kind: str, openid: str, messages: Any) -> dict:
        """官方一侧没有合并转发卡片：它的各页依次发出。

        每页只有一张图的卡片这样发会刷屏（而且每条消息最多带一张图），所以把这些图在 stitch_height
        允许的范围内拼成尽量少的长图。

        文字页则根本不逐页发：这样的卡片就是一个列表（给全体成员签到的结果有 194 页，就是 194 条消息）。它们一起发出，每页跟在它原来所挂的名字后面，按长度合成尽量少的消息。
        """
        first: int | None = None
        words: list[str] = []  # 前后相连、等着一起发出的文字页
        own_name = (getattr(getattr(self.qq, "self_info", None), "username", None) or "").strip()

        async def say_words() -> None:
            nonlocal first
            for text in gathered(words):
                sent = await self.send(kind, openid, Message(MessageSegment.text(text)))
                first = first or sent
            words.clear()

        pictures = self._lone_pictures(messages) if self.stitch_height else None
        if pictures:
            try:
                joined = await asyncio.to_thread(stitch, pictures, self.stitch_height)
            except Exception as e:
                logger.warning(f"official bridge: pages not joined into one picture ({type(e).__name__}: {e}); sent one by one")
                joined = pictures
            if len(joined) < len(pictures):
                for blob in joined:
                    sent = await self.send(kind, openid, Message(MessageSegment.image(blob)))
                    first = first or sent
                return {"message_id": first, "forward_id": "", "res_id": ""}
        for node in as_message(messages):
            if node.type != "node":
                continue
            data = node.data
            content = data.get("content")
            if content is None:
                continue
            content = as_message(content)
            nested = [seg for seg in content if seg.type == "node"]
            if not nested and all(seg.type == "text" for seg in content):
                name = str(data.get("nickname") or data.get("name") or "").strip()
                page = page_of_words("" if name == own_name else name, "".join(str(seg.data.get("text", "")) for seg in content))
                if page:
                    words.append(page)
                continue
            await say_words()
            if nested:
                sent = (await self._forward(kind, openid, nested))["message_id"]
            else:
                sent = await self.send(kind, openid, content)
            first = first or sent
        await say_words()
        if first is None:
            raise ActionFailed(status="failed", retcode=1200, msg="empty forward", wording="消息为空")
        return {"message_id": first, "forward_id": "", "res_id": ""}

    def _lone_pictures(self, messages: Any) -> list[bytes] | None:
        """每一页都恰好是一张这里存着的图片的卡片，返回这些图片；其他卡片返回 None。"""
        pictures: list[bytes] = []
        for node in as_message(messages):
            content = node.data.get("content") if node.type == "node" else None
            if content is None:
                return None
            parts = [seg for seg in as_message(content) if not (seg.type == "text" and not str(seg.data.get("text", "")).strip())]
            if len(parts) != 1 or parts[0].type != "image":
                return None
            try:
                blob, _ = self._read_file(parts[0].data.get("file") or parts[0].data.get("url"))
            except ActionFailed:
                return None
            if blob is None:
                return None
            pictures.append(blob)
        return pictures if len(pictures) > 1 else None

    async def api_send_group_forward_msg(self, group_id: Any, messages: Any = None, **_: Any) -> dict:
        return await self._forward(GROUP, self._group(group_id), messages)

    async def api_send_private_forward_msg(self, user_id: Any, messages: Any = None, **_: Any) -> dict:
        return await self._forward(C2C, self._user(user_id), messages)

    async def api_send_forward_msg(self, messages: Any = None, message_type: str | None = None, group_id: Any = None, user_id: Any = None, **_: Any) -> dict:
        if message_type == "private" or (message_type is None and not group_id):
            return await self.api_send_private_forward_msg(user_id=user_id, messages=messages)
        return await self.api_send_group_forward_msg(group_id=group_id, messages=messages)

    async def api_delete_msg(self, message_id: Any, **_: Any) -> None:
        record = self.ledger.get(int(message_id))
        if record is None:
            raise ActionFailed(status="failed", retcode=1200, msg="message not found", wording="消息不存在")
        for official in record.official_ids:
            if record.kind == GROUP:
                await self.qq.delete_group_message(group_openid=record.openid, message_id=official)
            else:
                await self.qq.delete_c2c_message(openid=record.openid, message_id=official)

    async def api_get_msg(self, message_id: Any, **_: Any) -> dict:
        record = self.ledger.get(int(message_id))
        if record is None or record.event is None:
            raise ActionFailed(status="failed", retcode=1200, msg="message not found", wording="消息不存在")
        return record.event

    async def api_get_image(self, file: Any = None, **_: Any) -> dict:
        """图片到达时自带地址；没有什么需要查的。"""
        address = str(file or "")
        if not address.startswith(("http://", "https://")):
            raise ActionFailed(status="failed", retcode=1200, msg="picture not found", wording="图片不存在")
        return {"file": address, "url": address}

    async def api_get_group_file_url(self, file_id: Any = None, **_: Any) -> dict:
        address = str(file_id or "")
        if not address.startswith(("http://", "https://")):
            raise ActionFailed(status="failed", retcode=1200, msg="file not found", wording="文件不存在")
        return {"url": address}

    async def api_get_login_info(self, **_: Any) -> dict:
        name = ""
        try:
            name = self.qq.self_info.username or ""
        except Exception:
            pass
        return {"user_id": self.self_id, "nickname": name or next(iter(self.bot.config.nickname), "") or str(self.self_id)}

    async def api_get_stranger_info(self, user_id: Any, **_: Any) -> dict:
        row = self.idmap.user(int(user_id))
        return {"user_id": int(user_id), "nickname": (row["nickname"] if row else "") or str(user_id), "sex": "unknown", "age": 0}

    async def api_get_friend_list(self, **_: Any) -> list[dict]:
        return [{"user_id": row["uid"], "nickname": row["nickname"] or str(row["uid"]), "remark": ""} for row in self.idmap.friends()]

    def _group_info(self, row: Any) -> dict:
        return {
            "group_id": row["gid"],
            "group_name": row["name"] or f"群{row['gid']}",
            "member_count": self.idmap.count_known(row["openid"], row["gid"], self.self_id),
            "max_member_count": 0,
        }

    async def api_get_group_list(self, no_cache: bool = False, **_: Any) -> list[dict]:
        groups = {row["gid"]: self._group_info(row) for row in self.idmap.groups()}
        theirs = await self.census_groups() if no_cache and self.census_groups is not None else None
        if theirs is not None:
            # 要求重新查询时，这份列表是用来判断谁还在机器人服务的任何一个群里的（见
            # api_get_group_member_list）：两个机器人所在的全部群。只有本机器人在、还没见过任何成员的群
            # 不计入，因为对它什么也说不了。
            # 原机器人的群无法确定时，整个查询失败。
            groups = {gid: group for gid, group in groups.items() if gid < GROUP_BASE or group["member_count"]}
            for group in theirs:
                gid = int(group["group_id"])
                groups[gid] = {**groups.get(gid, {}), **group}
            return list(groups.values())
        if self.other_groups is not None:  # 给定时任务的统一列表：两个机器人所在的全部群
            for group in await self.other_groups():
                gid = int(group["group_id"])
                groups[gid] = {**groups.get(gid, {}), **group}
        return list(groups.values())

    async def api_get_group_info(self, group_id: Any, **_: Any) -> dict:
        row = self.idmap.group(int(group_id))
        if row is None:
            raise ActionFailed(status="failed", retcode=1200, msg=f"group {group_id} is unknown", wording="机器人不在该群")
        return self._group_info(row)

    def _member_info(self, gid: int, row: Any) -> dict:
        uid = self.idmap.user_id(row["openid"])
        nickname = self._name_of(uid, None)
        if uid >= USER_BASE and nickname == str(uid):
            nickname = ""  # 名字还不知道（比如只收到过他的进群事件）：占位号不是 QQ 号，不拿它充当名字
        return {
            "group_id": gid, "user_id": uid, "nickname": nickname, "card": row["nickname"] or "", "sex": "unknown", "age": 0, "area": "",
            "join_time": int(row["joined_at"] or 0), "last_sent_time": int(row["last_seen"] or 0), "level": "0",
            "role": row["role"] or "member", "unfriendly": False, "title": "", "title_expire_time": 0, "card_changeable": False,
        }

    def _roster_info(self, gid: int, row: Any) -> dict:
        uid = row["uid"]
        return {
            "group_id": gid, "user_id": uid, "nickname": row["nickname"] or self._name_of(uid, None), "card": row["card"] or "", "sex": "unknown", "age": 0, "area": "",
            "join_time": int(row["joined_at"] or 0), "last_sent_time": 0, "level": "0",
            "role": row["role"] or "member", "unfriendly": False, "title": "", "title_expire_time": 0, "card_changeable": False,
        }

    def _members_known(self, gid: int, group_openid: str) -> list[dict]:
        """本机器人见过的成员，后面再接上另一个机器人最后一次给出的该群完整名单上的成员。

        那份名单停留在那个机器人最后在群里的那天：之后退群的人仍在上面，之后进群的人要等他发言才有。
        """
        seen = [self._member_info(gid, row) for row in self.idmap.members(group_openid)]
        have = {member["user_id"] for member in seen} | {self.self_id}
        return seen + [self._roster_info(gid, row) for row in self.idmap.roster(gid) if row["uid"] not in have]

    async def api_get_group_member_info(self, group_id: Any, user_id: Any, **_: Any) -> dict:
        gid, uid = int(group_id), int(user_id)
        if uid == self.self_id:
            # 问“机器人是不是这里的管理员”是为了知道它在群里能做什么；原机器人也在时，需要管理员的事
            # 由它来做：取两者中较高的身份。
            info = await self.api_get_login_info()
            openid = self.idmap.group_openid(gid)
            own = self.idmap.member(openid, SELF) if openid else None
            roles = [own["role"] if own else ("member" if openid else None), await self.other_role(gid) if self.other_role is not None else None]
            role = max((r for r in roles if r), key=("member", "admin", "owner").index, default=None)
            if role is None:
                raise ActionFailed(status="failed", retcode=1200, msg=f"group {gid} is unknown", wording="机器人不在该群")
            return {"group_id": gid, "user_id": uid, "nickname": info["nickname"], "card": "", "role": role, "sex": "unknown", "age": 0, "join_time": 0, "last_sent_time": 0, "level": "0", "title": ""}
        group_openid = self._group(gid)
        openid = self.idmap.user_openid(uid)
        if openid and self.idmap.has_departed(group_openid, openid):  # 平台说他已退群
            raise ActionFailed(status="failed", retcode=1200, msg="member not found", wording="群成员不存在")
        row = self.idmap.member(group_openid, openid) if openid else None
        if row is None:
            kept = self.idmap.roster_member(gid, uid)
            if kept is None:
                raise ActionFailed(status="failed", retcode=1200, msg="member not found", wording="群成员不存在")
            return self._roster_info(gid, kept)
        return self._member_info(gid, row)

    async def _refresh_members(self, group_openid: str) -> bool:
        """从平台读取成员名单存进编号映射表；返回是否完整读完。

        GET /v2/groups/{group_openid}/members，每页 30 个。这个权限是逐个机器人单独开通的（未开通时
        返回 11253），所以调用方用的一直是见过的成员；这里读到的只是补充。平时由 _member_task 安排。
        不删任何成员行：某人退了 A 群、在 B 群潜水没被见过，A 读成功删了他、B 这次又读失败，他就成了不在任何群。
        隔开的两次完整读取都没有、第二次还核对得上的人，才记成已退群（见 _read_members）。这个标记影响普通名单、成员信息和
        present_numbers / last_present（周检据此判断谁可能已经离开）；no_cache 名单和人数不看它。
        """
        async with self._members_lock:  # 一次只读一个群：频率限制是整个机器人共用的
            return await self._read_members(group_openid)

    async def _paced(self, url: Any, params: dict | None = None) -> Any:
        """读成员名单的一次请求。全机器人共用一个节奏：任意两次之间至少隔 MEMBER_PAGE_PACE 秒（不分哪个群，
        失败了也算一次）。平台说太频繁（429）时，之后 MEMBERS_BACKOFF 秒谁都不发，连续再遇到就翻倍，
        最多 MEMBERS_BACKOFF_MAX 秒；成功一次就恢复。"""
        wait = self._members_next_at - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            answer = await self.qq._request(Request("GET", url, params=params) if params is not None else Request("GET", url))
        except QQActionFailed as e:
            if isinstance(e, QQRateLimited) or getattr(e, "status_code", None) == 429:
                self._members_backoff = min(MEMBERS_BACKOFF_MAX, self._members_backoff * 2 or MEMBERS_BACKOFF)
                self._members_next_at = time.monotonic() + self._members_backoff
                logger.info(f"official bridge: member lists are read too often for the platform: pausing for {int(self._members_backoff)} s")
            else:
                self._members_next_at = time.monotonic() + MEMBER_PAGE_PACE
            raise
        except BaseException:
            self._members_next_at = time.monotonic() + MEMBER_PAGE_PACE
            raise
        self._members_backoff = 0.0
        self._members_next_at = time.monotonic() + MEMBER_PAGE_PACE
        return answer

    async def _read_members(self, group_openid: str) -> bool:
        """_refresh_members 的本体，调用方已拿着 _members_lock。返回是否完整读完。

        开读时间和"读的过程中出现过的人"都从真正开读算起（拿到锁之后）。
        next_cursor 是空串才算读到头（文档："空串表示已到末页"）。下面任一情况都算没读完，谁也不记：
        一页不是 dict，members 不是列表，next_cursor 不是字符串，或者带着非 0 的 code；一页是空的却还有下一页；
        members 里有不是对象、或没有 member_openid 的项；cursor 回到读过的一页（含 A-B-A）；到了 MEMBER_PAGES 页还没完；
        名单里一个真人都没有。
        读完了，名单上没有也不一定就是退了群（名单一边读一边在变，平台也会答错），所以第一次只记一笔、照样算在场。
        下一次读完（_member_task 保证隔着 MEMBERS_REFRESH_EVERY 以上）还没有他、中间也没露过面，并且这一次下面几样都成立，
        才记成退群：读的过程中没人进群、退群；读前、读后各问一次的群人数一样，而且正好是名单里不重复的人和机器人的个数；
        名单上见到过在那个群发过言的人（编号是同一套，见过一次即可）。人数和名单各算不算机器人、算不算本机器人，没有实物可查：
        只认相等，差一个也不确认（错的方向是晚确认）；开通以后按日志里那行的几个数再调。
        每一页回来先确认机器人还在这个群里：读的过程中被移出的群，不能再往记录里写。
        """
        row = self.idmap.group_by_openid(group_openid)
        if row is None:
            return False
        gid = row["gid"]
        base = self.qq.adapter.get_api_base().joinpath("v2", "groups", group_openid)
        listed: set[str] = set()
        humans: set[str] = set()
        present: set[str] = set()  # 读的过程中在这个群发言、进群的人：他那一页可能在他进群之前就读过了
        changed: set[str] = set()  # 读的过程中发言、进群、退群的人：以事件为准，旧的一页里再出现不写他
        moved: set[str] = set()  # 读的过程中进群、退群的人（没写是谁的是空串）：有的话这一次谁也不确认退群
        waiting: set[str] = set()  # 早先一次就不在名单上、这次又没有的人
        counted = recounted = None  # 有人可能要记成退群时，读之前、读之后各问一次群人数
        entry = (present, changed, moved)
        self._reading.setdefault(group_openid, []).append(entry)
        started = int(time.time())  # 取整到秒：平台事件的时间是整秒，同一秒里收到的退群不算在开读之前
        complete = False
        try:
            known = self.idmap.known_in(group_openid, gid)  # 只有开读之前就在记录里的人，才可能因为名单上没有而记成退群
            doubted = self.idmap.unlisted_in(group_openid) & known
            if doubted:
                counted = await self._member_count(group_openid, gid)
            cursor, seen = "", {""}
            for _ in range(MEMBER_PAGES):
                page = await self._paced(base.joinpath("members"), {"cursor": cursor})
                still = self.idmap.group_by_openid(group_openid)
                if still is None or still["gid"] != gid:
                    logger.info(f"official bridge: the bot left group {gid} while reading its member list: reading stopped")
                    return False
                if (not isinstance(page, dict) or not isinstance(page.get("members"), list) or not isinstance(page.get("next_cursor"), str)
                        or page.get("code") not in (None, 0) or (page["next_cursor"] and not page["members"])):
                    logger.info(f"official bridge: the member list of group {gid} stopped at an answer that is not a page: nobody noted as gone")
                    return False
                if any(not isinstance(member, dict) or not member.get("member_openid") for member in page["members"]):
                    logger.info(f"official bridge: the member list of group {gid} has an entry that is not a member: nobody noted as gone")
                    return False
                with self.idmap.transaction():  # 一页一次写盘：人多的群第一次读时，不必每人等一次硬盘
                    for member in page["members"]:
                        openid = member["member_openid"]
                        listed.add(openid)
                        if member.get("bot"):
                            self.idmap.clear_departed(group_openid, openid, started)  # 别的机器人也会在记录里：列出来就是还在
                            continue
                        humans.add(openid)
                        if openid in changed:
                            continue
                        try:
                            joined = datetime.fromisoformat(str(member.get("joined_at"))).timestamp()
                        except ValueError:
                            joined = 0
                        self.idmap.user_id(openid, member.get("username"))
                        # 只去掉开读之前记下的退群标记：读的过程中才退群的人，旧的一页里还会有他
                        self.idmap.touch_member(group_openid, openid, member.get("member_role"), None, joined, listed=True, at=started)
                cursor = page["next_cursor"]
                if not cursor:
                    complete = True
                    break
                if cursor in seen:
                    logger.info(f"official bridge: the member list of group {gid} came back to a page already read: nobody noted as gone")
                    return False
                seen.add(cursor)
            if complete and humans:
                waiting = doubted - listed - present
                if waiting and counted and not moved:
                    recounted = await self._member_count(group_openid, gid)  # 还算在读：这期间的事件照样记着
        finally:
            readers = [r for r in self._reading.get(group_openid, []) if r is not entry]
            if readers:
                self._reading[group_openid] = readers
            else:
                self._reading.pop(group_openid, None)
        if not complete:
            logger.info(f"official bridge: the member list of group {gid} has more than {MEMBER_PAGES} pages: nobody noted as gone")
            return False
        if not humans:
            logger.info(f"official bridge: the member list of group {gid} names nobody but bots: nobody noted as gone")
            return False
        still = self.idmap.group_by_openid(group_openid)
        if still is None or still["gid"] != gid:
            return False
        self.idmap.set_meta(f"members_read:{group_openid}", str(time.time()))
        same_ids = self.idmap.meta(MEMBER_IDS_SEEN) is not None
        # 只认名单上发过言的人：只在名单上见过的证明不了什么，答错的名单也会把他们写进来
        if not same_ids and any(row["last_seen"] and row["openid"] in humans for row in self.idmap.members(group_openid)):
            self.idmap.set_meta(MEMBER_IDS_SEEN, "1")
            same_ids = True
        waiting -= present
        if waiting:
            if moved or not same_ids or not counted or recounted != counted or counted != len(listed):
                logger.info(f"official bridge: the member list of group {gid} does not prove anyone gone ({len(listed)} entries, {len(humans)} of them people, count {counted} then {recounted}, "
                            f"{len(moved)} came or went meanwhile, ids seen speaking: {same_ids})")
                waiting = set()  # 这次不确认；新缺席的照样记下第一次，早先记下的时间不动
        noted = self.idmap.mark_absent(group_openid, gid, listed | present, started, known, waiting)
        if noted:
            logger.info(f"official bridge: the member list of group {gid} was read in full: {noted} members on record are not on it, noted as gone")
        return True

    async def _member_count(self, group_openid: str, gid: int) -> int | None:
        """平台现在说这个群有多少人（核对名单用）；问不到时为 None。"""
        try:
            return int((await self.group_info(group_openid) or {}).get("group_member_num") or 0) or None
        except Exception as e:
            logger.info(f"official bridge: the member count of group {gid} not read ({type(e).__name__})")
            return None

    def _list_read_at(self, group_openid: str) -> float:
        """上次完整读完这个群的平台名单是什么时候（存在表里，重启不丢）；没读完过时为 0。"""
        return float(self.idmap.meta(f"members_read:{group_openid}") or 0)

    async def read_due_lists(self) -> int:
        """把该读的活跃群的平台名单读一轮（__init__ 每小时叫一次），返回读完了几个群。

        present_numbers 只看表：只潜水的人要靠名单才会被记成在场，漏了退群事件的人也要靠它才会被记成退群。
        一次只排一个群，别人现要的名单插得进来。最久没读的先读；上次没读成的（报错、没读完、没权限）6 小时内不再试，
        而且排在读成过的后面：一个总是失败的群挡不住别的群，重启也一样。没权限的那几个小时里不查表、不发请求。
        """
        now = time.time()
        if now < self._members_denied_until:
            return 0
        due = []
        for row in self.idmap.groups():
            openid = row["openid"]
            read, tried = self._list_read_at(openid), float(self.idmap.meta(f"members_tried:{openid}") or 0)
            if now - read >= MEMBERS_DAILY_AFTER and now - tried >= MEMBERS_REFRESH_EVERY:
                due.append((tried > read, max(read, tried), openid))
        done = 0
        for *_, openid in sorted(due):
            if (task := self._member_task(openid)) is not None and await asyncio.shield(task):
                done += 1
        if done:
            logger.info(f"official bridge: member lists read in full: {done} of {len(due)} groups that were due")
        return done

    def _member_task(self, group_openid: str) -> asyncio.Task | None:
        """这个群正在排队或在读的那一次成员名单读取；到期了就排上一次，在后台跑。每群最多一个。
        没开权限时（11253）6 小时内哪个群都不排。从上次排上、上次读完（存在表里）两者里晚的算起，MEMBERS_REFRESH_EVERY
        秒内不再读：两次"名单上没有他"因此总隔着这么久，重启也一样。读失败或没读完的 MEMBERS_RETRY_AFTER 秒后可以再排。"""
        task = self._member_tasks.get(group_openid)
        if task is not None and not task.done():
            return task
        now = time.time()
        if now < self._members_denied_until:
            return None
        if now - max(self._members_read_at.get(group_openid, 0), self._list_read_at(group_openid)) < MEMBERS_REFRESH_EVERY:
            return None
        self._members_read_at[group_openid] = now
        task = asyncio.create_task(self._refresh_quietly(group_openid))
        self._member_tasks[group_openid] = task
        task.add_done_callback(lambda done, key=group_openid: self._member_tasks.pop(key, None) if self._member_tasks.get(key) is done else None)
        return task

    async def _refresh_quietly(self, group_openid: str) -> bool:
        """排上的那一次：拿到锁之后再看一次有没有权限（排队期间别的群可能已经问出没有）。失败只记一行。"""
        try:
            async with self._members_lock:
                if time.time() < self._members_denied_until:
                    return False
                complete = await self._read_members(group_openid)
        except Exception as e:
            if getattr(e, "code", None) == MEMBERS_NOT_GRANTED:
                self._members_denied_until = time.time() + 6 * 3600
            else:
                self._members_read_at[group_openid] = time.time() - MEMBERS_REFRESH_EVERY + MEMBERS_RETRY_AFTER
            code = getattr(e, "code", None) or type(e).__name__
            if code == MEMBERS_NOT_GRANTED:
                logger.debug(f"official bridge: member list not refreshed ({code}), using members seen so far")
            else:
                try:
                    row = self.idmap.group_by_openid(group_openid)
                except sqlite3.Error:  # 查不了群号，也得把这一次了结
                    row = None
                logger.info(f"official bridge: the member list of group {row['gid'] if row else '?'} not read ({code}), using members seen so far")
            self._note_not_read(group_openid)
            return False
        if not complete:
            self._members_read_at[group_openid] = time.time() - MEMBERS_REFRESH_EVERY + MEMBERS_RETRY_AFTER
            self._note_not_read(group_openid)
        return complete

    def _note_not_read(self, group_openid: str) -> None:
        """记下这个群这次没读成（重启不丢）：每小时那一轮 6 小时内不再试它。"""
        try:
            if self.idmap.group_by_openid(group_openid) is not None:  # 已经不在的群不留记录
                self.idmap.set_meta(f"members_tried:{group_openid}", str(time.time()))
        except sqlite3.Error as e:
            logger.warning(f"official bridge: a failed member list read not written to the id map ({type(e).__name__})")

    async def group_info(self, group_openid: str) -> dict | None:
        """GET /v2/groups/{group_openid}/info：群名、人数等（没有群号）。群已解散或机器人已不在群里时返回 None。
        全机器人共用一个节奏：两次之间至少隔 GROUP_INFO_PACE 秒。"""
        url = self.qq.adapter.get_api_base().joinpath("v2", "groups", group_openid, "info")
        now = time.monotonic()
        wait = self._info_next_at - now
        self._info_next_at = max(now, self._info_next_at) + GROUP_INFO_PACE  # 先占下这一格：同时来问的各等各的
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            return await self.qq._request(Request("GET", url)) or {}
        except QQActionFailed as e:
            if e.code == GROUP_GONE:
                return None
            raise

    async def check_group(self, openid: str) -> str:
        """取一个群的群名记下来，返回 "named"。平台说群不存在（已解散，或机器人已不在群里）时，第一次只记下时间、
        返回 "doubtful"；至少隔 GROUP_GONE_CONFIRM 秒的一次检查又这么说，才和被移出一样清掉这个群并告诉各插件，
        返回 "gone"。平台答错一次，就会让只在这个群被见过的人全部失去在场的证据。查询失败时抛出异常。"""
        key = f"group_gone_once:{openid}"
        info = await self.group_info(openid)
        if info is None:
            first = float(self.idmap.meta(key) or 0)
            if first and time.time() - first >= GROUP_GONE_CONFIRM:
                self.leave_group(openid)  # forget_group 一并清掉这条记录
                return "gone"
            if not first:
                self.idmap.set_meta(key, str(time.time()))
            row = self.idmap.group_by_openid(openid)
            logger.info(f"official bridge: group {row['gid'] if row else '?'} was said not to exist; it is cleared only if a later check says so again")
            return "doubtful"
        if self.idmap.meta(key) is not None:
            self.idmap.drop_meta(key)
        if name := str(info.get("group_name") or "").strip():
            self.idmap.note_group(openid, name=name)
        return "named"

    def _forget_group(self, openid: str) -> int | None:
        """清掉一个机器人已不在的群，连同内存里"刚读过它的名单"：它再出现时，下一轮就读。"""
        self._members_read_at.pop(openid, None)
        return self.idmap.forget_group(openid)

    def leave_group(self, openid: str) -> None:
        """机器人已不在这个群：删掉它的记录，并像被移出时一样告诉各插件（它们各自清掉该群的数据）。"""
        gid = self._forget_group(openid)
        if gid is None:
            return
        logger.info(f"official bridge: group {gid} is gone: its records were removed")
        notice = GroupDecreaseNoticeEvent(time=int(time.time()), self_id=self.self_id, post_type="notice", notice_type="group_decrease",
                                          sub_type="kick_me", user_id=self.self_id, group_id=gid, operator_id=self.self_id)
        self.dispatch(notice, None)

    async def check_groups(self) -> dict[str, int]:
        """逐个查询各群：更新群名（群改名后跟着变），清掉已解散的群。按平台的频率限制慢慢查。"""
        tally: dict[str, int] = {}
        for row in self.idmap.groups(active_only=False):
            if not row["active"]:  # 标成停用、还没清掉的群
                self.leave_group(row["openid"])
                tally["gone"] = tally.get("gone", 0) + 1
                continue
            try:
                outcome = await self.check_group(row["openid"])
            except Exception as e:
                outcome = "failed"
                logger.info(f"official bridge: group {row['gid']} info not read ({type(e).__name__})")
            tally[outcome] = tally.get(outcome, 0) + 1
            await asyncio.sleep(GROUP_INFO_PACE)
        self.idmap.set_meta("groups_checked_at", str(time.time()))
        logger.info(f"official bridge: groups checked: {tally}")
        return tally

    async def api_get_group_member_list(self, group_id: Any, no_cache: bool = False, **_: Any) -> list[dict]:
        gid = int(group_id)
        group_openid = self._group(gid)
        if no_cache and not self.members_authoritative:
            # 调用方要一份最新的完整名单，是拿它当谁已退群的证据（周检现在不读它，改问 present_numbers）。原机器人在的群，
            # 由原机器人给出名单（调用不会到这里）。其他情况以本机器人自己的记录为准：
            # 只有它待过的群，用它见过的成员；原机器人已离开的群，用这些成员加上原机器人离开那天保存的最后
            # 一份完整名单。其他情况不回答：带着原群号却没有这样一份最终名单的群（原机器人只是离线，或留过
            # 名单却没标成最终），会让所有没发过言的人看起来都已退群。只有一种例外，见下面的分支。
            refused = ActionFailed(status="failed", retcode=1403, msg="member list is not authoritative on the official bot", wording="官方机器人的成员列表不完整")
            if gid >= GROUP_BASE:
                if self.census_groups is None:
                    raise refused
                try:
                    await self.census_groups()
                except Exception:
                    raise refused from None
            elif not (self.idmap.roster_final(gid) and self.idmap.roster(gid)):
                # 例外：这个群在名单表里一行都没有，而且原机器人已被确认不在任何群。没有名单的来历有三种：
                # 它退出之后本机器人才进群、又被绑上原群号；对上之后它在第一次存名单之前就走了；名单一直没读成。
                # 这时这个群和只有本机器人待过的群一样，用它见过的成员；通过原机器人绑定、还没被认出的人
                # 由等待期管着，不靠这份名单。原机器人只是离线、还在别的群、或留过名单却没标成最终的，照旧不回答。
                if self.idmap.roster(gid) or self.census_groups is None or self.old_bot_gone_at is None:
                    raise refused
                try:
                    theirs = await self.census_groups()  # 现问一次：它若又回到了某个群，“已不在任何群”的记录会随之收回
                except Exception:
                    raise refused from None
                if theirs or not self.old_bot_gone_at():
                    raise refused
        task = self._member_task(group_openid)  # 平台成员名单（开了权限才有）在后台读
        complete = False
        if no_cache and task is not None:
            # 要最新完整名单的调用方等这个群在排或在读的那一次：读到的人会补进记录。等的人被取消也不打断它；
            # 读失败就照旧用已经记下的。普通名单不等，这次用已经记下的。
            try:
                complete = await asyncio.shield(task)
            except Exception:
                complete = False
        if no_cache and self.members_authoritative and not (complete or time.time() - self._list_read_at(group_openid) < MEMBERS_REFRESH_EVERY):
            # 把名单当作权威的部署：最近没有一次完整读完这个群，就不回答（见过的人不等于群里的人）
            raise ActionFailed(status="failed", retcode=1403, msg="member list not read in full lately", wording="官方机器人的成员列表不完整")
        if no_cache and gid >= GROUP_BASE:
            return [self._member_info(gid, row) for row in self.idmap.members(group_openid)]
        if no_cache:
            return self._members_known(gid, group_openid)
        # 平台说已退群的人不再列出（榜单的“本群”、谁能上榜都按这份名单）。要最新完整名单的（no_cache）和群人数
        # 照旧算他们，两者口径一致。
        gone = self.idmap.departed_uids(group_openid)
        return [member for member in self._members_known(gid, group_openid) if member["user_id"] not in gone]

    async def _upload_file(self, kind: str, openid: str, file: Any, name: str | None) -> None:
        blob, url = self._read_file(file)
        await self._post(kind, openid, Part(file_type=4, data=blob, url=url, name=name or "file"))

    async def api_upload_group_file(self, group_id: Any, file: Any, name: str | None = None, **_: Any) -> None:
        await self._upload_file(GROUP, self._group(group_id), file, name)

    async def api_upload_private_file(self, user_id: Any, file: Any, name: str | None = None, **_: Any) -> None:
        await self._upload_file(C2C, self._user(user_id), file, name)

    async def api_get_version_info(self, **_: Any) -> dict:
        # 这里应答其扩展接口的客户端名称（消息表情回应，空操作）。
        return {"app_name": "NapCat.Onebot", "app_version": "official-bridge", "protocol_version": "v11"}

    async def api_get_status(self, **_: Any) -> dict:
        return {"online": True, "good": True}

    async def api_can_send_image(self, **_: Any) -> dict:
        return {"yes": True}

    async def api_can_send_record(self, **_: Any) -> dict:
        return {"yes": True}

    # 官方一侧没有这些概念的操作；回答“完成”让调用方不再报错。
    async def _noop(self, **_: Any) -> None:
        return None

    api_set_msg_emoji_like = api_set_group_reaction = api_set_reaction = _noop
    api_set_group_add_request = api_set_friend_add_request = _noop

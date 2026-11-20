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
from nonebot.adapters.qq.models import Action, Button, InlineKeyboard, InlineKeyboardRow, Media, MessageKeyboard, MessageMarkdown, MessageReference, Permission, RenderData
from nonebot.adapters.qq.models import MessagePromptKeyboard, PromptAction, PromptButton, PromptContent, PromptKeyboardModel, PromptRenderData, PromptRow
from nonebot.drivers import Driver, Request
from nonebot.matcher import current_event, current_matcher

from .idmap import GROUP_BASE, SELF, IdMap

GROUP, C2C = "group", "c2c"

# 被动回复：（收到的消息在多少秒内可以回复，每条消息可回复几次）。比文档写的 60 分钟 x 5 次（群）和
# 60 分钟 x 4 次（私聊）略少一点，免得上传慢时越过界限。群原来是 5 分钟，平台 2026-11 改成了 60 分钟。
PASSIVE = {GROUP: (3500, 5), C2C: (3500, 4)}
INLINE_UPLOAD_MAX = 6 * 1024 * 1024  # 更大的文件走分片上传
URL_REFUSED = 40_054_010
# 在群里 @全体成员：平台自己的标记，用在文字消息里，仅限身为群管理员的机器人，每个群每天一次
# （平台文档）。
EVERYONE = "<qqbot-at-everyone />"
UNPROMPTED_REFUSED = 40_034_105  # 该群不允许机器人在没被搭话时主动发言
MUTED_RETRY = 600.0  # 某群拒收过主动消息后，隔多少秒再试
MEMBERS_NOT_GRANTED = 11253  # 成员名单权限是逐个机器人单独开通的
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
        self._addressed: deque[int] = deque(maxlen=500)  # 已经点过名的指令消息：同一次回复只 @ 一次
        self.members_authoritative = members_authoritative
        self.ledger = Ledger()
        self.recent: dict[tuple[str, str], deque[Inbound]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._members_denied_until = 0.0
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
            gid = self.idmap.group_id(group_openid)
            extras = AUTHOR_EXTRAS.pop(event.id, {})
            card = str(extras.get("member_nickname") or "")
            for mention in extras.get("mentions") or []:  # 成员身份和名字唯一会出现的另一个地方
                mentioned = mention.get("member_openid")
                if mention.get("is_you"):
                    self.idmap.touch_member(group_openid, SELF, mention.get("member_role"))
                elif mentioned and not mention.get("bot"):
                    self.idmap.user_id(mentioned, mention.get("username"))
                    self.idmap.touch_member(group_openid, mentioned, mention.get("member_role"), mention.get("member_nickname") or mention.get("username"))
            uid = self.idmap.user_id(author.member_openid, author.username)
            self.idmap.touch_member(group_openid, author.member_openid, author.member_role, card or author.username, seen=True)
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
                # 和成员添加机器人一样，这个事件也可以回复：进群时说的话因此不算主动消息，
                # 而群主允许之前，群里是不许主动消息的。
                joined = Inbound(GROUP, event.group_openid, event.event_id, time.time(), event=True) if getattr(event, "event_id", None) else None
                return GroupIncreaseNoticeEvent(**common, notice_type="group_increase", sub_type="invite"), joined
            self.idmap.forget_group(event.group_openid)  # 不在的群一律清掉，不留记录
            return GroupDecreaseNoticeEvent(**common, notice_type="group_decrease", sub_type="kick_me"), None

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

    async def _post(self, kind: str, openid: str, part: Part) -> tuple[str, str | None]:
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

        rows：由 [按钮上的字, 点击后发出的指令] 组成的列表。插件通过 plugins/quick_buttons.py 请求，
        它只对本机器人这样做。mention：在群里开头 @ 这位成员（QQ 号或编号）。
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

        插件通过 plugins/quick_buttons.py 请求，它只对本机器人这样做。
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
            "member_count": len(self._members_known(row["gid"], row["openid"])),
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
        name = row["nickname"] or self._name_of(uid, None)
        return {
            "group_id": gid, "user_id": uid, "nickname": self._name_of(uid, None) if row["nickname"] else name, "card": row["nickname"] or "", "sex": "unknown", "age": 0, "area": "",
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
        row = self.idmap.member(group_openid, openid) if openid else None
        if row is None:
            kept = self.idmap.roster_member(gid, uid)
            if kept is None:
                raise ActionFailed(status="failed", retcode=1200, msg="member not found", wording="群成员不存在")
            return self._roster_info(gid, kept)
        return self._member_info(gid, row)

    async def _refresh_members(self, group_openid: str) -> int:
        """从平台读取成员名单存进编号映射表；返回列出的成员数。

        GET /v2/groups/{group_openid}/members，每页 30 个。这个权限是逐个机器人单独开通的（未开通时
        返回 11253），所以调用方会退而使用见过发言的成员。
        """
        url = self.qq.adapter.get_api_base().joinpath("v2", "groups", group_openid, "members")
        listed: set[str] = set()
        cursor = ""
        for _page in range(400):
            page = await self.qq._request(Request("GET", url, params={"cursor": cursor}))
            for member in (page or {}).get("members") or []:
                openid = member.get("member_openid")
                if not openid:
                    continue
                listed.add(openid)
                if member.get("bot"):
                    continue
                try:
                    joined = datetime.fromisoformat(str(member.get("joined_at"))).timestamp()
                except ValueError:
                    joined = 0
                self.idmap.user_id(openid, member.get("username"))
                self.idmap.touch_member(group_openid, openid, member.get("member_role"), None, joined)
            cursor = (page or {}).get("next_cursor") or ""
            if not cursor:
                break
        if listed:
            self.idmap.drop_members(group_openid, listed)
        return len(listed)

    async def group_info(self, group_openid: str) -> dict | None:
        """GET /v2/groups/{group_openid}/info：群名、人数等（没有群号）。群已解散或机器人已不在群里时返回 None。"""
        url = self.qq.adapter.get_api_base().joinpath("v2", "groups", group_openid, "info")
        try:
            return await self.qq._request(Request("GET", url)) or {}
        except QQActionFailed as e:
            if e.code == GROUP_GONE:
                return None
            raise

    async def check_group(self, openid: str) -> str:
        """取一个群的群名记下来，返回 "named"。平台说群不存在（已解散，或机器人已不在群里）时，
        和被移出一样清掉这个群并告诉各插件，返回 "gone"。查询失败时抛出异常。"""
        info = await self.group_info(openid)
        if info is None:
            self.leave_group(openid)
            return "gone"
        if name := str(info.get("group_name") or "").strip():
            self.idmap.note_group(openid, name=name)
        return "named"

    def leave_group(self, openid: str) -> None:
        """机器人已不在这个群：删掉它的记录，并像被移出时一样告诉各插件（它们各自清掉该群的数据）。"""
        gid = self.idmap.forget_group(openid)
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
            # 调用方要一份最新的完整名单，是拿它当谁已退群的证据（森空岛体检据此解绑）。原机器人在的群，
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
        try:
            if time.time() >= self._members_denied_until:
                await self._refresh_members(group_openid)
        except Exception as e:
            if getattr(e, "code", None) == MEMBERS_NOT_GRANTED:
                self._members_denied_until = time.time() + 6 * 3600
            logger.debug(f"official bridge: member list not refreshed ({getattr(e, 'code', type(e).__name__)}), using members seen so far")
        if no_cache and gid >= GROUP_BASE:
            return [self._member_info(gid, row) for row in self.idmap.members(group_openid)]
        return self._members_known(gid, group_openid)

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

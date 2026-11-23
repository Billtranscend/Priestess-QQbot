"""日志里不记聊天内容。

NoneBot 会为收到的每个事件记一行日志，消息事件的这一行末尾是完整的消息文字
（"[message.group.normal]: Message 12 from 34@[群:56] '...'"）。journal 会保留好几周，
日志里就会存着每个群的聊天副本。一个 loguru patcher 在任何 handler 看到这行之前把文字换成它的长度；
消息、发送者和群的编号保留，方便排查。适配器没有对应类的消息事件会把全部字段原样记下来，
里面同样有文字：这种行只保留事件名。官方适配器解析不了的事件也会把整个载荷原样记下来（聊天内容、
入群申请里的昵称和验证留言），异常信息里还会带着字段的值：这种行只保留事件名和异常的类型。
认不出类型的网关载荷同样会原样记下来：这种行不留内容。

最先加载，保证第一个事件到达前 patcher 已经就位。
"""

from __future__ import annotations

import re

from nonebot import logger
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Log privacy",
    description="日志里不记录聊天内容，只保留消息编号、发送者与群号。",
    usage="无指令，自动生效。",
    type="application",
)

EVENT = re.compile(r"^(?P<head>.*? \| \[message(?:_sent)?\.[\w.]*\]: )(?P<rest>.*)$", re.S)
UNPARSED = re.compile(r"^(?P<head>QQ \| Failed to parse event )(?P<rest>.*)$", re.S)
UNKNOWN = re.compile(r"^(?P<head>QQ \| Unknown payload from server: )")  # 只认适配器自己这一行：聊天文字和载荷里也会出现这句话
KIND = re.compile(r"\btype='([A-Z_]+)'")
DESCRIBED = re.compile(r"^(?P<who>Message -?\d+ from \S+) (?P<text>.*)$", re.S)


def _hide_text(record) -> None:
    if unknown := UNKNOWN.match(record["message"]):
        record["message"] = f"{unknown['head']}<payload omitted>"
        return
    if unparsed := UNPARSED.match(record["message"]):
        kinds = KIND.findall(unparsed["rest"])  # 载荷自己的 type 排在 data 之后：取最后一个
        error = record["exception"]
        why = f", {error.type.__name__}" if error and error.type else ""
        record["message"] = f"{unparsed['head']}{kinds[-1] if kinds else '?'} <payload omitted{why}>"
        record["exception"] = None
        return
    if (event := EVENT.match(record["message"])) is None:
        return
    if described := DESCRIBED.match(event["rest"]):
        record["message"] = f"{event['head']}{described['who']} <text, {len(described['text'])} chars>"
    else:
        record["message"] = f"{event['head']}<fields omitted>"


logger.configure(patcher=_hide_text)

"""群里不发多页结果前的预告。

多页结果的指令（/zmd抽卡记录、/mrfz抽卡记录、干员列表）在发结果之前先发一句预告（"…过多，将以多张图片形式发送"、
"…将按卡池类型分成多张图片发送"）。多页结果在群里本来就要发好几条，预告只会再占一次被动回复（桥接的 PASSIVE），
所以本插件在群里把它吞掉。私聊不处理，那里的预告照常发出。
"""

from __future__ import annotations

import re
from typing import Any

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.exception import MockApiException
from nonebot.matcher import current_event
from nonebot.plugin import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="Forward reply owner",
    description="群里不发「…过多，将以多张图片形式发送」「…将按卡池类型分成多张图片发送」这类预告。",
    usage="无指令，自动生效。",
    type="application",
)

NOTICE = re.compile(r"^.+?(?:过多，将以多张图片形式发送|将按卡池类型分成多张图片发送)$")


def _plain(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    try:
        return Message(message).extract_plain_text().strip()
    except Exception:
        return ""


@Bot.on_calling_api
async def _drop_notice(bot: Bot, api: str, data: dict[str, Any]) -> None:
    if api not in ("send_msg", "send_group_msg") or not isinstance(current_event.get(None), GroupMessageEvent):
        return
    if NOTICE.match(_plain(data.get("message"))):
        raise MockApiException({"message_id": 0})

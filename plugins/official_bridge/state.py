"""桥接各部分共用的东西：配置、编号映射表、各个桥接实例，以及当前在线的机器人。"""

from __future__ import annotations

import asyncio
import json

from nonebot import get_bots, get_driver, get_plugin_config, logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBot
from pydantic import BaseModel

require("nonebot_plugin_localstore")

import nonebot_plugin_localstore as store  # noqa: E402

from .bridge import Bridge, BridgeBot  # noqa: E402
from .idmap import IdMap  # noqa: E402
from .relay import Relay  # noqa: E402

try:
    from ..strict_command import strict  # noqa: E402
except ImportError:  # 单独加载时
    strict = None

class Config(BaseModel):
    official_bridge_primary: bool | None = None  # None：没有任何 OneBot 机器人连着时
    official_bridge_trusted_adders: list[str] = []
    official_bridge_takeover: str = "all"  # 已关联的群里官方机器人回应所有人（"all"）或只回应已关联成员（"members"）
    official_bridge_ears: bool = True  # 在两个机器人都在的已关联群里，原机器人收到的消息转给官方机器人
    official_bridge_legacy_sends: bool = False  # 官方机器人发不了的，是否允许原机器人代发
    official_bridge_relay_always: bool = True  # 原机器人也连着时，其他 OneBot 程序仍由官方机器人服务
    official_bridge_onebot_clients: list[dict] = []
    official_bridge_self_id: str | None = None
    official_bridge_uins: list[str] = []  # 其他账号看到的官方机器人 QQ 号（事先知道时填）
    official_bridge_shadow_unlinked: bool = True
    official_bridge_auto_link: bool = True
    official_bridge_mention_markup: bool = False
    official_bridge_members_authoritative: bool = False
    official_bridge_forward_stitch_height: int = 0  # 合并转发卡片的各页图片拼成不超过这个高度的长图；0：逐页发送


config = get_plugin_config(Config)
driver = get_driver()
DATA_DIR = store.get_plugin_data_dir()
idmap = IdMap(DATA_DIR / "idmap.sqlite3")
bridges: dict[str, Bridge] = {}
online: set[str] = set()  # 当前连着的官方机器人（AppID）
background: set[asyncio.Task] = set()  # 运行中的任务放在这里，免得被垃圾回收

def legacy_bots() -> list[OneBot]:
    return [b for b in get_bots().values() if isinstance(b, OneBot) and not isinstance(b, BridgeBot)]

# 往聊天里发东西的 API。其他带群号的调用都是只有 QQ 客户端才能完整回答的查询，
# 原机器人还在该群时就交给它去问。
SENDS = {
    "send_msg", "send_group_msg", "send_private_msg", "send_forward_msg", "send_group_forward_msg", "send_private_forward_msg",
    "upload_group_file", "upload_private_file",
    "send_card",  # 本桥接自有：文字下面带按钮（plugins/quick_buttons.py）
    "send_hints",  # 本桥接自有：私聊消息下面附带指令，点一下即可发送（plugins/quick_buttons.py）
}

# OneBot 一侧看到的官方机器人 QQ 号（自动学到的，或由 OFFICIAL_BRIDGE_UINS 给出）。
official_uins: set[str] = set(json.loads(idmap.meta("official_uins") or "[]")) | {str(uin) for uin in config.official_bridge_uins}

def learn_uin(uin: str) -> None:
    if uin not in official_uins:
        official_uins.add(uin)
        idmap.set_meta("official_uins", json.dumps(sorted(official_uins)))
        logger.info(f"official bridge: the official bot is {uin} on the OneBot side; its messages are left alone there")

relay = Relay(config.official_bridge_onebot_clients, lambda: next((b for q, b in bridges.items() if q in online), None))

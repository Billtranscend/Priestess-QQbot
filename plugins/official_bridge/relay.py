"""让其他 OneBot v11 程序也能用上官方机器人。

为 OneBot v11 编写的独立程序（这里是跑在自己容器里的 nonebot-bison）等着某个 OneBot 实现像 NapCat
那样通过反向 WebSocket 连上它。本模块就是桥接这一侧的连接：以桥接机器人的号码连接配置的地址，
转发桥接机器人处理的事件，并像执行桥接机器人自身插件的调用那样（bridge.py）执行对方发回的 API 调用。

对方程序那边什么都不用改：地址、access token、指令都和原来一样。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.exception import ActionFailed
from nonebot.matcher import current_event

from .bridge import Bridge


def plain(value: Any) -> Any:
    if isinstance(value, Message):
        return [{"type": seg.type, "data": dict(seg.data)} for seg in value]
    if isinstance(value, MessageSegment):
        return {"type": value.type, "data": dict(value.data)}
    return str(value)


def wire(event: Any) -> dict[str, Any]:
    """按 OneBot 实现的发送格式给出该事件（在处理函数去掉 @ 之前取）。"""
    if isinstance(event, MessageEvent):
        message = event.original_message
        out: dict[str, Any] = dict(
            time=event.time, self_id=event.self_id, post_type="message", message_type=event.message_type, sub_type=event.sub_type,
            message_id=event.message_id, user_id=event.user_id, message=plain(message), raw_message=str(message), font=0,
            sender={k: v for k, v in event.sender.dict().items() if v is not None},
        )
        if getattr(event, "group_id", None) is not None:
            out["group_id"] = event.group_id
        return out
    return {k: v for k, v in event.dict().items() if v is not None and k != "to_me"}


ANSWER_WINDOW = 30  # 秒：对方在成员消息之后这么久之内发到同一个群的，当作回答那条消息


class Relay:
    def __init__(self, clients: list[dict[str, Any]], bridge: Callable[[], Bridge | None]) -> None:
        self.clients = [c for c in clients if c.get("url")]
        self._bridge = bridge
        self._tasks: list[asyncio.Task] = []
        self._sockets: dict[str, Any] = {}
        self._pending: set[asyncio.Task] = set()
        self._asked: dict[int, tuple[float, MessageEvent]] = {}  # 群 -> 最近转给对方的一条成员消息（及时间）

    @property
    def active(self) -> bool:
        return bool(self._tasks)

    @property
    def connected(self) -> list[str]:
        return sorted(self._sockets)

    def start(self) -> None:
        if not self._tasks and self.clients:
            self._tasks = [asyncio.create_task(self._run(conf)) for conf in self.clients]

    def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    def forward(self, payload: dict[str, Any], event: Any = None) -> None:
        if isinstance(event, MessageEvent) and getattr(event, "group_id", None):  # 对方很可能就是在回答这条
            self._asked[int(event.group_id)] = (time.monotonic(), event)
            while len(self._asked) > 200:
                self._asked.pop(next(iter(self._asked)))
        for socket in list(self._sockets.values()):
            self._spawn(self._send(socket, payload))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    @staticmethod
    async def _send(socket: Any, payload: dict[str, Any]) -> None:
        try:
            await socket.send(json.dumps(payload, ensure_ascii=False, default=plain))
        except Exception as e:
            logger.debug(f"official bridge relay: frame not sent ({type(e).__name__})")

    async def _answer(self, socket: Any, frame: dict[str, Any]) -> None:
        bridge = self._bridge()
        action, echo = str(frame.get("action") or ""), frame.get("echo")
        action = action.removesuffix("_async").removesuffix("_rate_limited")
        try:
            if bridge is None:
                raise ActionFailed(status="failed", retcode=1400, msg="the official bot is not connected", wording="官方机器人未连接")
            params = frame.get("params") or {}
            # 对方（例如 Bison）回答群里成员的指令时，机器人这边没有“正在处理的消息”，只能主动发：
            # 不让主动发言的群就收不到。刚转过去的那条成员消息还新鲜，就当作在回答它（挂在它上面回复）。
            asked = self._asked.get(int(params.get("group_id") or 0)) if action.startswith("send_") else None
            token = current_event.set(asked[1]) if asked and time.monotonic() - asked[0] < ANSWER_WINDOW else None
            try:
                data = await bridge.adapter._call_api(bridge.bot, action, **params)
            finally:
                if token is not None:
                    current_event.reset(token)
            reply = {"status": "ok", "retcode": 0, "data": data, "echo": echo}
        except ActionFailed as e:
            reply = {"status": "failed", "retcode": e.info.get("retcode") or 1, "data": None, "msg": e.info.get("msg"), "wording": e.info.get("wording"), "echo": echo}
        await self._send(socket, reply)

    async def _run(self, conf: dict[str, Any]) -> None:
        import websockets

        url, wait = str(conf["url"]), 5.0
        while True:
            bridge = self._bridge()
            if bridge is None:
                await asyncio.sleep(5)
                continue
            headers = {"X-Self-ID": bridge.bot.self_id, "X-Client-Role": "Universal", "User-Agent": "OneBot/11 official-bridge"}
            if conf.get("access_token"):
                headers["Authorization"] = f"Bearer {conf['access_token']}"
            try:
                async with websockets.connect(url, additional_headers=headers, max_size=64 * 1024 * 1024, open_timeout=20) as socket:
                    self._sockets[url] = socket
                    wait = 5.0
                    logger.info(f"official bridge relay: connected to {url} as {bridge.bot.self_id}")
                    await self._send(socket, dict(time=int(time.time()), self_id=int(bridge.bot.self_id), post_type="meta_event", meta_event_type="lifecycle", sub_type="connect"))
                    async for raw in socket:
                        try:
                            frame = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(frame, dict) and frame.get("action"):
                            self._spawn(self._answer(socket, frame))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.info(f"official bridge relay: {url} not connected ({type(e).__name__}), trying again in {int(wait)} s")
            finally:
                self._sockets.pop(url, None)
            await asyncio.sleep(wait)
            wait = min(wait * 2, 60.0)

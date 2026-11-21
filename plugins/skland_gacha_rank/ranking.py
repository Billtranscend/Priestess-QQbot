"""两款游戏共用的欧非榜部分：怎么排名、谁上榜，以及在指令之间保存的数字。

排名：每个榜按欧气排（Luck），UP 少于 MIN_UP 个的人不参与排名。谁上榜、用什么名字：entries_of。

统计所有人意味着要读每一条保存的记录（成员多时有上百万行），一条指令根本吃不消。所以每个成员的
数字会保存下来（插件数据目录里每款游戏一个文件，以及内存里），连同算出它们所依据的东西：角色、
记录条数、最新一条记录。一条指令只向数据库要每个成员的这三项，只重算有变化的人。什么算 UP 取决于
卡池表，而卡池表随游戏更新变化：保存的数字带着卡池表的指纹，指纹不同时所有人都重算（而且每晚
无论如何重算一次）；卡池表没加载时什么都不算。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger
from nonebot_plugin_orm import get_session
from nonebot_plugin_skland.model import CharacterDefault, GachaRecord
from sqlalchemy import func, select

from plugins.shared import write_json

if TYPE_CHECKING:
    from .pictures import Look

MIN_UP = 4  # UP 少于这个数，样本太小，不参与排名（设成 2 时会让一对特别欧或特别非的人登顶）
NAMELESS = "未命名"  # 不知道名字的游戏角色
REFRESH_AFTER = 600  # 保存的数字用多少秒后，再去核对一次保存的记录
CHUNK = 4  # 一次读取并计算记录的成员数；中间机器人可以回应别人（设成 8 时每次要占用 0.85 秒）

Signature = tuple[int, int, int]  # 成员的数字是依据什么算的：默认角色、记录条数、最新一条记录
Kept = dict[int, tuple[Signature, Any]]  # 成员（机器人自己的用户 id）-> 数字所依据的东西，以及数字


@dataclass
class Shown:
    """榜上的一个人除了数字以外的部分；每款游戏的 Entry 是它和该游戏 Stat 的组合。"""

    ids: frozenset[str] = frozenset()  # 这个成员在聊天里被认出的那些号码
    name: str = NAMELESS
    local: bool = False  # 是显示榜单的这个群的成员


@dataclass(frozen=True)
class Board:
    pool: str  # 角色池 / 武器池 / 限定寻访
    command: str
    english: str
    basis: str
    target: str  # 一个 UP 指什么，带量词
    measure: str
    empty: str
    avg: Callable[[Any], float]
    count: Callable[[Any], int]

    def luck(self, entries: list) -> Luck:
        """欧气的两个基准，取自上榜（UP 不少于 MIN_UP）的所有人。"""
        eligible = [e for e in entries if self.count(e) >= MIN_UP]
        ups = sum(self.count(e) for e in eligible)
        if not ups:
            return Luck(0.0, 1.0)
        mean = sum(self.avg(e) * self.count(e) for e in eligible) / ups
        spread = (sum(self.count(e) * (self.avg(e) - mean) ** 2 for e in eligible) / len(eligible)) ** 0.5
        return Luck(mean, spread or 1.0)

    def ranked(self, entries: list) -> list:
        """最欧的在前，按欧气排（Luck）；UP 少于 MIN_UP 的不参与排名。"""
        luck = self.luck(entries)
        return sorted((e for e in entries if self.count(e) >= MIN_UP), key=lambda e: (-luck.of(self.avg(e), self.count(e)), -self.count(e)))


@dataclass(frozen=True)
class Luck:
    """欧气 =（全服平均 − 他的平均）× √UP 数 ÷ 正常波动。

    只按平均数排，两端全是 UP 刚够 MIN_UP 的人：UP 少，平均数本来就忽高忽低。乘上 √UP 数，二十个 UP
    一直比平均少用二十多抽，和四个 UP 平均少用六十抽一样难得；再除以正常波动，0 是平均水平，
    +2 以上已经很少见，+3 以上极少见，负数同理是非（抽卡有保底，两头并不对称，所以只说“很少见”，
    不说几里挑一）。同一个 UP 数时，顺序和按平均数排一样。
    """

    mean: float  # 上榜所有人合起来，平均多少抽出一个 UP
    spread: float  # 出一个 UP 要多少抽，平时上下差多少（由上榜所有人的数字估出）

    def of(self, avg: float, count: int) -> float:
        return (self.mean - avg) * count**0.5 / self.spread


def luck_text(value: float) -> str:
    """欧气写出来的样子：+2.4、−1.3（用真正的减号，和加号一样宽）；四舍五入后是零的写 0.0，不带正负号。"""
    text = f"{value:+.1f}"
    return "0.0" if text in ("+0.0", "-0.0") else text.replace("-", "−")


def entries_of(stats: dict[int, Any], ids: dict[int, set[str]], roles: dict[int, str], cards: dict[str, str], optout: set[str], entry: type) -> list:
    """要排名的成员：有数字、仍在绑定、且没有选择退出的所有人。

    ids：每个成员在聊天里被认出的号码；roles：每个成员的游戏角色名；
    cards：号码 -> 名字，用于显示榜单的这个群的成员（私聊里没有）；entry：这款游戏的 Entry。
    """
    entries = []
    for owner, stat in stats.items():
        known = ids.get(owner)
        if not known or known & optout or f"o{owner}" in optout:
            continue
        here = next((number for number in sorted(known) if number in cards), None)
        name = (cards[here] if here is not None else "") or (roles.get(owner) or "").strip() or NAMELESS
        entries.append(entry(**asdict(stat), ids=frozenset(known), name=name, local=here is not None))
    return entries


# ---------------------------------------------------------------- 所有人的数字，在指令之间保存


class NotReady(Exception):
    """卡池表还没加载：判断不了什么算 UP，所以什么都不算。"""


def load_snapshot(path: Path, version: int, stat: type) -> tuple[str, Kept]:
    """磁盘上保存的数字，以及计算时所依据的卡池表指纹；没有保存、或是按别的规则算的，就什么都不返回。"""
    try:
        saved = json.loads(path.read_text("utf-8"))
        if saved.get("version") != version:
            return "", {}
        return str(saved["pools"]), {int(owner): ((role, count, newest), stat(*numbers)) for owner, (role, count, newest, *numbers) in saved["members"].items()}
    except FileNotFoundError:
        return "", {}
    except Exception as e:  # 宁可重算也不信它
        logger.warning(f"Gacha leaderboard: the kept numbers in {path.name} could not be read ({type(e).__name__}); everyone is counted again")
        return "", {}


def save_snapshot(kept: Kept, pools: str, path: Path, version: int) -> None:
    members = {str(owner): [*signature, *asdict(stat).values()] for owner, (signature, stat) in kept.items()}
    write_json(path, {"version": version, "pools": pools, "members": members})


def stale(now: dict[int, Signature], kept: Kept) -> list[int]:
    """需要（重新）计算数字的成员：新成员，以及角色或记录有变化的成员。"""
    return [owner for owner, signature in now.items() if owner not in kept or kept[owner][0] != signature]


@dataclass(eq=False)
class Game:
    """一款游戏的欧非榜：从记录算数字的规则、它的榜、保存数字和退出名单的文件、图的样式和文字，
    以及保存的数字本身（kept 等，在指令之间留在内存里）。"""

    app_code: str  # 默认角色表（skland_character_default）里的游戏
    name: str  # 终末地 / 明日方舟
    code: str  # 图上的英文名
    command: str  # 总览的指令，也用来退出和加入
    boards: tuple[Board, ...]  # 第一个是主榜：上面一个人都没有时总览不出图
    stat: type  # 一个成员的数字
    entry: type  # 数字加上显示用的部分（Shown）
    count: Callable[[list[GachaRecord]], Any]  # 一个成员保存的记录 -> 他的数字
    pools_key: Callable[[], str]  # 卡池表所认定的 UP 的指纹；还没加载时为空
    snapshot: Path
    version: int  # 计算方法变了就加一，这样之前保存的数字都不会再用
    optout: Path  # 退出了这款游戏欧非榜的号码和人（o<成员 id>）
    look: Look
    basis: str  # 图的页脚第一段：数据从哪来、怎么算
    log: str  # 日志里的名字
    role_name: Callable[[str], str]  # 游戏角色名在榜上显示的样子
    kept_pools: str = field(default="", init=False)  # 数字计算时所依据的卡池表指纹
    kept: Kept = field(default_factory=dict, init=False)
    checked_at: float | None = field(default=None, init=False)  # 上次拿保存的数字核对记录时的 time.monotonic()
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self) -> None:
        self.kept_pools, self.kept = load_snapshot(self.snapshot, self.version, self.stat)

    def _of_default_roles(self, *columns):
        """对每个成员默认角色保存的记录做的查询。"""
        return (
            select(*columns)
            .join(GachaRecord, GachaRecord.character_id == CharacterDefault.character_id)
            .where(CharacterDefault.app_code == self.app_code)
        )

    async def refresh(self, everyone: bool = False) -> None:
        """让保存的数字跟上保存的记录；`everyone` 为真时所有成员都重算。调用时须持有 lock。"""
        pools = self.pools_key()
        if not pools:
            raise NotReady
        everyone = everyone or pools != self.kept_pools
        async with get_session() as session:
            rows = (
                await session.execute(
                    self._of_default_roles(CharacterDefault.owner_id, CharacterDefault.character_id, func.count(GachaRecord.id), func.max(GachaRecord.id))
                    .group_by(CharacterDefault.owner_id, CharacterDefault.character_id)
                )
            ).all()
        now: dict[int, Signature] = {owner: (role, count, newest) for owner, role, count, newest in rows}
        todo = list(now) if everyone else stale(now, self.kept)
        gone = [owner for owner in self.kept if owner not in now]
        for owner in gone:
            del self.kept[owner]
        started = time.monotonic()
        for at in range(0, len(todo), CHUNK):
            chunk = todo[at : at + CHUNK]
            async with get_session() as session:
                found = (await session.execute(self._of_default_roles(CharacterDefault.owner_id, GachaRecord).where(CharacterDefault.owner_id.in_(chunk)))).all()
            records: dict[int, list[GachaRecord]] = {}
            for owner, record in found:
                records.setdefault(owner, []).append(record)
            for owner, theirs in records.items():
                self.kept[owner] = (now[owner], self.count(theirs))
            await asyncio.sleep(0)  # 长时间的计算不能让机器人没法回应
        self.checked_at = time.monotonic()
        if todo or gone or pools != self.kept_pools:
            save_snapshot(self.kept, pools, self.snapshot, self.version)
        self.kept_pools = pools
        if len(todo) > CHUNK:
            logger.info(f"{self.log}: counted {len(todo)} of {len(now)} members in {time.monotonic() - started:.0f}s")

    async def stats(self) -> dict[int, Any]:
        """每个成员的数字；保存的超过 REFRESH_AFTER 秒时先核对一次。"""
        async with self.lock:
            if self.checked_at is None or time.monotonic() - self.checked_at > REFRESH_AFTER:
                await self.refresh()
            return {owner: stat for owner, (_, stat) in self.kept.items()}

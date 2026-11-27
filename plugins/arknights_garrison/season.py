"""卫戍协议本赛季的对局摘要：森空岛每次只给最近 100 局，读到的对局都存下来，常用策略、常用盟约、Boss 击杀和游戏时长按整个赛季算。

存在一个 SQLite 库里（每天的本机备份会带上它：记下的对局过了 100 局就再也读不回来）：
    games(角色号的哈希, 赛季, 对局 id, 开始, 结束, 策略, 完成, Boss, 隐秘 Boss, 盟约 [[盟约, 层数], ...], 版本)
只存这几样，不存阵容和名字。对局长期保存，换了赛季也留着（图上只统计当前赛季）；不再绑定的角色的对局由 prune 删掉
（每天统计卫戍榜之后）。

赛季按森空岛的 activityId 分（和官方生涯、上游 GarrisonView 相同）：规程序列I（2026-11 至 2027-05）是一个 activityId，
其间 2027-01 的协议更新换的是版本（versionId），所以每局记下版本，以后要按版本分开也还能分。
官方生涯里有本赛季用得最多的三个策略和场数（每天 10:00 刷新），这三个取官方场数和记录到的场数中较大的。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from nonebot import logger
from nonebot.compat import model_dump
from nonebot_plugin_skland.schemas.arknights.garrison import (
    GarrisonBondStat,
    GarrisonCareer,
    GarrisonRecord,
    GarrisonResource,
    GarrisonResources,
    GarrisonStrategyStat,
    GarrisonView,
    _band_item,
    _bond_item,
)

MAX_RECORDS = 5000  # 每个角色每赛季最多存的局数，多了丢最早的（序列I约半年，打得多的人一天十几局）
SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    role TEXT NOT NULL, activity TEXT NOT NULL, id TEXT NOT NULL,
    started INTEGER NOT NULL, ended INTEGER NOT NULL, band TEXT NOT NULL, done INTEGER NOT NULL,
    boss INTEGER NOT NULL, hidden INTEGER NOT NULL, bonds TEXT NOT NULL, version TEXT NOT NULL,
    PRIMARY KEY (role, activity, id)
) WITHOUT ROWID
"""


def role_key(uid: str) -> str:
    """库里记的角色：角色号的哈希，看不出是谁。"""
    return hashlib.sha256(f"garrison:{uid}".encode()).hexdigest()[:24]


def brief(record: GarrisonRecord) -> list:
    """一局的摘要。盟约只记激活了的，同一盟约记这一局里最高的层数（和上游 GarrisonView 的算法相同）。"""
    layers: dict[str, int] = {}
    for bond in record.bondInfo:
        if bond.activate and bond.bondId.strip():
            layers[bond.bondId] = max(layers.get(bond.bondId, bond.layer), bond.layer)
    killed = [boss for boss in record.acBossInfo if boss.isKilled]
    return [
        record.gameStartTs, record.gameEndTs, record.bandId.strip(), record.completeState,
        len(killed), sum(boss.isHidden for boss in killed), [[bond, layer] for bond, layer in layers.items()], record.versionId,
    ]


def _well_formed(entry) -> bool:
    """形状对的摘要（库被手改坏了的条目跳过）。"""
    return (isinstance(entry, list) and len(entry) == 8 and all(isinstance(entry[i], int) for i in (0, 1, 3, 4, 5))
            and isinstance(entry[2], str) and isinstance(entry[6], list) and isinstance(entry[7], str))


def _layers(entry: list) -> list[tuple[str, int]]:
    return [(pair[0], pair[1]) for pair in entry[6] if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str) and isinstance(pair[1], int)]


def _bonds(text: str) -> list:
    try:
        bonds = json.loads(text)
    except (TypeError, ValueError):
        return []
    return bonds if isinstance(bonds, list) else []


class History:
    """每个角色本赛季记录到的对局。读写都是同步的：几个查询同时到也不会交错。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA auto_vacuum = FULL")  # 只在建库时生效：删了旧赛季，文件跟着变小
        db.execute(SCHEMA)
        return db

    @staticmethod
    def _load(db: sqlite3.Connection, activity: str, role: str) -> dict[str, list]:
        rows = db.execute("SELECT id, started, ended, band, done, boss, hidden, bonds, version FROM games WHERE role = ? AND activity = ?", (role, activity))
        return {row[0]: [*row[1:7], _bonds(row[7]), row[8]] for row in rows}

    def load(self, activity: str, uid: str) -> dict[str, list]:
        if not self.path.is_file():
            return {}
        try:
            with closing(self._connect()) as db:
                return self._load(db, activity, role_key(uid))
        except sqlite3.Error as e:
            logger.warning(f"Arknights stronghold: season records not read ({type(e).__name__})")
            return {}

    def remember(self, activity: str, uid: str, records: list[GarrisonRecord]) -> dict[str, list]:
        """把这次读到的本赛季对局补进去，返回本赛季记录到的全部对局；库用不了时只返回这次读到的。"""
        fresh = {r.id: brief(r) for r in records if r.activityId == activity and r.id}
        role = role_key(uid)
        rows = [(role, activity, game_id, *entry[:6], json.dumps(entry[6], ensure_ascii=False), entry[7]) for game_id, entry in fresh.items()]
        try:
            with closing(self._connect()) as db, db:
                db.executemany("INSERT OR IGNORE INTO games VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
                count = db.execute("SELECT count(*) FROM games WHERE role = ? AND activity = ?", (role, activity)).fetchone()[0]
                if count > MAX_RECORDS:
                    db.execute("DELETE FROM games WHERE role = ? AND activity = ? AND id IN "
                               "(SELECT id FROM games WHERE role = ? AND activity = ? ORDER BY started LIMIT ?)",
                               (role, activity, role, activity, count - MAX_RECORDS))
                return self._load(db, activity, role)
        except sqlite3.Error as e:
            logger.warning(f"Arknights stronghold: season records not saved ({type(e).__name__})")
            return fresh

    def prune(self, uids: set[str]) -> int:
        """删掉不在 uids（还绑定着的明日方舟角色）里的角色的全部对局。
        uids 为空时什么也不删（多半是没读到角色表，不是真的没人了）。返回删掉了几个角色。"""
        if not uids or not self.path.is_file():
            return 0
        keep = {role_key(uid) for uid in uids}
        with closing(self._connect()) as db, db:
            gone = [role for (role,) in db.execute("SELECT DISTINCT role FROM games") if role not in keep]
            db.executemany("DELETE FROM games WHERE role = ?", [(role,) for role in gone])
        return len(gone)


@dataclass
class Season:
    games: int  # 记录到的局数
    bands: list[tuple[str, int, int, int]]  # (策略, 显示的场数, 记录到的场数, 其中完成的)，排好序
    bonds: list[tuple[str, int, int, int]]  # (盟约, 场数, 其中完成的, 最高层数)，排好序
    boss: int
    hidden: int
    seconds: int | None  # 没有一局有完整的起止时间时为 None


def _rate(count: int, wins: int) -> float:
    return wins / count if count else 0.0  # 没有记录到的：同场数时又因为没有最近一局，排在最后


def summarize(records: dict[str, list], official: dict[str, int] | None = None) -> Season:
    """按记录到的对局统计。场数多的在前，场数相同时完成率高的在前（策略再按最近用过的在前）。"""
    official = official or {}
    bands: dict[str, list[int]] = {}  # 策略 -> [场数, 完成, 最近一局的开始时间]
    bonds: dict[str, list[int]] = {}  # 盟约 -> [场数, 完成, 最高层数]
    boss = hidden = seconds = timed = 0
    entries = [entry for entry in records.values() if _well_formed(entry)]
    for entry in entries:
        start, end, band, done, killed, hidden_killed = entry[:6]
        done = int(done == 1)  # 和上游一样，只有 1 算完成
        if band:
            row = bands.setdefault(band, [0, 0, 0])
            row[0] += 1
            row[1] += done
            row[2] = max(row[2], start)
        for bond, layer in _layers(entry):
            row = bonds.setdefault(bond, [0, 0, layer])
            row[0] += 1
            row[1] += done
            row[2] = max(row[2], layer)
        boss += killed
        hidden += hidden_killed
        if 0 < start <= end:
            seconds += end - start
            timed += 1
    shown = [(band, max(count, official.get(band, 0)), count, wins, last) for band, (count, wins, last) in bands.items()]
    shown += [(band, count, 0, 0, 0) for band, count in official.items() if band and count > 0 and band not in bands]
    shown.sort(key=lambda r: (-r[1], -_rate(r[2], r[3]), -r[4]))
    bond_rows = sorted(((bond, count, wins, top) for bond, (count, wins, top) in bonds.items()), key=lambda r: (-r[1], -_rate(r[1], r[2]), -r[3]))
    return Season(len(entries), [r[:4] for r in shown], bond_rows, boss, hidden, seconds if timed else None)


def official_bands(career: GarrisonCareer) -> dict[str, int]:
    """官方生涯里本赛季用得最多的三个策略 -> 场数。"""
    pairs = [(career.top1BandUseId, career.top1BandUseCnt), (career.top2BandUseId, career.top2BandUseCnt), (career.top3BandUseId, career.top3BandUseCnt)]
    return {band.strip(): count for band, count in pairs if band.strip() and count}


def _duration(seconds: int) -> str:
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}时{minutes}分" if hours else f"{minutes}分"


def _listing(resources: GarrisonResources, activity: str, table: str, key: str) -> GarrisonResource:
    """写名字和图标用的资源表：当前版本的；协议更新后换下的策略、盟约当前版本里没有，用还有它的旧版本的。"""
    current = resources.current_resource(activity)
    if key in getattr(current, table):
        return current
    return next((resource for resource in resources.resources if key in getattr(resource, table)), current)


def season_view(view: GarrisonView, records: dict[str, list], career: GarrisonCareer, resources: GarrisonResources, activity: str) -> GarrisonView:
    """把上游按最近 100 局算的偏好、Boss 击杀和游戏时长换成按本赛季记录到的对局算的。"""
    season = summarize(records, official_bands(career))
    if not season.games and not season.bands:
        return view
    badges = {badge.id: badge for badge in career.bandBadges}
    strategies = [
        GarrisonStrategyStat(**{**model_dump(_band_item(_listing(resources, activity, "bandInfo", band), band, badge=badges.get(band))), "count": count},
                             recent_count=seen, recent_wins=wins)
        for band, count, seen, wins in season.bands
    ]
    bonds = [
        GarrisonBondStat(**{**model_dump(_bond_item(_listing(resources, activity, "bondInfo", bond), bond)), "count": count}, recent_count=count, max_layer=top)
        for bond, count, _, top in season.bonds
    ]
    scope = f"本赛季记录到 {season.games} 局" if season.games else "暂无战绩记录"
    metrics = []
    for metric in view.metrics:
        if metric.label == "Boss 击杀" and season.games:
            metric = metric.model_copy(update={"value": str(season.boss), "secondary_value": str(season.hidden), "detail": scope})
        elif metric.label == "游戏时长" and season.games:
            metric = metric.model_copy(update={"value": _duration(season.seconds) if season.seconds is not None else "未知", "detail": scope})
        metrics.append(metric)
    return view.model_copy(update={"strategies": strategies, "bonds": bonds, "metrics": metrics, "recent_count": season.games, "recent_scope": scope})

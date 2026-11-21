"""明日方舟欧非榜：只有限定寻访一个榜，明日方舟的图样式（ark_theme，和 /卫戍榜 一样）。

计入哪些卡池：只算 6★ UP 合计占 6★ 70% 的卡池（两个各 35%）：gachaRuleType 1 的限定寻访，外加 2020 年春节的
地生五金（它在表里记成 0）。UP 名单和比例取自卡池详情（gacha_details 的 upCharInfo 里 rarityRank 5 那一组，各自的
percent 相加；上游 _get_up_chars 在没有 upCharInfo 时拿 availCharInfo 充数，这里不用）。
其余卡池都不算：标准寻访（存下来的记录太少，凑够 4 个 UP 的没几个人）、中坚寻访、联动寻访、联合行动、常驻、
跨年欢庆、定向甄选、归航寻访、新人特惠寻访。
算进来的卡池里，不在这个卡池 UP 名单里的 6★ 都算歪，所以五倍权重的往期限定也是歪。

平均每个 UP 的抽数，和终末地一样（plugins/skland_limited.py 的 up_avg、weapon_up_avg）：
限定寻访的抽数，减去最后一个 UP 之后的抽数（当前垫抽，还没出 UP），除以 UP 个数。没出 UP 就结束的旧卡池，
抽数照算（已经花掉了）。限定寻访每期单独保底、不继承，和终末地的武器池一样，只扣所有限定寻访里最后一个 UP
之后的抽（这些抽若落在已经结束的一期，其实已经作废，也一并不算）。
免费的抽不算：只能去掉标了 is_free 的行，而森空岛的明日方舟记录不标免费寻访（限定寻访每天的免费单抽
和普通的抽分不开）。小黑盒导入的行（pos < 0；3、4 星是占位行）是真实的抽，照算；它们的 pos 随时间
递增，所以全部按（时间，pos）排先后。

卡池表或卡池详情没加载时什么都不算（NotReady）。上游的模型丢掉了 percent，所以 UP 名单和比例从上游
缓存的原始文件（gacha_details.json）读，文件变了才重读。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from nonebot import logger
from nonebot_plugin_skland.data_source import gacha_table_data
from nonebot_plugin_skland.model import GachaRecord
from nonebot_plugin_skland.services.gacha import group_gacha_records

from plugins import ark_theme

from .pictures import FULL_COLUMNS, Look
from .ranking import MIN_UP, Board, Shown

WIDTH = 1000
SCALE, QUALITY = 1.5, 80  # 和 /卫戍榜 一样：深色底上的小字，1.25x q65 发虚
SNAPSHOT_VERSION = 2  # 计算方法变了就加一，这样之前保存的数字都不会再用
SIX = 5  # 上游的稀有度从 0 算起
LIMITED_SHARE = 0.7  # 限定寻访：6★ UP 合计占 6★ 的比例
BASIS = (
    f"数据来自已保存的抽卡记录（每天 01:00 自动更新）；平均抽数 = 抽数 ÷ UP 数，没出 UP 就结束的卡池抽数照算，"
    f"最后一个 UP 之后的当前垫抽不算；至少出过 {MIN_UP} 个 UP 才参与排名<br>"
    "大多数人的记录是官方接口给的最近几个月，加上绑定后我每天保存的；导入过小黑盒的人才有更早的记录"
)


@dataclass
class Stat:
    """一个成员的数字，来自其默认明日方舟角色保存的记录。"""

    limited_avg: float
    limited_up: int
    limited_pulls: int


@dataclass
class Entry(Shown, Stat):
    """榜上的一个人。"""


LIMITED = Board(
    pool="限定寻访",
    command="/mrfz限定欧非榜",
    english="Limited",
    basis="限定寻访「平均多少抽出一个 UP 干员」，不在当期 UP 里的 6★（含往期限定）算歪",
    target="一个 UP 干员",
    measure="个",
    empty="还没有可统计的明日方舟限定寻访抽卡记录",
    avg=lambda e: e.limited_avg,
    count=lambda e: e.limited_up,
)


def role_name(nickname: str) -> str:
    """游戏里的名字去掉 # 后面的编号：带着编号，完整榜单的一格只放得下两三个字。"""
    return re.sub(r"#\d+$", "", nickname.strip())


# ---------------------------------------------------------------- 计入的卡池


_counted: tuple[tuple, dict[str, tuple[str, ...]]] | None = None  # （卡池表和详情文件的样子，计入的卡池）


def up_lists(raw: dict) -> dict[str, tuple[tuple[str, ...], float]]:
    """卡池 id -> （6★ UP 干员，它们合计的比例），只取 upCharInfo；没有 6★ UP 的卡池不在里面。"""
    found = {}
    for pool in raw.get("gachaPoolClient") or []:
        detail = ((pool.get("gachaPoolDetail") or {}).get("detailInfo") or {}).get("upCharInfo") or {}
        six = [group for group in detail.get("perCharList") or [] if group.get("rarityRank") == SIX]
        ups = tuple(char for group in six for char in group.get("charIdList") or [])
        if ups:
            found[pool["gachaPoolId"]] = (ups, sum(float(group.get("percent") or 0) * len(group.get("charIdList") or []) for group in six))
    return found


def counted() -> dict[str, tuple[str, ...]]:
    """计入欧非榜的卡池 -> 它的 6★ UP 干员；卡池表或卡池详情还没加载时为空。"""
    global _counted
    tables, path = gacha_table_data.gacha_table, gacha_table_data._details_path
    if not tables or not gacha_table_data.gacha_details:
        return {}
    try:
        info = path.stat()
        shape = (id(tables), len(tables), info.st_mtime_ns, info.st_size)
        if _counted is None or _counted[0] != shape:
            ups = up_lists(json.loads(path.read_text("utf-8")))
            pools = {}
            for table in tables:
                six, share = ups.get(table.gachaPoolId, ((), 0.0))
                if six and round(share, 2) == LIMITED_SHARE:
                    pools[table.gachaPoolId] = six
            _counted = (shape, pools)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        logger.warning(f"Arknights gacha leaderboard: the pool details could not be read ({type(e).__name__})")
        return {}
    return _counted[1]


def pools_key() -> str:
    """计入的卡池和它们的 UP 的指纹；卡池表还没加载时为空。"""
    pools = counted()
    if not pools:
        return ""
    said = sorted((pool_id, sorted(ups)) for pool_id, ups in pools.items())
    return hashlib.sha256(json.dumps(said).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- 一个成员的数字


def numbers(pools: list) -> tuple[float, int, int]:
    """一组卡池的（平均每个 UP 的抽数，UP 个数，抽数）；平均不含最后一个 UP 之后的抽，没有 UP 时为 0。"""
    pulls = sorted(
        ((group.gacha_ts, pull.pos, pull.rarity == SIX and pull.char_id in pool.up_six_chars) for pool in pools for group in pool.records for pull in group.pulls),
        key=lambda pull: pull[:2],
    )
    ups = sum(up for *_, up in pulls)
    if not ups:
        return 0.0, 0, len(pulls)
    tail = next(i for i, (*_, up) in enumerate(reversed(pulls)) if up)
    return (len(pulls) - tail) / ups, ups, len(pulls)


def stat_of(records: list[GachaRecord]) -> Stat:
    pools = counted()
    limited = []
    for pool in group_gacha_records([r for r in records if not r.is_free]).pools:
        if (ups := pools.get(pool.gachaPoolId)) is not None:
            pool.up_six_chars = list(ups)  # 上游在没有 upCharInfo 时用 availCharInfo，这里只认真正的 UP
            limited.append(pool)
    return Stat(*numbers(limited))


# ---------------------------------------------------------------- 图的样式：/卫戍榜 的部件，同样的排版


def head(code: str, title: str, subtitle: str, date: str) -> str:
    return (
        '<div class="ak-head"><span class="ak-corner tl"></span><span class="ak-corner br"></span>'
        f'<div class="code">DATE<b>{date}</b></div><div class="k">{code}</div><h1>{title}</h1><p>{subtitle}</p></div>'
    )


CSS = """
body { width: %dpx; padding: 24px; }
.ak-head { padding-right: 150px; }
.summary { display: flex; gap: 10px; margin-top: 16px; }
.summary div { flex: 1; position: relative; background: var(--ak-panel); border: 1px solid var(--ak-line); padding: 9px 14px 10px;
  font-size: 14px; font-weight: 700; color: var(--ak-sub); }
.summary div:before { content: ""; position: absolute; left: -1px; top: -1px; width: 26px; height: 3px; background: var(--ak-blue); }
.summary b { display: block; margin-top: 2px; font-family: var(--ak-num); font-size: 36px; line-height: 1.1; color: #fff; }
.summary div.hl b { color: var(--ak-yellow); }
.mine { position: relative; margin-top: 12px; padding: 11px 16px 11px 24px; background: #101012; border: 1px solid var(--ak-line); font-size: 18px; color: var(--ak-ink); }
.mine:before { content: ""; position: absolute; left: -1px; top: -1px; bottom: -1px; width: 6px; background: var(--ak-blue); }
.mine b { font-family: var(--ak-num); font-weight: 700; font-size: 23px; color: var(--ak-yellow); padding: 0 2px; }
.mine i { font-style: normal; font-family: var(--ak-num); font-weight: 600; font-size: 21px; color: #fff; }
.mine div + div { margin-top: 5px; }
.mine em { font-style: normal; color: var(--ak-sub); }
.pair { display: flex; gap: 14px; align-items: flex-start; }
.pair .ak-sec { flex: 1; min-width: 0; }
.unlucky > h3:before { background: var(--ak-red); }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; }
td { padding: 7px 12px; border-top: 1px solid var(--ak-line); font-size: 18px; white-space: nowrap; }
tr:first-child td { border-top: 0; }
td.rk { width: 50px; padding-right: 0; }
td.rk span { display: inline-block; width: 30px; height: 28px; line-height: 27px; text-align: center; font-family: var(--ak-num); font-weight: 700;
  font-size: 18px; color: var(--ak-sub); background: var(--ak-panel-2); border: 1px solid var(--ak-line-2); }
tr:nth-child(1) td.rk span { background: var(--ak-yellow); color: #111; border-color: var(--ak-yellow); }
tr:nth-child(2) td.rk span { background: #c9ced6; color: #111; border-color: #c9ced6; }
tr:nth-child(3) td.rk span { background: #c8794a; color: #fff; border-color: #c8794a; }
td.nm { color: #fff; font-weight: 700; }
td.nm div { display: flex; align-items: center; min-width: 0; }
td.nm em { font-style: normal; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.nm s, .grid s { text-decoration: none; flex: none; margin-left: 8px; padding: 0 6px; font-size: 12px; font-weight: 400; line-height: 1.5;
  color: #fff; background: var(--ak-blue-2); border: 1px solid var(--ak-blue); vertical-align: 2px; }
td.val { width: 104px; text-align: right; font-family: var(--ak-num); font-weight: 700; font-size: 25px; line-height: 1; color: #fff; }
td.val small { font-family: var(--ak-cjk); font-size: 13px; color: var(--ak-faint); font-weight: 400; margin-left: 3px; }
tr:nth-child(1) td.val b { color: var(--ak-yellow); }
td.cnt { width: 112px; text-align: right; font-size: 13px; color: var(--ak-faint); line-height: 1.25; }
td.cnt small { display: block; font-size: 12px; color: var(--ak-sub); }
td.cnt b { font-family: var(--ak-num); font-weight: 600; font-size: 18px; color: #fff; margin-right: 2px; }
tr.me td { background: rgba(28, 167, 230, .16); }
tr.me td:first-child { box-shadow: inset 4px 0 0 var(--ak-blue); }
.grid { display: grid; grid-auto-flow: column; grid-template-columns: repeat(%d, 1fr); gap: 0 18px; padding: 8px 16px 10px; }
.grid span { display: flex; align-items: center; gap: 10px; font-size: 17px; padding: 5px 6px; border-bottom: 1px solid var(--ak-line); overflow: hidden; color: #fff; }
.grid span.me { background: rgba(28, 167, 230, .22); box-shadow: inset 0 0 0 1px var(--ak-blue); }
.grid i { font-style: normal; font-family: var(--ak-num); font-weight: 600; font-size: 17px; color: var(--ak-faint); width: 30px; flex: none; text-align: right; }
.grid span.top i { color: var(--ak-yellow); font-weight: 700; }
.grid span.me i { color: #fff; }
.grid em { font-style: normal; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.grid s { margin-left: -3px; }
.grid u { margin-left: auto; text-decoration: none; font-size: 13px; color: var(--ak-sub); flex: none; }
.grid q { width: 52px; text-align: right; font-family: var(--ak-num); font-size: 14px; color: var(--ak-sub); flex: none; }
.grid q:before, .grid q:after { content: none; }
.grid b { width: 52px; text-align: right; font-family: var(--ak-num); font-weight: 700; font-size: 20px; flex: none; color: var(--ak-yellow); }
.how { margin-top: 16px; padding-bottom: 14px; }
.how .eq { padding: 8px 22px 0; font-size: 21px; color: #fff; }
.how math[display="block"] { display: block; text-align: left; margin: 0; }
.how mtext, .how mo, .how mn { font-family: var(--ak-cjk); }
.how p { margin: 12px 22px 0; font-size: 15px; line-height: 1.7; color: var(--ak-sub); }
.how .nums { margin: 10px 22px 0; display: flex; gap: 28px; font-size: 16px; color: var(--ak-sub); }
.how .nums b { font-family: var(--ak-num); font-size: 20px; color: var(--ak-yellow); }
.skip { margin-top: 16px; padding: 9px 16px; text-align: center; font-size: 16px; color: var(--ak-faint); background: var(--ak-panel-2); border: 1px dashed var(--ak-line-2); }
"""

LOOK = Look(
    css=ark_theme.css() + CSS % (WIDTH, FULL_COLUMNS),
    body="ak",
    sec="ak-sec",
    head=head,
    foot=ark_theme.foot,
    width=WIDTH,
    scale=SCALE,
    quality=QUALITY,
)

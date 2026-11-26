"""终末地欧非榜：限定角色池和武器池两个榜，终末地的图样式（ef_theme）。

统计沿用 Skland 自己的分组（group_ef_gacha_records）和 /zmd抽卡记录 的 UP 计算
（plugins/skland_limited.py：限定寻访按当期 UP，辉光庆典和重构寻访按每个限定干员），所以数字和
/zmd抽卡记录 一致。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from nonebot_plugin_skland.data_source import ef_gacha_pool_data
from nonebot_plugin_skland.model import GachaRecord
from nonebot_plugin_skland.services.gacha import group_ef_gacha_records

from plugins import ef_theme, skland_limited

from .pictures import FULL_COLUMNS, Look
from .ranking import MIN_UP, Board, Shown

WIDTH = 1000
SNAPSHOT_VERSION = 3  # 计算方法变了就加一，这样之前保存的数字都不会再用
BASIS = f"数据来自已保存的抽卡记录（每天 00:30–01:30 自动更新，不含免费十连和当前垫抽），至少出过 {MIN_UP} 个 UP 才参与排名"


@dataclass
class Stat:
    """一个成员的数字，来自其默认终末地角色保存的记录。"""

    up_avg: float
    up_count: int
    special_pulls: int
    weapon_avg: float
    weapon_up: int


@dataclass
class Entry(Shown, Stat):
    """榜上的一个人。"""


CHARACTERS = Board(
    pool="角色池",
    command="/角色欧非榜",
    english="Operator",
    basis="限定池（含庆典、重构寻访）「平均多少抽出一个 UP 干员」，免费十连出的 UP 不计",
    target="一个 UP 干员",
    measure="个",
    empty="还没有可统计的终末地限定池抽卡记录",
    avg=lambda e: e.up_avg,
    count=lambda e: e.up_count,
)
WEAPONS = Board(
    pool="武器池",
    command="/武器欧非榜",
    english="Weapon",
    basis="武器池「平均多少抽出一把 UP 武器」",
    target="一把 UP 武器",
    measure="把",
    empty="还没有可统计的终末地武器池抽卡记录",
    avg=lambda e: e.weapon_avg,
    count=lambda e: e.weapon_up,
)


def pools_key() -> str:
    """卡池表所认定的 UP 的指纹；还没加载时为空。"""
    tables = ef_gacha_pool_data.pool_table
    if not tables:
        return ""
    said = sorted((pool_id, str(table.pool_type), str(table.up6_name or ""), sorted(table.up_six_char_ids)) for pool_id, table in tables.items())
    return hashlib.sha256(json.dumps(said, ensure_ascii=False).encode()).hexdigest()[:16]


def stat_of(records: list[GachaRecord]) -> Stat:
    stats = group_ef_gacha_records(records)
    # 与 /zmd抽卡记录 用同样的 UP 查法；本地表里没有的武器池，每个 6★ 都算歪。
    for pool in stats.weapon_pools:
        if local_pool := ef_gacha_pool_data.get_pool(pool.pool_id):
            pool.up_six_chars = local_pool.up_six_char_ids
    special, others = skland_limited.split(stats)
    skland_limited.mark(special + others)
    return Stat(
        up_avg=skland_limited.up_avg(special, others) or 0.0,  # 没有付费 UP 时不上榜（付费 UP 少于 MIN_UP）
        up_count=skland_limited.paid_up_count(special + others),
        special_pulls=sum(p.paid_pulls for p in special + others),
        weapon_avg=skland_limited.weapon_up_avg(stats.weapon_pools),
        weapon_up=skland_limited.up_count(stats.weapon_pools),
    )


# ---------------------------------------------------------------- 图的样式

CSS = """
body { width: %dpx; padding: 26px; }
.num { font-family: var(--ef-num); font-variant-numeric: tabular-nums; }
.summary { display: flex; gap: 12px; margin-top: 16px; }
.summary div { flex: 1; padding: 10px 16px 12px; background: var(--ef-panel); border: 1px solid var(--ef-line); border-top: 3px solid var(--ef-ink);
  font-size: 16px; color: var(--ef-sub); }
.summary b { display: block; margin-top: 2px; font-family: var(--ef-num); font-weight: 700; font-size: 40px; line-height: 1.05; color: var(--ef-ink); }
.summary div.hl b { display: inline-block; padding: 0 8px; background: var(--ef-yellow); border: 1px solid var(--ef-ink); }
.mine { position: relative; margin-top: 12px; padding: 11px 16px 11px 24px; background: var(--ef-dark); color: #f4f4f0; font-size: 19px; }
.mine:before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 8px; background: var(--ef-yellow); }
.mine b { font-family: var(--ef-num); font-weight: 700; font-size: 24px; color: var(--ef-yellow); padding: 0 2px; }
.mine i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 22px; }
.mine div + div { margin-top: 5px; }
.mine em { font-style: normal; color: #b9b9b2; }
.pair { display: flex; gap: 14px; align-items: flex-start; }
.pair .ef-sec { flex: 1; min-width: 0; }
.unlucky > h3:before { background: var(--ef-ink); }
table { width: 100%%; border-collapse: collapse; table-layout: fixed; }
td { padding: 7px 12px; border-top: 1px solid var(--ef-line-2); font-size: 19px; white-space: nowrap; }
tr:first-child td { border-top: 0; }
td.rk { width: 48px; padding-right: 0; }
td.rk span { display: inline-block; width: 28px; height: 28px; line-height: 27px; text-align: center; font-family: var(--ef-num); font-weight: 700;
  font-size: 18px; background: var(--ef-panel-2); border: 1px solid var(--ef-line); }
tr:nth-child(1) td.rk span { background: var(--ef-yellow); border-color: var(--ef-ink); }
tr:nth-child(2) td.rk span { background: var(--ef-silver); color: var(--ef-ink); border-color: var(--ef-ink); } tr:nth-child(3) td.rk span { background: var(--ef-copper); color: #fff; border-color: var(--ef-ink); }
td.nm div { display: flex; align-items: center; min-width: 0; }
td.nm em { font-style: normal; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.nm s, .grid s { text-decoration: none; flex: none; margin-left: 7px; padding: 0 5px; font-size: 12px; font-weight: 400; line-height: 1.5;
  color: var(--ef-ink); background: var(--ef-panel); border: 1px solid var(--ef-ink); vertical-align: 2px; }
td.val { width: 104px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 25px; line-height: 1; }
td.val small { font-family: var(--ef-cjk); font-size: 14px; color: var(--ef-sub); font-weight: 400; margin-left: 3px; }
tr:nth-child(1) td.val b { font-weight: 700; background: var(--ef-yellow); padding: 0 4px; }
td.val b { font-weight: 700; }
td.cnt { width: 112px; text-align: right; font-size: 14px; color: var(--ef-sub); line-height: 1.25; }
td.cnt small { display: block; font-size: 12px; }
td.cnt b { font-family: var(--ef-num); font-weight: 600; font-size: 18px; color: var(--ef-ink); margin-right: 2px; }
tr.me td { background: rgba(255, 225, 0, .3); }
tr.me td.nm { font-weight: 700; }
tr.me td:first-child { box-shadow: inset 4px 0 0 var(--ef-ink); }
.grid { display: grid; grid-auto-flow: column; grid-template-columns: repeat(%d, 1fr); gap: 0 18px; padding: 8px 16px 10px; }
.grid span { display: flex; align-items: center; gap: 10px; font-size: 17px; padding: 5px 6px; border-bottom: 1px solid var(--ef-line-2); overflow: hidden; }
.grid span.me { background: var(--ef-yellow); box-shadow: inset 0 0 0 1px var(--ef-ink); }
.grid i { font-style: normal; font-family: var(--ef-num); font-weight: 600; font-size: 17px; color: var(--ef-sub); width: 30px; flex: none; text-align: right; }
.grid span.me i { color: var(--ef-ink); }
.grid em { font-style: normal; min-width: 0; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
.grid s { margin-left: -3px; }
.grid u { margin-left: auto; text-decoration: none; font-size: 13px; color: var(--ef-sub); flex: none; }
.grid q { width: 52px; text-align: right; font-family: var(--ef-num); font-size: 14px; color: var(--ef-sub); flex: none; }
.grid q:before, .grid q:after { content: none; }
.grid b { width: 52px; text-align: right; font-family: var(--ef-num); font-weight: 700; font-size: 20px; flex: none; }
.grid span.top i { color: var(--ef-ink); font-weight: 700; }
.how { margin-top: 16px; padding-bottom: 14px; }
.how .eq { padding: 8px 22px 0; font-size: 21px; }
.how math[display="block"] { display: block; text-align: left; margin: 0; }
.how mtext, .how mo, .how mn { font-family: var(--ef-cjk); }
.how p { margin: 12px 22px 0; font-size: 15px; line-height: 1.7; color: var(--ef-sub); }
.how .nums { margin: 10px 22px 0; display: flex; gap: 28px; font-size: 16px; }
.how .nums b { font-family: var(--ef-num); font-size: 20px; }
.skip { margin-top: 16px; padding: 9px 16px; text-align: center; font-size: 16px; color: var(--ef-sub); background: var(--ef-panel-2); border: 1px dashed var(--ef-line); }
"""

LOOK = Look(
    css=ef_theme.css() + CSS % (WIDTH, FULL_COLUMNS),
    body="ef",
    sec="ef-sec",
    head=lambda code, title, subtitle, date: ef_theme.head(code, title, subtitle, f"DATE<b>{date}</b>"),
    foot=ef_theme.foot,
    width=WIDTH,
    scale=ef_theme.PAGE_SCALE,
    quality=ef_theme.PAGE_QUALITY,
)

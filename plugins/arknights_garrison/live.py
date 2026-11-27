"""卫戍协议的奖杯和场次补到最新一局。

官方生涯的数字（奖杯、独立/联合的场次和完成局数）每天 10:00 刷新，只算到北京时间当天 0 点前结束的对局；
对局记录却是实时的。截止之后结束的对局从记录里补上：奖杯用最新一局打完后的数，场次和完成局数按模式加上。
截止时间用奖杯核对（截止前最后一局打完后的奖杯应当等于官方奖杯）：对不上就是官方今天还没刷新，往前一天再对；
都对不上就不补，照用官方数字。称号、六维、最佳记录、已获赞和官方前三策略没有逐局数据，仍按官方每天刷新。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nonebot_plugin_skland.schemas.arknights.garrison import GarrisonCareer, GarrisonMode, GarrisonRecord, GarrisonView

BEIJING = timezone(timedelta(hours=8))
REFRESH_HOUR = 10  # 官方每天几点刷新
DAY = 86400
MODES = {"独立": "SINGLE", "联合": "MULTI"}


def _games(records: list[GarrisonRecord], activity: str) -> list[GarrisonRecord]:
    """本赛季的对局，去重，按结束时间排好。"""
    unique = {r.id: r for r in records if r.activityId == activity and r.id}
    return sorted(unique.values(), key=lambda r: (r.gameEndTs, r.gameStartTs))


def official_cutoff(games: list[GarrisonRecord], trophies: int | None, now: float) -> int | None:
    """官方数字算到的时间（北京时间某天 0 点，按对局结束时间）；对不上奖杯时为 None。games 按结束时间排好。"""
    today = datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    cut = int(today.timestamp()) - (DAY if now < today.timestamp() + REFRESH_HOUR * 3600 else 0)
    for cutoff in (cut, cut - DAY):  # 过了 10 点官方还没刷新时，还是前一天的
        before = [game for game in games if game.gameEndTs < cutoff]
        expected = before[-1].trophyAfter if before else (games[0].trophyBefore if games else 0)
        if expected == (trophies or 0):
            return cutoff
    return None


def catch_up(view: GarrisonView, records: list[GarrisonRecord], career: GarrisonCareer, activity: str, now: float) -> GarrisonView:
    """奖杯、总场次、独立/联合的场次和完成局数补上官方截止之后结束的对局。"""
    games = _games(records, activity)
    cutoff = official_cutoff(games, career.totalTrophyCnt, now)
    later = [game for game in games if cutoff is not None and game.gameEndTs >= cutoff]
    if not later:
        return view
    modes = []
    for mode in view.modes:
        added = [game for game in later if game.modeType == MODES.get(mode.label)]
        total, wins = mode.total + len(added), mode.wins + sum(game.completeState == 1 for game in added)
        modes.append(GarrisonMode(label=mode.label, total=total, wins=wins, losses=total - wins, win_percent=100 * wins / total if total else None))
    updates = {"奖杯数量": str(later[-1].trophyAfter), "总模拟数": str(sum(mode.total for mode in modes))}
    metrics = [metric.model_copy(update={"value": updates[metric.label]}) if metric.label in updates else metric for metric in view.metrics]
    return view.model_copy(update={"modes": modes, "metrics": metrics})

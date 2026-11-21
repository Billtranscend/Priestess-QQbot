"""欧非榜的图：总览（每个榜前十和后十）和完整榜单，两款游戏同一套标记，各用自己的样式（Look）。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from .ranking import MIN_UP, Board, Luck, luck_text

if TYPE_CHECKING:
    from .ranking import Game

TOP = 10  # 总览里欧 / 非每张表的行数
FULL_COLUMNS = 3  # 完整榜单按列从上往下读
FULL_ENDS = 150  # 上榜人数超过这个数两倍的完整榜单，只从两端各列这么多人
HERE = "<s>本群</s>"


@dataclass(frozen=True)
class Look:
    """一款游戏的图的样式：主题的部件，加上本插件的 CSS（两款游戏的 CSS 用同样的类名）。"""

    css: str  # 整页的 CSS
    body: str  # <body> 的 class
    sec: str  # 分区面板的 class
    head: Callable[[str, str, str, str], str]  # (英文标题, 标题, 副标题, 日期) -> 标题板
    foot: Callable[[str], str]
    width: int
    scale: float
    quality: int


def _name(entry) -> str:
    """名字和「本群」标记；名字太长时只截名字（CSS 的 td.nm em），标记始终完整。"""
    return f"<div><em>{escape(entry.name)}</em>{HERE if entry.local else ''}</div>"


def _rows(board: Board, entries: list, luck: Luck, requester: str) -> str:
    """按显示的顺序；每张表从 1 数起。"""
    return "".join(
        f'<tr class="{"me" if requester in e.ids else ""}"><td class="rk"><span>{shown}</span></td><td class="nm">{_name(e)}</td>'
        f'<td class="val"><b>{board.avg(e):.1f}</b><small>抽</small></td>'
        f'<td class="cnt"><b>{board.count(e)}</b> {board.measure} UP<small>欧气 {luck_text(luck.of(board.avg(e), board.count(e)))}</small></td></tr>'
        for shown, e in enumerate(entries, 1)
    )


def _tables(look: Look, board: Board, ranked: list, requester: str) -> str:
    """某个榜最欧的十人和最非的十人，并排显示；上榜不到二十人时两张表各列一半，同一个人不会两边都有。"""

    luck = board.luck(ranked)
    half = min(TOP, -(-len(ranked) // 2))
    lucky, unlucky = ranked[:half], ranked[half:][::-1][:TOP]

    def table(title: str, kind: str, entries: list) -> str:
        return (
            f'<div class="{look.sec} {kind}"><h3>{board.pool} · {title}<small>{kind.upper()} · TOP {len(entries)}</small></h3>'
            f"<table>{_rows(board, entries, luck, requester)}</table></div>"
        )

    return f'<div class="pair">{table("欧皇榜", "lucky", lucky)}{table("非酋榜", "unlucky", unlucky)}</div>'


def _mine(board: Board, ranked: list, entries: list, requester: str) -> str:
    """一行字，写出查询者在某个榜上的名次；没有保存的记录时为空。"""
    if not any(requester in e.ids for e in entries):
        return ""
    label = f"你的{board.pool}排名："
    pos = next((i for i, e in enumerate(ranked, 1) if requester in e.ids), None)
    if pos is None:
        return f"<div>{label}<em>UP 不足 {MIN_UP} {board.measure}，暂未上榜</em></div>"
    me = ranked[pos - 1]
    luck = board.luck(ranked)
    mine = luck.of(board.avg(me), board.count(me))
    return (
        f"<div>{label}第 <b>{pos}</b> / <i>{len(ranked)}</i> 名，欧气 <b>{luck_text(mine)}</b>，"
        f"平均 <b>{board.avg(me):.1f}</b> 抽出{board.target}（共 <i>{board.count(me)}</i> {board.measure}）{_beats(board, ranked, luck, me, mine)}</div>"
    )


def _beats(board: Board, ranked: list, luck: Luck, me, mine: float) -> str:
    """“，比 x% 的人欧”：榜上其他人里欧气比查询者低的占比，保留一位小数（向下取）；榜上只有他一人时为空。"""
    others = [e for e in ranked if e is not me]
    if not others:
        return ""
    lower = sum(luck.of(board.avg(e), board.count(e)) < mine for e in others)
    tenths = lower * 1000 // len(others)
    return f"，比 <b>{tenths // 10}.{tenths % 10}%</b> 的人欧"


def _average(board: Board, ranked: list) -> float:
    """平均 UP 抽数：上榜所有人合起来算（总抽数 ÷ 总 UP 数），和欧气用的全服平均是同一个数。"""
    return board.luck(ranked).mean


# 公式用 MathML 排（浏览器自带，和 LaTeX 排出来的一样：分数线、随内容伸长的根号、上标）
def _t(words: str) -> str:
    return f"<mtext>{words}</mtext>"


def _o(sign: str) -> str:
    return f"<mo>{sign}</mo>"


def _sqrt(inside: str) -> str:
    return f"<msqrt><mrow>{inside}</mrow></msqrt>"


def _frac(top: str, bottom: str) -> str:
    return f"<mfrac><mrow>{top}</mrow><mrow>{bottom}</mrow></mfrac>"


def _math(body: str, block: bool = True) -> str:
    return f'<math{" display=\"block\"" if block else ""}><mrow>{body}</mrow></math>'


def _how(look: Look, boards: list[tuple[Board, Luck]]) -> str:
    """欧气怎么算：公式，以及这次用到的两个基准数，谁都能自己算一遍。"""
    numbers = "".join(
        f"<div>{b.pool}：全服平均 <b>{luck.mean:.1f}</b> 抽，正常波动 <b>{luck.spread:.1f}</b> 抽</div>" for b, luck in boards
    )
    return f"""<div class="{look.sec} how"><h3>欧气怎么算<small>HOW IT WORKS</small></h3>
<div class="eq">{_math(_t("欧气") + _o("=") + _frac(_o("(") + _t("全服平均") + _o("−") + _t("他的平均") + _o(")") + _o("×") + _sqrt(_t("UP 数")), _t("正常波动")))}</div>
<div class="eq">{_math(_t("全服平均") + _o("=") + _frac(_o("∑") + _t("每人的抽数"), _o("∑") + _t("每人的 UP 数")))}</div>
<div class="eq">{_math("<msup><mrow>" + _t("正常波动") + "</mrow><mn>2</mn></msup>" + _o("=") + _frac(_o("∑") + _t("UP 数") + _o("×") + "<msup><mrow>" + _o("(") + _t("全服平均") + _o("−") + _t("他的平均") + _o(")") + "</mrow><mn>2</mn></msup>", _t("上榜人数")))}</div>
<p>正常波动是从榜上所有人的数字算出来的：每个人的平均离全服平均差多少，平方后乘他的 UP 数，全部加起来除以上榜人数，得到的是正常波动的平方，开平方就是正常波动。
这样全体的欧气以 0 为中心、上下大约差 1：0 是平均水平，越大越欧，越小越非；+2 以上已经很少见，+3 以上极少见，负数同理。
乘上 {_math(_sqrt(_t('UP 数')), block=False)} 是因为 UP 越多，平均数越稳：二十个 UP 一直比平均少用二十多抽，和四个 UP 平均少用六十抽一样难得。</p>
<div class="nums">{numbers}</div></div>"""


def _page(game: Game, code: str, title: str, subtitle: str, body: str, note: str, how: str = "") -> str:
    """how：欧气算法那一块（_how），放在榜单之后、页脚之前。"""
    look = game.look
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{look.css}</style></head><body class="{look.body}">'
        + look.head(code, f"{game.name} · <em>{title}</em>", subtitle, f"{datetime.now(ZoneInfo('Asia/Shanghai')):%y-%m-%d}")
        + body
        + how
        + look.foot(
            f"{game.basis}<br>"
            f"榜单不分群：本群成员显示群名片并标有「本群」，其他人显示游戏内的角色名<br>"
            f"{note}不想上榜可发送 {game.command} 退出，重新加入发送 {game.command} 加入"
        )
        + "</body></html>"
    )


def render_overview(game: Game, entries: list, requester: str) -> str | None:
    """这款游戏的每个榜，各列前十和后十；主榜上一个能排名的人都没有时为 None。"""
    boards = [(b, ranked) for b in game.boards if (ranked := b.ranked(entries))]
    if not boards or boards[0][0] is not game.boards[0]:
        return None
    tiles = "".join(
        f'<div>{b.pool}上榜<b>{len(ranked)}</b></div><div class="hl">{b.pool}平均 UP 抽数<br><b>{_average(b, ranked):.1f}</b></div>'
        for b, ranked in boards
    )
    if len(boards) == 1:
        tiles += f"<div>有抽卡记录<b>{len(entries)}</b></div>"
    mine = "".join(_mine(b, ranked, entries, requester) for b, ranked in boards)
    return _page(
        game,
        f"{game.code} · Headhunting Luck Board",
        "欧非榜",
        f"所有绑定用户一起排名，不分群；按欧气排：平均多少抽出一个 UP，同时看出过多少个 UP；每个榜只列前 {TOP} 名和后 {TOP} 名",
        f'<div class="summary">{tiles}</div>'
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + "".join(_tables(game.look, b, ranked, requester) for b, ranked in boards),
        f"完整榜单发送 {' 或 '.join(b.command for b in game.boards)}；",
        _how(game.look, [(b, b.luck(ranked)) for b, ranked in boards]),
    )


def render_board(game: Game, board: Board, entries: list, requester: str) -> str | None:
    """一个榜：榜上所有人，或者超过 FULL_ENDS 两倍时只列两端；榜上没人能排名时为 None。"""
    ranked = board.ranked(entries)
    if not ranked:
        return None
    numbered = list(enumerate(ranked, 1))
    luck = board.luck(ranked)

    def section(title: str, part: list[tuple[int, object]], from_worst: bool = False) -> str:
        """part：（在整个榜上的名次，成员）。from_worst：非酋那一段，从最非的数起，第 1 就是最非的。"""
        shown = list(enumerate((e for _, e in part[::-1]), 1)) if from_worst else part
        cells = "".join(
            f'<span class="{"me" if requester in e.ids else "top" if n <= 3 else ""}"><i>{n}</i><em>{escape(e.name)}</em>{HERE if e.local else ""}'
            f"<u>{board.count(e)} {board.measure}</u><b>{board.avg(e):.1f}</b><q>{luck_text(luck.of(board.avg(e), board.count(e)))}</q></span>"
            for n, e in shown
        )
        rows = -(-len(part) // FULL_COLUMNS)
        return (
            f'<div class="{game.look.sec} rest"><h3>{title}<small class="cjk">名次 · 名字 · UP 数 · 平均抽数 · 欧气</small></h3>'
            f'<div class="grid" style="grid-template-rows: repeat({rows}, auto)">{cells}</div></div>'
        )

    if len(ranked) > 2 * FULL_ENDS:
        listing = (
            section(f"欧皇榜 · 前 {FULL_ENDS} 名", numbered[:FULL_ENDS])
            + f'<div class="skip">中间 {len(ranked) - 2 * FULL_ENDS} 人未列出</div>'
            + section(f"非酋榜 · 最非的 {FULL_ENDS} 名", numbered[-FULL_ENDS:], from_worst=True)
        )
    else:
        listing = section("完整榜单", numbered)
    mine = _mine(board, ranked, entries, requester)
    return _page(
        game,
        f"{game.code} · {board.english} Luck Board",
        f"{board.pool}欧非榜",
        f"所有绑定用户一起排名，不分群；按欧气排：{board.basis}，同时看出过多少个 UP",
        f"""<div class="summary">
    <div>参与排名<b>{len(ranked)}</b></div>
    <div>最欧<b>{board.avg(ranked[0]):.1f}</b></div>
    <div>最非<b>{board.avg(ranked[-1]):.1f}</b></div>
    <div class="hl">平均 UP 抽数<br><b>{_average(board, ranked):.1f}</b></div>
  </div>"""
        + (f'<div class="mine">{mine}</div>' if mine else "")
        + listing,
        f"前后十名总览发送 {game.command}；",
        _how(game.look, [(board, luck)]),
    )

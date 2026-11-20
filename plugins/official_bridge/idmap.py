"""官方机器人桥接的 OpenID <-> 编号映射。

官方 API 从不给出 QQ 号或群号：每个用户和群都是一个只对本机器人有意义的 OpenID。本项目的其他
部分（用户表、群插件的 JSON 文件、SUPERUSERS）都以编号为键，所以每个 OpenID 都分到一个编号：

- 首次见到时分配的*虚拟*编号（占位编号），取自 QQ 账号不可能用到的号段；或者
- OpenID 关联上之后，成员真实的 QQ 号 / 群真实的群号，此后存在该号下的数据重新归其所有。

单独存放在一个小 SQLite 文件里，主数据库因此不需要迁移。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# QQ 号和群号都是 32 位的；比这更大的数不会和真实号码冲突。
USER_BASE = 9 * 10**9
GROUP_BASE = 8 * 10**9
SELF = "@self"  # members 表中记录机器人自己在群里身份的那一行

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    openid TEXT PRIMARY KEY,
    uid INTEGER NOT NULL UNIQUE,
    linked INTEGER NOT NULL DEFAULT 0,
    nickname TEXT NOT NULL DEFAULT '',
    friend INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
    openid TEXT PRIMARY KEY,
    gid INTEGER NOT NULL UNIQUE,
    linked INTEGER NOT NULL DEFAULT 0,
    name TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    group_openid TEXT NOT NULL,
    openid TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    nickname TEXT NOT NULL DEFAULT '',
    joined_at REAL NOT NULL DEFAULT 0,
    last_seen REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (group_openid, openid)
);
CREATE TABLE IF NOT EXISTS roster (
    gid INTEGER NOT NULL,
    uid INTEGER NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    card TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'member',
    joined_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (gid, uid)
);
"""


class IdMap:
    on_link = None  # 关联完成后调用，参数为（成员原来的编号，成员现在的 QQ 号）

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._former: dict[int, str] = {}  # 本次运行中成员被认出前用的占位编号 -> 其 OpenID
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        if "linked_at" not in {row["name"] for row in self._db.execute("PRAGMA table_info(users)")}:
            self._db.execute("ALTER TABLE users ADD COLUMN linked_at REAL NOT NULL DEFAULT 0")
        have = {row["name"] for row in self._db.execute("PRAGMA table_info(groups)")}
        for column, kind in (("added_by", "TEXT NOT NULL DEFAULT ''"), ("hears_all", "INTEGER NOT NULL DEFAULT 0"), ("muted_at", "REAL NOT NULL DEFAULT 0")):
            if column not in have:
                self._db.execute(f"ALTER TABLE groups ADD COLUMN {column} {kind}")

    def close(self) -> None:
        self._db.close()

    # 用户

    def _next(self, table: str, column: str, base: int) -> int:
        """一个从未分配过的占位编号。

        持有者关联到真实号码后空出来的编号不能再给别人：原持有者存在它下面的东西（用户表里的
        绑定、订阅）会变成新来者的。所以记下最后分配的编号，而不是根据当前谁持有什么来推算。
        """
        key = f"last_{table}_{column}"
        row = self._db.execute(f"SELECT MAX({column}) FROM {table} WHERE {column} > ?", (base,)).fetchone()
        number = max(row[0] or base, int(self.meta(key) or 0)) + 1
        self.set_meta(key, str(number))
        return number

    def user_id(self, openid: str, nickname: str | None = None, friend: bool = False) -> int:
        row = self._db.execute("SELECT uid, nickname, friend FROM users WHERE openid = ?", (openid,)).fetchone()
        if row is None:
            uid = self._next("users", "uid", USER_BASE)
            self._db.execute(
                "INSERT INTO users (openid, uid, nickname, friend, updated_at) VALUES (?, ?, ?, ?, ?)",
                (openid, uid, nickname or "", int(friend), time.time()),
            )
            return uid
        if (nickname and nickname != row["nickname"]) or (friend and not row["friend"]):
            self._db.execute(
                "UPDATE users SET nickname = ?, friend = MAX(friend, ?), updated_at = ? WHERE openid = ?",
                (nickname or row["nickname"], int(friend), time.time(), openid),
            )
        return row["uid"]

    def user_openid(self, uid: int) -> str | None:
        row = self._db.execute("SELECT openid FROM users WHERE uid = ?", (int(uid),)).fetchone()
        return row["openid"] if row else None

    def user(self, uid: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM users WHERE uid = ?", (int(uid),)).fetchone()

    def user_by_openid(self, openid: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM users WHERE openid = ?", (openid,)).fetchone()

    def is_linked(self, openid: str) -> bool:
        row = self.user_by_openid(openid)
        return bool(row and row["linked"])

    def link_user(self, openid: str, qq: int) -> int | None:
        """给 `openid` 设定真实号码 `qq`。返回它之前的编号（新出现的为 None）。

        如果该号码原来由另一个 OpenID 持有，那个 OpenID 改用一个新的虚拟编号：一个 QQ 号同一时间
        只属于一个 OpenID，以最新的证明为准。
        """
        qq = int(qq)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            holder = self._db.execute("SELECT openid FROM users WHERE uid = ?", (qq,)).fetchone()
            if holder is not None and holder["openid"] != openid:
                self._db.execute(
                    "UPDATE users SET uid = ?, linked = 0, updated_at = ? WHERE openid = ?",
                    (self._next("users", "uid", USER_BASE), time.time(), holder["openid"]),
                )
            row = self._db.execute("SELECT uid FROM users WHERE openid = ?", (openid,)).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO users (openid, uid, linked, updated_at, linked_at) VALUES (?, ?, 1, ?, ?)", (openid, qq, time.time(), time.time())
                )
            else:
                self._db.execute(
                    "UPDATE users SET uid = ?, linked = 1, updated_at = ?, linked_at = ? WHERE openid = ?", (qq, time.time(), time.time(), openid)
                )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        before = row["uid"] if row else None
        if before is not None and before != qq and before >= USER_BASE:
            self._former[before] = openid
        if before is not None and before != qq and self.on_link is not None:
            self.on_link(before, qq)
        return before

    def former_openid(self, uid: int) -> str | None:
        """本次运行中，这个占位编号在被认出之前属于谁。

        回复某条消息时如果发送者恰好被认出，那条消息带的仍是原来的编号。占位编号不会重复分配，
        所以这条回复只可能是给这个人的。
        """
        return self._former.get(int(uid))

    def unlink_user(self, qq: int) -> bool:
        row = self._db.execute("SELECT openid FROM users WHERE uid = ? AND linked = 1", (int(qq),)).fetchone()
        if row is None:
            return False
        self._db.execute(
            "UPDATE users SET uid = ?, linked = 0, updated_at = ? WHERE openid = ?",
            (self._next("users", "uid", USER_BASE), time.time(), row["openid"]),
        )
        return True

    def linked_at(self, openid: str) -> float | None:
        """这个 OpenID 何时关联到它的 QQ 号；尚未关联时为 None。"""
        row = self.user_by_openid(openid)
        return float(row["linked_at"]) if row and row["linked"] else None

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def friends(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM users WHERE friend = 1").fetchall()

    def unfriend(self, openid: str) -> None:
        """成员删掉了机器人好友：不再算作好友（森空岛体检据此判断只在私聊里用机器人的人还在不在）。"""
        self._db.execute("UPDATE users SET friend = 0, updated_at = ? WHERE openid = ? AND friend = 1", (time.time(), openid))

    # 群

    def group_id(self, openid: str) -> int:
        row = self._db.execute("SELECT gid, active FROM groups WHERE openid = ?", (openid,)).fetchone()
        if row is None:
            gid = self._next("groups", "gid", GROUP_BASE)
            self._db.execute("INSERT INTO groups (openid, gid, updated_at) VALUES (?, ?, ?)", (openid, gid, time.time()))
            return gid
        if not row["active"]:
            self.set_group_active(openid, True)
        return row["gid"]

    def group_openid(self, gid: int) -> str | None:
        row = self._db.execute("SELECT openid FROM groups WHERE gid = ?", (int(gid),)).fetchone()
        return row["openid"] if row else None

    def group(self, gid: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM groups WHERE gid = ?", (int(gid),)).fetchone()

    def groups(self, active_only: bool = True) -> list[sqlite3.Row]:
        where = " WHERE active = 1" if active_only else ""
        return self._db.execute(f"SELECT * FROM groups{where} ORDER BY gid").fetchall()

    def note_group(self, openid: str, *, added_by: str | None = None, hears_all: bool | None = None, name: str | None = None) -> None:
        """记录谁把机器人拉进群、该群是否把所有消息都推送给机器人（而不只是 @ 它的），
        以及群名（向平台查到的，或并存时从原机器人那里抄的）。"""
        if name is not None:
            self._db.execute("UPDATE groups SET name = ? WHERE openid = ?", (name, openid))
        if added_by is not None:
            self._db.execute("UPDATE groups SET added_by = ? WHERE openid = ?", (added_by, openid))
        if hears_all is not None:
            self._db.execute("UPDATE groups SET hears_all = ? WHERE openid = ?", (int(hears_all), openid))

    def set_group_muted(self, openid: str, muted: bool) -> None:
        """该群是否拒收官方机器人的主动消息（不拒收时为 0，否则为最近一次拒收的时间）。"""
        self._db.execute("UPDATE groups SET muted_at = ? WHERE openid = ?", (time.time() if muted else 0, openid))

    def group_by_openid(self, openid: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM groups WHERE openid = ?", (openid,)).fetchone()

    def set_group_active(self, openid: str, active: bool) -> None:
        self._db.execute("UPDATE groups SET active = ?, updated_at = ? WHERE openid = ?", (int(active), time.time(), openid))

    def link_group(self, openid: str, gid: int) -> None:
        gid = int(gid)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            holder = self._db.execute("SELECT openid FROM groups WHERE gid = ?", (gid,)).fetchone()
            if holder is not None and holder["openid"] != openid:
                self._db.execute(
                    "UPDATE groups SET gid = ?, linked = 0, updated_at = ? WHERE openid = ?",
                    (self._next("groups", "gid", GROUP_BASE), time.time(), holder["openid"]),
                )
            self._db.execute(
                "INSERT INTO groups (openid, gid, linked, updated_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(openid) DO UPDATE SET gid = excluded.gid, linked = 1, updated_at = excluded.updated_at",
                (openid, gid, time.time()),
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    # 群里见过的成员

    seen_every = 600.0  # 已在表里的成员再发言时，last_seen 最多隔这么多秒重写一次（读它的只有成员信息里的 last_sent_time）

    def touch_member(self, group_openid: str, openid: str, role: str | None = None, nickname: str | None = None, joined_at: float | None = None, seen: bool = False) -> None:
        """记录群里的一个成员：`seen` 表示刚发过言，否则表示来自平台给出的名单。

        成员是新的，或名字、身份、入群时间有变化，立刻写。只是又发了一次言时按 seen_every 节流：
        每条群消息都走到这里，而每次写入都要等硬盘确认。
        """
        row = self._db.execute(
            "SELECT role, nickname, joined_at, last_seen FROM members WHERE group_openid = ? AND openid = ?", (group_openid, openid)
        ).fetchone()
        if row is not None:
            unchanged = (
                (role is None or role == row["role"])
                and (not nickname or nickname == row["nickname"])
                and (not joined_at or joined_at <= 0 or joined_at == row["joined_at"])
            )
            if unchanged and (not seen or time.time() - (row["last_seen"] or 0) < self.seen_every):
                return
        last_seen = time.time() if seen else 0
        self._db.execute(
            "INSERT INTO members (group_openid, openid, role, nickname, joined_at, last_seen) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(group_openid, openid) DO UPDATE SET "
            "role = COALESCE(?, role), nickname = COALESCE(NULLIF(?, ''), nickname), "
            "joined_at = CASE WHEN ? > 0 THEN ? ELSE joined_at END, last_seen = MAX(last_seen, ?)",
            (group_openid, openid, role or "member", nickname or "", joined_at or 0, last_seen,
             role, nickname or "", joined_at or 0, joined_at or 0, last_seen),
        )

    def member(self, group_openid: str, openid: str) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM members WHERE group_openid = ? AND openid = ?", (group_openid, openid)).fetchone()

    def members(self, group_openid: str) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM members WHERE group_openid = ? AND openid != ?", (group_openid, SELF)).fetchall()

    # 另一个机器人最后一次给出的某群完整成员名单，留到那个机器人离开该群后使用

    def save_roster(self, gid: int, members: list[dict]) -> None:
        rows = [
            (int(gid), int(m["user_id"]), str(m.get("nickname") or ""), str(m.get("card") or ""), str(m.get("role") or "member"), float(m.get("join_time") or 0))
            for m in members if m.get("user_id")
        ]
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM roster WHERE gid = ?", (int(gid),))
            self._db.executemany("INSERT OR REPLACE INTO roster (gid, uid, nickname, card, role, joined_at) VALUES (?, ?, ?, ?, ?, ?)", rows)
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"roster_taken:{int(gid)}", str(time.time()))
            )
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise

    def forget_group(self, openid: str) -> int | None:
        """机器人不在这个群了（被移出或群已解散）：删掉关于它的一切，返回它原来的编号。
        再被拉进来时，它会是一个全新的群。"""
        row = self.group_by_openid(openid)
        if row is None:
            return None
        gid = int(row["gid"])
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._db.execute("DELETE FROM members WHERE group_openid = ?", (openid,))
            self._db.execute("DELETE FROM roster WHERE gid = ?", (gid,))
            self._db.execute("DELETE FROM meta WHERE key IN (?, ?)", (f"roster_taken:{gid}", f"roster_final:{gid}"))
            self._db.execute("DELETE FROM groups WHERE openid = ?", (openid,))
            self._db.execute("COMMIT")
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return gid

    def roster(self, gid: int) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM roster WHERE gid = ?", (int(gid),)).fetchall()

    def roster_member(self, gid: int, uid: int) -> sqlite3.Row | None:
        return self._db.execute("SELECT * FROM roster WHERE gid = ? AND uid = ?", (int(gid), int(uid))).fetchone()

    def roster_taken(self, gid: int) -> float:
        return float(self.meta(f"roster_taken:{int(gid)}") or 0)

    def roster_final(self, gid: int) -> bool:
        """给出名单的机器人离开该群后为 True：此后不会再有新的名单。"""
        return self.meta(f"roster_final:{int(gid)}") is not None

    def set_roster_final(self, gid: int, final: bool) -> None:
        if final:
            self.set_meta(f"roster_final:{int(gid)}", str(time.time()))
        else:
            self._db.execute("DELETE FROM meta WHERE key = ?", (f"roster_final:{int(gid)}",))

    def drop_members(self, group_openid: str, keep: set[str]) -> None:
        for row in self.members(group_openid):
            if row["openid"] not in keep:
                self._db.execute("DELETE FROM members WHERE group_openid = ? AND openid = ?", (group_openid, row["openid"]))

"""持久化的、不含凭据的任务队列。不导入 NoneBot。"""
from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Collection
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
LOGIN_EXPIRED = "login_expired"  # 同步结果：每晚的同步因登录已过期而失败


def weekly_turn(owner_id: int, day: date) -> bool:
    """每个成员每周轮到一天（按 owner_id 错开）。"""
    return owner_id % 7 == day.toordinal() % 7


def login_expired(exc: BaseException) -> bool:
    """换取授权时森空岛明确说登录已过期：重新扫码之前不会好。限流、超时和其他环节的错误都不算。"""
    said = str(exc)
    return "使用token获得认证代码失败" in said and "登录已过期" in said


DAY_STARTS = (0, 30)  # 每晚同步的时刻（北京时间，时、分）：两款游戏的 TRIGGER 和这里的“一晚”都从这里算


def daily_slot(now: datetime) -> str:
    local = now.astimezone(TZ)
    last = local.replace(hour=DAY_STARTS[0], minute=DAY_STARTS[1], second=0, microsecond=0)
    if last > local:
        last -= timedelta(days=1)
    return last.isoformat()


def _new_night(stored: str, now: datetime) -> bool:
    """存着的那一晚已经过去。按时间比，不按字符串：时刻改早以后，旧时刻记下的这一晚（如今天 01:00）
    还在新的这一晚（今天 00:30）之后，不算又到了一晚，不会在白天多同步一次。读不懂的记录算过去了。"""
    try:
        return datetime.fromisoformat(stored) < datetime.fromisoformat(daily_slot(now))
    except ValueError:
        return True


class Queue:
    def __init__(self, path: Path):
        self.path = path
        if path.is_symlink() or path.parent.is_symlink():
            raise RuntimeError("unsafe queue path")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if path.stat().st_uid != os.getuid():
            raise RuntimeError("queue owner mismatch")
        path.chmod(0o600)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pending (
                    user_id INTEGER PRIMARY KEY, generation INTEGER NOT NULL,
                    reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS results (
                    user_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
                    updated REAL NOT NULL, records INTEGER NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _enqueue(db, user_id, reason):
        db.execute("""INSERT INTO pending(user_id,generation,reason) VALUES (?,1,?)
            ON CONFLICT(user_id) DO UPDATE SET generation=generation+1,
            reason=excluded.reason, attempts=0, due=0""", (user_id, reason))

    def enqueue(self, user_id: int, reason="bind"):
        with self.connection() as db:
            self._enqueue(db, user_id, reason)

    def initialize_day(self, now: datetime):
        # 安装时不会立刻为所有已有账号发起请求。
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO meta VALUES ('day',?)", (daily_slot(now),))

    def day_due(self, now: datetime) -> bool:
        with self.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key='day'").fetchone()
        return row is not None and _new_night(row[0], now)

    def enqueue_day(self, now: datetime, users: list[int]) -> int:
        """排入这一晚的同步，返回排了多少人。登录已过期的成员只在每周轮到的那天排。"""
        today = now.astimezone(TZ).date()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key='day'").fetchone()
            if row and not _new_night(row[0], now):
                return 0
            expired = {uid for uid, in db.execute("SELECT user_id FROM results WHERE status=?", (LOGIN_EXPIRED,))}
            queued = [uid for uid in users if uid not in expired or weekly_turn(uid, today)]
            for uid in queued:
                self._enqueue(db, uid, "daily")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('day',?)", (daily_slot(now),))
            return len(queued)

    def next(self, busy: Collection[int] = ()):
        """下一个到期的任务项；busy 里的成员正被别的 worker 处理，跳过。"""
        busy = list(busy)
        skip = f"AND user_id NOT IN ({','.join('?' * len(busy))})" if busy else ""
        with self.connection() as db:
            return db.execute(f"""SELECT user_id,generation,reason,attempts FROM pending
                WHERE due<=? {skip} ORDER BY CASE reason WHEN 'bind' THEN 0 ELSE 1 END, rowid LIMIT 1""",
                (time.time(), *busy)).fetchone()

    def waiting(self, user_id: int, reason: str = "bind") -> bool:
        """该用户的这类同步已在排队或正在运行，且尚未失败。"""
        with self.connection() as db:
            return db.execute("SELECT 1 FROM pending WHERE user_id=? AND reason=? AND attempts=0",
                              (user_id, reason)).fetchone() is not None

    def finish(self, item, status: str, records=0, retry=False):
        uid, generation, _, attempts = item
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?)",
                       (uid, status, time.time(), records))
            if retry and attempts < 2:
                db.execute("""UPDATE pending SET attempts=attempts+1,due=?
                    WHERE user_id=? AND generation=?""",
                    (time.time() + (300 if attempts == 0 else 1800), uid, generation))
            else:
                db.execute("DELETE FROM pending WHERE user_id=? AND generation=?", (uid, generation))

    def fail(self, item, exc: BaseException) -> tuple[str, bool]:
        """记下一次失败的同步，返回（记下的状态，是不是最终结果）。

        每晚同步时登录已过期的不重试；绑定后的同步一律照常重试。
        """
        uid, _, reason, attempts = item
        if reason == "daily" and login_expired(exc):
            self.finish(item, LOGIN_EXPIRED)
            return LOGIN_EXPIRED, not self.waiting(uid)  # 期间重新扫了码：等绑定后的那次同步
        self.finish(item, "failed", retry=True)
        return "failed", attempts >= 2


def record_values(character_id, record):
    return dict(
        character_id=character_id, item_type=record.item_type,
        pool_id=record.poolId, pool_name=record.poolName,
        char_id=record.item_id, char_name=record.item_name,
        rarity=record.rarity, is_new=record.isNew, is_free=record.is_free_pull,
        gacha_ts=record.gacha_ts_sec, pos=record.seq_id_int, pool_version=getattr(record, "poolVersion", None),
    )

"""跟踪服务的数据库。只存本服务的数据，不连接其他服务的数据库。

权限检查由 TrackingService 负责，这里只管读写。
写事务里不能做网络调用，否则会长时间占着写锁。
现在用 SQLite 做本地开发，正式环境计划换成 PostgreSQL。

表的含义：
    cursors / inbox          已处理到哪个事件、处理过哪些事件（防重复、防跳号）
    commands                 幂等键 → 首次处理的结果
    decisions(_history)      跟进决定及其历史
    watches / watch_history  关注设置及其历史
    changes                  检测到的变化
    reassessments(_history)  自动复核命令及其状态历史
    notifications(_reads)    站内提醒，以及每个人的已读记录
历史类的表只能插入，不能修改或删除（用触发器保证）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .diff import canonical, fingerprint


class TrackingError(Exception):
    """跟踪服务的业务错误。code 是错误码，status 是对应的 HTTP 状态码。"""

    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def text(value, limit=128, *, empty=False):
    """检查是合法的字符串：不超长、不含空字符；empty=False 时不能全是空白。"""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or "\x00" in value:
        raise TrackingError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise TrackingError("invalid_input") from None
    return value


def integer(value, minimum=0, maximum=2**63 - 1):
    """检查是范围内的整数（bool 不算）。"""
    if type(value) is not int or not minimum <= value <= maximum:
        raise TrackingError("invalid_version")
    return value


def identifier(value):
    """检查是 64 位十六进制的公告 ID。"""
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise TrackingError("invalid_notice_id")
    return value


def now():
    """当前时间，整数秒。"""
    return int(time.time())


def utc(value=None):
    """把时间戳转成 UTC 的 ISO 8601 字符串，例如 2026-10-01T03:56:00Z。"""
    return (
        datetime.fromtimestamp(now() if value is None else value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def new_id():
    """随机生成一个 32 位十六进制 ID。"""
    return secrets.token_hex(16)


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS cursors(owner TEXT PRIMARY KEY,seq INTEGER NOT NULL)",
    """CREATE TABLE IF NOT EXISTS inbox(owner TEXT NOT NULL,seq INTEGER NOT NULL,event_id TEXT NOT NULL,
        digest TEXT NOT NULL,replay INTEGER NOT NULL,PRIMARY KEY(owner,seq),UNIQUE(owner,event_id))""",
    """CREATE TABLE IF NOT EXISTS commands(workspace_id TEXT NOT NULL,operation TEXT NOT NULL,key TEXT NOT NULL,
        digest TEXT NOT NULL,result TEXT NOT NULL,PRIMARY KEY(workspace_id,operation,key))""",
    """CREATE TABLE IF NOT EXISTS decisions(workspace_id TEXT NOT NULL,notice_id TEXT NOT NULL,
        version INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,notice_id))""",
    """CREATE TABLE IF NOT EXISTS decision_history(workspace_id TEXT NOT NULL,notice_id TEXT NOT NULL,
        version INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(workspace_id,notice_id,version))""",
    """CREATE TABLE IF NOT EXISTS watches(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,notice_id TEXT NOT NULL,
        version INTEGER NOT NULL,active INTEGER NOT NULL,payload TEXT NOT NULL,bundle TEXT NOT NULL,
        catalog_seq INTEGER NOT NULL,profile_revision INTEGER NOT NULL,UNIQUE(workspace_id,notice_id))""",
    """CREATE TABLE IF NOT EXISTS watch_history(watch_id TEXT NOT NULL REFERENCES watches(id),version INTEGER NOT NULL,
        payload TEXT NOT NULL,PRIMARY KEY(watch_id,version))""",
    """CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,
        workspace_id TEXT NOT NULL,watch_id TEXT NOT NULL REFERENCES watches(id),rule_version TEXT NOT NULL,
        fingerprint TEXT NOT NULL,payload TEXT NOT NULL,UNIQUE(watch_id,rule_version,fingerprint))""",
    """CREATE TABLE IF NOT EXISTS reassessments(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,
        watch_id TEXT NOT NULL REFERENCES watches(id),change_id TEXT NOT NULL REFERENCES changes(id),
        command_key TEXT NOT NULL UNIQUE,request TEXT NOT NULL,state TEXT NOT NULL,run_id TEXT,
        version INTEGER NOT NULL,lease_epoch INTEGER NOT NULL,lease_until INTEGER NOT NULL,attempts INTEGER NOT NULL,
        next_attempt INTEGER NOT NULL,result TEXT,reason TEXT,UNIQUE(watch_id,change_id))""",
    """CREATE TABLE IF NOT EXISTS reassessment_history(seq INTEGER PRIMARY KEY,reassessment_id TEXT NOT NULL REFERENCES reassessments(id),
        payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS notifications(seq INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,workspace_id TEXT NOT NULL,
        watch_id TEXT NOT NULL,rule_version TEXT NOT NULL,fingerprint TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
        UNIQUE(watch_id,rule_version,fingerprint,kind))""",
    """CREATE TABLE IF NOT EXISTS notification_reads(workspace_id TEXT NOT NULL,notification_id TEXT NOT NULL REFERENCES notifications(id),
        user_id TEXT NOT NULL,read_at TEXT NOT NULL,PRIMARY KEY(workspace_id,notification_id,user_id))""",
    "CREATE INDEX IF NOT EXISTS watch_company ON watches(workspace_id,active)",
    "CREATE INDEX IF NOT EXISTS change_company ON changes(workspace_id,seq)",
    "CREATE INDEX IF NOT EXISTS notification_company ON notifications(workspace_id,seq)",
)


class TrackingStore:
    """跟踪服务的数据存储。root 是数据目录，里面放 tracking.sqlite3。"""

    def __init__(self, root):
        root = Path(root).absolute()
        self.db = None
        try:
            if root.is_symlink():
                raise TrackingError("invalid_store_directory")
            root.mkdir(parents=True, exist_ok=True)
            marker = root / ".bidradar-tracking-v1"
            if any(
                (root / name).is_symlink()
                for name in (marker.name, "tracking.sqlite3", "tracking.sqlite3-wal", "tracking.sqlite3-shm")
            ):
                raise TrackingError("invalid_store_directory")
            if not marker.exists() and any(root.iterdir()):
                raise TrackingError("store_directory_not_empty")
            marker.touch(exist_ok=True)
            self.db = sqlite3.connect(root / "tracking.sqlite3", timeout=5, isolation_level=None)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            with self.tx():
                if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                    raise TrackingError("unsupported_store_version", 503)
                for statement in SCHEMA:
                    self.db.execute(statement)
                for table in (
                    "inbox",
                    "commands",
                    "decision_history",
                    "watch_history",
                    "changes",
                    "reassessment_history",
                    "notifications",
                ):
                    for action in ("UPDATE", "DELETE"):
                        self.db.execute(
                            f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                            "BEGIN SELECT RAISE(ABORT,'immutable_record'); END"
                        )
                self.db.execute("PRAGMA user_version=1")
        except (OSError, sqlite3.Error):
            self.close()
            raise TrackingError("storage_unavailable", 503) from None
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def tx(self, *, write=True):
        """开启事务。写事务用 BEGIN IMMEDIATE 一开始就拿写锁。数据库错误统一转成 storage_unavailable。"""
        if self.db is None:
            raise TrackingError("store_closed", 503)
        try:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield
            self.db.commit()
        except (sqlite3.Error, json.JSONDecodeError):
            self.db.rollback()
            raise TrackingError("storage_unavailable", 503) from None
        except BaseException:
            self.db.rollback()
            raise

    def cached(self, workspace_id, operation, key, request):
        """查幂等键。返回 (请求指纹, 之前的结果或 None)。同一个键但请求不同，报 idempotency_conflict。"""
        text(key)
        digest = fingerprint(request)
        row = self.db.execute(
            "SELECT * FROM commands WHERE workspace_id=? AND operation=? AND key=?", (workspace_id, operation, key)
        ).fetchone()
        if row is not None and row["digest"] != digest:
            raise TrackingError("idempotency_conflict", 409)
        return digest, json.loads(row["result"]) if row else None

    def remember(self, workspace_id, operation, key, digest, result):
        """记下这个幂等键对应的结果，之后同样的请求直接返回它。"""
        self.db.execute(
            "INSERT INTO commands VALUES(?,?,?,?,?)", (workspace_id, operation, key, digest, canonical(result))
        )
        return result

    def cursor(self, owner):
        """某个事件来源（catalog 或 workspace）已经连续处理到的序号。"""
        row = self.db.execute("SELECT seq FROM cursors WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    def watch_row(self, workspace_id, watch_id):
        """读取一条关注记录，不存在就报 not_found。"""
        row = self.db.execute(
            "SELECT * FROM watches WHERE workspace_id=? AND id=?", (workspace_id, watch_id)
        ).fetchone()
        if row is None:
            raise TrackingError("not_found", 404)
        return row

    def notify(self, watch, change, kind, *, replay=False):
        """发一条站内提醒。同一个变化、同一种提醒只会有一条；重建历史（replay）时不发。"""
        if replay:
            return
        value = {
            "schema_version": 1,
            "id": new_id(),
            "workspace_id": watch["workspace_id"],
            "watch_id": watch["id"],
            "change_id": change["id"],
            "kind": kind,
            "created_at": utc(),
        }
        self.db.execute(
            "INSERT INTO notifications(id,workspace_id,watch_id,rule_version,fingerprint,kind,payload) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(watch_id,rule_version,fingerprint,kind) DO NOTHING",
            (
                value["id"],
                watch["workspace_id"],
                watch["id"],
                watch["rule_version"],
                change["fingerprint"],
                kind,
                canonical(value),
            ),
        )

    def reassessment_event(self, reassessment_id, state, reason=None, run_id=None):
        """给复核命令追加一条状态历史。"""
        self.db.execute(
            "INSERT INTO reassessment_history(reassessment_id,payload) VALUES(?,?)",
            (reassessment_id, canonical({"state": state, "reason": reason, "run_id": run_id, "recorded_at": utc()})),
        )

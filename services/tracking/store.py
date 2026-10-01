"""tracking独占的本地SQLite；只保存自己的事务，不连接其他服务业务库。

Store是服务内部持久层，公共动作由TrackingService先验证当前授权。开发恢复验证
不等于PostgreSQL/RLS或生产备份验收。网络/模型调用绝不能在tx写锁里执行。
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
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def text(value, limit=128, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or "\x00" in value:
        raise TrackingError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise TrackingError("invalid_input") from None
    return value


def integer(value, minimum=0, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise TrackingError("invalid_version")
    return value


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise TrackingError("invalid_notice_id")
    return value


def now():
    return int(time.time())


def utc(value=None):
    return datetime.fromtimestamp(now() if value is None else value, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_id():
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
    def __init__(self, root):
        root = Path(root).absolute()
        self.db = None
        try:
            if root.is_symlink():
                raise TrackingError("invalid_store_directory")
            root.mkdir(parents=True, exist_ok=True)
            marker = root / ".bidradar-tracking-v1"
            if any((root / name).is_symlink() for name in (marker.name, "tracking.sqlite3", "tracking.sqlite3-wal", "tracking.sqlite3-shm")):
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
                for table in ("inbox", "commands", "decision_history", "watch_history", "changes", "reassessment_history", "notifications"):
                    for action in ("UPDATE", "DELETE"):
                        self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                                        "BEGIN SELECT RAISE(ABORT,'immutable_record'); END")
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
        text(key)
        digest = fingerprint(request)
        row = self.db.execute("SELECT * FROM commands WHERE workspace_id=? AND operation=? AND key=?", (workspace_id, operation, key)).fetchone()
        if row is not None and row["digest"] != digest:
            raise TrackingError("idempotency_conflict", 409)
        return digest, json.loads(row["result"]) if row else None

    def remember(self, workspace_id, operation, key, digest, result):
        self.db.execute("INSERT INTO commands VALUES(?,?,?,?,?)", (workspace_id, operation, key, digest, canonical(result)))
        return result

    def cursor(self, owner):
        row = self.db.execute("SELECT seq FROM cursors WHERE owner=?", (owner,)).fetchone()
        return row[0] if row else 0

    def watch_row(self, workspace_id, watch_id):
        row = self.db.execute("SELECT * FROM watches WHERE workspace_id=? AND id=?", (workspace_id, watch_id)).fetchone()
        if row is None:
            raise TrackingError("not_found", 404)
        return row

    def notify(self, watch, change, kind, *, replay=False):
        if replay:
            return
        value = {"schema_version": 1, "id": new_id(), "workspace_id": watch["workspace_id"], "watch_id": watch["id"],
                 "change_id": change["id"], "kind": kind, "created_at": utc()}
        self.db.execute("INSERT INTO notifications(id,workspace_id,watch_id,rule_version,fingerprint,kind,payload) VALUES(?,?,?,?,?,?,?) "
                        "ON CONFLICT(watch_id,rule_version,fingerprint,kind) DO NOTHING",
                        (value["id"], watch["workspace_id"], watch["id"], watch["rule_version"], change["fingerprint"], kind, canonical(value)))

    def reassessment_event(self, reassessment_id, state, reason=None, run_id=None):
        self.db.execute("INSERT INTO reassessment_history(reassessment_id,payload) VALUES(?,?)",
                        (reassessment_id, canonical({"state": state, "reason": reason, "run_id": run_id, "recorded_at": utc()})))

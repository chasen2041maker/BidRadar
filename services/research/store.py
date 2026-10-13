"""研究服务的数据库：任务、报告、取消命令、状态变更历史。

几个关键设计：
    - 受理时先把任务存下来、返回编号，worker 之后才执行。刷新页面或断网都不会重复建任务。
    - 租约（lease）：worker 领取任务时拿到一个租约和递增的 lease_epoch。
      每次写入前都核对 epoch，租约过期被别的 worker 接手后，旧 worker 的写入会被拒绝。
    - 取消写进数据库，worker 每个动作前都会查，不靠中断线程。
    - 报告和状态历史只能插入，不能修改或删除（用触发器保证）。
    - 模型费用的恢复由独立的预算账本负责，不在这里。

现在用 SQLite 做本地开发，正式环境计划换成 PostgreSQL。
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4

from .budget import canonical

# 终态：到了这些状态，任务不会再被 worker 领取。
TERMINAL = {"succeeded", "partial", "failed", "cancelled", "waiting_input"}


class ResearchError(Exception):
    """研究服务的业务错误。code 是错误码，status 是对应的 HTTP 状态码。"""

    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


class ResearchStore:
    """研究服务的数据存储。root 是数据目录，里面放 research.sqlite3。"""

    def __init__(self, root):
        self.root = Path(root).absolute()
        self.db = None
        marker = self.root / ".bidradar-research-v1"
        if self.root.is_symlink():
            raise ResearchError("invalid_store")
        self.root.mkdir(parents=True, exist_ok=True)
        if not marker.exists() and any(self.root.iterdir()):
            raise ResearchError("store_directory_not_empty")
        database = self.root / "research.sqlite3"
        if marker.exists() and not database.exists():
            raise ResearchError("research_store_missing", 503)
        if any(
            p.is_symlink()
            for p in (marker, database, self.root / "research.sqlite3-wal", self.root / "research.sqlite3-shm")
        ):
            raise ResearchError("invalid_store")
        self.db = sqlite3.connect(database, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,actor_id TEXT NOT NULL,"
                "command_key TEXT NOT NULL,request_hash TEXT NOT NULL,request TEXT NOT NULL,manifest TEXT NOT NULL,"
                "membership_version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT,version INTEGER NOT NULL,"
                "created_at REAL NOT NULL,updated_at REAL NOT NULL,started_at REAL,lease_owner TEXT,lease_until REAL,"
                "lease_epoch INTEGER NOT NULL DEFAULT 0,checkpoint TEXT,retry_count INTEGER NOT NULL DEFAULT 0,"
                "next_attempt_at REAL NOT NULL DEFAULT 0, UNIQUE(workspace_id,command_key))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS reports(run_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,"
                "payload TEXT NOT NULL,trace TEXT NOT NULL,quality TEXT NOT NULL,created_at REAL NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS cancel_commands(workspace_id TEXT NOT NULL,command_key TEXT NOT NULL,"
                "actor_id TEXT NOT NULL,run_id TEXT NOT NULL,PRIMARY KEY(workspace_id,command_key))"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,"
                "workspace_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,version INTEGER NOT NULL,recorded_at REAL NOT NULL)"
            )
            self.db.execute(
                "CREATE INDEX IF NOT EXISTS research_queue ON runs(state,next_attempt_at,lease_until,created_at)"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS research_workspace ON runs(workspace_id,created_at,id)")
            for table in ("reports", "events"):
                for action in ("UPDATE", "DELETE"):
                    self.db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                        "BEGIN SELECT RAISE(ABORT,'immutable_record'); END"
                    )
        marker.touch(exist_ok=True)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def transaction(self):
        """开启写事务。BEGIN IMMEDIATE 一开始就拿写锁，避免"先读后写"之间被别人插队。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _event(self, run_id):
        """把任务当前的状态追加到历史表。"""
        self.db.execute(
            "INSERT INTO events(run_id,workspace_id,state,reason,version,recorded_at) "
            "SELECT id,workspace_id,state,reason,version,updated_at FROM runs WHERE id=?",
            (run_id,),
        )

    def _row(self, workspace_id, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE workspace_id=? AND id=?", (workspace_id, run_id)).fetchone()
        if row is None:
            raise ResearchError("not_found", 404)
        return row

    def command(self, workspace_id, actor_id, key, request_hash=None):
        """按幂等键查找任务。同一个键但请求内容不同，报 idempotency_conflict。"""
        row = self.db.execute(
            "SELECT * FROM runs WHERE workspace_id=? AND command_key=?", (workspace_id, key)
        ).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or request_hash is not None and row["request_hash"] != request_hash:
            raise ResearchError("idempotency_conflict")
        return self.public(workspace_id, row["id"])

    def create(self, request, request_hash, manifest, membership_version):
        """新建一个排队中的任务。并发重复提交时只会建出一个。每家公司同时最多 3 个未完成任务。"""
        with self.transaction():
            previous = self.command(request["workspace_id"], request["actor_id"], request["key"], request_hash)
            if previous is not None:
                return previous
            active = self.db.execute(
                "SELECT count(*) FROM runs WHERE workspace_id=? AND state IN ('queued','running','retry_wait')",
                (request["workspace_id"],),
            ).fetchone()[0]
            if active >= 3:
                raise ResearchError("workspace_queue_limit", 429)
            run_id, now = uuid4().hex, time.time()
            self.db.execute(
                "INSERT INTO runs(id,workspace_id,actor_id,command_key,request_hash,request,manifest,membership_version,"
                "state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',1,?,?)",
                (
                    run_id,
                    request["workspace_id"],
                    request["actor_id"],
                    request["key"],
                    request_hash,
                    canonical(request),
                    canonical(manifest),
                    membership_version,
                    now,
                    now,
                ),
            )
            self._event(run_id)
            return self.public(request["workspace_id"], run_id)

    def public(self, workspace_id, run_id):
        """返回可以给前端看的任务信息和报告。不包含恢复用的对话记录和模型原始回复。"""
        row = self._row(workspace_id, run_id)
        manifest, request = json.loads(row["manifest"]), json.loads(row["request"])
        result = {k: row[k] for k in ("id", "workspace_id", "actor_id", "state", "reason", "version", "retry_count")}
        # 数据库里存秒级时间戳，方便比较租约；对外统一转成 UTC 的 ISO 8601 字符串。
        result.update(
            {
                key: datetime.fromtimestamp(row[key], timezone.utc).isoformat().replace("+00:00", "Z")
                for key in ("created_at", "updated_at")
            }
        )
        result["freshness"] = {"profile": "not_checked", "catalog": "not_checked"}
        result.update(
            {
                "kind": request.get("kind", "analysis"),
                "mode": request.get("mode", "agent"),
                "notice_id": request["notice_id"],
                "profile_revision": manifest["profile"]["revision"],
                "parent_run_id": request.get("parent_run_id"),
                "report": None,
                "trace": [],
                "quality": None,
            }
        )
        observations = manifest["observations"]
        result.update(
            {
                "simulation": all(o.get("simulation") is True for o in observations),
                "title": observations[0].get("title") or observations[0].get("facts", {}).get("title", {}).get("value"),
                "scope": {
                    "notice_id": request["notice_id"],
                    "observation_ids": [o["observation_id"] for o in observations],
                    "profile_revision": manifest["profile"]["revision"],
                    "catalog_snapshot": manifest["catalog_snapshot"],
                },
                "material_status": {"full_tender_read": False, "source": "normalized_snippets"},
            }
        )
        report = self.db.execute(
            "SELECT * FROM reports WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)
        ).fetchone()
        if report:
            result.update(
                {
                    "report": json.loads(report["payload"]),
                    "trace": json.loads(report["trace"]),
                    "quality": json.loads(report["quality"]),
                }
            )
        return result

    def list_runs(self, workspace_id, *, before=None, limit=30):
        """按创建时间倒序分页。before 是上一页最后一个任务的 ID，必须属于本公司。"""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResearchError("invalid_limit", 400)
        if before is not None and (not isinstance(before, str) or not re.fullmatch(r"[a-f0-9]{32}", before)):
            raise ResearchError("invalid_cursor", 400)
        cursor_time = None
        if before is not None:
            cursor = self.db.execute(
                "SELECT created_at FROM runs WHERE workspace_id=? AND id=?", (workspace_id, before)
            ).fetchone()
            if cursor is None:
                raise ResearchError("invalid_cursor", 400)
            cursor_time = cursor["created_at"]
        # 先按创建时间排序；时间相同时再按 ID 排，只是为了顺序稳定，ID 大小本身没有含义。
        rows = self.db.execute(
            "SELECT id FROM runs WHERE workspace_id=? AND (? IS NULL OR created_at<? "
            "OR (created_at=? AND id<?)) ORDER BY created_at DESC,id DESC LIMIT ?",
            (workspace_id, cursor_time, cursor_time, cursor_time, before, limit + 1),
        ).fetchall()
        items = [self.public(workspace_id, row["id"]) for row in rows[:limit]]
        return {
            "items": items,
            "next_before": rows[limit - 1]["id"] if len(rows) > limit else None,
            "order": "created_at_desc_id_desc",
        }

    def load(self, workspace_id, run_id):
        """读取任务的完整记录（含 manifest、请求和进度），供服务内部使用。"""
        row = dict(self._row(workspace_id, run_id))
        for key in ("manifest", "request", "checkpoint"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def claim(self, worker_id, *, lease_seconds=90):
        """领取一个可执行的任务：排队中的、到了重试时间的、或者租约已过期的。

        领取时 lease_epoch 加一，之前持有这个任务的 worker 从此不能再写入。
        执行时间从第一次领取开始算，最多 300 秒；超时的任务直接记为失败。
        """
        now = time.time()
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM runs WHERE state='queued' OR (state='retry_wait' AND next_attempt_at<=?) "
                "OR (state='running' AND lease_until<=?) ORDER BY created_at,id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            if row["started_at"] is not None and now - row["started_at"] >= 300:
                self.db.execute(
                    "UPDATE runs SET state='failed',reason='execution_deadline',version=version+1,updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                self._event(row["id"])
                return None
            self.db.execute(
                "UPDATE runs SET state='running',reason=NULL,version=version+1,updated_at=?,started_at=coalesce(started_at,?),"
                "lease_owner=?,lease_until=?,lease_epoch=lease_epoch+1 WHERE id=?",
                (now, now, worker_id, now + lease_seconds, row["id"]),
            )
            self._event(row["id"])
            return self.load(row["workspace_id"], row["id"])

    def check_lease(self, run, *, renew=True):
        """确认这个 worker 仍然持有任务：没被取消、租约没过期、没被别人接手。顺便续期租约。

        每个有副作用的动作前都要调用，而且查的是数据库，不是内存里的状态。
        """
        with self.transaction():
            row = self._row(run["workspace_id"], run["id"])
            now = time.time()
            if row["state"] != "running" or row["lease_epoch"] != run["lease_epoch"] or row["lease_until"] <= now:
                raise ResearchError("lease_lost")
            if now - row["started_at"] >= 300:
                raise ResearchError("execution_deadline")
            if renew:
                self.db.execute("UPDATE runs SET lease_until=? WHERE id=?", (now + 90, row["id"]))

    def checkpoint(self, run, value):
        """保存 Agent 的进度。只有当前持有租约的 worker 能写。"""
        raw = canonical(value)
        if len(raw.encode()) > 4 * 1024 * 1024:
            raise ResearchError("checkpoint_too_large")
        with self.transaction():
            self._writable(run)
            self.db.execute("UPDATE runs SET checkpoint=?,updated_at=? WHERE id=?", (raw, time.time(), run["id"]))

    def _writable(self, run):
        """确认当前 worker 仍有写权限，返回任务记录。"""
        row = self._row(run["workspace_id"], run["id"])
        if row["state"] != "running" or row["lease_epoch"] != run["lease_epoch"] or row["lease_until"] <= time.time():
            raise ResearchError("lease_lost")
        return row

    def finish(self, run, result):
        """保存报告并把任务标为 succeeded 或 partial。"""
        if result["state"] not in ("succeeded", "partial"):
            raise ResearchError("invalid_result")
        with self.transaction():
            self._writable(run)
            now = time.time()
            self.db.execute(
                "INSERT INTO reports VALUES(?,?,?,?,?,?)",
                (
                    run["id"],
                    run["workspace_id"],
                    canonical(result["report"]),
                    canonical(result["trace"]),
                    canonical(result["quality"]),
                    now,
                ),
            )
            self.db.execute(
                "UPDATE runs SET state=?,reason=NULL,version=version+1,updated_at=?,lease_until=NULL WHERE id=?",
                (result["state"], now, run["id"]),
            )
            self._event(run["id"])

    def stop(self, run, state, reason):
        """任务出错时停下：failed、waiting_input 或 retry_wait。重试超过 3 次就改为 failed。"""
        if state not in ("failed", "waiting_input", "retry_wait"):
            raise ResearchError("invalid_state")
        with self.transaction():
            row = self._writable(run)
            retries = row["retry_count"] + (state == "retry_wait")
            if retries > 3:
                state, reason = "failed", "dependency_retry_exhausted"
            now = time.time()
            self.db.execute(
                "UPDATE runs SET state=?,reason=?,version=version+1,updated_at=?,retry_count=?,next_attempt_at=?,lease_until=NULL WHERE id=?",
                (state, reason, now, retries, now + min(30, 5 * retries), run["id"]),
            )
            self._event(run["id"])

    def cancel(self, workspace_id, run_id, *, actor_id=None, key=None):
        """取消任务。重复取消没有副作用；已完成的任务和报告保持不变。

        注意：已经发出去的模型请求收不回来，取消只能阻止下一个动作。
        """
        with self.transaction():
            row = self._row(workspace_id, run_id)
            if actor_id is not None or key is not None:
                if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in (actor_id, key)):
                    raise ResearchError("invalid_input", 400)
                previous = self.db.execute(
                    "SELECT actor_id,run_id FROM cancel_commands WHERE workspace_id=? AND command_key=?",
                    (workspace_id, key),
                ).fetchone()
                if previous is not None and tuple(previous) != (actor_id, run_id):
                    raise ResearchError("idempotency_conflict")
                # 取消命令和取消动作在同一个事务里保存。同一个键之后不能拿去取消另一个任务。
                self.db.execute(
                    "INSERT OR IGNORE INTO cancel_commands VALUES(?,?,?,?)", (workspace_id, key, actor_id, run_id)
                )
            if row["state"] not in TERMINAL:
                self.db.execute(
                    "UPDATE runs SET state='cancelled',reason='user_cancelled',version=version+1,updated_at=?,lease_until=NULL WHERE id=?",
                    (time.time(), run_id),
                )
                self._event(run_id)
            return self.public(workspace_id, run_id)

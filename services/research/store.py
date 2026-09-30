"""研究服务自己的持久任务与不可变报告；不导入其它服务的业务 ORM。

受理先提交编号，Worker 后执行。租约代数用于写入栅栏，取消不依赖线程中断；
外部调用可能已经计费，由独立预算账本负责恢复语义。SQLite 仅为本地验证环境。
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .budget import canonical

TERMINAL = {"succeeded", "partial", "failed", "cancelled", "waiting_input"}


class ResearchError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


class ResearchStore:
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
        if any(p.is_symlink() for p in (marker, database, self.root / "research.sqlite3-wal", self.root / "research.sqlite3-shm")):
            raise ResearchError("invalid_store")
        self.db = sqlite3.connect(database, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        with self.transaction():
            self.db.execute("CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,actor_id TEXT NOT NULL,"
                            "command_key TEXT NOT NULL,request_hash TEXT NOT NULL,request TEXT NOT NULL,manifest TEXT NOT NULL,"
                            "membership_version INTEGER NOT NULL,state TEXT NOT NULL,reason TEXT,version INTEGER NOT NULL,"
                            "created_at REAL NOT NULL,updated_at REAL NOT NULL,started_at REAL,lease_owner TEXT,lease_until REAL,"
                            "lease_epoch INTEGER NOT NULL DEFAULT 0,checkpoint TEXT,retry_count INTEGER NOT NULL DEFAULT 0,"
                            "next_attempt_at REAL NOT NULL DEFAULT 0, UNIQUE(workspace_id,command_key))")
            self.db.execute("CREATE TABLE IF NOT EXISTS reports(run_id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,"
                            "payload TEXT NOT NULL,trace TEXT NOT NULL,quality TEXT NOT NULL,created_at REAL NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,"
                            "workspace_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT,version INTEGER NOT NULL,recorded_at REAL NOT NULL)")
            self.db.execute("CREATE INDEX IF NOT EXISTS research_queue ON runs(state,next_attempt_at,lease_until,created_at)")
            self.db.execute("CREATE INDEX IF NOT EXISTS research_workspace ON runs(workspace_id,created_at,id)")
            for table in ("reports", "events"):
                for action in ("UPDATE", "DELETE"):
                    self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                                    "BEGIN SELECT RAISE(ABORT,'immutable_record'); END")
        marker.touch(exist_ok=True)

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _event(self, run_id):
        self.db.execute("INSERT INTO events(run_id,workspace_id,state,reason,version,recorded_at) "
                        "SELECT id,workspace_id,state,reason,version,updated_at FROM runs WHERE id=?", (run_id,))

    def _row(self, workspace_id, run_id):
        row = self.db.execute("SELECT * FROM runs WHERE workspace_id=? AND id=?", (workspace_id, run_id)).fetchone()
        if row is None:
            raise ResearchError("not_found", 404)
        return row

    def command(self, workspace_id, actor_id, key, request_hash=None):
        row = self.db.execute("SELECT * FROM runs WHERE workspace_id=? AND command_key=?", (workspace_id, key)).fetchone()
        if row is None:
            return None
        if row["actor_id"] != actor_id or request_hash is not None and row["request_hash"] != request_hash:
            raise ResearchError("idempotency_conflict")
        return self.public(workspace_id, row["id"])

    def create(self, request, request_hash, manifest, membership_version):
        """只接所属领域已验证的冻结输入；并发重复受理归并到一个任务。"""
        with self.transaction():
            previous = self.command(request["workspace_id"], request["actor_id"], request["key"], request_hash)
            if previous is not None:
                return previous
            active = self.db.execute("SELECT count(*) FROM runs WHERE workspace_id=? AND state IN ('queued','running','retry_wait')",
                                     (request["workspace_id"],)).fetchone()[0]
            if active >= 3:
                raise ResearchError("workspace_queue_limit", 429)
            run_id, now = uuid4().hex, time.time()
            self.db.execute("INSERT INTO runs(id,workspace_id,actor_id,command_key,request_hash,request,manifest,membership_version,"
                            "state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'queued',1,?,?)",
                            (run_id, request["workspace_id"], request["actor_id"], request["key"], request_hash,
                             canonical(request), canonical(manifest), membership_version, now, now))
            self._event(run_id)
            return self.public(request["workspace_id"], run_id)

    def public(self, workspace_id, run_id):
        row = self._row(workspace_id, run_id)
        manifest, request = json.loads(row["manifest"]), json.loads(row["request"])
        result = {k: row[k] for k in ("id", "workspace_id", "actor_id", "state", "reason", "version", "created_at", "updated_at", "retry_count")}
        result.update({"kind": request.get("kind", "analysis"), "mode": request.get("mode", "agent"),
                       "notice_id": request["notice_id"], "profile_revision": manifest["profile"]["revision"],
                       "parent_run_id": request.get("parent_run_id"), "report": None, "trace": [], "quality": None})
        observations = manifest["observations"]
        result.update({"simulation": all(o.get("simulation") is True for o in observations),
                       "title": observations[0].get("title") or observations[0].get("facts", {}).get("title", {}).get("value"),
                       "scope": {"notice_id": request["notice_id"], "observation_ids": [o["observation_id"] for o in observations],
                                 "profile_revision": manifest["profile"]["revision"], "catalog_snapshot": manifest["catalog_snapshot"]},
                       "material_status": {"full_tender_read": False, "source": "normalized_snippets"}})
        report = self.db.execute("SELECT * FROM reports WHERE workspace_id=? AND run_id=?", (workspace_id, run_id)).fetchone()
        if report:
            result.update({"report": json.loads(report["payload"]), "trace": json.loads(report["trace"]), "quality": json.loads(report["quality"])})
        # 不输出恢复消息/原始模型回答；UI展示工具轨迹和可复核报告，不展示隐藏思维。
        return result

    def list_runs(self, workspace_id, *, before=None, limit=30):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResearchError("invalid_limit", 400)
        if before is not None and (not isinstance(before, str) or len(before) > 128):
            raise ResearchError("invalid_cursor", 400)
        rows = self.db.execute("SELECT id FROM runs WHERE workspace_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                               (workspace_id, before, before, limit + 1)).fetchall()
        items = [self.public(workspace_id, row["id"]) for row in rows[:limit]]
        return {"items": items, "next_before": rows[limit - 1]["id"] if len(rows) > limit else None, "order": "id_desc"}

    def load(self, workspace_id, run_id):
        row = dict(self._row(workspace_id, run_id))
        for key in ("manifest", "request", "checkpoint"):
            row[key] = json.loads(row[key]) if row[key] else None
        return row

    def claim(self, worker_id, *, lease_seconds=90):
        """过期租约可接续；旧Worker持有的epoch不再拥有写权。总执行窗口从首次claim算。"""
        now = time.time()
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE state='queued' OR (state='retry_wait' AND next_attempt_at<=?) "
                                  "OR (state='running' AND lease_until<=?) ORDER BY created_at,id LIMIT 1", (now, now)).fetchone()
            if row is None:
                return None
            if row["started_at"] is not None and now - row["started_at"] >= 300:
                self.db.execute("UPDATE runs SET state='failed',reason='execution_deadline',version=version+1,updated_at=? WHERE id=?", (now, row["id"]))
                self._event(row["id"])
                return None
            self.db.execute("UPDATE runs SET state='running',reason=NULL,version=version+1,updated_at=?,started_at=coalesce(started_at,?),"
                            "lease_owner=?,lease_until=?,lease_epoch=lease_epoch+1 WHERE id=?",
                            (now, now, worker_id, now + lease_seconds, row["id"]))
            self._event(row["id"])
            return self.load(row["workspace_id"], row["id"])

    def check_lease(self, run, *, renew=True):
        """每次副作用前重验持久取消/租约，不能只看内存中的run.state。"""
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
        raw = canonical(value)
        if len(raw.encode()) > 4 * 1024 * 1024:
            raise ResearchError("checkpoint_too_large")
        with self.transaction():
            self._writable(run)
            self.db.execute("UPDATE runs SET checkpoint=?,updated_at=? WHERE id=?", (raw, time.time(), run["id"]))

    def _writable(self, run):
        row = self._row(run["workspace_id"], run["id"])
        if row["state"] != "running" or row["lease_epoch"] != run["lease_epoch"] or row["lease_until"] <= time.time():
            raise ResearchError("lease_lost")
        return row

    def finish(self, run, result):
        if result["state"] not in ("succeeded", "partial"):
            raise ResearchError("invalid_result")
        with self.transaction():
            self._writable(run)
            now = time.time()
            self.db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?)", (run["id"], run["workspace_id"], canonical(result["report"]),
                            canonical(result["trace"]), canonical(result["quality"]), now))
            self.db.execute("UPDATE runs SET state=?,reason=NULL,version=version+1,updated_at=?,lease_until=NULL WHERE id=?",
                            (result["state"], now, run["id"]))
            self._event(run["id"])

    def stop(self, run, state, reason):
        if state not in ("failed", "waiting_input", "retry_wait"):
            raise ResearchError("invalid_state")
        with self.transaction():
            row = self._writable(run)
            retries = row["retry_count"] + (state == "retry_wait")
            if retries > 3:
                state, reason = "failed", "dependency_retry_exhausted"
            now = time.time()
            self.db.execute("UPDATE runs SET state=?,reason=?,version=version+1,updated_at=?,retry_count=?,next_attempt_at=?,lease_until=NULL WHERE id=?",
                            (state, reason, now, retries, now + min(30, 5 * retries), run["id"]))
            self._event(run["id"])

    def cancel(self, workspace_id, run_id):
        """取消幂等且持久；不会删除已完成报告，也不能声称撤回已发送模型请求。"""
        with self.transaction():
            row = self._row(workspace_id, run_id)
            if row["state"] not in TERMINAL:
                self.db.execute("UPDATE runs SET state='cancelled',reason='user_cancelled',version=version+1,updated_at=?,lease_until=NULL WHERE id=?",
                                (time.time(), run_id))
                self._event(run_id)
            return self.public(workspace_id, run_id)

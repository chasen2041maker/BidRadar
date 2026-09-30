"""ingestion本地运行账本：原字节不可变，获取记录、解析和业务版本分别保存。

仅供公共资料开发阶段使用；其他服务不能直读此库，也不是公司资料/RLS后端。
文件先落盘、数据库后提交；进程中断可能留下未引用文件。读取时验证文件及指纹，
不承诺未经实测的断电恢复或生产RPO/RTO。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
from uuid import uuid4


def utc_now() -> str:
    """实际获取/记账时间，UTC秒精度；不当作来源发布日期。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StoreError(RuntimeError):
    pass


class Store:
    """每个目录只有一个ingestion账本；租约防止同一运行被两个执行者重复提交。

    目录由可信本地用户控制，不能把外部网页字段当作路径；这里不提供多用户沙盒。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise StoreError("store_symlink")
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".bidradar-ingestion-v1"
        if not marker.exists() and any(self.root.iterdir()):
            raise StoreError("store_directory_not_empty")
        marker.touch(exist_ok=True)
        self.blobs = self.root / "blobs"
        if self.blobs.is_symlink() or (self.root / "ingestion.sqlite3").is_symlink():
            raise StoreError("store_symlink")
        self.blobs.mkdir(exist_ok=True)
        self.db = sqlite3.connect(self.root / "ingestion.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        # 运行请求与队列先持久化；分析任务仍属于未来research自己的数据库。
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                request_json TEXT NOT NULL, status TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                lease_token TEXT, lease_until REAL, error_code TEXT);
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                kind TEXT NOT NULL, url TEXT NOT NULL, parent_url TEXT,
                state TEXT NOT NULL DEFAULT 'pending', payload_json TEXT NOT NULL,
                UNIQUE(run_id,kind,url));
            CREATE TABLE IF NOT EXISTS versions(
                id TEXT PRIMARY KEY, source_id TEXT NOT NULL, url TEXT NOT NULL,
                sha256 TEXT NOT NULL, version INTEGER NOT NULL,
                first_captured_at TEXT NOT NULL, UNIQUE(source_id,url,sha256),
                UNIQUE(source_id,url,version));
            CREATE TABLE IF NOT EXISTS captures(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                task_id INTEGER UNIQUE REFERENCES tasks(id), kind TEXT NOT NULL,
                url TEXT NOT NULL, final_url TEXT, fetched_at TEXT NOT NULL,
                http_status INTEGER, sha256 TEXT, byte_count INTEGER,
                version_id TEXT REFERENCES versions(id), headers_json TEXT NOT NULL,
                attempts INTEGER NOT NULL, error_code TEXT,
                parser_version TEXT, parsed_json TEXT);
            CREATE TABLE IF NOT EXISTS replays(
                id TEXT PRIMARY KEY, capture_id TEXT NOT NULL REFERENCES captures(id),
                parser_version TEXT NOT NULL, created_at TEXT NOT NULL, parsed_json TEXT NOT NULL);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def create_run(self, request: dict, key: str) -> tuple[str, bool]:
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise StoreError("invalid_idempotency_key")
        serialized = encode_json(request)
        with self.transaction():
            old = self.db.execute("SELECT * FROM runs WHERE request_key=?", (key,)).fetchone()
            if old:
                if old["request_json"] != serialized:
                    raise StoreError("idempotency_conflict")
                return old["id"], False
            run_id = uuid4().hex
            now = utc_now()
            self.db.execute("INSERT INTO runs VALUES(?,?,?,'queued',?,?,NULL,NULL,NULL)",
                            (run_id, key, serialized, now, now))
        return run_id, True

    def run(self, run_id: str) -> dict:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError("run_not_found")
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result.pop("lease_token")
        return result

    def acquire(self, run_id: str) -> str:
        """恢复仅接管失效租约；取消是持久终态，不能靠resume绕过。"""
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise StoreError("run_not_found")
            if row["status"] in ("cancelled", "succeeded", "partial", "blocked", "failed"):
                raise StoreError("run_terminal")
            if row["lease_until"] and row["lease_until"] > time.time():
                raise StoreError("run_busy")
            token = uuid4().hex
            self.db.execute("UPDATE runs SET status='running',lease_token=?,lease_until=?,updated_at=? WHERE id=?",
                            (token, time.time() + 120, utc_now(), run_id))
        return token

    def _check_lease(self, run_id: str, token: str):
        row = self.db.execute("SELECT status,lease_token,lease_until FROM runs WHERE id=?", (run_id,)).fetchone()
        if (row is None or row["status"] != "running" or row["lease_token"] != token
                or row["lease_until"] <= time.time()):
            raise StoreError("lease_lost_or_cancelled")

    def heartbeat(self, run_id: str, token: str):
        with self.transaction():
            self._check_lease(run_id, token)
            self.db.execute("UPDATE runs SET lease_until=?,updated_at=? WHERE id=?",
                            (time.time() + 120, utc_now(), run_id))

    def release(self, run_id: str, token: str):
        """进程正常中断时立即释放；硬崩溃后由租约超时恢复，已提交队列不重做。"""
        with self.db:
            self.db.execute("UPDATE runs SET status='queued',lease_token=NULL,lease_until=NULL,updated_at=? "
                            "WHERE id=? AND lease_token=? AND status='running'", (utc_now(), run_id, token))

    def cancel(self, run_id: str):
        self.run(run_id)
        with self.db:
            self.db.execute("UPDATE runs SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? "
                            "WHERE id=? AND status IN ('queued','running')", (utc_now(), run_id))

    def finish(self, run_id: str, token: str, status: str, error: str | None = None):
        if status not in ("succeeded", "partial", "blocked", "failed"):
            raise StoreError("invalid_terminal_status")
        with self.transaction():
            self._check_lease(run_id, token)
            self.db.execute("UPDATE runs SET status=?,error_code=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                            (status, error, utc_now(), run_id))

    def enqueue(self, run_id: str, token: str, tasks: list[dict]):
        with self.transaction():
            self._check_lease(run_id, token)
            self._enqueue(run_id, tasks)

    def _enqueue(self, run_id, tasks):
        for task in tasks:
            self.db.execute("INSERT INTO tasks(run_id,kind,url,parent_url,payload_json) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(run_id,kind,url) DO NOTHING",
                            (run_id, task["kind"], task["url"], task.get("parent_url"), encode_json(task.get("payload", {}))))

    def next_task(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND state='pending' ORDER BY id LIMIT 1", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def task_count(self, run_id: str, kind: str) -> int:
        return self.db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()[0]

    def task_urls(self, run_id: str, kind: str) -> set[str]:
        return {row[0] for row in self.db.execute("SELECT url FROM tasks WHERE run_id=? AND kind=?", (run_id, kind))}

    def put_blob(self, data: bytes) -> str:
        digest = sha256(data).hexdigest()
        target = self.blobs / (digest + ".bin")
        if target.exists():
            self.read_blob(digest)
            return digest
        with tempfile.NamedTemporaryFile(dir=self.blobs, delete=False) as file:
            temporary = Path(file.name)
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        try:
            # 硬链接提供“仅创建不覆盖”；并发写同一哈希时校验已有内容。
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read_blob(digest)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read_blob(self, digest: str) -> bytes:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise StoreError("invalid_blob_id")
        path = self.blobs / (digest + ".bin")
        if path.is_symlink():
            raise StoreError("blob_symlink")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise StoreError("blob_missing") from exc
        if sha256(data).hexdigest() != digest:
            raise StoreError("blob_corrupt")
        return data

    def record(self, run_id: str, token: str, task: dict, *, response=None,
               parsed: dict | None = None, parser_version: str | None = None,
               error: str | None = None, http_status: int | None = None, attempts: int = 0,
               following: list[dict] = ()) -> str:
        """获取记录、队列完成与后继登记同事务；恢复不会漏掉已解析出的后继任务。"""
        self._check_lease(run_id, token)
        digest = self.put_blob(response.body) if response is not None else None
        capture_id = uuid4().hex
        captured_at = response.fetched_at if response is not None else utc_now()
        with self.transaction():
            self._check_lease(run_id, token)
            stored = self.db.execute("SELECT * FROM tasks WHERE id=? AND run_id=? AND state='pending'",
                                     (task["id"], run_id)).fetchone()
            if stored is None:
                raise StoreError("task_not_pending_in_run")
            # 只接受账本中的URL/类型，调用方的字典不能伪造另一运行的任务。
            task = dict(stored)
            version_id = None
            if digest is not None:
                version_id = sha256(("cn_ccgp\n" + task["url"] + "\n" + digest).encode()).hexdigest()
                latest = self.db.execute("SELECT max(version) FROM versions WHERE source_id='cn_ccgp' AND url=?",
                                         (task["url"],)).fetchone()[0] or 0
                self.db.execute("INSERT INTO versions VALUES(?,'cn_ccgp',?,?,?,?) "
                                "ON CONFLICT(source_id,url,sha256) DO NOTHING",
                                (version_id, task["url"], digest, latest + 1, captured_at))
            # 只记录无凭据、无Set-Cookie的必要响应头；原URL仅来自受控入口。
            headers = ({key: val for key, val in response.headers.items()
                        if key.lower() in ("content-type", "etag", "last-modified", "content-length")}
                       if response is not None else {})
            self.db.execute("INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (capture_id, run_id, task["id"], task["kind"], task["url"],
                             response.final_url if response else None, captured_at,
                             response.http_status if response else http_status, digest,
                             len(response.body) if response else None, version_id, encode_json(headers),
                             response.attempts if response else attempts, error, parser_version,
                             encode_json(parsed) if parsed is not None else None))
            self.db.execute("UPDATE tasks SET state=? WHERE id=? AND run_id=?",
                            ("error" if error else "done", task["id"], run_id))
            self._enqueue(run_id, following)
        return capture_id

    def capture(self, capture_id: str) -> dict:
        row = self.db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if row is None:
            raise StoreError("capture_not_found")
        result = dict(row)
        result["parsed"] = json.loads(result.pop("parsed_json")) if result["parsed_json"] else None
        result["headers"] = json.loads(result.pop("headers_json"))
        return result

    def report(self, run_id: str) -> dict:
        """这是本地公共材料结果，不把获取成功解释为资格满足或产品覆盖率。"""
        run = self.run(run_id)
        captures = [self.capture(row[0]) for row in self.db.execute("SELECT id FROM captures WHERE run_id=? ORDER BY rowid", (run_id,))]
        attachments = {item["url"]: item for item in captures if item["kind"] == "attachment"}
        for capture in captures:
            if not capture["parsed"]:
                continue
            for link in capture["parsed"].get("attachments", ()):
                fetched = attachments.get(link["url"])
                if fetched:
                    # 展示投影组合当前附件获取结果，数据库里的历史解析记录不回写。
                    link["fetch_status"] = "failed" if fetched["error_code"] else "fetched"
                    link["capture_id"] = fetched["id"]
                    link["error_code"] = fetched["error_code"]
                else:
                    link["fetch_status"] = link["status"]
        return {"schema_version": 1, "run": run, "captures": captures,
                "pending": self.db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND state='pending'", (run_id,)).fetchone()[0]}

    def save_replay(self, capture_id: str, parser_version: str, parsed: dict) -> dict:
        """重解析生成独立记录，原获取和当时的解析结论保持不变。"""
        self.capture(capture_id)
        record = {"id": uuid4().hex, "capture_id": capture_id, "parser_version": parser_version,
                  "created_at": utc_now(), "parsed": parsed}
        with self.db:
            self.db.execute("INSERT INTO replays VALUES(?,?,?,?,?)", (record["id"], capture_id,
                            parser_version, record["created_at"], encode_json(parsed)))
        return record

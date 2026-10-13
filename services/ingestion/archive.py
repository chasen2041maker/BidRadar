"""采集服务的本地存储：采集运行、待抓取队列、抓取记录、原始文件。

存什么：
    runs      一次采集运行（请求参数、状态、租约）
    tasks     这次运行要抓的页面队列（列表页、公告页、附件）
    captures  每次抓取的结果：状态码、响应头、原始文件的 SHA-256、当时的解析结果
    versions  同一个 URL 的内容版本：内容变了就是新版本
    replays   之后用新解析器重新解析的结果（不覆盖当初的结论）
    blobs/    原始文件，按 SHA-256 命名，写入后不再修改

顺序上先写文件、再提交数据库。进程中途退出可能留下没被引用的文件，但不会出现"数据库有记录、文件却不存在"。
读取文件时会重新校验 SHA-256。

这是采集服务私有的存储，其他服务只能拿到导出的 JSON，不能直接读这里。
只用于公开资料的本地开发，没有做过断电恢复测试。
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
    """当前 UTC 时间（精确到秒），用作抓取和记账时间。注意：这不是公告的发布日期。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def encode_json(value) -> str:
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StoreError(RuntimeError):
    """存储层的错误，带错误码 code。"""

    pass


class Store:
    """采集存储。一个数据目录对应一个账本。

    同一次运行同时只能有一个执行者：执行前要先拿租约（acquire），执行中定期续期（heartbeat）。
    文件路径只由 SHA-256 决定，网页上的任何内容都不会被拿来当路径。
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
        # 先把运行请求和抓取队列存下来，中断后可以接着跑。
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
        """关闭数据库连接。"""
        self.db.close()

    @contextmanager
    def transaction(self):
        """开启写事务。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def create_run(self, request: dict, key: str) -> tuple[str, bool]:
        """登记一次采集运行，返回 (run_id, 是否新建)。同一个幂等键重复登记会返回原来的运行。"""
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
        """读取运行记录。"""
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise StoreError("run_not_found")
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result.pop("lease_token")
        return result

    def acquire(self, run_id: str) -> str:
        """拿到这次运行的执行租约，返回租约令牌。

        别人的租约还有效时拿不到；已取消或已结束的运行也不能再执行。
        """
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
        """确认租约仍属于这个令牌且没过期。"""
        row = self.db.execute("SELECT status,lease_token,lease_until FROM runs WHERE id=?", (run_id,)).fetchone()
        if (row is None or row["status"] != "running" or row["lease_token"] != token
                or row["lease_until"] <= time.time()):
            raise StoreError("lease_lost_or_cancelled")

    def heartbeat(self, run_id: str, token: str):
        """续期租约，同时确认运行没被取消。"""
        with self.transaction():
            self._check_lease(run_id, token)
            self.db.execute("UPDATE runs SET lease_until=?,updated_at=? WHERE id=?",
                            (time.time() + 120, utc_now(), run_id))

    def release(self, run_id: str, token: str):
        """主动释放租约（比如按了 Ctrl+C）。

        进程崩溃来不及释放时，等租约过期后别人就能接手；已经完成的抓取不会重做。
        """
        with self.db:
            self.db.execute("UPDATE runs SET status='queued',lease_token=NULL,lease_until=NULL,updated_at=? "
                            "WHERE id=? AND lease_token=? AND status='running'", (utc_now(), run_id, token))

    def cancel(self, run_id: str):
        """取消运行。取消是终态，之后不能再恢复执行。"""
        self.run(run_id)
        with self.db:
            self.db.execute("UPDATE runs SET status='cancelled',lease_token=NULL,lease_until=NULL,updated_at=? "
                            "WHERE id=? AND status IN ('queued','running')", (utc_now(), run_id))

    def finish(self, run_id: str, token: str, status: str, error: str | None = None):
        """把运行标为结束状态并释放租约。"""
        if status not in ("succeeded", "partial", "blocked", "failed"):
            raise StoreError("invalid_terminal_status")
        with self.transaction():
            self._check_lease(run_id, token)
            self.db.execute("UPDATE runs SET status=?,error_code=?,lease_token=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                            (status, error, utc_now(), run_id))

    def enqueue(self, run_id: str, token: str, tasks: list[dict]):
        """往抓取队列里加任务（需要持有租约）。"""
        with self.transaction():
            self._check_lease(run_id, token)
            self._enqueue(run_id, tasks)

    def _enqueue(self, run_id, tasks):
        for task in tasks:
            self.db.execute("INSERT INTO tasks(run_id,kind,url,parent_url,payload_json) VALUES(?,?,?,?,?) "
                            "ON CONFLICT(run_id,kind,url) DO NOTHING",
                            (run_id, task["kind"], task["url"], task.get("parent_url"), encode_json(task.get("payload", {}))))

    def next_task(self, run_id: str) -> dict | None:
        """取下一个还没完成的抓取任务；没有了就返回 None。"""
        row = self.db.execute("SELECT * FROM tasks WHERE run_id=? AND state='pending' ORDER BY id LIMIT 1", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def task_count(self, run_id: str, kind: str) -> int:
        """某类任务（列表页、公告、附件）一共有多少个。"""
        return self.db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()[0]

    def task_urls(self, run_id: str, kind: str) -> set[str]:
        """某类任务的全部 URL。"""
        return {row[0] for row in self.db.execute("SELECT url FROM tasks WHERE run_id=? AND kind=?", (run_id, kind))}

    def put_blob(self, data: bytes) -> str:
        """保存原始文件，返回它的 SHA-256。同样的内容只存一份，已存在的文件不会被覆盖。"""
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
            # 先写临时文件，再用硬链接放到正式位置：目标已存在时链接会失败，不会覆盖。
            # 如果别的进程同时写了同一个文件，就核对已有内容是否一致。
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read_blob(digest)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def read_blob(self, digest: str) -> bytes:
        """按 SHA-256 读取原始文件，并重新校验内容没有被改过。"""
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
        """记录一次抓取的结果。

        "保存抓取记录""把任务标为完成""把解析出的新链接加入队列"在同一个事务里完成，
        所以中断后恢复时，不会出现任务已完成、但它解析出的后续页面没进队列的情况。
        """
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
            # URL 和任务类型以数据库里的为准，调用方传进来的值只用来定位任务。
            task = dict(stored)
            version_id = None
            if digest is not None:
                # 来源以运行记录里的为准，不同来源的版本记录互不混淆。
                source = self.run(run_id)["request"].get("source_id", "cn_ccgp")
                version_id = sha256((source + "\n" + task["url"] + "\n" + digest).encode()).hexdigest()
                latest = self.db.execute("SELECT max(version) FROM versions WHERE source_id=? AND url=?",
                                         (source, task["url"])).fetchone()[0] or 0
                self.db.execute("INSERT INTO versions VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(source_id,url,sha256) DO NOTHING",
                                (version_id, source, task["url"], digest, latest + 1, captured_at))
            # 只保存必要的响应头，不保存 Set-Cookie 之类可能含凭据的头。
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
        """读取一条抓取记录。"""
        row = self.db.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if row is None:
            raise StoreError("capture_not_found")
        result = dict(row)
        result["parsed"] = json.loads(result.pop("parsed_json")) if result["parsed_json"] else None
        result["headers"] = json.loads(result.pop("headers_json"))
        return result

    def report(self, run_id: str) -> dict:
        """一次运行的汇总：运行状态、每次抓取的结果。只说明抓到了什么，不说明这些项目适不适合谁。"""
        run = self.run(run_id)
        captures = [self.capture(row[0]) for row in self.db.execute("SELECT id FROM captures WHERE run_id=? ORDER BY rowid", (run_id,))]
        attachments = {item["url"]: item for item in captures if item["kind"] == "attachment"}
        for capture in captures:
            if not capture["parsed"]:
                continue
            for link in capture["parsed"].get("attachments", ()):
                fetched = attachments.get(link["url"])
                if fetched:
                    # 在返回结果里补上附件的最新抓取状态；数据库里当初的解析结果保持不变。
                    link["fetch_status"] = "failed" if fetched["error_code"] else "fetched"
                    link["capture_id"] = fetched["id"]
                    link["error_code"] = fetched["error_code"]
                else:
                    link["fetch_status"] = link["status"]
        return {"schema_version": 1, "run": run, "captures": captures,
                "pending": self.db.execute("SELECT count(*) FROM tasks WHERE run_id=? AND state='pending'", (run_id,)).fetchone()[0]}

    def save_replay(self, capture_id: str, parser_version: str, parsed: dict) -> dict:
        """保存一次重新解析的结果。新结果单独存一条，当初的抓取记录和解析结论不变。"""
        self.capture(capture_id)
        record = {"id": uuid4().hex, "capture_id": capture_id, "parser_version": parser_version,
                  "created_at": utc_now(), "parsed": parsed}
        with self.db:
            self.db.execute("INSERT INTO replays VALUES(?,?,?,?,?)", (record["id"], capture_id,
                            parser_version, record["created_at"], encode_json(parsed)))
        return record

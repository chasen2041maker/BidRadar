"""采集记录的存储：运行、待抓取队列、抓取记录、内容版本、重新解析的结果。

旧版这里是一个 SQLite 文件，v2 换成 PostgreSQL 的 ingestion schema，对外的方法和含义保持不变，
所以 pipeline.py、tianjin.py、export.py 几乎原样搬了过来。原始文件交给 infra.blobs。

为什么每个方法都自己提交事务：采集要能随时中断、随时恢复。每抓完一页就提交一次，
进程在任何时候崩溃，最多重抓正在抓的那一页，已经记下的不会丢。

同一次运行同时只能有一个执行者：执行前要先拿租约（acquire），执行中定期续期（heartbeat）。
文件路径只由 SHA-256 决定，网页上的任何内容都不会被拿来当路径。

这是采集模块私有的存储，别的模块只能通过 ingestion.service 拿到导出的数据。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.blobs import BlobError, BlobStore
from bidradar.infra.ids import fingerprint, new_id
from bidradar.modules.ingestion.tables import CaptureRow, FetchRunRow, FetchTaskRow, ReplayRow, UrlVersionRow

LEASE = timedelta(seconds=120)
TERMINAL = ("cancelled", "succeeded", "partial", "blocked", "failed")


def utc_now() -> str:
    """当前 UTC 时间（精确到秒），用作抓取和记账时间。注意：这不是公告的发布日期。"""
    return clock.iso(clock.now())


def encode_json(value) -> str:
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class StoreError(RuntimeError):
    """存储层的错误，带错误码 code。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Store:
    """采集存储。session 是数据库会话，blobs 是原始文件目录。"""

    def __init__(self, session: Session, blobs: BlobStore):
        self.session = session
        self.blobs = blobs

    def close(self) -> None:
        """为了和旧版接口一致保留。会话由调用方负责关闭。"""

    def _commit(self) -> None:
        self.session.commit()

    # ---- 运行 ----

    def create_run(self, request: dict, key: str) -> tuple[str, bool]:
        """登记一次采集运行，返回 (run_id, 是否新建)。同一个幂等键重复登记会返回原来的运行。"""
        if not isinstance(key, str) or not key.strip() or len(key) > 128:
            raise StoreError("invalid_idempotency_key")
        digest = fingerprint(request)
        old = self.session.scalar(select(FetchRunRow).where(FetchRunRow.request_key == key))
        if old:
            if old.request_hash != digest:
                raise StoreError("idempotency_conflict")
            return old.id, False
        now = clock.now()
        run_id = new_id()
        self.session.add(
            FetchRunRow(
                id=run_id,
                request_key=key,
                request=request,
                request_hash=digest,
                status="queued",
                created_at=now,
                updated_at=now,
            )
        )
        self._commit()
        return run_id, True

    def _run_row(self, run_id: str, *, for_update: bool = False) -> FetchRunRow:
        statement = select(FetchRunRow).where(FetchRunRow.id == run_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise StoreError("run_not_found")
        return row

    def run(self, run_id: str) -> dict:
        """读取运行记录（不含租约令牌）。"""
        row = self._run_row(run_id)
        return {
            "id": row.id,
            "request_key": row.request_key,
            "request": row.request,
            "status": row.status,
            "created_at": clock.iso(row.created_at),
            "updated_at": clock.iso(row.updated_at),
            "lease_until": row.lease_until.timestamp() if row.lease_until else None,
            "error_code": row.error_code,
        }

    def acquire(self, run_id: str) -> str:
        """拿到这次运行的执行租约，返回租约令牌。别人的租约还有效时拿不到；已结束的运行也不能再执行。"""
        row = self._run_row(run_id, for_update=True)
        if row.status in TERMINAL:
            self.session.rollback()
            raise StoreError("run_terminal")
        now = clock.now()
        if row.lease_until and row.lease_until > now:
            self.session.rollback()
            raise StoreError("run_busy")
        token = new_id()
        row.status, row.lease_token, row.lease_until, row.updated_at = "running", token, now + LEASE, now
        self._commit()
        return token

    def _check_lease(self, run_id: str, token: str) -> FetchRunRow:
        row = self._run_row(run_id, for_update=True)
        if row.status != "running" or row.lease_token != token or row.lease_until <= clock.now():
            self.session.rollback()
            raise StoreError("lease_lost_or_cancelled")
        return row

    def heartbeat(self, run_id: str, token: str) -> None:
        """续期租约，同时确认运行没被取消。"""
        row = self._check_lease(run_id, token)
        now = clock.now()
        row.lease_until, row.updated_at = now + LEASE, now
        self._commit()

    def release(self, run_id: str, token: str) -> None:
        """主动释放租约（比如按了 Ctrl+C）。进程崩溃来不及释放时，等租约过期后别人就能接手。"""
        self.session.rollback()
        row = self._run_row(run_id, for_update=True)
        if row.lease_token == token and row.status == "running":
            row.status, row.lease_token, row.lease_until, row.updated_at = "queued", None, None, clock.now()
        self._commit()

    def cancel(self, run_id: str) -> None:
        """取消运行。取消是终态，之后不能再恢复执行。"""
        row = self._run_row(run_id, for_update=True)
        if row.status in ("queued", "running"):
            row.status, row.lease_token, row.lease_until, row.updated_at = "cancelled", None, None, clock.now()
        self._commit()

    def finish(self, run_id: str, token: str, status: str, error: str | None = None) -> None:
        """把运行标为结束状态并释放租约。"""
        if status not in ("succeeded", "partial", "blocked", "failed"):
            raise StoreError("invalid_terminal_status")
        row = self._check_lease(run_id, token)
        row.status, row.error_code, row.lease_token, row.lease_until = status, error, None, None
        row.updated_at = clock.now()
        self._commit()

    # ---- 抓取队列 ----

    def enqueue(self, run_id: str, token: str, tasks: list[dict]) -> None:
        """往抓取队列里加任务（需要持有租约）。同一个 URL 在同一次运行里只排一次。"""
        self._check_lease(run_id, token)
        self._enqueue(run_id, tasks)
        self._commit()

    def _enqueue(self, run_id: str, tasks) -> None:
        for task in tasks:
            self.session.execute(
                insert(FetchTaskRow)
                .values(
                    run_id=run_id,
                    kind=task["kind"],
                    url=task["url"],
                    parent_url=task.get("parent_url"),
                    state="pending",
                    payload=task.get("payload", {}),
                )
                .on_conflict_do_nothing(index_elements=["run_id", "kind", "url"])
            )

    @staticmethod
    def _task(row: FetchTaskRow) -> dict:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "kind": row.kind,
            "url": row.url,
            "parent_url": row.parent_url,
            "state": row.state,
            "payload": row.payload,
        }

    def next_task(self, run_id: str) -> dict | None:
        """取下一个还没完成的抓取任务；没有了就返回 None。"""
        row = self.session.scalar(
            select(FetchTaskRow)
            .where(FetchTaskRow.run_id == run_id, FetchTaskRow.state == "pending")
            .order_by(FetchTaskRow.id)
            .limit(1)
        )
        return self._task(row) if row else None

    def task_count(self, run_id: str, kind: str) -> int:
        return self.session.scalar(
            select(func.count())
            .select_from(FetchTaskRow)
            .where(FetchTaskRow.run_id == run_id, FetchTaskRow.kind == kind)
        )

    def task_urls(self, run_id: str, kind: str) -> set[str]:
        return set(
            self.session.scalars(
                select(FetchTaskRow.url).where(FetchTaskRow.run_id == run_id, FetchTaskRow.kind == kind)
            )
        )

    # ---- 原始文件 ----

    def put_blob(self, data: bytes) -> str:
        try:
            return self.blobs.put(data)
        except BlobError as exc:
            raise StoreError(exc.code) from exc

    def read_blob(self, digest: str) -> bytes:
        try:
            return self.blobs.read(digest)
        except BlobError as exc:
            raise StoreError(exc.code) from exc

    def blob_digests(self) -> list[str]:
        """抓取记录引用到的全部原始文件，给 verify 命令逐个校验。"""
        return list(self.session.scalars(select(CaptureRow.sha256).where(CaptureRow.sha256.is_not(None)).distinct()))

    # ---- 抓取记录 ----

    def record(
        self,
        run_id: str,
        token: str,
        task: dict,
        *,
        response=None,
        parsed: dict | None = None,
        parser_version: str | None = None,
        error: str | None = None,
        http_status: int | None = None,
        attempts: int = 0,
        following: list[dict] = (),
    ) -> str:
        """记录一次抓取的结果。

        "保存抓取记录""把任务标为完成""把解析出的新链接加入队列"在同一个事务里完成，
        所以中断后恢复时，不会出现任务已完成、但它解析出的后续页面没进队列的情况。
        """
        self._check_lease(run_id, token)
        digest = self.put_blob(response.body) if response is not None else None
        captured_at = _time(response.fetched_at) if response is not None else clock.now()
        run = self._check_lease(run_id, token)
        stored = self.session.scalar(
            select(FetchTaskRow).where(
                FetchTaskRow.id == task["id"], FetchTaskRow.run_id == run_id, FetchTaskRow.state == "pending"
            )
        )
        if stored is None:
            self.session.rollback()
            raise StoreError("task_not_pending_in_run")
        # URL 和任务类型以数据库里的为准，调用方传进来的值只用来定位任务。
        version_id = None
        if digest is not None:
            # 来源以运行记录里的为准，不同来源的版本记录互不混淆。
            source = run.request.get("source_id", "cn_ccgp")
            version_id = sha256((source + "\n" + stored.url + "\n" + digest).encode()).hexdigest()
            # 两次运行同时存同一个网址的新内容时，会读到同一个"最大版本号"、都写成下一号，后提交的撞上唯一约束。
            # 按网址加一把事务锁（提交时释放），同一个网址的版本号就一个一个分配。第一个键把这把锁和别的模块的锁分开。
            self.session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtext("ingestion.url_versions"), func.hashtext(source + "\n" + stored.url)
                    )
                )
            )
            latest = self.session.scalar(
                select(func.coalesce(func.max(UrlVersionRow.version), 0)).where(
                    UrlVersionRow.source_id == source, UrlVersionRow.url == stored.url
                )
            )
            self.session.execute(
                insert(UrlVersionRow)
                .values(
                    id=version_id,
                    source_id=source,
                    url=stored.url,
                    sha256=digest,
                    version=latest + 1,
                    first_captured_at=captured_at,
                )
                .on_conflict_do_nothing(index_elements=["source_id", "url", "sha256"])
            )
        # 只保存必要的响应头，不保存 Set-Cookie 之类可能含凭据的头。
        headers = (
            {
                key: val
                for key, val in response.headers.items()
                if key.lower() in ("content-type", "etag", "last-modified", "content-length")
            }
            if response is not None
            else {}
        )
        capture_id = new_id()
        self.session.add(
            CaptureRow(
                id=capture_id,
                run_id=run_id,
                task_id=stored.id,
                kind=stored.kind,
                url=stored.url,
                final_url=response.final_url if response else None,
                fetched_at=captured_at,
                http_status=response.http_status if response else http_status,
                sha256=digest,
                byte_count=len(response.body) if response else None,
                version_id=version_id,
                headers=headers,
                attempts=response.attempts if response else attempts,
                error_code=error,
                parser_version=parser_version,
                parsed=parsed,
            )
        )
        stored.state = "error" if error else "done"
        self._enqueue(run_id, following)
        self._commit()
        return capture_id

    @staticmethod
    def _capture(row: CaptureRow) -> dict:
        return {
            "id": row.id,
            "run_id": row.run_id,
            "task_id": row.task_id,
            "kind": row.kind,
            "url": row.url,
            "final_url": row.final_url,
            "fetched_at": clock.iso(row.fetched_at),
            "http_status": row.http_status,
            "sha256": row.sha256,
            "byte_count": row.byte_count,
            "version_id": row.version_id,
            "headers": row.headers,
            "attempts": row.attempts,
            "error_code": row.error_code,
            "parser_version": row.parser_version,
            # 返回副本：report() 会在结果里补附件状态，不能改到会话里缓存的对象。
            "parsed": json.loads(json.dumps(row.parsed)) if row.parsed is not None else None,
        }

    def capture(self, capture_id: str) -> dict:
        row = self.session.get(CaptureRow, capture_id)
        if row is None:
            raise StoreError("capture_not_found")
        return self._capture(row)

    def report(self, run_id: str) -> dict:
        """一次运行的汇总：运行状态、每次抓取的结果。只说明抓到了什么，不说明这些项目适不适合谁。"""
        run = self.run(run_id)
        captures = [
            self._capture(row)
            for row in self.session.scalars(
                select(CaptureRow).where(CaptureRow.run_id == run_id).order_by(CaptureRow.seq)
            )
        ]
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
        pending = self.session.scalar(
            select(func.count())
            .select_from(FetchTaskRow)
            .where(FetchTaskRow.run_id == run_id, FetchTaskRow.state == "pending")
        )
        return {"schema_version": 1, "run": run, "captures": captures, "pending": pending}

    def save_replay(self, capture_id: str, parser_version: str, parsed: dict) -> dict:
        """保存一次重新解析的结果。新结果单独存一条，当初的抓取记录和解析结论不变。"""
        self.capture(capture_id)
        now = clock.now()
        record = {
            "id": new_id(),
            "capture_id": capture_id,
            "parser_version": parser_version,
            "created_at": clock.iso(now),
            "parsed": parsed,
        }
        self.session.add(
            ReplayRow(
                id=record["id"], capture_id=capture_id, parser_version=parser_version, created_at=now, parsed=parsed
            )
        )
        self._commit()
        return record

"""任务队列：领取、SKIP LOCKED、租约过期后被接手、围栏（旧 Worker 写不进结果）、失败重试。"""

from datetime import timedelta

import pytest
from sqlalchemy.orm import Session

from bidradar.infra import clock, jobs
from bidradar.infra.blobs import BlobStore
from bidradar.infra.tables import JobRow
from bidradar.modules.documents import service as documents
from bidradar.modules.documents.tables import ChunkRow
from tests_v2.factories import TENDER_PDF, make_company


def advance(monkeypatch: pytest.MonkeyPatch, delta: timedelta) -> None:
    later = clock.now() + delta
    monkeypatch.setattr(clock, "now", lambda: later)


def test_enqueue_dedupes_and_claim_takes_oldest_first(db_session: Session) -> None:
    first = jobs.enqueue(db_session, "demo", {"n": 1}, dedupe_key="demo:1")
    assert jobs.enqueue(db_session, "demo", {"n": 1}, dedupe_key="demo:1") is None
    jobs.enqueue(db_session, "demo", {"n": 2})
    db_session.commit()
    job = jobs.claim(db_session, ["demo"], "w1")
    assert (job.id, job.attempts) == (first, 1)
    assert jobs.claim(db_session, ["other"], "w1") is None


def test_delayed_job_waits(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs.enqueue(db_session, "demo", {}, delay=timedelta(minutes=5))
    db_session.commit()
    assert jobs.claim(db_session, ["demo"], "w1") is None
    advance(monkeypatch, timedelta(minutes=6))
    assert jobs.claim(db_session, ["demo"], "w1") is not None


def test_running_job_with_live_lease_is_not_claimed_twice(db_session: Session) -> None:
    jobs.enqueue(db_session, "demo", {})
    db_session.commit()
    assert jobs.claim(db_session, ["demo"], "w1") is not None
    assert jobs.claim(db_session, ["demo"], "w2") is None


def test_failure_retries_then_gives_up(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs.enqueue(db_session, "demo", {}, max_attempts=2)
    db_session.commit()
    job = jobs.claim(db_session, ["demo"], "w1")
    assert jobs.fail(db_session, job, "Boom", delay=timedelta(seconds=10)) == "queued"
    advance(monkeypatch, timedelta(seconds=11))
    job = jobs.claim(db_session, ["demo"], "w1")
    assert job.attempts == 2
    assert jobs.fail(db_session, job, "Boom") == "failed"
    assert db_session.get(JobRow, job.id).last_error == "Boom"


def test_killed_worker_is_taken_over_and_results_are_written_once(
    db_session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """阶段 3 的完成标准：Worker 中途被杀，租约过期后任务被重新领取，而且结果只写一次。

    Worker A 领到解析任务后"卡死"（没写结果）。租约过期，Worker B 接手并完成。
    A 醒来再写结果：围栏检查发现租约已经不是自己的，写入被拒绝。
    """
    blobs = BlobStore(tmp_path)
    wid, users = make_company(db_session)
    document = documents.upload(
        db_session, blobs, users["member"].id, wid, filename="a.pdf", data=TENDER_PDF.read_bytes()
    )
    stuck = jobs.claim(db_session, [documents.JOB_PARSE], "worker-a")

    advance(monkeypatch, jobs.DEFAULT_LEASE + timedelta(seconds=1))
    taken = jobs.claim(db_session, [documents.JOB_PARSE], "worker-b")
    assert (taken.id, taken.attempts) == (stuck.id, 2)
    documents.handle_parse(db_session, blobs, taken)

    with pytest.raises(jobs.LeaseLost):
        documents.handle_parse(db_session, blobs, stuck)
    db_session.rollback()
    chunk_count = db_session.query(ChunkRow).filter(ChunkRow.document_id == document.id).count()
    assert chunk_count == documents.get_document(db_session, users["member"].id, wid, document.id).chunk_count > 0
    assert db_session.get(JobRow, stuck.id).status == "succeeded"


def test_job_that_keeps_crashing_workers_ends_up_failed(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    jobs.enqueue(db_session, "demo", {}, max_attempts=2)
    db_session.commit()
    for _ in range(2):
        assert jobs.claim(db_session, ["demo"], "w") is not None
        advance(monkeypatch, jobs.DEFAULT_LEASE + timedelta(seconds=1))
    assert jobs.claim(db_session, ["demo"], "w") is None
    assert db_session.query(JobRow).one().status == "failed"


def test_cancelled_job_cannot_be_completed(db_session: Session) -> None:
    jobs.enqueue(db_session, "demo", {}, dedupe_key="demo:c")
    db_session.commit()
    job = jobs.claim(db_session, ["demo"], "w")
    jobs.cancel(db_session, "demo:c")
    db_session.commit()
    with pytest.raises(jobs.LeaseLost):
        jobs.complete(db_session, job)

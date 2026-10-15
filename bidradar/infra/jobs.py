"""后台任务队列：PostgreSQL 表 + SELECT ... FOR UPDATE SKIP LOCKED + 租约。

为什么不用 Redis、Celery：少一个组件。更重要的是，建任务和写业务数据可以在同一个事务里提交：
上传文件和"解析这个文件"的任务要么都成功、要么都没发生，不会出现"文件存了、任务没排上"。

一个任务的一生：

    enqueue()   业务代码在自己的事务里排一个任务（不提交，跟着业务数据一起提交）
    claim()     Worker 领取：SKIP LOCKED 让多个 Worker 同时领时互不阻塞、也不会领到同一个任务
    extend()    任务跑得久，中途续租约；续不上说明租约已经被别人接手，立刻停手
    hold()      写结果之前，在同一个事务里确认"租约还是我的"（围栏，fencing）
    complete()  标为完成，和结果在同一个事务里提交
    fail()      出错：还有重试次数就过一会儿再排，没有了就标为失败

"任务不会被处理两次"靠的是 hold()：Worker A 卡住、租约过期，Worker B 接手并完成了任务。
A 醒来以后想写结果，hold() 发现租约已经不是自己的，抛 LeaseLost，A 的事务整个回滚，结果只写了一次。
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.tables import JobRow

DEFAULT_LEASE = timedelta(seconds=60)


class LeaseLost(Exception):
    """租约已经不属于当前 Worker：过期后被别人领走了，或者任务被取消了。"""


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    delay: timedelta | None = None,
    max_attempts: int = 3,
) -> int | None:
    """在当前事务里排一个任务，返回任务 ID。不提交事务。

    dedupe_key 相同的任务已经存在时（不管它排队中还是已完成），不再新建，返回 None。
    """
    now = clock.now()
    return session.scalar(
        insert(JobRow)
        .values(
            kind=kind,
            payload=payload,
            dedupe_key=dedupe_key,
            status="queued",
            attempts=0,
            max_attempts=max_attempts,
            run_after=now + (delay or timedelta(0)),
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(JobRow.id)
    )


def claim(session: Session, kinds: list[str], worker_id: str, lease: timedelta = DEFAULT_LEASE) -> Job | None:
    """领取一个可以执行的任务并提交；没有就返回 None。

    可以执行的任务：排队中且到了执行时间，或者执行中但租约已经过期（执行它的 Worker 崩了）。
    重试次数已经用完、租约又过期的任务直接标为失败：一个总把 Worker 搞崩的任务，不能无限循环下去。
    """
    now = clock.now()
    session.execute(
        update(JobRow)
        .where(JobRow.status == "running", JobRow.lease_until < now, JobRow.attempts >= JobRow.max_attempts)
        .values(status="failed", last_error="lease_expired_too_many_times", lease_owner=None, updated_at=now)
    )
    candidate = (
        select(JobRow.id)
        .where(
            JobRow.kind.in_(kinds),
            JobRow.attempts < JobRow.max_attempts,
            ((JobRow.status == "queued") & (JobRow.run_after <= now))
            | ((JobRow.status == "running") & (JobRow.lease_until < now)),
        )
        .order_by(JobRow.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    row = session.scalar(
        update(JobRow)
        .where(JobRow.id == candidate)
        .values(
            status="running",
            lease_owner=worker_id,
            lease_until=now + lease,
            attempts=JobRow.attempts + 1,
            updated_at=now,
        )
        .returning(JobRow)
    )
    session.commit()
    if row is None:
        return None
    return Job(
        id=row.id,
        kind=row.kind,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        lease_owner=worker_id,
    )


def hold(session: Session, job: Job) -> None:
    """在当前事务里锁住任务这一行，确认租约还是自己的。不是就抛 LeaseLost。不提交事务。

    写任务结果的事务要先调它：这样"确认租约"和"写结果"是原子的，中间不会被别人接手。
    """
    row = session.scalar(select(JobRow).where(JobRow.id == job.id).with_for_update())
    if row is None or row.status != "running" or row.lease_owner != job.lease_owner or row.lease_until <= clock.now():
        raise LeaseLost(job.id)


def extend(session: Session, job: Job, lease: timedelta = DEFAULT_LEASE) -> None:
    """续租约并提交。长任务在每一步之间调一次。"""
    hold(session, job)
    session.execute(update(JobRow).where(JobRow.id == job.id).values(lease_until=clock.now() + lease))
    session.commit()


def complete(session: Session, job: Job) -> None:
    """标为完成。不提交：调用方把它和任务结果放在同一个事务里提交。"""
    hold(session, job)
    session.execute(
        update(JobRow)
        .where(JobRow.id == job.id)
        .values(status="succeeded", lease_owner=None, lease_until=None, updated_at=clock.now())
    )


def fail(
    session: Session, job: Job, error: str, *, retry: bool = True, delay: timedelta = timedelta(seconds=30)
) -> str:
    """记一次失败并提交，返回任务的新状态。租约已经丢了就什么都不做（别人在处理）。

    还有重试次数并且 retry=True：过 delay 之后重新排队。否则标为失败，等人处理。
    """
    session.rollback()
    try:
        hold(session, job)
    except LeaseLost:
        session.rollback()
        return "lease_lost"
    status = "queued" if retry and job.attempts < job.max_attempts else "failed"
    now = clock.now()
    session.execute(
        update(JobRow)
        .where(JobRow.id == job.id)
        .values(
            status=status,
            last_error=error[:2000],
            lease_owner=None,
            lease_until=None,
            run_after=now + delay,
            updated_at=now,
        )
    )
    session.commit()
    return status


def cancel(session: Session, dedupe_key: str) -> None:
    """取消一个还没完成的任务。不提交。正在执行的 Worker 下一次 hold() 时会发现并停手。"""
    session.execute(
        update(JobRow)
        .where(JobRow.dedupe_key == dedupe_key, JobRow.status.in_(("queued", "running")))
        .values(status="cancelled", lease_owner=None, lease_until=None, updated_at=clock.now())
    )


def get(session: Session, job_id: int) -> JobRow | None:
    return session.get(JobRow, job_id)

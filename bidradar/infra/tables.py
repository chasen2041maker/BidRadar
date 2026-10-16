"""infra 自己的表，放在 infra schema 里。所有模块共用，但 infra 不认识任何业务。

- idempotency_keys：幂等键。同一个修改请求重复提交，只执行一次，返回第一次的结果。
- outbox：待投递的事件。业务数据和事件在同一个事务里写入，再由 Worker 投递给订阅方。
- jobs：后台任务队列。建任务和写业务数据在同一个事务里提交，不会出现"数据写了、任务没排上"。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Identity, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("infra")


class IdempotencyKey(Base):
    """一条幂等记录：在 scope（通常是公司 ID）里，operation 这个动作用 key 提交过一次，结果是 response。

    request_hash 是请求内容（含操作人）的指纹。同一个键、不同的内容，说明客户端把键用错了，报冲突。
    记录只插入、不修改，也不过期：键是客户端随机生成的，存着成本很低，删了反而可能让旧请求再执行一次。
    """

    __tablename__ = "idempotency_keys"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    """一个待投递的事件。

    event_key 由业务内容决定（比如 profile:<公司>:<版本>），同一件事只会有一条记录：
    重复发布不会产生重复事件。id 是自增序号，消费者按它的顺序处理。
    published_at 为空表示还没投递。
    """

    __tablename__ = "outbox"
    __table_args__ = (
        # 只给"还没投递"的事件建索引：投递完的事件越积越多，但没人再按这个条件查它们。
        Index("ix_outbox_pending", "id", postgresql_where="published_at IS NULL"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_key: Mapped[str] = mapped_column(String(200), unique=True)
    topic: Mapped[str] = mapped_column(String(100))
    payload: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)


class JobRow(Base):
    """一个后台任务。Worker 用 SELECT ... FOR UPDATE SKIP LOCKED 领取，领到后持有租约。

    状态：queued（排队）→ running（执行中）→ succeeded / failed / cancelled。
    running 但租约已过期，说明执行它的 Worker 崩了，别的 Worker 可以重新领取。
    dedupe_key 不为空时唯一：同一件事（比如"解析文档 X"）只会排一个任务。
    """

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_ready", "kind", "run_after", postgresql_where="status IN ('queued', 'running')"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Any] = mapped_column(JSONB)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

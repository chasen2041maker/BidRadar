"""tracking 的表定义，全部放在 PostgreSQL 的 tracking schema 里。

    decisions / decision_history     对每条公告的人工决定，及其历史
    watches / watch_history          关注设置，以及用来比较的基线（关注时公告的样子）
    changes                          检测到的变化：哪些维度、前后对比
    notifications / notification_reads  站内提醒，以及每个人的已读记录
    reassessments                    自动复核：一次变化触发的一次研究

全部是公司私有数据，都带 workspace_id。改了这里，要用 alembic revision --autogenerate 生成迁移。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("tracking")


class DecisionRow(Base):
    """当前决定。version 是乐观锁：两个人同时改，后提交的会收到冲突，而不是悄悄覆盖。"""

    __tablename__ = "decisions"

    workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    notice_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(32))
    # 做决定时看到的是公告的哪个版本。公告后来更新了，能看出这个决定是基于旧版本做的。
    observation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DecisionHistoryRow(Base):
    __tablename__ = "decision_history"
    __table_args__ = (UniqueConstraint("workspace_id", "notice_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    notice_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    actor_id: Mapped[str] = mapped_column(String(32))
    observation_id: Mapped[str] = mapped_column(String(64))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatchRow(Base):
    """一个关注。baseline 是上一次比较后的公告包（catalog.research_bundle 的结果），下次变化跟它比。"""

    __tablename__ = "watches"
    __table_args__ = (
        UniqueConstraint("workspace_id", "notice_id"),
        Index("ix_watches_notice_active", "notice_id", "active"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    notice_id: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean)
    auto_reassess: Mapped[bool] = mapped_column(Boolean)
    actor_id: Mapped[str] = mapped_column(String(32))
    # 开启自动复核时，发起人的成员版本。之后版本一变（被改角色、移出再加回），委托就失效。
    membership_version: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[list[str]] = mapped_column(JSONB)
    warnings: Mapped[list[str]] = mapped_column(JSONB)
    baseline: Mapped[dict[str, Any]] = mapped_column(JSONB)
    baseline_snapshot: Mapped[int] = mapped_column(BigInteger)
    profile_version: Mapped[int] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WatchHistoryRow(Base):
    __tablename__ = "watch_history"
    __table_args__ = (UniqueConstraint("watch_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    watch_id: Mapped[str] = mapped_column(String(32), ForeignKey("watches.id"))
    version: Mapped[int] = mapped_column(Integer)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChangeRow(Base):
    """检测到的一次变化。同一个关注、同一版比较规则、同样的内容变化只记一次（fingerprint 唯一）。"""

    __tablename__ = "changes"
    __table_args__ = (
        UniqueConstraint("watch_id", "rule_version", "fingerprint"),
        Index("ix_changes_workspace_seq", "workspace_id", "seq"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    watch_id: Mapped[str] = mapped_column(String(32), ForeignKey("watches.id"))
    notice_id: Mapped[str] = mapped_column(String(64))
    # 变化从哪来：catalog（公告有新版本）或 workspace（档案确认了新版本）
    source: Mapped[str] = mapped_column(String(16))
    rule_version: Mapped[str] = mapped_column(String(32))
    fingerprint: Mapped[str] = mapped_column(String(64))
    diff: Mapped[dict[str, Any]] = mapped_column(JSONB)
    catalog_snapshot: Mapped[int] = mapped_column(BigInteger)
    profile_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationRow(Base):
    """一条站内提醒。dedupe_key 唯一：同一个变化、同一种提醒只有一条。"""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_workspace_seq", "workspace_id", "seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    notice_id: Mapped[str] = mapped_column(String(64))
    watch_id: Mapped[str | None] = mapped_column(String(32))
    change_id: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NotificationReadRow(Base):
    """已读按人记：一个人读了，不影响同事看到"未读"。"""

    __tablename__ = "notification_reads"

    notification_id: Mapped[str] = mapped_column(String(32), ForeignKey("notifications.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    read_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReassessmentRow(Base):
    """一次自动复核：某个变化触发、按关注里的委托去做的一次研究。

    state：pending（等着建研究任务）→ dispatched（研究任务已建）→ finished / cancelled / rejected。
    关注被修改、发起人权限变化、同一关注又有更新的变化，都会让还没结束的旧复核取消。
    """

    __tablename__ = "reassessments"
    __table_args__ = (Index("ix_reassessments_watch_state", "watch_id", "state"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    watch_id: Mapped[str] = mapped_column(String(32), ForeignKey("watches.id"))
    change_id: Mapped[str] = mapped_column(String(32), ForeignKey("changes.id"), unique=True)
    version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

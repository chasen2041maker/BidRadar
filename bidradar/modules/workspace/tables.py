"""workspace 的表定义，全部放在 PostgreSQL 的 workspace schema 里。

改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。

私有表都带 workspace_id。现在由 service 和 repo 保证"只查本公司的数据"；
行级安全（RLS）是第二道防线，见 docs/exercises.md 的练习 1。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata

ROLE_CHECK = "role IN ('admin', 'member', 'viewer')"


class Base(DeclarativeBase):
    metadata = module_metadata("workspace")


class WorkspaceRow(Base):
    """一家公司。current_profile_version 是当前正式档案的版本号，0 表示还没有正式档案。

    它同时是档案的乐观锁：提议和确认都要带上"我看到的版本号"，不一致就说明别人刚改过。
    """

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    current_profile_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MembershipRow(Base):
    """谁在哪家公司、什么角色、是否有效。被移出的成员不删行，只把 active 置为 False，历史仍可追溯。

    version 是乐观锁：管理员 A 刚把某人移出，管理员 B 用没刷新的旧页面改他的角色，
    B 带的版本号已经过期，修改会被拒绝，而不是把已移出的人"复活"。
    """

    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint(ROLE_CHECK, name="role"),
        Index("ix_memberships_user_id", "user_id", "active"),
    )

    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), primary_key=True)
    # user_id 是 identity 模块的用户 ID，跨模块只存值，不建外键。
    user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MembershipEventRow(Base):
    """成员变更的审计记录，只插入不修改。actor_id 为空表示命令行初始化，不冒充成某个管理员的操作。"""

    __tablename__ = "membership_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "version"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["memberships.workspace_id", "memberships.user_id"]),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    user_id: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str | None] = mapped_column(String(32))
    old_role: Mapped[str | None] = mapped_column(String(16))
    old_active: Mapped[bool | None] = mapped_column(Boolean)
    new_role: Mapped[str] = mapped_column(String(16))
    new_active: Mapped[bool] = mapped_column(Boolean)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProfileProposalRow(Base):
    """一份档案修改提议。成员和管理员都能提，只插入不修改；是否已确认看 profile_versions 里有没有它。"""

    __tablename__ = "profile_proposals"
    __table_args__ = (Index("ix_profile_proposals_workspace_id", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"))
    base_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    proposed_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProfileVersionRow(Base):
    """一个正式档案版本，只插入不修改。每家公司从 1 开始递增。"""

    __tablename__ = "profile_versions"

    workspace_id: Mapped[str] = mapped_column(String(32), ForeignKey("workspaces.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 一份提议最多确认一次。
    proposal_id: Mapped[str] = mapped_column(String(32), ForeignKey("profile_proposals.id"), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confirmed_by: Mapped[str] = mapped_column(String(32))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

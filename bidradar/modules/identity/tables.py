"""identity 的表定义，全部放在 PostgreSQL 的 identity schema 里。

改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("identity")


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 存的是统一转成小写后的用户名，唯一约束才能挡住 Demo.Admin 和 demo.admin 两个账号并存。
    username: Mapped[str] = mapped_column(String(64), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SessionRow(Base):
    """一个登录会话。主键是会话令牌的哈希，令牌原文只在浏览器的 Cookie 里。"""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_user_id", "user_id"), Index("ix_sessions_expires_at", "expires_at"))

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    # 同一个模块里的表可以建外键；跨模块的 ID 只存值（见 docs/dev-guide.md 的"数据库迁移"一节）。
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginLimitRow(Base):
    """按用户名记登录失败次数。用户名先哈希再存：不存在的用户名也会有一行，不想把别人试过的名字留在库里。"""

    __tablename__ = "login_limits"

    username_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failures: Mapped[int] = mapped_column(Integer)

"""ingestion 的表定义，全部放在 PostgreSQL 的 ingestion schema 里。

    fetch_runs    一次采集运行：请求参数、幂等键、状态、租约
    fetch_tasks   这次运行要抓的页面队列：列表页、公告页、附件
    captures      每次抓取的结果：状态码、响应头、原始文件的 SHA-256、当时的解析结果
    url_versions  同一个 URL 的内容版本：内容变了就是新版本
    replays       之后用新解析器重新解析的结果，不覆盖当初的结论

原始文件本身不进数据库，按 SHA-256 存在 infra.blobs 管的目录里。
改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Identity, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("ingestion")


class FetchRunRow(Base):
    __tablename__ = "fetch_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 幂等键：同一个键重复登记，返回原来的运行；键相同但请求参数不同，报冲突。
    request_key: Mapped[str] = mapped_column(String(128), unique=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 同一次运行同时只能有一个执行者：执行前拿租约，执行中定期续期。进程崩了，租约过期后别人能接手。
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class FetchTaskRow(Base):
    __tablename__ = "fetch_tasks"
    __table_args__ = (UniqueConstraint("run_id", "kind", "url"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("fetch_runs.id"))
    kind: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    parent_url: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class UrlVersionRow(Base):
    __tablename__ = "url_versions"
    __table_args__ = (
        UniqueConstraint("source_id", "url", "sha256"),
        UniqueConstraint("source_id", "url", "version"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    first_captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CaptureRow(Base):
    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 写入顺序。汇总时按它排序，和抓取的先后一致。
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("fetch_runs.id"))
    task_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("fetch_tasks.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    http_status: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    byte_count: Mapped[int | None] = mapped_column(Integer)
    version_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("url_versions.id"))
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempts: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    parser_version: Mapped[str | None] = mapped_column(String(64))
    parsed: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class ReplayRow(Base):
    __tablename__ = "replays"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    capture_id: Mapped[str] = mapped_column(String(32), ForeignKey("captures.id"))
    parser_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parsed: Mapped[dict[str, Any]] = mapped_column(JSONB)

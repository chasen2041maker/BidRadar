"""documents 的表定义，全部放在 PostgreSQL 的 documents schema 里。

    documents  上传的招标文件（或以后从公开来源取得的附件）。文件本身按 SHA-256 存在 infra.blobs 管的目录里
    chunks     解析出的切片：带页码、章节路径，研究时引用的就是它们

workspace_id 为空表示公开文件（各公司共用）；不为空表示某家公司私有的上传，只有这家公司能读。
改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("documents")


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # 同一家公司重复上传同一份文件（内容相同），只存一份。公开文件（workspace_id 为空）之间也一样，
        # 所以要 NULLS NOT DISTINCT：默认情况下两个 NULL 不算重复。
        UniqueConstraint("workspace_id", "sha256", postgresql_nulls_not_distinct=True),
        Index("ix_documents_workspace_notice", "workspace_id", "notice_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32))
    notice_id: Mapped[str | None] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(200))
    file_type: Mapped[str] = mapped_column(String(8))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    # queued（等待解析）→ parsing → parsed / failed
    status: Mapped[str] = mapped_column(String(16))
    parser_version: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    warning: Mapped[str | None] = mapped_column(String(64))
    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    parsed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChunkRow(Base):
    __tablename__ = "chunks"
    __table_args__ = (UniqueConstraint("document_id", "seq"),)

    # sha256(文档 SHA-256、序号、文本 SHA-256)，同一份文件、同一版解析器永远得到同样的 ID。
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(32), ForeignKey("documents.id"))
    seq: Mapped[int] = mapped_column(Integer)
    page: Mapped[int | None] = mapped_column(Integer)
    section_path: Mapped[list[str]] = mapped_column(JSONB)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(64))

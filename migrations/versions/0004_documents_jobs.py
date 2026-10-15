"""阶段 3：documents（招标文件、切片）和 infra.jobs（后台任务队列）的表。

由 alembic revision --autogenerate 生成，人工读过：全部是新建表。

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=True),
        sa.Column("notice_id", sa.String(length=64), nullable=True),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("file_type", sa.String(length=8), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("parser_version", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("warning", sa.String(length=64), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "workspace_id", "sha256", name=op.f("uq_documents_workspace_id_sha256"), postgresql_nulls_not_distinct=True
        ),
        schema="documents",
    )
    op.create_index(
        "ix_documents_workspace_notice", "documents", ["workspace_id", "notice_id"], unique=False, schema="documents"
    )
    op.create_table(
        "chunks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("document_id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section_path", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.documents.id"], name=op.f("fk_chunks_document_id_documents")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("document_id", "seq", name=op.f("uq_chunks_document_id_seq")),
        schema="documents",
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_jobs_dedupe_key")),
        schema="infra",
    )
    op.create_index(
        "ix_jobs_ready",
        "jobs",
        ["kind", "run_after"],
        unique=False,
        schema="infra",
        postgresql_where="status IN ('queued', 'running')",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_jobs_ready", table_name="jobs", schema="infra", postgresql_where="status IN ('queued', 'running')"
    )
    op.drop_table("jobs", schema="infra")
    op.drop_table("chunks", schema="documents")
    op.drop_index("ix_documents_workspace_notice", table_name="documents", schema="documents")
    op.drop_table("documents", schema="documents")

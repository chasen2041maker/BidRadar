"""阶段 2：catalog（公告、观察、导入批次）和 ingestion（采集运行、抓取队列、抓取记录、内容版本）的表。

由 alembic revision --autogenerate 生成，人工读过：全部是新建表。

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "imports",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.String(length=128), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("run_status", sa.String(length=16), nullable=False),
        sa.Column("observations", sa.Integer(), nullable=False),
        sa.Column("failures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_imports")),
        sa.UniqueConstraint("seq", name=op.f("uq_imports_seq")),
        schema="catalog",
    )
    op.create_table(
        "notices",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("source_record_key", sa.Text(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notices")),
        schema="catalog",
    )
    op.create_table(
        "notice_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("import_id", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("normalizer_rank", sa.Integer(), nullable=False),
        sa.Column("simulation", sa.Boolean(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["import_id"], ["catalog.imports.id"], name=op.f("fk_notice_versions_import_id_imports")
        ),
        sa.ForeignKeyConstraint(
            ["notice_id"], ["catalog.notices.id"], name=op.f("fk_notice_versions_notice_id_notices")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notice_versions")),
        sa.UniqueConstraint("seq", name=op.f("uq_notice_versions_seq")),
        schema="catalog",
    )
    op.create_index(
        "ix_notice_versions_notice", "notice_versions", ["notice_id", "observed_at"], unique=False, schema="catalog"
    )
    op.create_table(
        "fetch_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("request_key", sa.String(length=128), nullable=False),
        sa.Column("request", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_token", sa.String(length=32), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_runs")),
        sa.UniqueConstraint("request_key", name=op.f("uq_fetch_runs_request_key")),
        schema="ingestion",
    )
    op.create_table(
        "url_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("first_captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_url_versions")),
        sa.UniqueConstraint("source_id", "url", "sha256", name=op.f("uq_url_versions_source_id_url_sha256")),
        sa.UniqueConstraint("source_id", "url", "version", name=op.f("uq_url_versions_source_id_url_version")),
        schema="ingestion",
    )
    op.create_table(
        "fetch_tasks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("parent_url", sa.Text(), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion.fetch_runs.id"], name=op.f("fk_fetch_tasks_run_id_fetch_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fetch_tasks")),
        sa.UniqueConstraint("run_id", "kind", "url", name=op.f("uq_fetch_tasks_run_id_kind_url")),
        schema="ingestion",
    )
    op.create_table(
        "captures",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.BigInteger(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("byte_count", sa.Integer(), nullable=True),
        sa.Column("version_id", sa.String(length=64), nullable=True),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("parser_version", sa.String(length=64), nullable=True),
        sa.Column("parsed", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion.fetch_runs.id"], name=op.f("fk_captures_run_id_fetch_runs")),
        sa.ForeignKeyConstraint(
            ["task_id"], ["ingestion.fetch_tasks.id"], name=op.f("fk_captures_task_id_fetch_tasks")
        ),
        sa.ForeignKeyConstraint(
            ["version_id"], ["ingestion.url_versions.id"], name=op.f("fk_captures_version_id_url_versions")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_captures")),
        sa.UniqueConstraint("seq", name=op.f("uq_captures_seq")),
        sa.UniqueConstraint("task_id", name=op.f("uq_captures_task_id")),
        schema="ingestion",
    )
    op.create_table(
        "replays",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("capture_id", sa.String(length=32), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("parsed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(["capture_id"], ["ingestion.captures.id"], name=op.f("fk_replays_capture_id_captures")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_replays")),
        schema="ingestion",
    )


def downgrade() -> None:
    op.drop_table("replays", schema="ingestion")
    op.drop_table("captures", schema="ingestion")
    op.drop_table("fetch_tasks", schema="ingestion")
    op.drop_table("url_versions", schema="ingestion")
    op.drop_table("fetch_runs", schema="ingestion")
    op.drop_index("ix_notice_versions_notice", table_name="notice_versions", schema="catalog")
    op.drop_table("notice_versions", schema="catalog")
    op.drop_table("notices", schema="catalog")
    op.drop_table("imports", schema="catalog")

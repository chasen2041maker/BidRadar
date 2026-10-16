"""阶段 4：research（研究任务、步骤结果、报告、模型调用账本、抽取缓存）的表。

由 alembic revision --autogenerate 生成，人工读过：全部是新建表。

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "requirement_sets",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_requirement_sets")),
        schema="research",
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("parent_run_id", sa.String(length=32), nullable=True),
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("catalog_snapshot", sa.BigInteger(), nullable=False),
        sa.Column("observation_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("document_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_version", sa.String(length=32), nullable=False),
        sa.Column("versions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("estimate_micro", sa.BigInteger(), nullable=False),
        sa.Column("delegation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        schema="research",
    )
    op.create_index(
        "ix_runs_workspace_created", "runs", ["workspace_id", "created_at"], unique=False, schema="research"
    )
    op.create_table(
        "llm_calls",
        sa.Column("call_key", sa.String(length=200), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("reserved", sa.BigInteger(), nullable=False),
        sa.Column("charged", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("price_version", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], name=op.f("fk_llm_calls_run_id_runs")),
        sa.PrimaryKeyConstraint("call_key", name=op.f("pk_llm_calls")),
        schema="research",
    )
    op.create_index("ix_llm_calls_workspace", "llm_calls", ["workspace_id", "state"], unique=False, schema="research")
    op.create_table(
        "reports",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], name=op.f("fk_reports_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_reports")),
        schema="research",
    )
    op.create_table(
        "run_steps",
        sa.Column("run_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["research.runs.id"], name=op.f("fk_run_steps_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", "name", "input_hash", name=op.f("pk_run_steps")),
        schema="research",
    )


def downgrade() -> None:
    op.drop_table("run_steps", schema="research")
    op.drop_table("reports", schema="research")
    op.drop_index("ix_llm_calls_workspace", table_name="llm_calls", schema="research")
    op.drop_table("llm_calls", schema="research")
    op.drop_index("ix_runs_workspace_created", table_name="runs", schema="research")
    op.drop_table("runs", schema="research")
    op.drop_table("requirement_sets", schema="research")

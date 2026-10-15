"""阶段 5：tracking（决定、关注、变化、提醒、自动复核）的表。

由 alembic revision --autogenerate 生成，人工读过：全部是新建表。

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decision_history")),
        sa.UniqueConstraint(
            "workspace_id", "notice_id", "version", name=op.f("uq_decision_history_workspace_id_notice_id_version")
        ),
        schema="tracking",
    )
    op.create_table(
        "decisions",
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("observation_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "notice_id", name=op.f("pk_decisions")),
        schema="tracking",
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("watch_id", sa.String(length=32), nullable=True),
        sa.Column("change_id", sa.String(length=32), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
        sa.UniqueConstraint("seq", name=op.f("uq_notifications_seq")),
        schema="tracking",
    )
    op.create_index(
        "ix_notifications_workspace_seq", "notifications", ["workspace_id", "seq"], unique=False, schema="tracking"
    )
    op.create_table(
        "watches",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("auto_reassess", sa.Boolean(), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=False),
        sa.Column("membership_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("baseline", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("baseline_snapshot", sa.BigInteger(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watches")),
        sa.UniqueConstraint("workspace_id", "notice_id", name=op.f("uq_watches_workspace_id_notice_id")),
        schema="tracking",
    )
    op.create_index("ix_watches_notice_active", "watches", ["notice_id", "active"], unique=False, schema="tracking")
    op.create_table(
        "changes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("watch_id", sa.String(length=32), nullable=False),
        sa.Column("notice_id", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("catalog_snapshot", sa.BigInteger(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["watch_id"], ["tracking.watches.id"], name=op.f("fk_changes_watch_id_watches")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_changes")),
        sa.UniqueConstraint("seq", name=op.f("uq_changes_seq")),
        sa.UniqueConstraint(
            "watch_id", "rule_version", "fingerprint", name=op.f("uq_changes_watch_id_rule_version_fingerprint")
        ),
        schema="tracking",
    )
    op.create_index("ix_changes_workspace_seq", "changes", ["workspace_id", "seq"], unique=False, schema="tracking")
    op.create_table(
        "notification_reads",
        sa.Column("notification_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["tracking.notifications.id"],
            name=op.f("fk_notification_reads_notification_id_notifications"),
        ),
        sa.PrimaryKeyConstraint("notification_id", "user_id", name=op.f("pk_notification_reads")),
        schema="tracking",
    )
    op.create_table(
        "watch_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("watch_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["watch_id"], ["tracking.watches.id"], name=op.f("fk_watch_history_watch_id_watches")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_watch_history")),
        sa.UniqueConstraint("watch_id", "version", name=op.f("uq_watch_history_watch_id_version")),
        schema="tracking",
    )
    op.create_table(
        "reassessments",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("watch_id", sa.String(length=32), nullable=False),
        sa.Column("change_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["change_id"], ["tracking.changes.id"], name=op.f("fk_reassessments_change_id_changes")
        ),
        sa.ForeignKeyConstraint(["watch_id"], ["tracking.watches.id"], name=op.f("fk_reassessments_watch_id_watches")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reassessments")),
        sa.UniqueConstraint("change_id", name=op.f("uq_reassessments_change_id")),
        schema="tracking",
    )
    op.create_index(
        "ix_reassessments_watch_state", "reassessments", ["watch_id", "state"], unique=False, schema="tracking"
    )


def downgrade() -> None:
    op.drop_index("ix_reassessments_watch_state", table_name="reassessments", schema="tracking")
    op.drop_table("reassessments", schema="tracking")
    op.drop_table("watch_history", schema="tracking")
    op.drop_table("notification_reads", schema="tracking")
    op.drop_index("ix_changes_workspace_seq", table_name="changes", schema="tracking")
    op.drop_table("changes", schema="tracking")
    op.drop_index("ix_watches_notice_active", table_name="watches", schema="tracking")
    op.drop_table("watches", schema="tracking")
    op.drop_index("ix_notifications_workspace_seq", table_name="notifications", schema="tracking")
    op.drop_table("notifications", schema="tracking")
    op.drop_table("decisions", schema="tracking")
    op.drop_table("decision_history", schema="tracking")

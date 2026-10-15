"""阶段 1：identity（用户、会话、登录限流）、workspace（公司、成员、档案）和 infra（幂等键、outbox）的表。

由 alembic revision --autogenerate 生成，人工读过：全部是新建表，没有改名或删列，不会丢数据。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_limits",
        sa.Column("username_key", sa.String(length=64), nullable=False),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("username_key", name=op.f("pk_login_limits")),
        schema="identity",
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        schema="identity",
    )
    op.create_table(
        "sessions",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("csrf_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["identity.users.id"], name=op.f("fk_sessions_user_id_users")),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_sessions")),
        schema="identity",
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"], unique=False, schema="identity")
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False, schema="identity")
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("current_profile_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        schema="workspace",
    )
    op.create_table(
        "memberships",
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member', 'viewer')", name=op.f("ck_memberships_role")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.workspaces.id"], name=op.f("fk_memberships_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_memberships")),
        schema="workspace",
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id", "active"], unique=False, schema="workspace")
    op.create_table(
        "profile_proposals",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("proposed_by", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.workspaces.id"], name=op.f("fk_profile_proposals_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_proposals")),
        schema="workspace",
    )
    op.create_index(
        "ix_profile_proposals_workspace_id",
        "profile_proposals",
        ["workspace_id", "created_at"],
        unique=False,
        schema="workspace",
    )
    op.create_table(
        "membership_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("old_role", sa.String(length=16), nullable=True),
        sa.Column("old_active", sa.Boolean(), nullable=True),
        sa.Column("new_role", sa.String(length=16), nullable=False),
        sa.Column("new_active", sa.Boolean(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace.memberships.workspace_id", "workspace.memberships.user_id"],
            name=op.f("fk_membership_events_workspace_id_user_id_memberships"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_membership_events")),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "version", name=op.f("uq_membership_events_workspace_id_user_id_version")
        ),
        schema="workspace",
    )
    op.create_table(
        "profile_versions",
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_by", sa.String(length=32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["workspace.profile_proposals.id"],
            name=op.f("fk_profile_versions_proposal_id_profile_proposals"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspace.workspaces.id"], name=op.f("fk_profile_versions_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("workspace_id", "version", name=op.f("pk_profile_versions")),
        sa.UniqueConstraint("proposal_id", name=op.f("uq_profile_versions_proposal_id")),
        schema="workspace",
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "operation", "key", name=op.f("pk_idempotency_keys")),
        schema="infra",
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("event_key", sa.String(length=200), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("event_key", name=op.f("uq_outbox_event_key")),
        schema="infra",
    )
    op.create_index(
        "ix_outbox_pending", "outbox", ["id"], unique=False, schema="infra", postgresql_where="published_at IS NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_pending", table_name="outbox", schema="infra", postgresql_where="published_at IS NULL")
    op.drop_table("outbox", schema="infra")
    op.drop_table("idempotency_keys", schema="infra")
    op.drop_table("profile_versions", schema="workspace")
    op.drop_table("membership_events", schema="workspace")
    op.drop_index("ix_profile_proposals_workspace_id", table_name="profile_proposals", schema="workspace")
    op.drop_table("profile_proposals", schema="workspace")
    op.drop_index("ix_memberships_user_id", table_name="memberships", schema="workspace")
    op.drop_table("memberships", schema="workspace")
    op.drop_table("workspaces", schema="workspace")
    op.drop_index("ix_sessions_user_id", table_name="sessions", schema="identity")
    op.drop_index("ix_sessions_expires_at", table_name="sessions", schema="identity")
    op.drop_table("sessions", schema="identity")
    op.drop_table("users", schema="identity")
    op.drop_table("login_limits", schema="identity")

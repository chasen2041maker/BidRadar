"""为每个模块建一个 PostgreSQL schema，此外什么都不建。

每个模块只读写自己的 schema。以后要把某个模块拆成独立服务，把它的 schema 整个搬走就行。

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.schema import CreateSchema, DropSchema

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 名单直接写在这里，不从应用代码导入。迁移发布后内容就不能再变，而应用代码会继续改。
SCHEMAS = ("identity", "workspace", "catalog", "ingestion", "documents", "research", "tracking", "infra")


def upgrade() -> None:
    for name in SCHEMAS:
        op.execute(CreateSchema(name))


def downgrade() -> None:
    # 不加 CASCADE：schema 里还有表就报错。宁可回退失败，也不悄悄删掉数据。
    for name in reversed(SCHEMAS):
        op.execute(DropSchema(name))

"""documents 的表定义，全部放在 PostgreSQL 的 documents schema 里。

计划中的表：documents、chunks。现在还没有表。
改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from sqlalchemy.orm import DeclarativeBase

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("documents")

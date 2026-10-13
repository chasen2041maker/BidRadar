"""catalog 的表定义，全部放在 PostgreSQL 的 catalog schema 里。

计划中的表：notices、notice_versions、notice_links。现在还没有表。
改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from sqlalchemy.orm import DeclarativeBase

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("catalog")

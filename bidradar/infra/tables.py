"""infra 自己的表定义，放在 infra schema 里。

计划中的表：jobs（后台任务队列）、outbox（待投递的事件）。现在还没有表。
"""

from sqlalchemy.orm import DeclarativeBase

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("infra")

"""所有表定义的总清单。Alembic 生成和检查迁移、测试核对 schema 时都从这里取。

app 是唯一知道"一共有哪些模块"的地方，所以清单放在这里。新增一个模块，就在这里加一行。
"""

from bidradar.infra import tables as infra_tables
from bidradar.modules.catalog import tables as catalog_tables
from bidradar.modules.documents import tables as documents_tables
from bidradar.modules.identity import tables as identity_tables
from bidradar.modules.ingestion import tables as ingestion_tables
from bidradar.modules.research import tables as research_tables
from bidradar.modules.tracking import tables as tracking_tables
from bidradar.modules.workspace import tables as workspace_tables

ALL_METADATA = [
    identity_tables.Base.metadata,
    workspace_tables.Base.metadata,
    catalog_tables.Base.metadata,
    ingestion_tables.Base.metadata,
    documents_tables.Base.metadata,
    research_tables.Base.metadata,
    tracking_tables.Base.metadata,
    infra_tables.Base.metadata,
]

# 本项目用到的全部 schema。Alembic 只比较这些 schema，不碰 public 和数据库里的其他东西。
SCHEMAS = [metadata.schema for metadata in ALL_METADATA]

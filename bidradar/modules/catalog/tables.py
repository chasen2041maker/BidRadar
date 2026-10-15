"""catalog 的表定义，全部放在 PostgreSQL 的 catalog schema 里。

    notices          一条公告的身份：来源、是否演示、来源里的记录键。ID 由这三样算出，永远不变
    notice_versions  公告的每个观察（某次抓取得到的内容），只插入、不修改
    imports          每次导入的批次，包括失败记录：用来区分"抓取失败"和"没有项目"

"当前版本"不存成一列，而是查询时按规则选（repo.current_versions）：
这样搜索翻页能固定在一个快照上，期间新导入的数据不会让前后几页互相矛盾。

更正、结果公告和原公告的关联也不存表，查询时按 models.relationships 现算：
关联依赖整个目录里有哪些公告，存成表的话，每导入一条都要重算一遍。数据量上来以后再改成物化。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Identity, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("catalog")


class NoticeRow(Base):
    __tablename__ = "notices"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(64))
    simulation: Mapped[bool] = mapped_column(Boolean)
    source_record_key: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class NoticeVersionRow(Base):
    """一个观察。id 是观察内容的 SHA-256：内容一变 ID 就变，同样的内容重复导入只存一份。"""

    __tablename__ = "notice_versions"
    __table_args__ = (Index("ix_notice_versions_notice", "notice_id", "observed_at"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # 接收序号：目录收到数据的顺序，不代表公告的新旧。快照就是"序号不超过某个值"。
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    notice_id: Mapped[str] = mapped_column(String(64), ForeignKey("notices.id"))
    import_id: Mapped[str] = mapped_column(String(64), ForeignKey("imports.id"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    normalizer_rank: Mapped[int] = mapped_column(Integer)
    simulation: Mapped[bool] = mapped_column(Boolean)
    kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class ImportRow(Base):
    """一次导入。id 是整个批次的 SHA-256，同一个批次重复导入只记一次。"""

    __tablename__ = "imports"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), unique=True)
    run_id: Mapped[str] = mapped_column(String(128))
    source_id: Mapped[str] = mapped_column(String(64))
    simulation: Mapped[bool] = mapped_column(Boolean)
    run_status: Mapped[str] = mapped_column(String(16))
    observations: Mapped[int] = mapped_column(Integer)
    failures: Mapped[list[Any]] = mapped_column(JSONB)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

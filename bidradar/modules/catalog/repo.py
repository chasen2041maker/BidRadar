"""catalog 的数据库读写，只给本模块的 service 用。函数接收会话参数，只读写，不提交事务。"""

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert
from sqlalchemy.orm import Session

from bidradar.modules.catalog.tables import ImportRow, NoticeRow, NoticeVersionRow

# "当前版本"的排序规则：抓取时间新的优先；同一时刻抓到的，规范化器版本高的优先；最后看接收序号。
CURRENT_ORDER = (
    NoticeVersionRow.observed_at.desc(),
    NoticeVersionRow.normalizer_rank.desc(),
    NoticeVersionRow.seq.desc(),
)


def max_seq(session: Session) -> int:
    return session.scalar(select(func.coalesce(func.max(NoticeVersionRow.seq), 0)))


def max_import_seq(session: Session) -> int:
    return session.scalar(select(func.coalesce(func.max(ImportRow.seq), 0)))


def add_notice_if_missing(session: Session, row: dict[str, Any]) -> None:
    session.execute(insert(NoticeRow).values(**row).on_conflict_do_nothing(index_elements=["id"]))


def add_version_if_missing(session: Session, row: dict[str, Any]) -> int | None:
    """插入一个观察。已经存在（同样的内容导入过）就跳过，返回 None；新插入的返回接收序号。"""
    return session.scalar(
        insert(NoticeVersionRow)
        .values(**row)
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(NoticeVersionRow.seq)
    )


def add_import_if_missing(session: Session, row: dict[str, Any]) -> None:
    session.execute(insert(ImportRow).values(**row).on_conflict_do_nothing(index_elements=["id"]))


def _current(snapshot: int) -> Select:
    """快照范围内每条公告的当前版本（PostgreSQL 的 DISTINCT ON：每个 notice_id 取排序后的第一行）。"""
    return (
        select(NoticeVersionRow)
        .where(NoticeVersionRow.seq <= snapshot)
        .ext(distinct_on(NoticeVersionRow.notice_id))
        .order_by(NoticeVersionRow.notice_id, *CURRENT_ORDER)
    )


def search_current(
    session: Session,
    snapshot: int,
    *,
    simulation: bool,
    kind: str | None,
    keyword: str,
    offset: int,
    limit: int,
) -> tuple[list[NoticeVersionRow], int]:
    """在当前版本里搜索。筛选条件作用在当前版本上：旧版本里出现过的关键词，不会让公告被搜出来。"""
    current = _current(snapshot).subquery()
    statement = select(current).where(current.c.simulation.is_(simulation))
    if kind is not None:
        statement = statement.where(current.c.kind == kind)
    if keyword:
        # ILIKE 里的 % 和 _ 是通配符，用户输入的要转义，不然搜"100%"会匹配到别的东西。
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        statement = statement.where(current.c.search_text.ilike(f"%{escaped}%", escape="\\"))
    total = session.scalar(select(func.count()).select_from(statement.subquery()))
    rows = session.execute(
        statement.order_by(current.c.observed_at.desc(), current.c.notice_id).offset(offset).limit(limit)
    ).all()
    return [NoticeVersionRow(**row._mapping) for row in rows], total


def current_versions(
    session: Session, snapshot: int, *, simulation: bool | None = None, source_ids: set[str] | None = None
) -> list[NoticeVersionRow]:
    """快照里全部公告的当前版本。算关联时用；可以按是否演示、来源先缩小范围。"""
    current = _current(snapshot).subquery()
    statement = select(current)
    if simulation is not None:
        statement = statement.where(current.c.simulation.is_(simulation))
    if source_ids is not None:
        statement = statement.where(current.c.source_id.in_(source_ids))
    return [NoticeVersionRow(**row._mapping) for row in session.execute(statement).all()]


def current_version(session: Session, notice_id: str, snapshot: int) -> NoticeVersionRow | None:
    return session.scalar(
        select(NoticeVersionRow)
        .where(NoticeVersionRow.notice_id == notice_id, NoticeVersionRow.seq <= snapshot)
        .order_by(*CURRENT_ORDER)
        .limit(1)
    )


def history(session: Session, notice_id: str, snapshot: int) -> list[NoticeVersionRow]:
    return list(
        session.scalars(
            select(NoticeVersionRow)
            .where(NoticeVersionRow.notice_id == notice_id, NoticeVersionRow.seq <= snapshot)
            .order_by(*CURRENT_ORDER)
        )
    )


def get_version(session: Session, notice_id: str, observation_id: str) -> NoticeVersionRow | None:
    return session.scalar(
        select(NoticeVersionRow).where(NoticeVersionRow.notice_id == notice_id, NoticeVersionRow.id == observation_id)
    )


def versions_by_ids(session: Session, ids: list[str]) -> list[NoticeVersionRow]:
    if not ids:
        return []
    return list(session.scalars(select(NoticeVersionRow).where(NoticeVersionRow.id.in_(ids))))


def versions_after(session: Session, after: int, high: int, limit: int) -> list[NoticeVersionRow]:
    return list(
        session.scalars(
            select(NoticeVersionRow)
            .where(NoticeVersionRow.seq > after, NoticeVersionRow.seq <= high)
            .order_by(NoticeVersionRow.seq)
            .limit(limit)
        )
    )


def recent_imports(session: Session, imports_snapshot: int, simulation: bool, limit: int = 10) -> list[ImportRow]:
    return list(
        session.scalars(
            select(ImportRow)
            .where(ImportRow.seq <= imports_snapshot, ImportRow.simulation.is_(simulation))
            .order_by(ImportRow.seq.desc())
            .limit(limit)
        )
    )


def observed(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

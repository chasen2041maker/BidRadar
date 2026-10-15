"""documents 的数据库读写，只给本模块的 service 用。函数接收会话参数，只读写，不提交事务。"""

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from bidradar.modules.documents.tables import ChunkRow, DocumentRow

LIST_LIMIT = 200


def add(session: Session, row: object) -> None:
    session.add(row)
    session.flush()


def find_by_sha(session: Session, workspace_id: str | None, sha256: str) -> DocumentRow | None:
    return session.scalar(
        select(DocumentRow).where(
            DocumentRow.workspace_id.is_(None) if workspace_id is None else DocumentRow.workspace_id == workspace_id,
            DocumentRow.sha256 == sha256,
        )
    )


def get(session: Session, document_id: str, *, for_update: bool = False) -> DocumentRow | None:
    statement = select(DocumentRow).where(DocumentRow.id == document_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def visible(session: Session, workspace_id: str, document_id: str) -> DocumentRow | None:
    """这家公司能读的文件：自己上传的，或者公开的。别家公司的私有文件查不到。"""
    return session.scalar(
        select(DocumentRow).where(
            DocumentRow.id == document_id,
            or_(DocumentRow.workspace_id == workspace_id, DocumentRow.workspace_id.is_(None)),
        )
    )


def list_visible(session: Session, workspace_id: str, notice_id: str | None) -> list[DocumentRow]:
    statement = select(DocumentRow).where(
        or_(DocumentRow.workspace_id == workspace_id, DocumentRow.workspace_id.is_(None))
    )
    if notice_id is not None:
        statement = statement.where(DocumentRow.notice_id == notice_id)
    return list(session.scalars(statement.order_by(DocumentRow.created_at.desc()).limit(LIST_LIMIT)))


def replace_chunks(session: Session, document_id: str, rows: list[ChunkRow]) -> None:
    """换成新的一套切片。先删后插，在同一个事务里：读的人要么看到旧的一套，要么看到新的一套。"""
    session.execute(delete(ChunkRow).where(ChunkRow.document_id == document_id))
    session.add_all(rows)
    session.flush()


def chunks(session: Session, document_ids: list[str], *, offset: int = 0, limit: int | None = None) -> list[ChunkRow]:
    statement = (
        select(ChunkRow).where(ChunkRow.document_id.in_(document_ids)).order_by(ChunkRow.document_id, ChunkRow.seq)
    )
    if offset:
        statement = statement.offset(offset)
    if limit is not None:
        statement = statement.limit(limit)
    return list(session.scalars(statement))


def search_chunks(session: Session, document_ids: list[str], terms: list[str], limit: int) -> list[ChunkRow]:
    """含任一关键词的切片。关键词里的 % 和 _ 按普通字符处理。"""
    conditions = []
    for term in terms:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conditions.append(ChunkRow.text.ilike(f"%{escaped}%", escape="\\"))
    return list(
        session.scalars(
            select(ChunkRow)
            .where(ChunkRow.document_id.in_(document_ids), or_(*conditions))
            .order_by(ChunkRow.document_id, ChunkRow.seq)
            .limit(limit)
        )
    )

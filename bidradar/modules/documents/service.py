"""documents 的业务用例：上传招标文件、后台解析切片、给研究提供切片。

别的模块只能调用这个文件里的公开函数：
    get_chunks()      研究取材：一组文件的全部切片（检查这些文件确实属于这家公司或者是公开的）
    search_chunks()   追问时按关键词找切片
    get_document()    文件信息

一份文件的一生：
    upload()          检查类型和大小 → 存原始文件 → 记一行 documents（queued）→ 排一个解析任务。同一个事务
    handle_parse()    Worker 领到任务 → 解析 → 切片 → 写 chunks、文件改成 parsed。写之前确认租约还是自己的

为什么上传接口不直接解析：几百页的 PDF 要解析几十秒，放在请求里会超时；Worker 崩了还能重来。
"""

from sqlalchemy.orm import Session

from bidradar.infra import clock, jobs, outbox
from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError, not_found
from bidradar.infra.ids import new_id
from bidradar.modules.catalog import service as catalog
from bidradar.modules.documents import parsers, repo
from bidradar.modules.documents.models import (
    JOB_PARSE,
    MAX_UPLOAD_BYTES,
    MIME,
    TOPIC_DOCUMENT_PARSED,
    WARNING_NO_TEXT,
    Chunk,
    Document,
    FileType,
    chunk_blocks,
    clean_filename,
    detect_type,
)
from bidradar.modules.documents.tables import ChunkRow, DocumentRow
from bidradar.modules.workspace import service as workspace

PARSER_VERSION = "documents-v1"


def _view(row: DocumentRow) -> Document:
    return Document(
        id=row.id,
        workspace_id=row.workspace_id,
        notice_id=row.notice_id,
        filename=row.filename,
        file_type=row.file_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        status=row.status,
        error_code=row.error_code,
        warning=row.warning,
        page_count=row.page_count,
        chunk_count=row.chunk_count,
        uploaded_by=row.uploaded_by,
        created_at=row.created_at,
        parsed_at=row.parsed_at,
    )


def _chunk(row: ChunkRow) -> Chunk:
    return Chunk(
        id=row.id,
        document_id=row.document_id,
        seq=row.seq,
        page=row.page,
        section_path=tuple(row.section_path),
        kind=row.kind,
        text=row.text,
    )


def mime_type(document: Document) -> str:
    return MIME[FileType(document.file_type)]


# ---- 上传 ----


def upload(
    session: Session,
    blobs: BlobStore,
    actor_id: str,
    workspace_id: str,
    *,
    filename: str,
    data: bytes,
    notice_id: str | None = None,
) -> Document:
    """上传一份招标文件。同一家公司重复上传同样内容的文件，返回第一次的那份，不重复解析。

    上传的文件是这家公司的私有数据：只有本公司成员能读，研究时也只用于本公司。
    """
    workspace.authorize(session, actor_id, workspace_id, "write")
    filename = clean_filename(filename)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError("file_too_large", 413)
    if not data:
        raise AppError("empty_file")
    file_type = detect_type(filename, data)
    if notice_id is not None:
        catalog.get_notice(session, notice_id)  # 公告不存在会报 404
    # 先存文件、再写库：中途崩了最多留下一个没人引用的文件，不会有"库里有记录、文件不存在"。
    digest = blobs.put(data)
    existing = repo.find_by_sha(session, workspace_id, digest)
    if existing is not None:
        return _view(existing)
    row = DocumentRow(
        id=new_id(),
        workspace_id=workspace_id,
        notice_id=notice_id,
        filename=filename,
        file_type=file_type.value,
        size_bytes=len(data),
        sha256=digest,
        status="queued",
        parser_version=PARSER_VERSION,
        chunk_count=0,
        uploaded_by=actor_id,
        created_at=clock.now(),
    )
    repo.add(session, row)
    # 解析器版本放进去重键：以后升级解析器，同一份文件可以再排一次解析。
    jobs.enqueue(session, JOB_PARSE, {"document_id": row.id}, dedupe_key=f"{JOB_PARSE}:{row.id}:{PARSER_VERSION}")
    session.commit()
    return _view(row)


# ---- 查看 ----


def _visible(session: Session, workspace_id: str, document_id: str) -> DocumentRow:
    row = repo.visible(session, workspace_id, document_id) if isinstance(document_id, str) else None
    if row is None:
        raise not_found()
    return row


def list_documents(session: Session, actor_id: str, workspace_id: str, notice_id: str | None = None) -> list[Document]:
    workspace.authorize(session, actor_id, workspace_id, "read")
    return [_view(row) for row in repo.list_visible(session, workspace_id, notice_id)]


def get_document(session: Session, actor_id: str, workspace_id: str, document_id: str) -> Document:
    workspace.authorize(session, actor_id, workspace_id, "read")
    return _view(_visible(session, workspace_id, document_id))


def document_chunks(
    session: Session, actor_id: str, workspace_id: str, document_id: str, *, offset: int = 0, limit: int = 100
) -> list[Chunk]:
    """给界面看的切片，分页。"""
    workspace.authorize(session, actor_id, workspace_id, "read")
    _visible(session, workspace_id, document_id)
    return [_chunk(row) for row in repo.chunks(session, [document_id], offset=offset, limit=limit)]


# ---- 给研究用 ----


def get_chunks(session: Session, workspace_id: str, document_ids: list[str]) -> list[Chunk]:
    """研究取材：这些文件的全部切片，按文件、序号排好。

    调用方应该已经检查过发起人的权限。这里再确认每份文件都属于这家公司（或者是公开的），并且已经解析完：
    研究任务里写错一个 ID，不能读到别家公司的文件。
    """
    for document_id in document_ids:
        row = _visible(session, workspace_id, document_id)
        if row.status != "parsed":
            raise AppError("document_not_parsed", 409)
    return [_chunk(row) for row in repo.chunks(session, list(document_ids))]


def search_chunks(
    session: Session, workspace_id: str, document_ids: list[str], query: str, limit: int = 8
) -> list[Chunk]:
    """按关键词找切片（追问用）。空格分开的多个词按"或"匹配。"""
    for document_id in document_ids:
        _visible(session, workspace_id, document_id)
    terms = [term for term in (query or "").split() if term][:8]
    if not terms:
        return []
    return [_chunk(row) for row in repo.search_chunks(session, list(document_ids), terms, max(1, min(limit, 20)))]


# ---- 后台解析 ----


def handle_parse(session: Session, blobs: BlobStore, job: jobs.Job) -> None:
    """Worker 执行解析任务。解析成功或者文件本身有问题（损坏、太大），任务都算完成；
    只有意料之外的异常才往外抛，由 Worker 按重试规则处理。
    """
    document_id = job.payload["document_id"]
    row = repo.get(session, document_id)
    if row is None or row.status == "parsed":
        jobs.complete(session, job)
        session.commit()
        return
    jobs.hold(session, job)
    row.status = "parsing"
    session.commit()

    try:
        file_type = FileType(row.file_type)
        drafts = chunk_blocks(parsers.parse(file_type, blobs.read(row.sha256)), row.sha256)
        error = None
    except AppError as exc:
        drafts, error = [], exc.code

    # 写结果之前，在同一个事务里确认租约还是自己的。如果这个 Worker 卡太久、任务已经被别人接手，
    # 这里会抛 LeaseLost，整个事务回滚：切片只会被写一次。
    jobs.hold(session, job)
    row = repo.get(session, document_id, for_update=True)
    now = clock.now()
    if error is not None:
        row.status, row.error_code = "failed", error
    else:
        repo.replace_chunks(
            session,
            document_id,
            [
                ChunkRow(
                    id=d.id,
                    document_id=document_id,
                    seq=d.seq,
                    page=d.page,
                    section_path=list(d.section_path),
                    kind=d.kind,
                    text=d.text,
                    text_hash=d.text_hash,
                )
                for d in drafts
            ],
        )
        row.status, row.error_code = "parsed", None
        row.chunk_count = len(drafts)
        row.page_count = max((d.page or 0 for d in drafts), default=0) or None
        row.warning = WARNING_NO_TEXT if not drafts else None
        row.parsed_at = now
        outbox.publish(
            session,
            TOPIC_DOCUMENT_PARSED,
            {"document_id": document_id, "workspace_id": row.workspace_id, "chunk_count": len(drafts)},
            event_key=f"document_parsed:{document_id}:{PARSER_VERSION}",
        )
    jobs.complete(session, job)
    session.commit()

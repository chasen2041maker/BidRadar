"""documents 的 HTTP 路由：上传招标文件、查看解析状态和切片。

上传用 multipart 表单（字段 file，可选 notice_id），修改类请求的 Cookie、Origin、CSRF 检查照样做。
"""

from typing import Annotated

from fastapi import APIRouter, File, Form, Path, Query, UploadFile

from bidradar.app.deps import AppSettings, CurrentUser, DbSession, WorkspaceId
from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError
from bidradar.modules.documents import service
from bidradar.modules.documents.models import MAX_UPLOAD_BYTES
from bidradar.modules.documents.schemas import ChunkListOut, ChunkOut, DocumentListOut, DocumentOut

router = APIRouter(prefix="/api/workspaces/{workspace_id}/documents", tags=["documents"])
DocumentId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


@router.post("")
async def upload_document(
    workspace_id: WorkspaceId,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
    file: Annotated[UploadFile, File()],
    notice_id: Annotated[str | None, Form(pattern=r"^[0-9a-f]{64}$")] = None,
) -> DocumentOut:
    # 多读 1 个字节：读到了就说明超过上限，不用把整个超大文件读进内存再判断。
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise AppError("file_too_large", 413)
    document = service.upload(
        session,
        BlobStore(settings.data_dir / "blobs"),
        user.id,
        workspace_id,
        filename=file.filename or "",
        data=data,
        notice_id=notice_id,
    )
    return DocumentOut.of(document)


@router.get("")
def list_documents(
    workspace_id: WorkspaceId,
    user: CurrentUser,
    session: DbSession,
    notice_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
) -> DocumentListOut:
    documents = service.list_documents(session, user.id, workspace_id, notice_id)
    return DocumentListOut(items=[DocumentOut.of(d) for d in documents])


@router.get("/{document_id}")
def get_document(
    workspace_id: WorkspaceId, document_id: DocumentId, user: CurrentUser, session: DbSession
) -> DocumentOut:
    return DocumentOut.of(service.get_document(session, user.id, workspace_id, document_id))


@router.get("/{document_id}/chunks")
def list_chunks(
    workspace_id: WorkspaceId,
    document_id: DocumentId,
    user: CurrentUser,
    session: DbSession,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ChunkListOut:
    chunks = service.document_chunks(session, user.id, workspace_id, document_id, offset=offset, limit=limit)
    return ChunkListOut(items=[ChunkOut.of(c) for c in chunks])

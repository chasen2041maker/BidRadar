"""documents 接口的响应结构（Pydantic），只给 api.py 用。上传用 multipart 表单，没有 JSON 请求体。"""

from pydantic import BaseModel

from bidradar.infra.api_types import IsoDateTime
from bidradar.modules.documents.models import Chunk, Document


class DocumentOut(BaseModel):
    id: str
    notice_id: str | None
    filename: str
    file_type: str
    size_bytes: int
    sha256: str
    status: str
    error_code: str | None
    warning: str | None
    page_count: int | None
    chunk_count: int
    visibility: str
    created_at: IsoDateTime
    parsed_at: IsoDateTime | None

    @classmethod
    def of(cls, value: Document) -> "DocumentOut":
        fields = {k: v for k, v in value.__dict__.items() if k in cls.model_fields}
        return cls(**fields, visibility="public" if value.workspace_id is None else "workspace")


class DocumentListOut(BaseModel):
    items: list[DocumentOut]


class ChunkOut(BaseModel):
    id: str
    seq: int
    page: int | None
    section_path: list[str]
    kind: str
    text: str

    @classmethod
    def of(cls, value: Chunk) -> "ChunkOut":
        return cls(
            id=value.id,
            seq=value.seq,
            page=value.page,
            section_path=list(value.section_path),
            kind=value.kind,
            text=value.text,
        )


class ChunkListOut(BaseModel):
    items: list[ChunkOut]

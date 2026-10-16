"""上传、去重、公司隔离、后台解析、给研究取切片。"""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.infra import jobs
from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError
from bidradar.infra.tables import JobRow
from bidradar.modules.documents import service
from bidradar.modules.documents.tables import ChunkRow
from tests.factories import TENDER_PDF, import_notice, make_company, upload_and_parse


@pytest.fixture
def blobs(tmp_path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def test_upload_queues_a_parse_job_in_the_same_transaction(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document = service.upload(
        db_session, blobs, users["member"].id, wid, filename="招标文件.pdf", data=TENDER_PDF.read_bytes()
    )
    assert document.status == "queued"
    job = db_session.scalar(select(JobRow).where(JobRow.kind == service.JOB_PARSE))
    assert job.payload == {"document_id": document.id}


def test_same_file_uploaded_twice_is_one_document(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    data = TENDER_PDF.read_bytes()
    first = service.upload(db_session, blobs, users["member"].id, wid, filename="a.pdf", data=data)
    second = service.upload(db_session, blobs, users["admin"].id, wid, filename="b.pdf", data=data)
    assert second.id == first.id
    assert db_session.scalar(select(func.count()).select_from(JobRow)) == 1


def test_viewer_cannot_upload(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    with pytest.raises(AppError, match="forbidden"):
        service.upload(db_session, blobs, users["viewer"].id, wid, filename="a.pdf", data=TENDER_PDF.read_bytes())


def test_uploads_are_private_to_the_company(db_session: Session, blobs: BlobStore) -> None:
    a, users_a = make_company(db_session, "a")
    b, users_b = make_company(db_session, "b")
    document_id = upload_and_parse(db_session, blobs, users_a["member"], a)
    # 公司 B 拿着 A 的文件 ID：看不到文件，研究时也取不到切片。
    with pytest.raises(AppError, match="not_found"):
        service.get_document(db_session, users_b["admin"].id, b, document_id)
    with pytest.raises(AppError, match="not_found"):
        service.get_chunks(db_session, b, [document_id])
    assert service.list_documents(db_session, users_b["admin"].id, b) == []
    # 公司 B 上传同一份文件，是 B 自己的一份，不会拿到 A 的。
    own = service.upload(db_session, blobs, users_b["admin"].id, b, filename="x.pdf", data=TENDER_PDF.read_bytes())
    assert own.id != document_id


def test_parse_creates_page_numbered_chunks(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document_id = upload_and_parse(db_session, blobs, users["member"], wid)
    document = service.get_document(db_session, users["viewer"].id, wid, document_id)
    assert (document.status, document.page_count) == ("parsed", 3)
    chunks = service.get_chunks(db_session, wid, [document_id])
    assert document.chunk_count == len(chunks) > 30
    assert all(c.page for c in chunks)


def test_research_cannot_use_unparsed_documents(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document = service.upload(
        db_session, blobs, users["member"].id, wid, filename="a.pdf", data=TENDER_PDF.read_bytes()
    )
    with pytest.raises(AppError, match="document_not_parsed"):
        service.get_chunks(db_session, wid, [document.id])


def test_corrupt_upload_fails_without_retry(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document_id = upload_and_parse_bytes(db_session, blobs, users["member"], wid, b"%PDF-1.7 broken")
    document = service.get_document(db_session, users["member"].id, wid, document_id)
    assert (document.status, document.error_code) == ("failed", "document_unreadable")
    assert db_session.scalar(select(JobRow.status)) == "succeeded"


def upload_and_parse_bytes(session, blobs, user, wid, data):
    document = service.upload(session, blobs, user.id, wid, filename="broken.pdf", data=data)
    job = jobs.claim(session, [service.JOB_PARSE], "w")
    service.handle_parse(session, blobs, job)
    return document.id


def test_search_chunks_finds_requirements(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document_id = upload_and_parse(db_session, blobs, users["member"], wid)
    found = service.search_chunks(db_session, wid, [document_id], "驻场")
    assert any("驻场服务" in c.text for c in found)
    # % 按普通字符匹配：只找到原文里真有百分号的付款和保证金条款，而不是匹配所有切片。
    assert all("%" in c.text for c in service.search_chunks(db_session, wid, [document_id], "%"))


def test_upload_can_link_to_a_notice(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    document = service.upload(
        db_session,
        blobs,
        users["member"].id,
        wid,
        filename="a.pdf",
        data=TENDER_PDF.read_bytes(),
        notice_id=notice["notice_id"],
    )
    assert [d.id for d in service.list_documents(db_session, users["viewer"].id, wid, notice["notice_id"])] == [
        document.id
    ]
    with pytest.raises(AppError, match="notice_not_found"):
        service.upload(db_session, blobs, users["member"].id, wid, filename="b.pdf", data=b"%PDF-x", notice_id="f" * 64)


def test_chunks_written_once_even_after_reparse(db_session: Session, blobs: BlobStore) -> None:
    wid, users = make_company(db_session)
    document_id = upload_and_parse(db_session, blobs, users["member"], wid)
    count = db_session.scalar(select(func.count()).select_from(ChunkRow).where(ChunkRow.document_id == document_id))
    # 再执行一次同一个解析任务（比如重复投递）：文件已经是 parsed，直接跳过。
    jobs.enqueue(db_session, service.JOB_PARSE, {"document_id": document_id})
    db_session.commit()
    job = jobs.claim(db_session, [service.JOB_PARSE], "w2")
    service.handle_parse(db_session, blobs, job)
    assert (
        db_session.scalar(select(func.count()).select_from(ChunkRow).where(ChunkRow.document_id == document_id))
        == count
    )

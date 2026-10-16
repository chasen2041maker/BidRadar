"""上传接口（multipart）和查看切片。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from bidradar.app.worker import Handler, tick
from bidradar.infra.blobs import BlobStore
from bidradar.modules.documents import service
from tests.factories import TENDER_DOCX, login, make_company


def test_upload_parse_and_read_chunks(client: TestClient, db_session: Session, tmp_path) -> None:
    client.app.state.settings.data_dir = tmp_path
    wid, _ = make_company(db_session)
    login(client, "a.member")
    response = client.post(
        f"/api/workspaces/{wid}/documents",
        files={"file": ("招标文件.docx", TENDER_DOCX.read_bytes(), "application/octet-stream")},
    )
    assert response.status_code == 200, response.text
    document = response.json()
    assert (document["status"], document["visibility"]) == ("queued", "workspace")

    # 像 Worker 一样执行一次：用测试会话代替会话工厂。
    blobs = BlobStore(tmp_path / "blobs")
    handlers = {
        service.JOB_PARSE: Handler(run=lambda s, j: service.handle_parse(s, blobs, j), lease=service.jobs.DEFAULT_LEASE)
    }

    class Factory:
        def __call__(self):
            return self

        def __enter__(self):
            return db_session

        def __exit__(self, *args):
            return False

    assert tick(Factory(), handlers, "test-worker") is True
    assert tick(Factory(), handlers, "test-worker") is False

    parsed = client.get(f"/api/workspaces/{wid}/documents/{document['id']}").json()
    assert parsed["status"] == "parsed"
    chunks = client.get(f"/api/workspaces/{wid}/documents/{document['id']}/chunks?limit=200").json()["items"]
    assert len(chunks) == parsed["chunk_count"]
    assert any(c["section_path"] == ["第三章 采购需求", "二、交付要求"] for c in chunks)


def test_upload_rejects_wrong_type_and_needs_csrf(client: TestClient, db_session: Session, tmp_path) -> None:
    client.app.state.settings.data_dir = tmp_path
    wid, _ = make_company(db_session)
    login(client, "a.member")
    bad = client.post(f"/api/workspaces/{wid}/documents", files={"file": ("x.pdf", b"<html>", "application/pdf")})
    assert (bad.status_code, bad.json()["error"]["code"]) == (415, "unsupported_file_type")
    del client.headers["X-CSRF-Token"]
    no_csrf = client.post(f"/api/workspaces/{wid}/documents", files={"file": ("x.pdf", b"%PDF-", "application/pdf")})
    assert no_csrf.status_code == 403

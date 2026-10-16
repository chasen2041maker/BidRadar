"""研究接口的 HTTP 路径：先预估、再确认发起；查看、列表、取消、费用；追问在没开模型时被拒绝。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from bidradar.agent.llm import FakeLLM, Pricing
from bidradar.infra import jobs
from bidradar.infra.blobs import BlobStore
from bidradar.modules.research import service
from bidradar.modules.research.models import JOB_RUN, Limits
from tests_v2.agent.support import responder
from tests_v2.factories import confirm_profile, import_notice, login, make_company, upload_and_parse


def setup(db_session: Session, tmp_path):
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    document_id = upload_and_parse(
        db_session, BlobStore(tmp_path / "blobs"), users["member"], wid, notice_id=notice["notice_id"]
    )
    return wid, notice["notice_id"], document_id


def test_estimate_then_run_then_read_the_report(client: TestClient, db_session: Session, tmp_path) -> None:
    wid, nid, did = setup(db_session, tmp_path)
    base = f"/api/workspaces/{wid}/research"
    login(client, "a.member")
    estimate = client.post(f"{base}/estimate", json={"notice_id": nid, "document_ids": [did]})
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["mode"] == "baseline" and estimate.json()["cost_micro"] == 0

    created = client.post(f"{base}/runs", json={"notice_id": nid, "document_ids": [did], "key": "r1"})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    assert created.json()["status"] == "queued"

    job = jobs.claim(db_session, [JOB_RUN], "test-worker")
    service.handle_run(db_session, job, FakeLLM(responder()), Limits(), Pricing())

    detail = client.get(f"{base}/runs/{run_id}").json()
    assert detail["status"] in ("succeeded", "partial") and detail["report_kind"] == "report"
    assert detail["report"]["findings"] and detail["freshness"]["profile"] == "current"
    assert [r["id"] for r in client.get(f"{base}/runs").json()["items"]] == [run_id]
    budget = client.get(f"{base}/budget").json()
    assert (budget["model_enabled"], budget["workspace_used"]) == (False, 0)

    asked = client.post(f"{base}/runs/{run_id}/questions", json={"question": "驻场几天？", "key": "q1"})
    assert asked.status_code == 409 and asked.json()["error"]["code"] == "model_disabled"


def test_viewer_can_read_but_not_start_or_cancel(client: TestClient, db_session: Session, tmp_path) -> None:
    wid, nid, did = setup(db_session, tmp_path)
    base = f"/api/workspaces/{wid}/research"
    login(client, "a.member")
    run_id = client.post(f"{base}/runs", json={"notice_id": nid, "document_ids": [did], "key": "r1"}).json()["id"]
    client.post("/api/logout")
    login(client, "a.viewer")
    assert client.post(f"{base}/runs", json={"notice_id": nid, "key": "r2"}).status_code == 403
    assert client.post(f"{base}/runs/{run_id}/cancel", json={"key": "c"}).status_code == 403
    assert client.get(f"{base}/runs/{run_id}").json()["status"] == "queued"


def test_cancel_over_http(client: TestClient, db_session: Session, tmp_path) -> None:
    wid, nid, did = setup(db_session, tmp_path)
    base = f"/api/workspaces/{wid}/research"
    login(client, "a.member")
    run_id = client.post(f"{base}/runs", json={"notice_id": nid, "document_ids": [did], "key": "r1"}).json()["id"]
    cancelled = client.post(f"{base}/runs/{run_id}/cancel", json={"key": "c"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    again = client.post(f"{base}/runs/{run_id}/cancel", json={"key": "c2"})
    assert again.status_code == 409


def test_bad_input_is_rejected(client: TestClient, db_session: Session, tmp_path) -> None:
    wid, nid, did = setup(db_session, tmp_path)
    base = f"/api/workspaces/{wid}/research"
    login(client, "a.member")
    assert client.post(f"{base}/estimate", json={"notice_id": "x"}).status_code == 400
    assert (
        client.post(f"{base}/runs", json={"notice_id": nid, "document_ids": [did, did], "key": "r"}).status_code == 400
    )
    assert client.get(f"{base}/runs/not-an-id").status_code in (400, 404)

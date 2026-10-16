"""档案和成员接口的完整 HTTP 路径。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.factories import PROFILE, confirm_profile, login, make_company


def test_profile_flow_over_http(client: TestClient, db_session: Session) -> None:
    wid, users = make_company(db_session)
    login(client, "a.member")
    proposal = client.post(
        f"/api/workspaces/{wid}/profile/proposals", json={"payload": PROFILE, "base_version": 0, "key": "p1"}
    )
    assert proposal.status_code == 200, proposal.text
    assert proposal.json()["status"] == "proposed"
    assert proposal.json()["created_at"].endswith("Z")

    login(client, "a.admin")
    confirmed = client.post(
        f"/api/workspaces/{wid}/profile/confirm",
        json={"proposal_id": proposal.json()["id"], "expected_version": 0, "key": "c1"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["version"] == 1

    overview = client.get(f"/api/workspaces/{wid}/profile").json()
    assert overview["current"]["payload"]["company_name"] == "山岚软件（虚构）"
    assert overview["proposals"][0]["confirmed_version"] == 1


def test_viewer_gets_403_on_write(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    login(client, "a.viewer")
    response = client.post(
        f"/api/workspaces/{wid}/profile/proposals", json={"payload": PROFILE, "base_version": 0, "key": "p1"}
    )
    assert response.status_code == 403


def test_other_company_is_404(client: TestClient, db_session: Session) -> None:
    a, users_a = make_company(db_session, "a")
    make_company(db_session, "b")
    confirm_profile(db_session, a, users_a["admin"])
    login(client, "b.admin")
    for path in ("profile", "members"):
        response = client.get(f"/api/workspaces/{a}/{path}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_member_management_over_http(client: TestClient, db_session: Session) -> None:
    wid, users = make_company(db_session)
    login(client, "a.admin")
    members = client.get(f"/api/workspaces/{wid}/members").json()["items"]
    target = next(m for m in members if m["username"] == "a.member")
    changed = client.post(
        f"/api/workspaces/{wid}/members/change",
        json={"target_user_id": target["user_id"], "role": "viewer", "active": True, "expected_version": 1},
    )
    assert changed.status_code == 200
    assert changed.json()["role"] == "viewer"
    history = client.get(f"/api/workspaces/{wid}/members/{target['user_id']}/history").json()["items"]
    assert [h["new_role"] for h in history] == ["member", "viewer"]


def test_bad_workspace_id_format_is_rejected_before_any_lookup(client: TestClient, db_session: Session) -> None:
    make_company(db_session)
    login(client, "a.admin")
    assert client.get("/api/workspaces/not-an-id/profile").status_code == 400


def test_write_requires_csrf(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    login(client, "a.admin")
    response = client.post(
        f"/api/workspaces/{wid}/profile/proposals",
        json={"payload": PROFILE, "base_version": 0, "key": "p1"},
        headers={"X-CSRF-Token": "x" * 43},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"

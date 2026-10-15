"""跟踪接口的 HTTP 路径。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests_v2.factories import import_notice, login, make_company


def test_decision_and_watch_over_http(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    notice = import_notice(db_session)
    nid = notice["notice_id"]
    login(client, "a.member")
    decided = client.post(
        f"/api/workspaces/{wid}/tracking/decisions/{nid}",
        json={"state": "follow_up", "reason": "值得跟进", "expected_version": 0, "key": "d1"},
    )
    assert decided.status_code == 200, decided.text
    assert client.get(f"/api/workspaces/{wid}/tracking/decisions/{nid}").json()["current"]["state"] == "follow_up"

    watched = client.post(
        f"/api/workspaces/{wid}/tracking/watches/{nid}", json={"active": True, "expected_version": 0, "key": "w1"}
    )
    assert watched.status_code == 200, watched.text
    assert client.get(f"/api/workspaces/{wid}/tracking/watches/{nid}").json()["watch"]["active"] is True
    assert client.get(f"/api/workspaces/{wid}/tracking/notifications").json() == {"items": [], "next_after": 0}


def test_viewer_cannot_change_tracking(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    notice = import_notice(db_session)
    login(client, "a.viewer")
    response = client.post(
        f"/api/workspaces/{wid}/tracking/watches/{notice['notice_id']}",
        json={"active": True, "expected_version": 0, "key": "w1"},
    )
    assert response.status_code == 403

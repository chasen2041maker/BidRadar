"""登录、会话、退出的完整 HTTP 路径：Cookie、CSRF、Origin。"""

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests_v2.factories import PASSWORD, login, make_user, make_workspace


def test_login_sets_cookies_and_hides_tokens(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    response = client.post("/api/login", json={"username": "alice", "password": PASSWORD})
    assert response.status_code == 200
    assert response.json() == {
        "user": {"id": response.json()["user"]["id"], "username": "alice", "display_name": "alice（虚构）"}
    }
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("br_session="))
    assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie
    assert any(c.startswith("br_csrf=") and "HttpOnly" not in c for c in cookies)
    # 令牌只在 Cookie 里，不在正文里。
    assert client.cookies["br_session"] not in response.text


def test_session_lists_workspaces(client: TestClient, db_session: Session) -> None:
    alice = make_user(db_session, "alice")
    wid = make_workspace(db_session, alice)
    login(client, "alice")
    body = client.get("/api/session").json()
    assert body["user"]["username"] == "alice"
    assert body["workspaces"] == [{"workspace_id": wid, "name": "山岚软件（虚构）", "role": "admin"}]


def test_session_requires_login(client: TestClient) -> None:
    response = client.get("/api/session")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"


def test_bad_credentials(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    response = client.post("/api/login", json={"username": "alice", "password": "wrong-password-123"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_unknown_fields_rejected(client: TestClient) -> None:
    response = client.post("/api/login", json={"username": "alice", "password": PASSWORD, "role": "admin"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_login_from_another_origin_rejected(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    response = client.post(
        "/api/login", json={"username": "alice", "password": PASSWORD}, headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_origin"


def test_post_without_csrf_header_rejected(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    login(client, "alice")
    del client.headers["X-CSRF-Token"]
    response = client.post("/api/logout", json={})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_failed"


def test_logout_ends_the_session(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    login(client, "alice")
    old_token = client.cookies["br_session"]
    assert client.post("/api/logout", json={}).json() == {"logged_out": True}
    # 就算浏览器还留着旧 Cookie，会话在服务端已经没了。
    client.cookies.set("br_session", old_token)
    assert client.get("/api/session").status_code == 401


def test_duplicate_session_cookie_rejected(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    login(client, "alice")
    token = client.cookies["br_session"]
    response = client.get("/api/session", headers={"Cookie": f"br_session={token}; br_session={token}"})
    assert response.status_code == 401


def test_relogin_revokes_previous_session(client: TestClient, db_session: Session) -> None:
    make_user(db_session, "alice")
    login(client, "alice")
    first = client.cookies["br_session"]
    login(client, "alice")
    client.cookies.set("br_session", first)
    assert client.get("/api/session").status_code == 401


def test_untrusted_host_rejected(client: TestClient) -> None:
    assert client.get("/health", headers={"Host": "evil.example"}).status_code == 400


def test_api_responses_are_not_cached(client: TestClient) -> None:
    response = client.get("/api/session")
    assert response.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in response.headers["content-security-policy"]

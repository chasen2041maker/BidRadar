"""商机目录接口：搜索、翻页快照、详情、权限。"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidradar.infra.tables import OutboxEvent
from tests.factories import import_notice, login, make_company


def page_url(wid: str, **params) -> str:
    query = "&".join(f"{k}={v}" for k, v in {"simulation": "true", **params}.items())
    return f"/api/workspaces/{wid}/notices?{query}"


def test_search_and_detail(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    notice = import_notice(db_session, title="虚构知识库平台采购公告")
    import_notice(db_session, title="虚构道路维修采购公告", key="https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t2.htm")
    login(client, "a.viewer")

    found = client.get(page_url(wid, keyword="知识库")).json()
    assert found["total"] == 1
    assert found["items"][0]["notice_id"] == notice["notice_id"]
    assert found["items"][0]["currentness"]["status"] in ("deadline_passed", "deadline_not_reached")

    detail = client.get(f"/api/workspaces/{wid}/notices/{notice['notice_id']}").json()
    assert detail["current"]["title"] == "虚构知识库平台采购公告"
    assert len(detail["history"]) == 1


def test_real_and_simulated_data_never_mix(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    import_notice(db_session)
    login(client, "a.viewer")
    assert client.get(page_url(wid, simulation="false")).json()["total"] == 0


def test_keyword_wildcards_are_literal(client: TestClient, db_session: Session) -> None:
    """用户输入的 % 和 _ 不能当成通配符。"""
    wid, _ = make_company(db_session)
    import_notice(db_session)
    login(client, "a.viewer")
    assert client.get(page_url(wid, keyword="%25")).json()["total"] == 0
    assert client.get(page_url(wid, keyword="_")).json()["total"] == 0


def test_pagination_is_pinned_to_a_snapshot(client: TestClient, db_session: Session) -> None:
    """翻页期间新导入的公告不会插进后面几页，第一页看到的总数前后一致。"""
    wid, _ = make_company(db_session)
    for i in range(3):
        import_notice(
            db_session,
            title=f"虚构采购公告{i}",
            key=f"https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t{i}.htm",
            observed=f"2026-09-2{i}T10:00:00Z",
        )
    login(client, "a.viewer")
    first = client.get(page_url(wid, page_size=2)).json()
    import_notice(db_session, title="翻页期间新到的公告", key="https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t9.htm")
    second = client.get(page_url(wid, page_size=2, cursor=first["next_cursor"])).json()
    assert first["total"] == second["total"] == 3
    assert [i["title"] for i in first["items"] + second["items"]] == ["虚构采购公告2", "虚构采购公告1", "虚构采购公告0"]
    assert second["next_cursor"] is None


def test_newer_observation_becomes_current_and_late_old_one_does_not(db_session: Session, client: TestClient) -> None:
    wid, _ = make_company(db_session)
    new = import_notice(db_session, title="新标题", observed="2026-10-02T00:00:00Z")
    import_notice(db_session, title="旧标题", observed="2026-09-01T00:00:00Z")
    login(client, "a.viewer")
    detail = client.get(f"/api/workspaces/{wid}/notices/{new['notice_id']}").json()
    assert detail["current"]["title"] == "新标题"
    assert [h["title"] for h in detail["history"]] == ["新标题", "旧标题"]


def test_import_is_idempotent_and_publishes_one_event_per_new_version(db_session: Session) -> None:
    notice = import_notice(db_session)
    import_notice(db_session)
    events = db_session.scalars(select(OutboxEvent).where(OutboxEvent.topic == "catalog.notice_version_added")).all()
    assert [e.payload["observation_id"] for e in events] == [notice["observation_id"]]


def test_outsider_cannot_search(client: TestClient, db_session: Session) -> None:
    a, _ = make_company(db_session, "a")
    make_company(db_session, "b")
    login(client, "b.viewer")
    assert client.get(page_url(a)).status_code == 404


def test_bad_cursor_rejected(client: TestClient, db_session: Session) -> None:
    wid, _ = make_company(db_session)
    login(client, "a.viewer")
    response = client.get(page_url(wid, cursor="not-a-cursor"))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


def test_cursor_with_trailing_garbage_is_rejected() -> None:
    """宽松的 base64 解码会忽略补位后面的字符：游标末尾多一个字母也要被认出来。"""
    import base64
    import json

    import pytest

    from bidradar.infra.errors import AppError
    from bidradar.modules.catalog.service import _decode_cursor

    state = {"snapshot": 1, "imports_snapshot": 1, "offset": 1, "filters": {}, "as_of": "x"}
    for pad in range(3):  # 让编码结果分别带 0、1、2 个补位"="
        state["as_of"] = "x" * (pad + 1)
        cursor = base64.urlsafe_b64encode(json.dumps(state, sort_keys=True).encode()).decode()
        assert _decode_cursor(cursor) == state
        with pytest.raises(AppError):
            _decode_cursor(cursor + "a")

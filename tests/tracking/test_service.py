"""跟踪：决定、关注、变化检测（直接调事件处理函数）、提醒去重、自动复核委托。"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidradar.infra.errors import AppError
from bidradar.infra.tables import OutboxEvent
from bidradar.modules.research.tables import RunRow
from bidradar.modules.tracking import service
from bidradar.modules.tracking.tables import NotificationRow, ReassessmentRow
from bidradar.modules.workspace import service as workspace
from tests.factories import PROFILE, confirm_profile, import_notice, make_company

DEADLINE_LATER = [
    ("项目编号", "DEMO-001"),
    ("采购单位", "虚构采购方"),
    ("预算（万元）", "12.34"),
    ("公告发布时间", "2026-09-29"),
    ("投标截止时间", "2026-10-20 09:30（北京时间）"),
]


def deliver(session: Session, *topics: str) -> None:
    """模拟 outbox 消费者：把还没处理的事件按顺序交给处理函数（练习 3 会写真正的消费者）。"""
    for event in session.scalars(
        select(OutboxEvent).where(OutboxEvent.published_at.is_(None)).order_by(OutboxEvent.id)
    ):
        handler = service.EVENT_HANDLERS.get(event.topic)
        if handler and (not topics or event.topic in topics):
            handler(session, event.payload)
        event.published_at = event.created_at
    session.flush()


def watch(session, users, wid, notice, **kwargs):
    return service.set_watch(
        session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=False,
        expected_version=0,
        key="w1",
        **kwargs,
    )


# ---- 决定 ----


def test_decision_versions_and_history(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    first = service.set_decision(
        db_session, users["member"].id, wid, notice["notice_id"], "follow_up", "看起来合适", 0, "d1"
    )
    assert (first.version, first.observation_id) == (1, notice["observation_id"])
    service.set_decision(db_session, users["admin"].id, wid, notice["notice_id"], "dismissed", "工期太紧", 1, "d2")
    value = service.get_decision(db_session, users["viewer"].id, wid, notice["notice_id"])
    assert value["current"].state == "dismissed"
    assert [d.state for d in value["history"]] == ["dismissed", "follow_up"]


def test_stale_decision_version_conflicts(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    service.set_decision(db_session, users["member"].id, wid, notice["notice_id"], "follow_up", "", 0, "d1")
    with pytest.raises(AppError, match="decision_version_conflict"):
        service.set_decision(db_session, users["admin"].id, wid, notice["notice_id"], "dismissed", "", 0, "d2")


def test_viewer_cannot_decide(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    with pytest.raises(AppError, match="forbidden"):
        service.set_decision(db_session, users["viewer"].id, wid, notice["notice_id"], "follow_up", "", 0, "d1")


# ---- 变化检测 ----


def test_new_notice_version_creates_one_change_and_one_notification(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    deliver(db_session)  # 关注之前的事件：基线已经包含它们，不算变化
    assert service.list_changes(db_session, users["viewer"].id, wid) == []

    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    changes = service.list_changes(db_session, users["viewer"].id, wid)
    assert len(changes) == 1 and "deadline" in changes[0].diff["scopes"]
    notifications = service.list_notifications(db_session, users["viewer"].id, wid)
    assert [(n.kind, n.change_id) for n in notifications] == [("change_detected", changes[0].id)]


def test_redelivered_event_does_not_duplicate(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    event = db_session.scalars(
        select(OutboxEvent).where(OutboxEvent.topic == "catalog.notice_version_added").order_by(OutboxEvent.id.desc())
    ).first()
    for _ in range(2):
        service.handle_notice_version_added(db_session, event.payload)
    assert len(service.list_changes(db_session, users["viewer"].id, wid)) == 1


def test_late_older_event_is_skipped(db_session: Session) -> None:
    """快照 6 已经包含快照 5 的内容。事件 6 先到、事件 5 后到，5 直接跳过，不会把基线倒退回去。"""
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    import_notice(
        db_session, title="虚构软件采购招标公告（第二次）", entries=DEADLINE_LATER, observed="2026-10-02T10:00:00Z"
    )
    events = list(
        db_session.scalars(
            select(OutboxEvent).where(OutboxEvent.topic == "catalog.notice_version_added").order_by(OutboxEvent.id)
        )
    )
    service.handle_notice_version_added(db_session, events[-1].payload)
    service.handle_notice_version_added(db_session, events[-2].payload)
    changes = service.list_changes(db_session, users["viewer"].id, wid)
    assert len(changes) == 1
    assert (
        service.get_watch(db_session, users["viewer"].id, wid, notice["notice_id"]).baseline_snapshot
        == events[-1].payload["seq"]
    )


def test_unrelated_notice_does_not_trigger(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    import_notice(db_session, title="另一个虚构项目", key="https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t99.htm")
    deliver(db_session)
    assert service.list_changes(db_session, users["viewer"].id, wid) == []


def test_profile_confirmation_is_a_change(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    deliver(db_session)
    confirm_profile(db_session, wid, users["admin"], {**PROFILE, "staffing": "6名软件工程师"})
    deliver(db_session)
    changes = service.list_changes(db_session, users["viewer"].id, wid)
    assert [c.diff["kind"] for c in changes] == ["profile_change"]


def test_other_company_changes_are_invisible(db_session: Session) -> None:
    a, users_a = make_company(db_session, "a")
    b, users_b = make_company(db_session, "b")
    notice = import_notice(db_session)
    watch(db_session, users_a, a, notice)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    assert service.list_changes(db_session, users_b["viewer"].id, b) == []
    assert service.list_notifications(db_session, users_b["viewer"].id, b) == []


def test_read_state_is_per_person(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    item = service.list_notifications(db_session, users["member"].id, wid)[0]
    service.mark_read(db_session, users["member"].id, wid, item.id)
    assert service.list_notifications(db_session, users["member"].id, wid)[0].read_at is not None
    assert service.list_notifications(db_session, users["viewer"].id, wid)[0].read_at is None


# ---- 自动复核委托 ----


def reassessments(session) -> list[ReassessmentRow]:
    return list(session.scalars(select(ReassessmentRow).order_by(ReassessmentRow.created_at)))


def test_auto_reassess_needs_a_profile_and_analyze_right(db_session: Session) -> None:
    wid, users = make_company(db_session)
    notice = import_notice(db_session)
    with pytest.raises(AppError, match="profile_required"):
        service.set_watch(
            db_session,
            users["member"].id,
            wid,
            notice["notice_id"],
            active=True,
            auto_reassess=True,
            expected_version=0,
            key="w",
        )
    confirm_profile(db_session, wid, users["admin"])
    with pytest.raises(AppError, match="forbidden"):
        service.set_watch(
            db_session,
            users["viewer"].id,
            wid,
            notice["notice_id"],
            active=True,
            auto_reassess=True,
            expected_version=0,
            key="v",
        )


def test_change_with_valid_delegation_queues_one_reassessment(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    service.set_watch(
        db_session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=True,
        expected_version=0,
        key="w",
    )
    deliver(db_session)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    [row] = reassessments(db_session)
    assert row.state == "dispatched"
    run = db_session.get(RunRow, row.run_id)
    # 复核按委托里的发起人建任务；没开模型时用规则基线，不花钱。
    assert (run.kind, run.actor_id, run.mode, run.status) == ("reassessment", users["member"].id, "baseline", "queued")
    assert run.delegation["reassessment_id"] == row.id


def test_finished_reassessment_records_the_result_and_notifies_once(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    service.set_watch(
        db_session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=True,
        expected_version=0,
        key="w",
    )
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    [row] = reassessments(db_session)
    payload = {"run_id": row.run_id, "workspace_id": wid, "kind": "reassessment", "status": "succeeded"}
    service.handle_run_finished(db_session, payload)
    service.handle_run_finished(db_session, payload)  # 事件可能投递两次
    db_session.flush()
    assert (row.state, row.reason) == ("finished", "succeeded")
    kinds = [n.kind for n in db_session.scalars(select(NotificationRow).where(NotificationRow.workspace_id == wid))]
    assert kinds.count(service.NOTIFY_REASSESSMENT) == 1


def test_without_delegation_there_is_no_reassessment(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    watch(db_session, users, wid, notice)
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    assert reassessments(db_session) == []


def test_access_change_voids_the_delegation(db_session: Session) -> None:
    """发起人被改成只读：之后的变化只提醒，不再自动复核；没结束的复核也作废。"""
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    service.set_watch(
        db_session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=True,
        expected_version=0,
        key="w",
    )
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    workspace.change_member(db_session, users["admin"].id, wid, users["member"].id, "viewer", True, 1)
    deliver(db_session)
    [cancelled] = reassessments(db_session)
    assert (cancelled.state, cancelled.reason) == ("cancelled", "access_changed")
    run = db_session.get(RunRow, cancelled.run_id)
    assert (run.status, run.reason) == ("cancelled", "reassessment_access_changed")
    import_notice(
        db_session, title="虚构软件采购招标公告（更正后）", entries=DEADLINE_LATER, observed="2026-10-03T10:00:00Z"
    )
    deliver(db_session)
    assert len(reassessments(db_session)) == 1
    assert len(service.list_changes(db_session, users["admin"].id, wid)) == 2


def test_changing_the_watch_cancels_open_reassessments(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    service.set_watch(
        db_session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=True,
        expected_version=0,
        key="w",
    )
    import_notice(db_session, entries=DEADLINE_LATER, observed="2026-10-01T10:00:00Z")
    deliver(db_session)
    service.set_watch(
        db_session,
        users["member"].id,
        wid,
        notice["notice_id"],
        active=True,
        auto_reassess=False,
        expected_version=1,
        key="w2",
    )
    [cancelled] = reassessments(db_session)
    assert (cancelled.state, cancelled.reason) == ("cancelled", "watch_changed")
    assert db_session.get(RunRow, cancelled.run_id).status == "cancelled"

"""workspace 的业务用例：权限、成员管理、档案提议和确认、幂等、事件、公司隔离。"""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidradar.infra.errors import AppError
from bidradar.infra.tables import OutboxEvent
from bidradar.modules.workspace import service
from bidradar.modules.workspace.models import Action, Role
from tests_v2.factories import PROFILE, confirm_profile, make_company, make_user


def events(session: Session, topic: str) -> list[dict]:
    rows = session.scalars(select(OutboxEvent).where(OutboxEvent.topic == topic).order_by(OutboxEvent.id))
    return [row.payload for row in rows]


# ---- 权限 ----


def test_authorize_returns_role_and_versions(db_session: Session) -> None:
    wid, users = make_company(db_session)
    access = service.authorize(db_session, users["member"].id, wid, Action.ANALYZE)
    assert access.role is Role.MEMBER
    assert access.membership_version == 1
    assert access.current_profile_version == 0


def test_viewer_cannot_write(db_session: Session) -> None:
    wid, users = make_company(db_session)
    with pytest.raises(AppError, match="forbidden"):
        service.authorize(db_session, users["viewer"].id, wid, Action.WRITE)


def test_outsider_gets_not_found_not_forbidden(db_session: Session) -> None:
    """不是成员时报"不存在"：外人猜不出这家公司存不存在。"""
    wid, _ = make_company(db_session)
    stranger = make_user(db_session, "stranger")
    with pytest.raises(AppError, match="not_found"):
        service.authorize(db_session, stranger.id, wid, Action.READ)
    with pytest.raises(AppError, match="not_found"):
        service.authorize(db_session, stranger.id, "0" * 32, Action.READ)


def test_removed_member_loses_access_immediately(db_session: Session) -> None:
    wid, users = make_company(db_session)
    service.change_member(db_session, users["admin"].id, wid, users["member"].id, "member", False, 1)
    with pytest.raises(AppError, match="not_found"):
        service.authorize(db_session, users["member"].id, wid, Action.READ)


def test_two_companies_cannot_see_each_other(db_session: Session) -> None:
    """应用层的公司隔离。数据库层的第二道防线（RLS）见 test_rls.py。"""
    a, users_a = make_company(db_session, "a")
    b, users_b = make_company(db_session, "b")
    confirm_profile(db_session, a, users_a["admin"])
    for action in (service.profile_overview, service.list_members):
        with pytest.raises(AppError, match="not_found"):
            action(db_session, users_b["admin"].id, a)
    assert [w.workspace_id for w in service.list_workspaces(db_session, users_b["admin"].id)] == [b]


# ---- 成员 ----


def test_change_member_bumps_version_and_records_history(db_session: Session) -> None:
    wid, users = make_company(db_session)
    member = service.change_member(db_session, users["admin"].id, wid, users["member"].id, "viewer", True, 1)
    assert (member.role, member.version) == (Role.VIEWER, 2)
    history = service.member_history(db_session, users["admin"].id, wid, users["member"].id)
    assert [(h.version, h.old_role, h.new_role, h.actor_id) for h in history] == [
        (1, None, "member", None),
        (2, "member", "viewer", users["admin"].id),
    ]


def test_stale_member_version_rejected(db_session: Session) -> None:
    """管理员 A 刚移出某人，管理员 B 用旧页面改他的角色：版本号过期，不能把人"复活"。"""
    wid, users = make_company(db_session)
    service.change_member(db_session, users["admin"].id, wid, users["member"].id, "member", False, 1)
    with pytest.raises(AppError, match="membership_version_conflict"):
        service.change_member(db_session, users["admin"].id, wid, users["member"].id, "admin", True, 1)


def test_last_admin_cannot_step_down(db_session: Session) -> None:
    wid, users = make_company(db_session)
    with pytest.raises(AppError, match="last_admin"):
        service.change_member(db_session, users["admin"].id, wid, users["admin"].id, "member", True, 1)


def test_only_admins_manage_members(db_session: Session) -> None:
    wid, users = make_company(db_session)
    with pytest.raises(AppError, match="forbidden"):
        service.change_member(db_session, users["member"].id, wid, users["viewer"].id, "member", True, 1)
    with pytest.raises(AppError, match="forbidden"):
        service.member_history(db_session, users["member"].id, wid, users["viewer"].id)


def test_member_list_includes_removed_members(db_session: Session) -> None:
    wid, users = make_company(db_session)
    service.change_member(db_session, users["admin"].id, wid, users["viewer"].id, "viewer", False, 1)
    members = {m.username: m for m in service.list_members(db_session, users["admin"].id, wid)}
    assert set(members) == {"a.admin", "a.member", "a.viewer"}
    assert members["a.viewer"].active is False


def test_membership_changes_publish_events(db_session: Session) -> None:
    wid, users = make_company(db_session)
    service.change_member(db_session, users["admin"].id, wid, users["member"].id, "viewer", True, 1)
    published = events(db_session, "workspace.access_changed")
    assert {"workspace_id": wid, "user_id": users["member"].id, "membership_version": 2} in published


# ---- 档案 ----


def test_propose_then_confirm_creates_version_one(db_session: Session) -> None:
    wid, users = make_company(db_session)
    proposal = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "k1")
    assert proposal.status == "proposed"
    version = service.confirm_profile(db_session, users["admin"].id, wid, proposal.id, 0, "k2")
    assert version.version == 1
    assert version.payload["company_name"] == "山岚软件（虚构）"
    assert version.proof_status == "not_provided"
    overview = service.profile_overview(db_session, users["viewer"].id, wid)
    assert overview.current.version == 1
    assert overview.proposals[0].status == "confirmed"
    assert service.authorize(db_session, users["member"].id, wid, Action.READ).current_profile_version == 1


def test_viewer_cannot_propose_and_member_cannot_confirm(db_session: Session) -> None:
    wid, users = make_company(db_session)
    with pytest.raises(AppError, match="forbidden"):
        service.propose_profile(db_session, users["viewer"].id, wid, PROFILE, 0, "k1")
    proposal = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "k1")
    with pytest.raises(AppError, match="forbidden"):
        service.confirm_profile(db_session, users["member"].id, wid, proposal.id, 0, "k2")


def test_proposal_on_stale_version_rejected(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    with pytest.raises(AppError, match="profile_version_conflict"):
        service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "late")


def test_cannot_confirm_a_proposal_based_on_an_old_version(db_session: Session) -> None:
    """两份提议都基于第 0 版。确认了第一份以后，第二份不能再确认，否则会悄悄覆盖第一份的修改。"""
    wid, users = make_company(db_session)
    first = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "k1")
    second = service.propose_profile(db_session, users["admin"].id, wid, {**PROFILE, "city": "北京"}, 0, "k2")
    service.confirm_profile(db_session, users["admin"].id, wid, first.id, 0, "c1")
    with pytest.raises(AppError, match="profile_version_conflict"):
        service.confirm_profile(db_session, users["admin"].id, wid, second.id, 1, "c2")


def test_proposal_confirmed_only_once(db_session: Session) -> None:
    wid, users = make_company(db_session)
    proposal = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "k1")
    service.confirm_profile(db_session, users["admin"].id, wid, proposal.id, 0, "c1")
    with pytest.raises(AppError, match="proposal_already_confirmed"):
        service.confirm_profile(db_session, users["admin"].id, wid, proposal.id, 1, "c2")


def test_same_key_replays_the_first_result(db_session: Session) -> None:
    """响应丢了、前端用同一个键重试：不会多出一份提议，也不会把版本号推两格。"""
    wid, users = make_company(db_session)
    first = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "same")
    again = service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "same")
    assert again.id == first.id
    v1 = service.confirm_profile(db_session, users["admin"].id, wid, first.id, 0, "same-confirm")
    v1_again = service.confirm_profile(db_session, users["admin"].id, wid, first.id, 0, "same-confirm")
    assert v1_again.version == v1.version == 1
    assert len(service.profile_overview(db_session, users["admin"].id, wid).history) == 1


def test_same_key_with_different_content_conflicts(db_session: Session) -> None:
    wid, users = make_company(db_session)
    service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "same")
    with pytest.raises(AppError, match="idempotency_conflict"):
        service.propose_profile(db_session, users["member"].id, wid, {**PROFILE, "city": "北京"}, 0, "same")


def test_another_member_reusing_a_key_conflicts(db_session: Session) -> None:
    """幂等指纹里有操作人：别人拿同一个键来，拿不到第一个人的结果。"""
    wid, users = make_company(db_session)
    service.propose_profile(db_session, users["member"].id, wid, PROFILE, 0, "same")
    with pytest.raises(AppError, match="idempotency_conflict"):
        service.propose_profile(db_session, users["admin"].id, wid, PROFILE, 0, "same")


def test_confirm_publishes_profile_event(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    assert events(db_session, "workspace.profile_confirmed") == [{"workspace_id": wid, "profile_version": 1}]


def test_get_profile_returns_frozen_versions(db_session: Session) -> None:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    confirm_profile(db_session, wid, users["admin"], {**PROFILE, "city": "北京"})
    assert service.get_profile(db_session, wid, 1).payload["city"] == "天津"
    assert service.get_profile(db_session, wid, 2).payload["city"] == "北京"
    assert service.current_profile(db_session, wid).version == 2
    with pytest.raises(AppError, match="not_found"):
        service.get_profile(db_session, wid, 3)

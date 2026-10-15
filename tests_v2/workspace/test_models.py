"""workspace 的纯逻辑：角色权限、档案字段、最后一个管理员。不连数据库。"""

import pytest

from bidradar.infra.errors import AppError
from bidradar.modules.workspace.models import Action, Role, allows, check_member_change, normalize_profile
from tests_v2.factories import PROFILE


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        (Role.ADMIN, set(Action)),
        (Role.MEMBER, {Action.READ, Action.WRITE, Action.ANALYZE, Action.TRACK}),
        (Role.VIEWER, {Action.READ}),
    ],
)
def test_role_actions(role: Role, allowed: set[Action]) -> None:
    assert {action for action in Action if allows(role, action)} == allowed


def test_profile_is_trimmed_and_blank_means_unfilled() -> None:
    profile = normalize_profile({**PROFILE, "city": "  天津  ", "cases": "   "})
    assert profile["city"] == "天津"
    assert profile["cases"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {**PROFILE, "company_name": None},
        {**PROFILE, "company_name": "  "},
        {k: v for k, v in PROFILE.items() if k != "city"},
        {**PROFILE, "extra_field": "x"},
        {**PROFILE, "capabilities": "x" * 4001},
        {**PROFILE, "city": 123},
        "not a dict",
    ],
)
def test_bad_profiles_rejected(payload: object) -> None:
    with pytest.raises(AppError, match="invalid_profile"):
        normalize_profile(payload)


def test_cannot_remove_the_last_admin() -> None:
    with pytest.raises(AppError, match="last_admin"):
        check_member_change(
            current_role=Role.ADMIN, current_active=True, new_role=Role.MEMBER, new_active=True, active_admins=1
        )
    with pytest.raises(AppError, match="last_admin"):
        check_member_change(
            current_role=Role.ADMIN, current_active=True, new_role=Role.ADMIN, new_active=False, active_admins=1
        )


def test_can_demote_an_admin_when_another_remains() -> None:
    check_member_change(
        current_role=Role.ADMIN, current_active=True, new_role=Role.MEMBER, new_active=True, active_admins=2
    )


def test_changing_a_non_admin_never_hits_the_rule() -> None:
    check_member_change(
        current_role=Role.MEMBER, current_active=True, new_role=Role.VIEWER, new_active=False, active_admins=1
    )

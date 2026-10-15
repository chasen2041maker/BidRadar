"""workspace 的领域对象和业务规则：角色能做什么、企业档案长什么样、成员变更的约束。

纯 Python，不 import FastAPI 或 SQLAlchemy，不连数据库也能直接单测。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from bidradar.infra.errors import AppError, conflict


class Role(StrEnum):
    """公司内的三种角色。平台没有"超级管理员"：谁都只能管自己所在的公司。"""

    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class Action(StrEnum):
    """需要检查权限的动作。别的模块调用 authorize() 时传其中一个。"""

    READ = "read"  # 看公告、档案、报告、提醒
    WRITE = "write"  # 提档案修改建议、上传招标文件
    ANALYZE = "analyze"  # 发起研究或追问（会花钱）
    TRACK = "track"  # 改跟进决定、关注设置
    ADMIN = "admin"  # 确认档案、管理成员


# 每种角色能做的动作。只读成员能看一切，但不能改任何东西，也不能发起会花钱的研究。
ROLE_ACTIONS: dict[Role, frozenset[Action]] = {
    Role.ADMIN: frozenset(Action),
    Role.MEMBER: frozenset({Action.READ, Action.WRITE, Action.ANALYZE, Action.TRACK}),
    Role.VIEWER: frozenset({Action.READ}),
}


def allows(role: Role, action: Action) -> bool:
    return action in ROLE_ACTIONS[role]


def parse_role(value: object) -> Role:
    try:
        return Role(value)
    except ValueError:
        raise AppError("invalid_role") from None


@dataclass(frozen=True)
class Access:
    """authorize() 的结果。

    membership_version 每次成员变更都加一。别的模块据此判断"权限有没有变过"：
    一个人被移出后又加回来，版本号也变了，所以他被移出前发起的旧任务不会因此恢复运行。
    """

    user_id: str
    workspace_id: str
    role: Role
    membership_version: int
    current_profile_version: int


# 企业档案的九个字段，以及每个字段的长度上限。
# 除公司名称外都可以空着。空着表示"还没填"，不表示"没有"：没填资质不代表没有资质，研究时会判成"未知"。
PROFILE_FIELDS: dict[str, int] = {
    "company_name": 200,
    "city": 200,
    "project_types": 4000,
    "capabilities": 4000,
    "delivery_constraints": 4000,
    "cases": 4000,
    "qualifications": 4000,
    "staffing": 4000,
    "commercial_constraints": 4000,
}

PROFILE_LABELS: dict[str, str] = {
    "company_name": "公司名称",
    "city": "所在城市",
    "project_types": "想接的项目类型",
    "capabilities": "能力",
    "delivery_constraints": "交付限制",
    "cases": "案例",
    "qualifications": "资质",
    "staffing": "人力排期",
    "commercial_constraints": "商务限制",
}


def normalize_profile(payload: object) -> dict[str, str | None]:
    """检查并整理一份档案：九个字段必须齐全，去掉首尾空白，空字符串转成 None。"""
    if not isinstance(payload, dict) or set(payload) != set(PROFILE_FIELDS):
        raise AppError("invalid_profile")
    result: dict[str, str | None] = {}
    for field, limit in PROFILE_FIELDS.items():
        value = payload[field]
        if value is None:
            result[field] = None
            continue
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            raise AppError("invalid_profile")
        result[field] = value.strip() or None
    if result["company_name"] is None:
        raise AppError("invalid_profile")
    return result


@dataclass(frozen=True)
class ProfileVersion:
    """一个正式档案版本。确认后不再修改，研究任务创建时固定引用其中一个版本。

    几个状态字段说明这份档案的可信程度：内容是企业自己确认的声明，证明材料没有提供，也没有核验过。
    研究时资格类要求因此一律判"未知"。
    """

    workspace_id: str
    version: int
    proposal_id: str
    payload: dict[str, Any]
    confirmed_by: str
    confirmed_at: datetime
    declaration_status: str = "user_confirmed"
    proof_status: str = "not_provided"
    validity_status: str = "not_checked"


@dataclass(frozen=True)
class ProfileProposal:
    id: str
    workspace_id: str
    base_version: int
    payload: dict[str, Any]
    proposed_by: str
    created_at: datetime
    confirmed_version: int | None

    @property
    def status(self) -> str:
        return "confirmed" if self.confirmed_version is not None else "proposed"


@dataclass(frozen=True)
class WorkspaceSummary:
    """当前用户所在的一家公司，以及他在这家公司的角色。"""

    workspace_id: str
    name: str
    role: Role


@dataclass(frozen=True)
class Member:
    user_id: str
    username: str
    display_name: str
    role: Role
    active: bool
    version: int


@dataclass(frozen=True)
class MemberEvent:
    version: int
    actor_id: str | None
    old_role: str | None
    old_active: bool | None
    new_role: str
    new_active: bool
    recorded_at: datetime


@dataclass(frozen=True)
class ProfileOverview:
    current: ProfileVersion | None
    history: list[ProfileVersion]
    proposals: list[ProfileProposal]


def check_member_change(
    *,
    current_role: Role,
    current_active: bool,
    new_role: Role,
    new_active: bool,
    active_admins: int,
) -> None:
    """公司至少要保留一个有效管理员，否则就没人能确认档案、管理成员了。"""
    removes_admin = current_active and current_role is Role.ADMIN and (not new_active or new_role is not Role.ADMIN)
    if removes_admin and active_admins <= 1:
        raise conflict("last_admin")


# 本模块发出的事件（写进 outbox）。payload 只带 ID 和版本号，不带档案正文。
TOPIC_ACCESS_CHANGED = "workspace.access_changed"
TOPIC_PROFILE_CONFIRMED = "workspace.profile_confirmed"

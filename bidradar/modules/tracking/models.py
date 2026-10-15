"""tracking 的领域规则：决定的几种状态、关注的维度、自动复核委托的有效期。

三件事要分清：
    - 决定（decision）：用户对一个公告的人工判断：待评估、跟进、放弃。由人决定，系统不自动改。
    - 关注（watch）：要不要盯着这个公告的变化。关注不会改变决定。
    - 自动复核委托（auto_reassess）：发现变化后，能不能自动花钱再做一次研究。
      默认关闭，最长 7 天，只在发起人的权限和档案版本都没变时有效。

纯 Python，不 import FastAPI 或 SQLAlchemy。比较两个版本的规则在 diff.py（从旧版原样搬来）。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from bidradar.infra.errors import AppError
from bidradar.modules.tracking.diff import SCOPES


class DecisionState(StrEnum):
    NEEDS_REVIEW = "needs_review"
    FOLLOW_UP = "follow_up"
    DISMISSED = "dismissed"


MAX_DELEGATION = timedelta(days=7)
MAX_REASON = 4000


def parse_decision(value: object) -> DecisionState:
    try:
        return DecisionState(value)
    except ValueError:
        raise AppError("invalid_decision") from None


def check_reason(value: object) -> str:
    if not isinstance(value, str) or len(value) > MAX_REASON or "\x00" in value:
        raise AppError("invalid_reason")
    return value.strip()


def check_scope(value: object) -> list[str]:
    """关注哪些维度的变化。不传就是全部维度。"""
    if value is None:
        return sorted(SCOPES)
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or any(not isinstance(item, str) or item not in SCOPES for item in value)
    ):
        raise AppError("invalid_watch_scope")
    return sorted(value)


def delegation_expiry(requested: datetime | None, now: datetime) -> datetime:
    """自动复核委托的到期时间：默认 7 天后，用户指定的也不能超过 7 天、不能早于现在。"""
    if requested is None:
        return now + MAX_DELEGATION
    if requested.tzinfo is None or not now < requested <= now + MAX_DELEGATION:
        raise AppError("invalid_delegation_expiry")
    return requested


@dataclass(frozen=True)
class Decision:
    workspace_id: str
    notice_id: str
    version: int
    state: str
    reason: str
    actor_id: str
    observation_id: str
    recorded_at: datetime


@dataclass(frozen=True)
class Watch:
    id: str
    workspace_id: str
    notice_id: str
    version: int
    active: bool
    auto_reassess: bool
    actor_id: str
    membership_version: int
    expires_at: datetime | None
    scope: list[str]
    warnings: list[str]
    baseline_snapshot: int
    profile_version: int
    recorded_at: datetime


def delegation_valid(
    watch: Watch,
    *,
    membership_version: int | None,
    current_profile_version: int | None,
    scopes: list[str],
    now: datetime,
) -> bool:
    """这次变化能不能触发自动复核。

    全部条件都要满足：关注开着自动复核且没过期；发起人现在仍有"分析"权限，并且成员版本没变
    （被移出又加回来也算变了）；档案版本和关注记下的一致；变化落在关注选择的维度里。
    任何一条不满足就只发提醒、不花钱。
    """
    return (
        watch.active
        and watch.auto_reassess
        and watch.expires_at is not None
        and watch.expires_at > now
        and membership_version == watch.membership_version
        and current_profile_version == watch.profile_version
        and bool(set(scopes) & set(watch.scope))
    )


@dataclass(frozen=True)
class Change:
    id: str
    seq: int
    workspace_id: str
    watch_id: str
    notice_id: str
    source: str
    diff: dict[str, Any]
    catalog_snapshot: int
    profile_version: int
    created_at: datetime


@dataclass(frozen=True)
class Notification:
    id: str
    seq: int
    workspace_id: str
    kind: str
    notice_id: str
    watch_id: str | None
    change_id: str | None
    payload: dict[str, Any]
    created_at: datetime
    read_at: datetime | None


NOTIFY_CHANGE = "change_detected"
NOTIFY_REASSESSMENT = "reassessment_finished"


@dataclass(frozen=True)
class Reassessment:
    id: str
    watch_id: str
    change_id: str
    state: str
    reason: str | None
    run_id: str | None
    created_at: datetime
    updated_at: datetime

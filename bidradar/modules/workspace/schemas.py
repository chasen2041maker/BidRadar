"""workspace 接口的请求和响应结构（Pydantic），只给 api.py 用。"""

from typing import Any

from pydantic import BaseModel, Field

from bidradar.infra.api_types import IsoDateTime, StrictRequest
from bidradar.modules.workspace.models import Member, MemberEvent, ProfileProposal, ProfileVersion


class ProfileVersionOut(BaseModel):
    workspace_id: str
    version: int
    proposal_id: str
    payload: dict[str, Any]
    confirmed_by: str
    confirmed_at: IsoDateTime
    declaration_status: str
    proof_status: str
    validity_status: str

    @classmethod
    def of(cls, value: ProfileVersion) -> "ProfileVersionOut":
        return cls(**value.__dict__)


class ProposalOut(BaseModel):
    id: str
    workspace_id: str
    base_version: int
    payload: dict[str, Any]
    proposed_by: str
    created_at: IsoDateTime
    status: str
    confirmed_version: int | None

    @classmethod
    def of(cls, value: ProfileProposal) -> "ProposalOut":
        return cls(**value.__dict__, status=value.status)


class ProfileOverviewOut(BaseModel):
    current: ProfileVersionOut | None
    history: list[ProfileVersionOut]
    proposals: list[ProposalOut]


class ProposeRequest(StrictRequest):
    # 档案字段的检查（九个字段、长度上限）在 models.normalize_profile 里，Worker 和命令行也走同一套规则。
    payload: dict[str, Any]
    base_version: int = Field(ge=0)
    key: str


class ConfirmRequest(StrictRequest):
    proposal_id: str = Field(max_length=64)
    expected_version: int = Field(ge=0)
    key: str


class MemberOut(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: str
    active: bool
    version: int

    @classmethod
    def of(cls, value: Member) -> "MemberOut":
        return cls(**{**value.__dict__, "role": value.role.value})


class MemberListOut(BaseModel):
    items: list[MemberOut]


class MemberEventOut(BaseModel):
    version: int
    actor_id: str | None
    old_role: str | None
    old_active: bool | None
    new_role: str
    new_active: bool
    recorded_at: IsoDateTime

    @classmethod
    def of(cls, value: MemberEvent) -> "MemberEventOut":
        return cls(**value.__dict__)


class MemberHistoryOut(BaseModel):
    items: list[MemberEventOut]


class ChangeMemberRequest(StrictRequest):
    target_user_id: str = Field(max_length=64)
    role: str
    active: bool
    expected_version: int = Field(ge=1)

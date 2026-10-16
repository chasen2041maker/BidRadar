"""tracking 接口的请求和响应结构（Pydantic），只给 api.py 用。"""

from typing import Any

from pydantic import BaseModel, Field

from bidradar.infra.api_types import IsoDateTime, StrictRequest
from bidradar.modules.tracking.models import Change, Decision, Notification, Watch


class EmptyRequest(StrictRequest):
    pass


class DecisionOut(BaseModel):
    notice_id: str
    version: int
    state: str
    reason: str
    actor_id: str
    observation_id: str
    recorded_at: IsoDateTime

    @classmethod
    def of(cls, value: Decision) -> "DecisionOut":
        return cls(**{k: v for k, v in value.__dict__.items() if k in cls.model_fields})


class DecisionDetailOut(BaseModel):
    current: DecisionOut | None
    history: list[DecisionOut]


class DecisionRequest(StrictRequest):
    state: str
    reason: str = Field(default="", max_length=4000)
    expected_version: int = Field(ge=0)
    key: str


class WatchOut(BaseModel):
    id: str
    notice_id: str
    version: int
    active: bool
    auto_reassess: bool
    actor_id: str
    expires_at: IsoDateTime | None
    scope: list[str]
    warnings: list[str]
    baseline_snapshot: int
    profile_version: int
    recorded_at: IsoDateTime

    @classmethod
    def of(cls, value: Watch) -> "WatchOut":
        return cls(**{k: v for k, v in value.__dict__.items() if k in cls.model_fields})


class WatchDetailOut(BaseModel):
    watch: WatchOut | None


class WatchRequest(StrictRequest):
    active: bool
    auto_reassess: bool = False
    expected_version: int = Field(ge=0)
    key: str
    expires_at: IsoDateTime | None = None
    scope: list[str] | None = None


class ChangeOut(BaseModel):
    id: str
    seq: int
    notice_id: str
    watch_id: str
    source: str
    diff: dict[str, Any]
    catalog_snapshot: int
    profile_version: int
    created_at: IsoDateTime

    @classmethod
    def of(cls, value: Change) -> "ChangeOut":
        return cls(**{k: v for k, v in value.__dict__.items() if k in cls.model_fields})


class NotificationOut(BaseModel):
    id: str
    seq: int
    kind: str
    notice_id: str
    watch_id: str | None
    change_id: str | None
    payload: dict[str, Any]
    created_at: IsoDateTime
    read_at: IsoDateTime | None

    @classmethod
    def of(cls, value: Notification) -> "NotificationOut":
        return cls(**{k: v for k, v in value.__dict__.items() if k in cls.model_fields})


class PageOut(BaseModel):
    items: list[Any]
    next_after: int


class ReadOut(BaseModel):
    id: str
    read_at: IsoDateTime


class ReassessmentOut(BaseModel):
    id: str
    watch_id: str
    change_id: str
    state: str
    reason: str | None
    run_id: str | None
    created_at: IsoDateTime
    updated_at: IsoDateTime

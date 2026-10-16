"""research 接口的请求和响应结构（Pydantic），只给 api.py 用。报告正文就是 agent.schemas.Report 的 JSON。"""

from typing import Any

from pydantic import BaseModel, Field

from bidradar.infra.api_types import IsoDateTime, StrictRequest
from bidradar.modules.research.models import BudgetSummary, Estimate, Run, RunDetail

NoticeIdStr = Field(pattern=r"^[0-9a-f]{64}$")


class EstimateRequest(StrictRequest):
    notice_id: str = NoticeIdStr
    document_ids: list[str] = Field(default_factory=list, max_length=10)


class CreateRunRequest(EstimateRequest):
    key: str


class QuestionRequest(StrictRequest):
    question: str = Field(min_length=1, max_length=2000)
    key: str


class CancelRequest(StrictRequest):
    key: str


class EstimateOut(BaseModel):
    mode: str
    documents: int
    chunks: int
    characters: int
    model_calls: int
    cost_micro: int
    run_cap_micro: int

    @classmethod
    def of(cls, value: Estimate) -> "EstimateOut":
        return cls(**value.__dict__)


class RunOut(BaseModel):
    id: str
    kind: str
    mode: str
    notice_id: str
    parent_run_id: str | None
    question: str | None
    status: str
    reason: str | None
    profile_version: int
    catalog_snapshot: int
    document_ids: list[str]
    config_version: str
    estimate_micro: int
    version: int
    created_at: IsoDateTime
    started_at: IsoDateTime | None
    finished_at: IsoDateTime | None

    @classmethod
    def of(cls, value: Run) -> "RunOut":
        return cls(**{k: v for k, v in value.__dict__.items() if k in cls.model_fields})


class RunListItemOut(RunOut):
    # 公告已经不在目录里时为 None
    title: str | None = None
    simulation: bool | None = None


class RunListOut(BaseModel):
    items: list[RunListItemOut]
    next_before: str | None


class RunDetailOut(RunOut):
    title: str
    simulation: bool
    report: dict[str, Any] | None
    report_kind: str | None
    freshness: dict[str, str]
    cost: dict[str, int]

    @classmethod
    def of_detail(cls, value: RunDetail) -> "RunDetailOut":
        base = {k: v for k, v in value.run.__dict__.items() if k in cls.model_fields}
        return cls(
            **base,
            title=value.title,
            simulation=value.simulation,
            report=value.report,
            report_kind=value.report_kind,
            freshness=value.freshness,
            cost=value.cost,
        )


class BudgetOut(BaseModel):
    total_cap: int
    workspace_cap: int
    run_cap: int
    workspace_used: int
    workspace_calls: int
    workspace_unknown: int
    price_version: str
    model_enabled: bool

    @classmethod
    def of(cls, value: BudgetSummary) -> "BudgetOut":
        return cls(**value.__dict__)

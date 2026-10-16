"""research 的 HTTP 路由：预估、发起研究、看进度和报告、追问、取消、费用。

发起研究分两步（决策 0010）：先 POST /research/estimate 看范围和预计费用（不花钱），
用户确认后再 POST /research/runs 建任务。两步都只有"分析"权限的成员能做。
"""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from bidradar.agent.llm import Pricing
from bidradar.app.deps import AppSettings, CurrentUser, DbSession, WorkspaceId
from bidradar.modules.research import service
from bidradar.modules.research.models import Limits
from bidradar.modules.research.schemas import (
    BudgetOut,
    CancelRequest,
    CreateRunRequest,
    EstimateOut,
    EstimateRequest,
    QuestionRequest,
    RunDetailOut,
    RunListItemOut,
    RunListOut,
    RunOut,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}/research", tags=["research"])
RunId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]


def limits_of(settings) -> Limits:
    return Limits(
        model_enabled=settings.model_enabled,
        total_cap=settings.budget_total_cap_micro,
        workspace_cap=settings.budget_workspace_cap_micro,
        run_cap=settings.budget_run_cap_micro,
    )


@router.post("/estimate")
def estimate(
    body: EstimateRequest, workspace_id: WorkspaceId, user: CurrentUser, session: DbSession, settings: AppSettings
) -> EstimateOut:
    value = service.estimate_run(
        session, user.id, workspace_id, body.notice_id, body.document_ids, limits_of(settings), Pricing()
    )
    return EstimateOut.of(value)


@router.post("/runs")
def create_run(
    body: CreateRunRequest, workspace_id: WorkspaceId, user: CurrentUser, session: DbSession, settings: AppSettings
) -> RunOut:
    run = service.create_analysis(
        session, user.id, workspace_id, body.notice_id, body.document_ids, body.key, limits_of(settings), Pricing()
    )
    return RunOut.of(run)


@router.get("/runs")
def list_runs(
    workspace_id: WorkspaceId,
    user: CurrentUser,
    session: DbSession,
    before: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> RunListOut:
    runs = service.list_runs(session, user.id, workspace_id, before, limit)
    titles = service.notice_titles(session, [r.notice_id for r in runs])
    items = []
    for run in runs:
        title, simulation = titles.get(run.notice_id, (None, None))
        items.append(RunListItemOut(**RunOut.of(run).model_dump(), title=title, simulation=simulation))
    return RunListOut(items=items, next_before=runs[-1].id if len(runs) == limit else None)


@router.get("/runs/{run_id}")
def get_run(workspace_id: WorkspaceId, run_id: RunId, user: CurrentUser, session: DbSession) -> RunDetailOut:
    return RunDetailOut.of_detail(service.get_run(session, user.id, workspace_id, run_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(
    body: CancelRequest, workspace_id: WorkspaceId, run_id: RunId, user: CurrentUser, session: DbSession
) -> RunOut:
    return RunOut.of(service.cancel_run(session, user.id, workspace_id, run_id, body.key))


@router.post("/runs/{run_id}/questions")
def ask(
    body: QuestionRequest,
    workspace_id: WorkspaceId,
    run_id: RunId,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> RunOut:
    run = service.create_question(session, user.id, workspace_id, run_id, body.question, body.key, limits_of(settings))
    return RunOut.of(run)


@router.get("/budget")
def budget(workspace_id: WorkspaceId, user: CurrentUser, session: DbSession, settings: AppSettings) -> BudgetOut:
    return BudgetOut.of(service.budget_summary(session, user.id, workspace_id, limits_of(settings), Pricing()))

"""tracking 的 HTTP 路由：跟进决定、关注、变化、提醒、自动复核记录。路径沿用旧工作台。"""

from typing import Annotated

from fastapi import APIRouter, Path, Query

from bidradar.app.deps import CurrentUser, DbSession, WorkspaceId
from bidradar.modules.tracking import service
from bidradar.modules.tracking.schemas import (
    ChangeOut,
    DecisionDetailOut,
    DecisionOut,
    DecisionRequest,
    EmptyRequest,
    NotificationOut,
    PageOut,
    ReadOut,
    ReassessmentOut,
    WatchDetailOut,
    WatchOut,
    WatchRequest,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}/tracking", tags=["tracking"])
NoticeId = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]
After = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.get("/decisions/{notice_id}")
def get_decision(
    workspace_id: WorkspaceId, notice_id: NoticeId, user: CurrentUser, session: DbSession
) -> DecisionDetailOut:
    value = service.get_decision(session, user.id, workspace_id, notice_id)
    return DecisionDetailOut(
        current=DecisionOut.of(value["current"]) if value["current"] else None,
        history=[DecisionOut.of(d) for d in value["history"]],
    )


@router.post("/decisions/{notice_id}")
def set_decision(
    body: DecisionRequest, workspace_id: WorkspaceId, notice_id: NoticeId, user: CurrentUser, session: DbSession
) -> DecisionOut:
    decision = service.set_decision(
        session, user.id, workspace_id, notice_id, body.state, body.reason, body.expected_version, body.key
    )
    return DecisionOut.of(decision)


@router.get("/watches/{notice_id}")
def get_watch(workspace_id: WorkspaceId, notice_id: NoticeId, user: CurrentUser, session: DbSession) -> WatchDetailOut:
    watch = service.get_watch(session, user.id, workspace_id, notice_id)
    return WatchDetailOut(watch=WatchOut.of(watch) if watch else None)


@router.post("/watches/{notice_id}")
def set_watch(
    body: WatchRequest, workspace_id: WorkspaceId, notice_id: NoticeId, user: CurrentUser, session: DbSession
) -> WatchOut:
    watch = service.set_watch(
        session,
        user.id,
        workspace_id,
        notice_id,
        active=body.active,
        auto_reassess=body.auto_reassess,
        expected_version=body.expected_version,
        key=body.key,
        expires_at=body.expires_at,
        scope=body.scope,
    )
    return WatchOut.of(watch)


@router.get("/changes")
def list_changes(
    workspace_id: WorkspaceId, user: CurrentUser, session: DbSession, after: After = 0, limit: Limit = 20
) -> PageOut:
    items = [ChangeOut.of(c) for c in service.list_changes(session, user.id, workspace_id, after, limit)]
    return PageOut(items=items, next_after=items[-1].seq if items else after)


@router.get("/notifications")
def list_notifications(
    workspace_id: WorkspaceId, user: CurrentUser, session: DbSession, after: After = 0, limit: Limit = 20
) -> PageOut:
    items = [NotificationOut.of(n) for n in service.list_notifications(session, user.id, workspace_id, after, limit)]
    return PageOut(items=items, next_after=items[-1].seq if items else after)


@router.post("/notifications/{notification_id}/read")
def mark_read(
    body: EmptyRequest,
    workspace_id: WorkspaceId,
    notification_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")],
    user: CurrentUser,
    session: DbSession,
) -> ReadOut:
    return ReadOut(id=notification_id, read_at=service.mark_read(session, user.id, workspace_id, notification_id))


@router.get("/reassessments")
def list_reassessments(workspace_id: WorkspaceId, user: CurrentUser, session: DbSession) -> dict:
    rows = service.list_reassessments(session, user.id, workspace_id)
    return {"items": [ReassessmentOut(**r.__dict__) for r in rows]}

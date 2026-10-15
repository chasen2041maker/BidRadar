"""workspace 的 HTTP 路由：企业档案和成员管理。

这里不写业务规则，也不直接读写数据库。权限检查在 service 里：
api 只负责把"谁（CurrentUser）、在哪家公司（WorkspaceId）、要做什么"交给 service。
"""

from fastapi import APIRouter, Path

from bidradar.app.deps import CurrentUser, DbSession, WorkspaceId
from bidradar.modules.workspace import service
from bidradar.modules.workspace.schemas import (
    ChangeMemberRequest,
    ConfirmRequest,
    MemberHistoryOut,
    MemberEventOut,
    MemberListOut,
    MemberOut,
    ProfileOverviewOut,
    ProfileVersionOut,
    ProposalOut,
    ProposeRequest,
)

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["workspace"])


@router.get("/profile")
def get_profile(workspace_id: WorkspaceId, user: CurrentUser, session: DbSession) -> ProfileOverviewOut:
    overview = service.profile_overview(session, user.id, workspace_id)
    return ProfileOverviewOut(
        current=ProfileVersionOut.of(overview.current) if overview.current else None,
        history=[ProfileVersionOut.of(v) for v in overview.history],
        proposals=[ProposalOut.of(p) for p in overview.proposals],
    )


@router.post("/profile/proposals")
def propose_profile(
    body: ProposeRequest, workspace_id: WorkspaceId, user: CurrentUser, session: DbSession
) -> ProposalOut:
    proposal = service.propose_profile(session, user.id, workspace_id, body.payload, body.base_version, body.key)
    return ProposalOut.of(proposal)


@router.post("/profile/confirm")
def confirm_profile(
    body: ConfirmRequest, workspace_id: WorkspaceId, user: CurrentUser, session: DbSession
) -> ProfileVersionOut:
    version = service.confirm_profile(session, user.id, workspace_id, body.proposal_id, body.expected_version, body.key)
    return ProfileVersionOut.of(version)


@router.get("/members")
def list_members(workspace_id: WorkspaceId, user: CurrentUser, session: DbSession) -> MemberListOut:
    return MemberListOut(items=[MemberOut.of(m) for m in service.list_members(session, user.id, workspace_id)])


@router.get("/members/{target_user_id}/history")
def member_history(
    workspace_id: WorkspaceId,
    target_user_id: str = Path(pattern=r"^[0-9a-f]{32}$"),
    *,
    user: CurrentUser,
    session: DbSession,
) -> MemberHistoryOut:
    events = service.member_history(session, user.id, workspace_id, target_user_id)
    return MemberHistoryOut(items=[MemberEventOut.of(e) for e in events])


@router.post("/members/change")
def change_member(
    body: ChangeMemberRequest, workspace_id: WorkspaceId, user: CurrentUser, session: DbSession
) -> MemberOut:
    member = service.change_member(
        session, user.id, workspace_id, body.target_user_id, body.role, body.active, body.expected_version
    )
    return MemberOut.of(member)

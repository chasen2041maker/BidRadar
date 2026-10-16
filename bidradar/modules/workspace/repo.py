"""workspace 的数据库读写，只给本模块的 service 用。

函数接收会话参数，只读写，不提交事务。查询私有数据的函数都要求传 workspace_id，
漏传就查不了：这是"只查本公司数据"的第一道防线。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.modules.workspace.models import ProfileProposal, ProfileVersion
from bidradar.modules.workspace.tables import (
    MembershipEventRow,
    MembershipRow,
    ProfileProposalRow,
    ProfileVersionRow,
    WorkspaceRow,
)

LIST_LIMIT = 200


def get_workspace(session: Session, workspace_id: str, *, for_update: bool = False) -> WorkspaceRow | None:
    statement = select(WorkspaceRow).where(WorkspaceRow.id == workspace_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def add(session: Session, row: object) -> None:
    session.add(row)
    session.flush()


def get_membership(
    session: Session, workspace_id: str, user_id: str, *, for_update: bool = False
) -> MembershipRow | None:
    statement = select(MembershipRow).where(
        MembershipRow.workspace_id == workspace_id, MembershipRow.user_id == user_id
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def active_workspaces(session: Session, user_id: str) -> list[tuple[WorkspaceRow, MembershipRow]]:
    rows = session.execute(
        select(WorkspaceRow, MembershipRow)
        .join(MembershipRow, MembershipRow.workspace_id == WorkspaceRow.id)
        .where(MembershipRow.user_id == user_id, MembershipRow.active.is_(True))
        .order_by(WorkspaceRow.created_at, WorkspaceRow.id)
        .limit(LIST_LIMIT)
    )
    return [(w, m) for w, m in rows]


def memberships(session: Session, workspace_id: str) -> list[MembershipRow]:
    return list(
        session.scalars(
            select(MembershipRow)
            .where(MembershipRow.workspace_id == workspace_id)
            .order_by(MembershipRow.updated_at, MembershipRow.user_id)
            .limit(LIST_LIMIT)
        )
    )


def count_active_admins(session: Session, workspace_id: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(MembershipRow)
        .where(
            MembershipRow.workspace_id == workspace_id,
            MembershipRow.active.is_(True),
            MembershipRow.role == "admin",
        )
    )


def membership_events(session: Session, workspace_id: str, user_id: str) -> list[MembershipEventRow]:
    return list(
        session.scalars(
            select(MembershipEventRow)
            .where(MembershipEventRow.workspace_id == workspace_id, MembershipEventRow.user_id == user_id)
            .order_by(MembershipEventRow.version)
            .limit(LIST_LIMIT)
        )
    )


def to_version(row: ProfileVersionRow) -> ProfileVersion:
    return ProfileVersion(
        workspace_id=row.workspace_id,
        version=row.version,
        proposal_id=row.proposal_id,
        payload=dict(row.payload),
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
    )


def get_profile_version(session: Session, workspace_id: str, version: int) -> ProfileVersion | None:
    row = session.get(ProfileVersionRow, (workspace_id, version))
    return to_version(row) if row else None


def profile_versions(session: Session, workspace_id: str) -> list[ProfileVersion]:
    rows = session.scalars(
        select(ProfileVersionRow)
        .where(ProfileVersionRow.workspace_id == workspace_id)
        .order_by(ProfileVersionRow.version.desc())
        .limit(LIST_LIMIT)
    )
    return [to_version(row) for row in rows]


def _confirmed_versions(session: Session, proposal_ids: list[str]) -> dict[str, int]:
    if not proposal_ids:
        return {}
    rows = session.execute(
        select(ProfileVersionRow.proposal_id, ProfileVersionRow.version).where(
            ProfileVersionRow.proposal_id.in_(proposal_ids)
        )
    )
    return {proposal_id: version for proposal_id, version in rows}


def _to_proposal(row: ProfileProposalRow, confirmed: dict[str, int]) -> ProfileProposal:
    return ProfileProposal(
        id=row.id,
        workspace_id=row.workspace_id,
        base_version=row.base_version,
        payload=dict(row.payload),
        proposed_by=row.proposed_by,
        created_at=row.created_at,
        confirmed_version=confirmed.get(row.id),
    )


def get_proposal(session: Session, workspace_id: str, proposal_id: str) -> ProfileProposal | None:
    row = session.scalar(
        select(ProfileProposalRow).where(
            ProfileProposalRow.workspace_id == workspace_id, ProfileProposalRow.id == proposal_id
        )
    )
    return _to_proposal(row, _confirmed_versions(session, [row.id])) if row else None


def proposals(session: Session, workspace_id: str) -> list[ProfileProposal]:
    rows = list(
        session.scalars(
            select(ProfileProposalRow)
            .where(ProfileProposalRow.workspace_id == workspace_id)
            .order_by(ProfileProposalRow.created_at.desc(), ProfileProposalRow.id)
            .limit(LIST_LIMIT)
        )
    )
    confirmed = _confirmed_versions(session, [row.id for row in rows])
    return [_to_proposal(row, confirmed) for row in rows]

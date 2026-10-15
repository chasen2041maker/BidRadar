"""workspace 的业务用例：一个函数对应一个业务动作，由它决定什么时候提交事务。

别的模块只能调用这个文件里的公开函数，最常用的两个：
    authorize()     此人能否在这家公司做某个动作。所有模块检查权限都调它
    get_profile()   取某个版本的正式档案（研究任务创建时固定引用一个版本）

阅读顺序：authorize() → propose_profile() → confirm_profile() → change_member()。

几条贯穿全文件的规则：
- 不是本公司有效成员，一律返回"不存在"（404），而不是"没权限"：免得别人靠错误信息猜出某家公司是否存在。
- 是成员但角色不够，返回 403。
- 修改档案和成员的动作，先锁住公司这一行（SELECT ... FOR UPDATE），同一家公司的这类修改按先后顺序执行。
  比如两个管理员同时把对方降级，不加锁的话两边都看到"还有两个管理员"，结果一个管理员都不剩。
"""

from sqlalchemy.orm import Session

from bidradar.infra import clock, idempotency, outbox
from bidradar.infra.errors import AppError, conflict, forbidden, not_found
from bidradar.infra.ids import new_id
from bidradar.modules.identity import service as identity
from bidradar.modules.workspace import repo
from bidradar.modules.workspace.models import (
    TOPIC_ACCESS_CHANGED,
    TOPIC_PROFILE_CONFIRMED,
    Access,
    Action,
    Member,
    MemberEvent,
    ProfileOverview,
    ProfileProposal,
    ProfileVersion,
    Role,
    WorkspaceSummary,
    allows,
    check_member_change,
    normalize_profile,
    parse_role,
)
from bidradar.modules.workspace.tables import (
    MembershipEventRow,
    MembershipRow,
    ProfileProposalRow,
    ProfileVersionRow,
    WorkspaceRow,
)


# ---- 权限 ----


def authorize(session: Session, user_id: str, workspace_id: str, action: Action | str) -> Access:
    """此人能否在这家公司执行 action。能就返回 Access（角色、成员版本、当前档案版本），不能就抛错。

    每个动作之前都要重新调一次，不能缓存：管理员刚把某人移出，他的下一个请求就应该被拒绝。
    只读，不提交事务。
    """
    action = Action(action)
    membership = repo.get_membership(session, workspace_id, user_id) if isinstance(workspace_id, str) else None
    if membership is None or not membership.active:
        raise not_found()
    role = Role(membership.role)
    if not allows(role, action):
        raise forbidden()
    workspace = repo.get_workspace(session, workspace_id)
    return Access(
        user_id=user_id,
        workspace_id=workspace_id,
        role=role,
        membership_version=membership.version,
        current_profile_version=workspace.current_profile_version,
    )


def list_workspaces(session: Session, user_id: str) -> list[WorkspaceSummary]:
    """此人有效加入的所有公司。"""
    return [
        WorkspaceSummary(workspace_id=w.id, name=w.name, role=Role(m.role))
        for w, m in repo.active_workspaces(session, user_id)
    ]


def _lock_workspace(session: Session, workspace_id: str) -> WorkspaceRow:
    row = repo.get_workspace(session, workspace_id, for_update=True)
    if row is None:
        raise not_found()
    return row


# ---- 命令行初始化 ----


def create_workspace(session: Session, name: str, admin_user_id: str) -> str:
    """新建公司，并把指定用户设为第一个管理员。只给命令行初始化用。"""
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 200:
        raise AppError("invalid_workspace_name")
    if not identity.get_users(session, [admin_user_id]):
        raise not_found()
    now = clock.now()
    workspace_id = new_id()
    repo.add(session, WorkspaceRow(id=workspace_id, name=name.strip(), current_profile_version=0, created_at=now))
    _write_membership(session, workspace_id, admin_user_id, Role.ADMIN, True, actor_id=None, previous=None)
    session.commit()
    return workspace_id


def add_member(session: Session, workspace_id: str, user_id: str, role: Role | str) -> None:
    """把用户加入公司。只给命令行初始化用；网页上的成员管理只能改已有成员的角色和状态。"""
    role = parse_role(role)
    _lock_workspace(session, workspace_id)
    if not identity.get_users(session, [user_id]):
        raise not_found()
    if repo.get_membership(session, workspace_id, user_id) is not None:
        raise conflict("member_exists")
    _write_membership(session, workspace_id, user_id, role, True, actor_id=None, previous=None)
    session.commit()


def _write_membership(
    session: Session,
    workspace_id: str,
    user_id: str,
    role: Role,
    active: bool,
    *,
    actor_id: str | None,
    previous: MembershipRow | None,
) -> MembershipRow:
    """新建或修改一条成员关系，同时写审计记录和 AccessChanged 事件。三件事在同一个事务里。"""
    now = clock.now()
    old_role, old_active = (previous.role, previous.active) if previous else (None, None)
    if previous is None:
        row = MembershipRow(
            workspace_id=workspace_id, user_id=user_id, role=role.value, active=active, version=1, updated_at=now
        )
        repo.add(session, row)
    else:
        row = previous
        row.role, row.active, row.version, row.updated_at = role.value, active, previous.version + 1, now
    repo.add(
        session,
        MembershipEventRow(
            workspace_id=workspace_id,
            user_id=user_id,
            version=row.version,
            actor_id=actor_id,
            old_role=old_role,
            old_active=old_active,
            new_role=role.value,
            new_active=active,
            recorded_at=now,
        ),
    )
    outbox.publish(
        session,
        TOPIC_ACCESS_CHANGED,
        {"workspace_id": workspace_id, "user_id": user_id, "membership_version": row.version},
        event_key=f"access:{workspace_id}:{user_id}:{row.version}",
    )
    return row


# ---- 成员管理 ----


def list_members(session: Session, actor_id: str, workspace_id: str) -> list[Member]:
    """本公司全部成员，包括已移出的。三种角色都能看。"""
    authorize(session, actor_id, workspace_id, Action.READ)
    rows = repo.memberships(session, workspace_id)
    users = identity.get_users(session, [row.user_id for row in rows])
    members = [
        Member(
            user_id=row.user_id,
            username=users[row.user_id].username,
            display_name=users[row.user_id].display_name,
            role=Role(row.role),
            active=row.active,
            version=row.version,
        )
        for row in rows
        if row.user_id in users
    ]
    return sorted(members, key=lambda m: m.username)


def member_history(session: Session, actor_id: str, workspace_id: str, target_user_id: str) -> list[MemberEvent]:
    """某个成员的变更历史，只有管理员能看。成员被移出后，历史仍保留。"""
    authorize(session, actor_id, workspace_id, Action.ADMIN)
    if repo.get_membership(session, workspace_id, target_user_id) is None:
        raise not_found()
    return [
        MemberEvent(
            version=row.version,
            actor_id=row.actor_id,
            old_role=row.old_role,
            old_active=row.old_active,
            new_role=row.new_role,
            new_active=row.new_active,
            recorded_at=row.recorded_at,
        )
        for row in repo.membership_events(session, workspace_id, target_user_id)
    ]


def change_member(
    session: Session,
    actor_id: str,
    workspace_id: str,
    target_user_id: str,
    role: Role | str,
    active: bool,
    expected_version: int,
) -> Member:
    """管理员修改成员的角色或启用状态。

    expected_version 是页面上看到的成员版本号，过期就报 membership_version_conflict。
    这个操作本身带版本号，重复提交第二次会因为版本过期被拒绝，所以不需要另外的幂等键。
    """
    role = parse_role(role)
    if type(active) is not bool:
        raise AppError("invalid_membership")
    _lock_workspace(session, workspace_id)
    authorize(session, actor_id, workspace_id, Action.ADMIN)
    target = repo.get_membership(session, workspace_id, target_user_id, for_update=True)
    if target is None:
        raise not_found()
    if target.version != expected_version:
        raise conflict("membership_version_conflict")
    check_member_change(
        current_role=Role(target.role),
        current_active=target.active,
        new_role=role,
        new_active=active,
        active_admins=repo.count_active_admins(session, workspace_id),
    )
    row = _write_membership(session, workspace_id, target_user_id, role, active, actor_id=actor_id, previous=target)
    session.commit()
    user = identity.get_users(session, [target_user_id])[target_user_id]
    return Member(
        user_id=row.user_id,
        username=user.username,
        display_name=user.display_name,
        role=Role(row.role),
        active=row.active,
        version=row.version,
    )


# ---- 企业档案 ----


def get_profile(session: Session, workspace_id: str, version: int) -> ProfileVersion:
    """取某个版本的正式档案。给别的模块用，调用方应该已经用 authorize() 检查过权限。"""
    if type(version) is not int or version < 1:
        raise AppError("invalid_profile_version")
    profile = repo.get_profile_version(session, workspace_id, version)
    if profile is None:
        raise not_found()
    return profile


def current_profile(session: Session, workspace_id: str) -> ProfileVersion | None:
    """当前正式档案；还没有正式档案时返回 None。调用方应该已经检查过权限。"""
    workspace = repo.get_workspace(session, workspace_id)
    if workspace is None or workspace.current_profile_version == 0:
        return None
    return repo.get_profile_version(session, workspace_id, workspace.current_profile_version)


def profile_overview(session: Session, actor_id: str, workspace_id: str) -> ProfileOverview:
    """当前正式档案、全部历史版本、全部提议。三种角色都能看。"""
    authorize(session, actor_id, workspace_id, Action.READ)
    history = repo.profile_versions(session, workspace_id)
    return ProfileOverview(
        current=history[0] if history else None,
        history=history,
        proposals=repo.proposals(session, workspace_id),
    )


def propose_profile(
    session: Session, actor_id: str, workspace_id: str, payload: object, base_version: int, key: str
) -> ProfileProposal:
    """提交一份档案修改提议。base_version 必须是当前正式版本，否则说明别人刚确认过新版本，要刷新后再提。"""
    payload = normalize_profile(payload)
    if type(base_version) is not int or base_version < 0:
        raise AppError("invalid_profile_version")
    workspace = _lock_workspace(session, workspace_id)
    authorize(session, actor_id, workspace_id, Action.WRITE)
    request = {"actor": actor_id, "payload": payload, "base_version": base_version}
    cached = idempotency.replay(session, scope=workspace_id, operation="propose_profile", key=key, request=request)
    if cached is not None:
        return repo.get_proposal(session, workspace_id, cached["proposal_id"])
    if workspace.current_profile_version != base_version:
        raise conflict("profile_version_conflict")
    proposal_id = new_id()
    repo.add(
        session,
        ProfileProposalRow(
            id=proposal_id,
            workspace_id=workspace_id,
            base_version=base_version,
            payload=payload,
            proposed_by=actor_id,
            created_at=clock.now(),
        ),
    )
    idempotency.remember(
        session,
        scope=workspace_id,
        operation="propose_profile",
        key=key,
        request=request,
        response={"proposal_id": proposal_id},
    )
    session.commit()
    return repo.get_proposal(session, workspace_id, proposal_id)


def confirm_profile(
    session: Session, actor_id: str, workspace_id: str, proposal_id: str, expected_version: int, key: str
) -> ProfileVersion:
    """管理员把一份提议确认为新的正式版本，并发出 ProfileConfirmed 事件。

    两个条件都要满足：当前版本还是 expected_version，并且这份提议就是基于这个版本提的。
    后一条防止管理员确认一份基于旧档案写的提议，悄悄覆盖掉中间别人确认过的修改。
    """
    if type(expected_version) is not int or expected_version < 0 or not isinstance(proposal_id, str):
        raise AppError("invalid_request")
    workspace = _lock_workspace(session, workspace_id)
    authorize(session, actor_id, workspace_id, Action.ADMIN)
    request = {"actor": actor_id, "proposal_id": proposal_id, "expected_version": expected_version}
    cached = idempotency.replay(session, scope=workspace_id, operation="confirm_profile", key=key, request=request)
    if cached is not None:
        return repo.get_profile_version(session, workspace_id, cached["version"])
    proposal = repo.get_proposal(session, workspace_id, proposal_id)
    if proposal is None:
        raise not_found()
    if proposal.confirmed_version is not None:
        raise conflict("proposal_already_confirmed")
    if workspace.current_profile_version != expected_version or proposal.base_version != expected_version:
        raise conflict("profile_version_conflict")
    version = expected_version + 1
    repo.add(
        session,
        ProfileVersionRow(
            workspace_id=workspace_id,
            version=version,
            proposal_id=proposal_id,
            payload=proposal.payload,
            confirmed_by=actor_id,
            confirmed_at=clock.now(),
        ),
    )
    workspace.current_profile_version = version
    outbox.publish(
        session,
        TOPIC_PROFILE_CONFIRMED,
        {"workspace_id": workspace_id, "profile_version": version},
        event_key=f"profile:{workspace_id}:{version}",
    )
    idempotency.remember(
        session,
        scope=workspace_id,
        operation="confirm_profile",
        key=key,
        request=request,
        response={"version": version},
    )
    session.commit()
    return repo.get_profile_version(session, workspace_id, version)

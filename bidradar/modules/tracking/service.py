"""tracking 的业务用例：跟进决定、关注、发现变化、站内提醒、自动复核。

两类入口：
    给用户的（api.py 调）：get/set_decision、get/set_watch、list_changes、list_notifications、mark_read
    给事件的（outbox 消费者调）：EVENT_HANDLERS 里的三个处理函数
        catalog.notice_version_added   公告有新版本 → 比较关注的基线 → 有实质变化就记变化、发提醒
        workspace.profile_confirmed     档案确认了新版本 → 记一次"档案变化"
        workspace.access_changed        成员权限变了 → 发起人的自动复核委托作废

事件处理函数**不提交事务**：由 outbox 消费者把"处理结果"和"这个事件已经处理过"放在同一个事务里提交。
这样一个事件要么完整处理并记为已处理，要么什么都没发生，可以安全地重新投递。见 docs/exercises.md 的练习 3。

几条保证：
- 同一个关注、同样的变化只记一次，只提醒一次（changes 和 notifications 上的唯一约束）。
- 事件乱序到达也不会漏：每个关注记着基线快照号，快照 6 已经包含了快照 5 的内容；晚到的 5 直接跳过。
- 没有有效的自动复核委托时，只提醒、不花钱。
"""

from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Any

from sqlalchemy.orm import Session

from bidradar.infra import clock, idempotency
from bidradar.infra.errors import AppError, conflict, not_found
from bidradar.infra.ids import new_id
from bidradar.modules.catalog import service as catalog
from bidradar.modules.research import service as research
from bidradar.modules.tracking import repo
from bidradar.modules.tracking.diff import RULE_VERSION, compare_bundles, fingerprint
from bidradar.modules.tracking.models import (
    NOTIFY_CHANGE,
    NOTIFY_REASSESSMENT,
    Change,
    Decision,
    Notification,
    Reassessment,
    Watch,
    check_reason,
    check_scope,
    delegation_expiry,
    delegation_valid,
    parse_decision,
)
from bidradar.modules.tracking.tables import (
    DecisionHistoryRow,
    DecisionRow,
    ReassessmentRow,
    WatchHistoryRow,
    WatchRow,
)
from bidradar.modules.workspace import service as workspace

PAGE_LIMIT = 100


def _check_notice(notice_id: object) -> str:
    if not isinstance(notice_id, str) or len(notice_id) != 64:
        raise AppError("invalid_notice_id")
    return notice_id


# ---- 决定 ----


def _decision(row: DecisionRow) -> Decision:
    return Decision(
        workspace_id=row.workspace_id,
        notice_id=row.notice_id,
        version=row.version,
        state=row.state,
        reason=row.reason,
        actor_id=row.actor_id,
        observation_id=row.observation_id,
        recorded_at=row.recorded_at,
    )


def get_decision(session: Session, actor_id: str, workspace_id: str, notice_id: str) -> dict[str, Any]:
    """当前决定和全部历史。还没做过决定时 current 为空。"""
    workspace.authorize(session, actor_id, workspace_id, "read")
    _check_notice(notice_id)
    row = repo.get_decision(session, workspace_id, notice_id)
    return {
        "current": _decision(row) if row else None,
        "history": repo.decision_history(session, workspace_id, notice_id),
    }


def set_decision(
    session: Session,
    actor_id: str,
    workspace_id: str,
    notice_id: str,
    state: str,
    reason: str,
    expected_version: int,
    key: str,
) -> Decision:
    """记录一个新决定：待评估、跟进或放弃。决定由人做，系统只记录，并记下当时看到的公告版本。"""
    state, reason = parse_decision(state), check_reason(reason)
    _check_notice(notice_id)
    if type(expected_version) is not int or expected_version < 0:
        raise AppError("invalid_version")
    workspace.authorize(session, actor_id, workspace_id, "track")
    request = {"actor": actor_id, "notice": notice_id, "state": state, "reason": reason, "expected": expected_version}
    cached = idempotency.replay(session, scope=workspace_id, operation="set_decision", key=key, request=request)
    if cached is not None:
        return _decision(repo.get_decision(session, workspace_id, notice_id))
    current = catalog.get_notice(session, notice_id)["current"]
    row = repo.get_decision(session, workspace_id, notice_id, for_update=True)
    if (row.version if row else 0) != expected_version:
        raise conflict("decision_version_conflict")
    now = clock.now()
    values = {
        "version": expected_version + 1,
        "state": state.value,
        "reason": reason,
        "actor_id": actor_id,
        "observation_id": current["observation_id"],
        "recorded_at": now,
    }
    if row is None:
        row = DecisionRow(workspace_id=workspace_id, notice_id=notice_id, **values)
        repo.add(session, row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    repo.add(session, DecisionHistoryRow(workspace_id=workspace_id, notice_id=notice_id, **values))
    idempotency.remember(
        session,
        scope=workspace_id,
        operation="set_decision",
        key=key,
        request=request,
        response={"version": row.version},
    )
    session.commit()
    return _decision(row)


# ---- 关注 ----


def get_watch(session: Session, actor_id: str, workspace_id: str, notice_id: str) -> Watch | None:
    workspace.authorize(session, actor_id, workspace_id, "read")
    row = repo.get_watch(session, workspace_id, _check_notice(notice_id))
    return repo.to_watch(row) if row else None


def set_watch(
    session: Session,
    actor_id: str,
    workspace_id: str,
    notice_id: str,
    *,
    active: bool,
    auto_reassess: bool,
    expected_version: int,
    key: str,
    expires_at: datetime | None = None,
    scope: list[str] | None = None,
) -> Watch:
    """开启、修改或停止关注。

    - 新建或重新开启时，把公告现在的样子存为比较基线；只是改设置时保留原基线，免得跳过还没处理的变化。
    - 开启自动复核需要"分析"权限（会花钱），并且公司已有正式档案。委托最长 7 天。
    - 任何修改都会让还没结束的旧复核失效：规则变了，旧授权就不再适用。
    """
    _check_notice(notice_id)
    if type(active) is not bool or type(auto_reassess) is not bool or (auto_reassess and not active):
        raise AppError("invalid_watch")
    if not auto_reassess and expires_at is not None:
        raise AppError("unexpected_expiry")
    scope = check_scope(scope)
    access = workspace.authorize(session, actor_id, workspace_id, "analyze" if auto_reassess else "track")
    if auto_reassess and access.current_profile_version < 1:
        raise conflict("profile_required")
    now = clock.now()
    expiry = delegation_expiry(expires_at, now) if auto_reassess else None
    request = {
        "actor": actor_id,
        "notice": notice_id,
        "active": active,
        "auto": auto_reassess,
        "expected": expected_version,
        "expires_at": clock.iso(expires_at),
        "scope": scope,
    }
    cached = idempotency.replay(session, scope=workspace_id, operation="set_watch", key=key, request=request)
    if cached is not None:
        return repo.to_watch(repo.get_watch(session, workspace_id, notice_id))
    row = repo.get_watch(session, workspace_id, notice_id, for_update=True)
    if (row.version if row else 0) != expected_version:
        raise conflict("watch_version_conflict")
    if row is None or (active and not row.active):
        bundle = catalog.research_bundle(session, notice_id)
        baseline, snapshot, profile_version = bundle, bundle["snapshot"], access.current_profile_version
    else:
        baseline, snapshot, profile_version = row.baseline, row.baseline_snapshot, row.profile_version
    warnings = (
        ["unstable_content_snapshot_identity"] if baseline["current"].get("identity_kind") == "content_snapshot" else []
    )
    values = {
        "version": expected_version + 1,
        "active": active,
        "auto_reassess": auto_reassess,
        "actor_id": actor_id,
        "membership_version": access.membership_version,
        "expires_at": expiry,
        "scope": scope,
        "warnings": warnings,
        "baseline": baseline,
        "baseline_snapshot": snapshot,
        "profile_version": profile_version,
        "recorded_at": now,
    }
    if row is None:
        row = WatchRow(id=new_id(), workspace_id=workspace_id, notice_id=notice_id, **values)
        repo.add(session, row)
    else:
        for name, value in values.items():
            setattr(row, name, value)
    repo.add(
        session,
        WatchHistoryRow(
            watch_id=row.id,
            version=row.version,
            settings={k: v for k, v in request.items() if k != "expected"} | {"expires_at": clock.iso(expiry)},
            recorded_at=now,
        ),
    )
    _cancel_open_reassessments(session, row.id, "watch_changed")
    idempotency.remember(
        session, scope=workspace_id, operation="set_watch", key=key, request=request, response={"version": row.version}
    )
    session.commit()
    return repo.to_watch(row)


# ---- 变化和提醒 ----


def _page_args(after: int, limit: int) -> None:
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= PAGE_LIMIT:
        raise AppError("invalid_query")


def list_changes(session: Session, actor_id: str, workspace_id: str, after: int = 0, limit: int = 20) -> list[Change]:
    workspace.authorize(session, actor_id, workspace_id, "read")
    _page_args(after, limit)
    return repo.changes_after(session, workspace_id, after, limit)


def list_notifications(
    session: Session, actor_id: str, workspace_id: str, after: int = 0, limit: int = 20
) -> list[Notification]:
    """本公司的提醒，带上当前用户自己的已读时间。"""
    workspace.authorize(session, actor_id, workspace_id, "read")
    _page_args(after, limit)
    return repo.notifications_after(session, workspace_id, actor_id, after, limit)


def mark_read(session: Session, actor_id: str, workspace_id: str, notification_id: str) -> datetime:
    workspace.authorize(session, actor_id, workspace_id, "read")
    if repo.get_notification(session, workspace_id, notification_id) is None:
        raise not_found()
    repo.mark_read(
        session,
        {"notification_id": notification_id, "user_id": actor_id, "workspace_id": workspace_id, "read_at": clock.now()},
    )
    session.commit()
    return repo.read_at(session, notification_id, actor_id)


def list_reassessments(session: Session, actor_id: str, workspace_id: str) -> list[Reassessment]:
    workspace.authorize(session, actor_id, workspace_id, "read")
    return [
        Reassessment(
            id=r.id,
            watch_id=r.watch_id,
            change_id=r.change_id,
            state=r.state,
            reason=r.reason,
            run_id=r.run_id,
            created_at=r.created_at,
            updated_at=r.updated_at,
        )
        for r in repo.reassessments(session, workspace_id)
    ]


# ---- 事件处理（由 outbox 消费者调用，不提交事务） ----


def _record_change(
    session: Session,
    watch: WatchRow,
    *,
    source: str,
    source_key: str,
    diff: dict[str, Any],
    snapshot: int,
    title: str,
    limits: research.Limits,
) -> None:
    """记一次变化、发一条提醒，委托有效时排一次自动复核。同样的变化重复送来，什么都不会多出来。"""
    now = clock.now()
    change_id = repo.add_change_if_new(
        session,
        {
            "id": new_id(),
            "workspace_id": watch.workspace_id,
            "watch_id": watch.id,
            "notice_id": watch.notice_id,
            "source": source,
            "rule_version": RULE_VERSION,
            "fingerprint": fingerprint([source, source_key, diff, watch.profile_version]),
            "diff": diff,
            "catalog_snapshot": snapshot,
            "profile_version": watch.profile_version,
            "created_at": now,
        },
    )
    if change_id is None:
        return
    repo.add_notification_if_new(
        session,
        {
            "id": new_id(),
            "workspace_id": watch.workspace_id,
            "kind": NOTIFY_CHANGE,
            "dedupe_key": f"{NOTIFY_CHANGE}:{change_id}",
            "notice_id": watch.notice_id,
            "watch_id": watch.id,
            "change_id": change_id,
            "payload": {"title": title, "scopes": diff["scopes"], "kind": diff["kind"]},
            "created_at": now,
        },
    )
    _maybe_reassess(session, watch, change_id, diff["scopes"], limits)


def _maybe_reassess(
    session: Session, watch: WatchRow, change_id: str, scopes: list[str], limits: research.Limits
) -> None:
    """委托有效就排一次自动复核；无效只记提醒，不花钱。"""
    if not watch.auto_reassess:
        return
    try:
        access = workspace.authorize(session, watch.actor_id, watch.workspace_id, "analyze")
    except AppError:
        return  # 发起人已经没有分析权限：委托作废，只提醒
    if not delegation_valid(
        repo.to_watch(watch),
        membership_version=access.membership_version,
        current_profile_version=access.current_profile_version,
        scopes=scopes,
        now=clock.now(),
    ):
        return
    # 同一个关注又有更新的变化：旧的、还没结束的复核作废，免得旧输入的结果被当成最新的。
    _cancel_open_reassessments(session, watch.id, "input_superseded")
    now = clock.now()
    row = ReassessmentRow(
        id=new_id(),
        workspace_id=watch.workspace_id,
        watch_id=watch.id,
        change_id=change_id,
        version=1,
        state="pending",
        created_at=now,
        updated_at=now,
    )
    repo.add(session, row)
    # 研究任务和复核记录在同一个事务里建：要么都有，要么都没有。研究那边按委托里的发起人检查权限。
    try:
        row.run_id = research.create_reassessment(
            session,
            watch.actor_id,
            watch.workspace_id,
            watch.notice_id,
            {"watch_id": watch.id, "change_id": change_id, "reassessment_id": row.id},
            limits,
        )
        row.state = "dispatched"
    except AppError as error:
        row.state, row.reason = "rejected", error.code


def _cancel_open_reassessments(session: Session, watch_id: str, reason: str) -> None:
    """取消还没结束的复核，连同它建的研究任务。已经发出去的模型请求收不回来，但之后不会再发新的。"""
    for row in repo.open_reassessments(session, watch_id):
        row.state, row.reason, row.version, row.updated_at = "cancelled", reason, row.version + 1, clock.now()
        if row.run_id:
            research.cancel_run_internal(session, row.run_id, "reassessment_" + reason)


def handle_notice_version_added(
    session: Session, payload: dict[str, Any], limits: research.Limits = research.Limits()
) -> None:
    """公告有了新版本（payload：notice_id、observation_id、seq）。

    对每个基线比这个快照旧的活跃关注，取关注公告在这个快照下的公告包，和基线比较。
    注意新版本可能属于另一条公告：一条更正公告的新版本，会改变被它更正的那条原公告的公告包。
    """
    seq = payload["seq"]
    for watch in repo.active_watches_behind(session, seq):
        try:
            bundle = catalog.research_bundle(session, watch.notice_id, snapshot=seq)
        except AppError:
            continue  # 这个快照里还没有被关注的公告（不应发生）：跳过，不改基线
        diff = compare_bundles(watch.baseline, bundle)
        watch.baseline, watch.baseline_snapshot = bundle, seq
        if diff["kind"] != "no_business_change":
            _record_change(
                session,
                watch,
                source="catalog",
                source_key=payload["observation_id"],
                diff=diff,
                snapshot=seq,
                title=bundle["current"]["title"],
                limits=limits,
            )


def handle_profile_confirmed(
    session: Session, payload: dict[str, Any], limits: research.Limits = research.Limits()
) -> None:
    """公司确认了新版档案（payload：workspace_id、profile_version）。对这家公司的每个关注记一次档案变化。"""
    version = payload["profile_version"]
    for watch in repo.active_watches_in(session, payload["workspace_id"]):
        if version <= watch.profile_version:
            continue
        before = watch.profile_version
        watch.profile_version = version
        diff = {
            "schema_version": 1,
            "rule_version": RULE_VERSION,
            "kind": "profile_change",
            "scopes": ["profile"],
            "entries": [{"scope": "profile", "path": "profile_version", "before": before, "after": version}],
            "unclassified": [],
        }
        _record_change(
            session,
            watch,
            source="workspace",
            source_key=f"profile:{version}",
            diff=diff,
            snapshot=watch.baseline_snapshot,
            title=watch.baseline["current"]["title"],
            limits=limits,
        )


def handle_access_changed(session: Session, payload: dict[str, Any]) -> None:
    """成员权限变了（payload：workspace_id、user_id、membership_version）。

    这个人开的自动复核委托全部作废：之后的变化只提醒，不再自动花钱。被移出后再加回来也一样。
    """
    for watch in repo.active_watches_in(session, payload["workspace_id"]):
        if watch.actor_id == payload["user_id"] and payload["membership_version"] > watch.membership_version:
            _cancel_open_reassessments(session, watch.id, "access_changed")


def handle_run_finished(session: Session, payload: dict[str, Any]) -> None:
    """研究任务结束了（payload：run_id、workspace_id、kind、status）。是自动复核的话，记下结果、发一条提醒。"""
    if payload.get("kind") != "reassessment":
        return
    row = repo.get_reassessment_by_run(session, payload["run_id"])
    if row is None or row.state != "dispatched":
        return
    row.state, row.reason, row.version, row.updated_at = "finished", payload["status"], row.version + 1, clock.now()
    watch = session.get(WatchRow, row.watch_id)
    repo.add_notification_if_new(
        session,
        {
            "id": new_id(),
            "workspace_id": row.workspace_id,
            "kind": NOTIFY_REASSESSMENT,
            "dedupe_key": f"{NOTIFY_REASSESSMENT}:{row.run_id}",
            "notice_id": watch.notice_id,
            "watch_id": watch.id,
            "change_id": row.change_id,
            "payload": {"run_id": row.run_id, "status": payload["status"], "title": watch.baseline["current"]["title"]},
            "created_at": clock.now(),
        },
    )


def event_handlers(limits: research.Limits) -> dict[str, Callable[[Session, dict[str, Any]], None]]:
    """outbox 消费者用的 {topic: 处理函数}。limits 决定自动复核用不用模型（没开模型时用规则基线）。"""
    return {
        catalog.TOPIC_VERSION_ADDED: partial(handle_notice_version_added, limits=limits),
        workspace.TOPIC_PROFILE_CONFIRMED: partial(handle_profile_confirmed, limits=limits),
        workspace.TOPIC_ACCESS_CHANGED: handle_access_changed,
        research.TOPIC_RUN_FINISHED: handle_run_finished,
    }


EVENT_HANDLERS = event_handlers(research.Limits())

"""tracking 的数据库读写，只给本模块的 service 用。函数接收会话参数，只读写，不提交事务。"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.modules.tracking.models import Change, Decision, Notification, Watch
from bidradar.modules.tracking.tables import (
    ChangeRow,
    DecisionHistoryRow,
    DecisionRow,
    NotificationReadRow,
    NotificationRow,
    ReassessmentRow,
    WatchRow,
)

LIST_LIMIT = 200
OPEN_REASSESSMENT = ("pending", "dispatched")


def add(session: Session, row: object) -> None:
    session.add(row)
    session.flush()


# ---- 决定 ----


def get_decision(
    session: Session, workspace_id: str, notice_id: str, *, for_update: bool = False
) -> DecisionRow | None:
    statement = select(DecisionRow).where(DecisionRow.workspace_id == workspace_id, DecisionRow.notice_id == notice_id)
    return session.scalar(statement.with_for_update() if for_update else statement)


def decision_history(session: Session, workspace_id: str, notice_id: str) -> list[Decision]:
    rows = session.scalars(
        select(DecisionHistoryRow)
        .where(DecisionHistoryRow.workspace_id == workspace_id, DecisionHistoryRow.notice_id == notice_id)
        .order_by(DecisionHistoryRow.version.desc())
        .limit(LIST_LIMIT)
    )
    return [
        Decision(
            workspace_id=r.workspace_id,
            notice_id=r.notice_id,
            version=r.version,
            state=r.state,
            reason=r.reason,
            actor_id=r.actor_id,
            observation_id=r.observation_id,
            recorded_at=r.recorded_at,
        )
        for r in rows
    ]


# ---- 关注 ----


def to_watch(row: WatchRow) -> Watch:
    return Watch(
        id=row.id,
        workspace_id=row.workspace_id,
        notice_id=row.notice_id,
        version=row.version,
        active=row.active,
        auto_reassess=row.auto_reassess,
        actor_id=row.actor_id,
        membership_version=row.membership_version,
        expires_at=row.expires_at,
        scope=list(row.scope),
        warnings=list(row.warnings),
        baseline_snapshot=row.baseline_snapshot,
        profile_version=row.profile_version,
        recorded_at=row.recorded_at,
    )


def get_watch(session: Session, workspace_id: str, notice_id: str, *, for_update: bool = False) -> WatchRow | None:
    statement = select(WatchRow).where(WatchRow.workspace_id == workspace_id, WatchRow.notice_id == notice_id)
    return session.scalar(statement.with_for_update() if for_update else statement)


def active_watches_behind(session: Session, snapshot: int) -> list[WatchRow]:
    """基线比这个快照旧的活跃关注。加行锁：同一个关注的两个事件不会同时处理、互相覆盖基线。"""
    return list(
        session.scalars(
            select(WatchRow)
            .where(WatchRow.active.is_(True), WatchRow.baseline_snapshot < snapshot)
            .order_by(WatchRow.id)
            .with_for_update()
        )
    )


def active_watches_in(session: Session, workspace_id: str) -> list[WatchRow]:
    return list(
        session.scalars(
            select(WatchRow)
            .where(WatchRow.workspace_id == workspace_id, WatchRow.active.is_(True))
            .order_by(WatchRow.id)
            .with_for_update()
        )
    )


# ---- 变化和提醒 ----


def add_change_if_new(session: Session, values: dict) -> str | None:
    """记一次变化；同样的变化已经记过就返回 None。"""
    return session.scalar(
        insert(ChangeRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["watch_id", "rule_version", "fingerprint"])
        .returning(ChangeRow.id)
    )


def add_notification_if_new(session: Session, values: dict) -> None:
    session.execute(insert(NotificationRow).values(**values).on_conflict_do_nothing(index_elements=["dedupe_key"]))


def changes_after(session: Session, workspace_id: str, after: int, limit: int) -> list[Change]:
    rows = session.scalars(
        select(ChangeRow)
        .where(ChangeRow.workspace_id == workspace_id, ChangeRow.seq > after)
        .order_by(ChangeRow.seq)
        .limit(limit)
    )
    return [
        Change(
            id=r.id,
            seq=r.seq,
            workspace_id=r.workspace_id,
            watch_id=r.watch_id,
            notice_id=r.notice_id,
            source=r.source,
            diff=r.diff,
            catalog_snapshot=r.catalog_snapshot,
            profile_version=r.profile_version,
            created_at=r.created_at,
        )
        for r in rows
    ]


def notifications_after(
    session: Session, workspace_id: str, user_id: str, after: int, limit: int
) -> list[Notification]:
    rows = session.execute(
        select(NotificationRow, NotificationReadRow.read_at)
        .outerjoin(
            NotificationReadRow,
            (NotificationReadRow.notification_id == NotificationRow.id) & (NotificationReadRow.user_id == user_id),
        )
        .where(NotificationRow.workspace_id == workspace_id, NotificationRow.seq > after)
        .order_by(NotificationRow.seq)
        .limit(limit)
    )
    return [
        Notification(
            id=n.id,
            seq=n.seq,
            workspace_id=n.workspace_id,
            kind=n.kind,
            notice_id=n.notice_id,
            watch_id=n.watch_id,
            change_id=n.change_id,
            payload=n.payload,
            created_at=n.created_at,
            read_at=read_at,
        )
        for n, read_at in rows
    ]


def get_notification(session: Session, workspace_id: str, notification_id: str) -> NotificationRow | None:
    return session.scalar(
        select(NotificationRow).where(
            NotificationRow.workspace_id == workspace_id, NotificationRow.id == notification_id
        )
    )


def mark_read(session: Session, values: dict) -> None:
    session.execute(
        insert(NotificationReadRow)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["notification_id", "user_id"])
    )


def read_at(session: Session, notification_id: str, user_id: str):
    return session.scalar(
        select(NotificationReadRow.read_at).where(
            NotificationReadRow.notification_id == notification_id, NotificationReadRow.user_id == user_id
        )
    )


# ---- 自动复核 ----


def open_reassessments(session: Session, watch_id: str) -> list[ReassessmentRow]:
    return list(
        session.scalars(
            select(ReassessmentRow)
            .where(ReassessmentRow.watch_id == watch_id, ReassessmentRow.state.in_(OPEN_REASSESSMENT))
            .with_for_update()
        )
    )


def reassessments(session: Session, workspace_id: str) -> list[ReassessmentRow]:
    return list(
        session.scalars(
            select(ReassessmentRow)
            .where(ReassessmentRow.workspace_id == workspace_id)
            .order_by(ReassessmentRow.created_at.desc())
            .limit(LIST_LIMIT)
        )
    )


def get_reassessment_by_run(session: Session, run_id: str) -> ReassessmentRow | None:
    return session.scalar(select(ReassessmentRow).where(ReassessmentRow.run_id == run_id).with_for_update())

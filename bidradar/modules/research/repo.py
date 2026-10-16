"""research 的数据库读写，只给本模块的 service 用。函数接收会话参数，只读写，不提交事务。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.modules.research.models import Run, RunStatus
from bidradar.modules.research.tables import LlmCallRow, ReportRow, RunRow

LIST_LIMIT = 100


def to_run(row: RunRow) -> Run:
    return Run(
        id=row.id,
        workspace_id=row.workspace_id,
        actor_id=row.actor_id,
        kind=row.kind,
        mode=row.mode,
        notice_id=row.notice_id,
        parent_run_id=row.parent_run_id,
        question=row.question,
        status=row.status,
        reason=row.reason,
        profile_version=row.profile_version,
        catalog_snapshot=row.catalog_snapshot,
        observation_ids=list(row.observation_ids),
        document_ids=list(row.document_ids),
        config_version=row.config_version,
        estimate_micro=row.estimate_micro,
        version=row.version,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def get(session: Session, workspace_id: str, run_id: str, *, for_update: bool = False) -> RunRow | None:
    statement = select(RunRow).where(RunRow.workspace_id == workspace_id, RunRow.id == run_id)
    return session.scalar(statement.with_for_update() if for_update else statement)


def get_any(session: Session, run_id: str, *, for_update: bool = False) -> RunRow | None:
    """Worker 用：按 ID 取任务，不限公司（任务本身带着公司 ID，之后的每一步都按它检查）。"""
    statement = select(RunRow).where(RunRow.id == run_id)
    return session.scalar(statement.with_for_update() if for_update else statement)


def list_runs(session: Session, workspace_id: str, before: str | None, limit: int) -> list[RunRow]:
    statement = select(RunRow).where(RunRow.workspace_id == workspace_id)
    if before is not None:
        anchor = get(session, workspace_id, before)
        if anchor is not None:
            statement = statement.where(
                (RunRow.created_at < anchor.created_at)
                | ((RunRow.created_at == anchor.created_at) & (RunRow.id < anchor.id))
            )
    return list(
        session.scalars(statement.order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(min(limit, LIST_LIMIT)))
    )


def open_runs(session: Session, workspace_id: str) -> int:
    return session.scalar(
        select(func.count())
        .select_from(RunRow)
        .where(RunRow.workspace_id == workspace_id, RunRow.status.in_((RunStatus.QUEUED, RunStatus.RUNNING)))
    )


def report(session: Session, run_id: str) -> ReportRow | None:
    return session.get(ReportRow, run_id)


def run_cost(session: Session, run_id: str) -> dict[str, int]:
    rows = session.execute(
        select(
            LlmCallRow.state,
            func.count(),
            func.coalesce(func.sum(LlmCallRow.charged), 0),
            func.coalesce(func.sum(LlmCallRow.reserved), 0),
        )
        .where(LlmCallRow.run_id == run_id)
        .group_by(LlmCallRow.state)
    ).all()
    calls = sum(r[1] for r in rows)
    charged = sum(int(r[2]) for r in rows if r[0] == "settled")
    open_reserved = sum(int(r[3]) for r in rows if r[0] in ("pending", "unknown"))
    unknown = sum(r[1] for r in rows if r[0] == "unknown")
    return {"calls": calls, "charged": charged, "reserved_open": open_reserved, "unknown": unknown}


def workspace_cost(session: Session, workspace_id: str) -> dict[str, int]:
    rows = session.execute(
        select(
            LlmCallRow.state,
            func.count(),
            func.coalesce(func.sum(LlmCallRow.charged), 0),
            func.coalesce(func.sum(LlmCallRow.reserved), 0),
        )
        .where(LlmCallRow.workspace_id == workspace_id)
        .group_by(LlmCallRow.state)
    ).all()
    used = sum(int(r[2]) for r in rows if r[0] == "settled") + sum(
        int(r[3]) for r in rows if r[0] in ("pending", "unknown")
    )
    return {"calls": sum(r[1] for r in rows), "used": used, "unknown": sum(r[1] for r in rows if r[0] == "unknown")}

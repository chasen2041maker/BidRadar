"""research 的业务用例：发起研究、执行研究（Worker）、查看报告、追问、取消、费用。

别的模块只能调用这个文件里的公开函数：
    create_reassessment()   tracking 发现变化、委托有效时，建一次自动复核（不提交事务，跟着 tracking 一起提交）
    cancel_run_internal()   tracking 的委托作废时，取消对应的研究任务（不提交事务）

一次研究的一生：
    estimate()          发起前：列出研究范围（公告、文件、片段数）和预计费用。不调模型，不花钱
    create_analysis()   用户确认后：固定输入（档案版本、公告版本、文件），建任务、排后台任务。同一个事务
    handle_run()        Worker 执行：取材料 → 跑流水线 → 存报告。每一步都落库，崩了从断点续跑
    get_run()           看进度和报告，以及报告的输入现在还是不是最新的

"浏览、搜索不调模型，只有用户明确确认后才花钱"（决策 0002、0010）：花钱的只有 handle_run，
而 handle_run 只执行 create_* 建出来的任务。
"""

from sqlalchemy.orm import Session

from bidradar.agent.followup import FollowupAgent
from bidradar.agent.llm import (
    BudgetedLLM,
    BudgetExceeded,
    CallFailed,
    CallOutcomeUnknown,
    DisabledLLM,
    LLMClient,
    LLMRequest,
    ModelDisabled,
    Pricing,
)
from bidradar.agent.pipeline import PipelineConfig, PipelineStopped, ResearchInputs, ResearchPipeline, versions
from bidradar.agent.schemas import Chunk, ProfileSnapshot, Report
from bidradar.infra import clock, idempotency, jobs, outbox
from bidradar.infra.errors import AppError, conflict, not_found
from bidradar.infra.ids import fingerprint, new_id
from bidradar.modules.catalog import service as catalog
from bidradar.modules.documents import service as documents
from bidradar.modules.research import repo
from bidradar.modules.research.models import (
    JOB_RUN,
    MAX_DOCUMENTS,
    MAX_OPEN_RUNS,
    MAX_QUESTION,
    TERMINAL,
    TOPIC_RUN_FINISHED,
    BudgetSummary,
    Estimate,
    Limits,
    Run,
    RunDetail,
    RunStatus,
    estimate,
    notice_chunks,
)
from bidradar.modules.research.runtime import ChunkToolBox, DbExtractionCache, DbLedger, DbStepStore, RunGuard
from bidradar.modules.research.tables import ReportRow, RunRow
from bidradar.modules.workspace import service as workspace

PROFILE_FIELDS = (
    "company_name",
    "city",
    "project_types",
    "capabilities",
    "delivery_constraints",
    "cases",
    "qualifications",
    "staffing",
    "commercial_constraints",
)


# ---- 材料 ----


def _check_documents(document_ids: object) -> list[str]:
    if not isinstance(document_ids, list) or len(document_ids) > MAX_DOCUMENTS:
        raise AppError("invalid_documents")
    if any(not isinstance(d, str) or len(d) != 32 for d in document_ids) or len(set(document_ids)) != len(document_ids):
        raise AppError("invalid_documents")
    return sorted(document_ids)


def _material(session: Session, workspace_id: str, observation_ids: list[str], document_ids: list[str]) -> list[Chunk]:
    """研究材料 = 公告（及有依据关联的更正、结果公告）的原文证据 + 上传的招标文件切片。"""
    chunks: list[Chunk] = []
    for observation in catalog.get_observations(session, observation_ids):
        chunks += notice_chunks(observation)
    for chunk in documents.get_chunks(session, workspace_id, document_ids):
        chunks.append(
            Chunk(
                id=chunk.id,
                document_id=chunk.document_id,
                seq=chunk.seq,
                page=chunk.page,
                section_path=chunk.section_path,
                kind=chunk.kind,
                text=chunk.text,
            )
        )
    return chunks


def _profile_snapshot(session: Session, workspace_id: str, version: int) -> ProfileSnapshot:
    profile = workspace.get_profile(session, workspace_id, version)
    if set(profile.payload) != set(PROFILE_FIELDS):
        # 档案字段和流水线的维度表对不上：说明有一边改了字段没同步，宁可停下也不能静默地少看字段。
        raise AppError("profile_fields_mismatch", 500)
    return ProfileSnapshot(version=profile.version, fields=dict(profile.payload), proof_status=profile.proof_status)


def _typical_call_cost(pricing: Pricing) -> int:
    """一次典型调用（约 6000 字输入、2000 token 输出）的预留额，用来粗估整次研究的费用上限。"""
    sample = LLMRequest(purpose="extract", call_key="estimate", system="", user="字" * 6000, max_output_tokens=2048)
    return pricing.estimate(sample)


# ---- 发起 ----


def _frozen_inputs(session: Session, workspace_id: str, notice_id: str, document_ids: list[str]):
    bundle = catalog.research_bundle(session, notice_id)
    observation_ids = [item["observation_id"] for item in bundle["observations"]]
    chunks = _material(session, workspace_id, observation_ids, document_ids)
    return bundle, observation_ids, chunks


def estimate_run(
    session: Session,
    actor_id: str,
    workspace_id: str,
    notice_id: str,
    document_ids: list[str],
    limits: Limits,
    pricing: Pricing,
) -> Estimate:
    """发起前给用户看的范围和费用上限。不调模型。"""
    workspace.authorize(session, actor_id, workspace_id, "analyze")
    document_ids = _check_documents(document_ids)
    _, _, chunks = _frozen_inputs(session, workspace_id, notice_id, document_ids)
    return estimate(
        limits.mode, chunks, documents=len(document_ids), call_cost=_typical_call_cost(pricing), run_cap=limits.run_cap
    )


def _new_run(
    session: Session,
    *,
    access,
    kind: str,
    mode: str,
    notice_id: str,
    document_ids: list[str],
    observation_ids: list[str],
    snapshot: int,
    profile_version: int,
    estimate_micro: int,
    parent_run_id: str | None = None,
    question: str | None = None,
    delegation: dict | None = None,
) -> RunRow:
    config_versions = versions(PipelineConfig(mode=mode))
    row = RunRow(
        id=new_id(),
        workspace_id=access.workspace_id,
        actor_id=access.user_id,
        membership_version=access.membership_version,
        kind=kind,
        mode=mode,
        notice_id=notice_id,
        parent_run_id=parent_run_id,
        question=question,
        status=RunStatus.QUEUED,
        profile_version=profile_version,
        catalog_snapshot=snapshot,
        observation_ids=observation_ids,
        document_ids=document_ids,
        config_version=config_versions["config_version"],
        versions=config_versions,
        estimate_micro=estimate_micro,
        delegation=delegation,
        version=1,
        created_at=clock.now(),
    )
    session.add(row)
    session.flush()
    jobs.enqueue(session, JOB_RUN, {"run_id": row.id}, dedupe_key=f"{JOB_RUN}:{row.id}", max_attempts=3)
    return row


def create_analysis(
    session: Session,
    actor_id: str,
    workspace_id: str,
    notice_id: str,
    document_ids: list[str],
    key: str,
    limits: Limits,
    pricing: Pricing,
) -> Run:
    """用户确认范围和费用后，建一个研究任务。输入在这一刻固定：档案当前版本、公告当前版本、所选文件。"""
    access = workspace.authorize(session, actor_id, workspace_id, "analyze")
    if access.current_profile_version < 1:
        raise conflict("profile_required")
    document_ids = _check_documents(document_ids)
    request = {"actor": actor_id, "notice": notice_id, "documents": document_ids}
    cached = idempotency.replay(session, scope=workspace_id, operation="create_analysis", key=key, request=request)
    if cached is not None:
        return repo.to_run(repo.get(session, workspace_id, cached["run_id"]))
    if repo.open_runs(session, workspace_id) >= MAX_OPEN_RUNS:
        raise AppError("workspace_queue_limit", 429)
    bundle, observation_ids, chunks = _frozen_inputs(session, workspace_id, notice_id, document_ids)
    planned = estimate(
        limits.mode, chunks, documents=len(document_ids), call_cost=_typical_call_cost(pricing), run_cap=limits.run_cap
    )
    row = _new_run(
        session,
        access=access,
        kind="analysis",
        mode=limits.mode,
        notice_id=notice_id,
        document_ids=document_ids,
        observation_ids=observation_ids,
        snapshot=bundle["snapshot"],
        profile_version=access.current_profile_version,
        estimate_micro=planned.cost_micro,
    )
    idempotency.remember(
        session, scope=workspace_id, operation="create_analysis", key=key, request=request, response={"run_id": row.id}
    )
    session.commit()
    return repo.to_run(row)


def create_question(
    session: Session,
    actor_id: str,
    workspace_id: str,
    parent_run_id: str,
    question: str,
    key: str,
    limits: Limits,
) -> Run:
    """围绕一份已完成的报告追问。沿用父报告的公告、文件和档案版本；追问必须用模型，没开模型时不能追问。"""
    access = workspace.authorize(session, actor_id, workspace_id, "analyze")
    if not limits.model_enabled:
        raise conflict("model_disabled")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise AppError("invalid_question")
    parent = repo.get(session, workspace_id, parent_run_id)
    if parent is None:
        raise not_found()
    report = repo.report(session, parent.id)
    if parent.status not in (RunStatus.SUCCEEDED, RunStatus.PARTIAL) or report is None or report.kind != "report":
        raise conflict("parent_report_unavailable")
    request = {"actor": actor_id, "parent": parent_run_id, "question": question.strip()}
    cached = idempotency.replay(session, scope=workspace_id, operation="create_question", key=key, request=request)
    if cached is not None:
        return repo.to_run(repo.get(session, workspace_id, cached["run_id"]))
    if repo.open_runs(session, workspace_id) >= MAX_OPEN_RUNS:
        raise AppError("workspace_queue_limit", 429)
    row = _new_run(
        session,
        access=access,
        kind="question",
        mode="agent",
        notice_id=parent.notice_id,
        document_ids=list(parent.document_ids),
        observation_ids=list(parent.observation_ids),
        snapshot=parent.catalog_snapshot,
        profile_version=parent.profile_version,
        estimate_micro=0,
        parent_run_id=parent.id,
        question=question.strip(),
    )
    idempotency.remember(
        session, scope=workspace_id, operation="create_question", key=key, request=request, response={"run_id": row.id}
    )
    session.commit()
    return repo.to_run(row)


def create_reassessment(
    session: Session, actor_id: str, workspace_id: str, notice_id: str, delegation: dict, limits: Limits
) -> str:
    """tracking 用：自动复核建一次研究。材料是这条公告关联的、本公司已解析完的全部文件。不提交事务。"""
    access = workspace.authorize(session, actor_id, workspace_id, "analyze")
    document_ids = sorted(
        d.id
        for d in documents.list_documents(session, actor_id, workspace_id, notice_id)
        if d.status == "parsed" and d.workspace_id == workspace_id
    )[:MAX_DOCUMENTS]
    bundle, observation_ids, _ = _frozen_inputs(session, workspace_id, notice_id, document_ids)
    row = _new_run(
        session,
        access=access,
        kind="reassessment",
        mode=limits.mode,
        notice_id=notice_id,
        document_ids=document_ids,
        observation_ids=observation_ids,
        snapshot=bundle["snapshot"],
        profile_version=access.current_profile_version,
        estimate_micro=0,
        delegation=delegation,
    )
    return row.id


# ---- 取消 ----


def _transition(row: RunRow, status: RunStatus, reason: str | None) -> None:
    row.status, row.reason, row.version = status.value, reason, row.version + 1
    if status in TERMINAL:
        row.finished_at = clock.now()


def cancel_run(session: Session, actor_id: str, workspace_id: str, run_id: str, key: str) -> Run:
    """取消一个还没结束的任务。已经发出去的模型请求收不回来，但之后不会再发新的。"""
    workspace.authorize(session, actor_id, workspace_id, "analyze")
    request = {"actor": actor_id, "run": run_id}
    cached = idempotency.replay(session, scope=workspace_id, operation="cancel_run", key=key, request=request)
    row = repo.get(session, workspace_id, run_id, for_update=True)
    if row is None:
        raise not_found()
    if cached is None:
        if row.status in TERMINAL:
            raise conflict("run_already_finished")
        _transition(row, RunStatus.CANCELLED, "cancelled_by_user")
        jobs.cancel(session, f"{JOB_RUN}:{row.id}")
        idempotency.remember(session, scope=workspace_id, operation="cancel_run", key=key, request=request, response={})
        session.commit()
    return repo.to_run(row)


def cancel_run_internal(session: Session, run_id: str, reason: str) -> None:
    """tracking 用：委托作废时取消对应的复核任务。不提交事务。"""
    row = repo.get_any(session, run_id, for_update=True)
    if row is not None and row.status not in TERMINAL:
        _transition(row, RunStatus.CANCELLED, reason)
        jobs.cancel(session, f"{JOB_RUN}:{row.id}")


# ---- 查看 ----


def _freshness(session: Session, row: RunRow) -> dict[str, str]:
    result = {}
    try:
        current = workspace.authorize(session, row.actor_id, row.workspace_id, "read").current_profile_version
        result["profile"] = "current" if current == row.profile_version else "changed"
    except AppError:
        result["profile"] = "unavailable"
    try:
        bundle = catalog.research_bundle(session, row.notice_id)
        ids = [item["observation_id"] for item in bundle["observations"]]
        result["catalog"] = "current" if ids == list(row.observation_ids) else "changed"
    except AppError:
        result["catalog"] = "unavailable"
    return result


def get_run(session: Session, actor_id: str, workspace_id: str, run_id: str) -> RunDetail:
    workspace.authorize(session, actor_id, workspace_id, "read")
    row = repo.get(session, workspace_id, run_id) if isinstance(run_id, str) else None
    if row is None:
        raise not_found()
    report = repo.report(session, row.id)
    try:
        current = catalog.get_notice(session, row.notice_id)["current"]
        title, simulation = current["title"], current["simulation"]
    except AppError:
        title, simulation = row.notice_id, True
    return RunDetail(
        run=repo.to_run(row),
        title=title,
        simulation=simulation,
        report=report.payload if report else None,
        report_kind=report.kind if report else None,
        freshness=_freshness(session, row),
        cost=repo.run_cost(session, row.id),
    )


def list_runs(
    session: Session, actor_id: str, workspace_id: str, before: str | None = None, limit: int = 30
) -> list[Run]:
    workspace.authorize(session, actor_id, workspace_id, "read")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AppError("invalid_query")
    return [repo.to_run(row) for row in repo.list_runs(session, workspace_id, before, limit)]


def budget_summary(
    session: Session, actor_id: str, workspace_id: str, limits: Limits, pricing: Pricing
) -> BudgetSummary:
    workspace.authorize(session, actor_id, workspace_id, "read")
    cost = repo.workspace_cost(session, workspace_id)
    return BudgetSummary(
        total_cap=limits.total_cap,
        workspace_cap=limits.workspace_cap,
        run_cap=limits.run_cap,
        workspace_used=cost["used"],
        workspace_calls=cost["calls"],
        workspace_unknown=cost["unknown"],
        price_version=pricing.version,
        model_enabled=limits.model_enabled,
    )


# ---- 执行（Worker） ----


def _extraction_key(chunks: list[Chunk], mode: str) -> str:
    config = versions(PipelineConfig(mode=mode))
    parts = {
        k: v for k, v in config.items() if k in ("pipeline", "dimensions", "prompt.extract", "prompt.extract_recall")
    }
    return fingerprint([sorted(c.id for c in chunks), parts, mode])


def _finish(
    session: Session, row: RunRow, job: jobs.Job, status: RunStatus, reason: str | None, report: tuple[str, dict] | None
) -> None:
    session.rollback()
    jobs.hold(session, job)
    row = repo.get_any(session, row.id, for_update=True)
    if row.status == RunStatus.CANCELLED:
        jobs.complete(session, job)
        session.commit()
        return
    _transition(row, status, reason)
    if report is not None:
        session.add(ReportRow(run_id=row.id, kind=report[0], payload=report[1], created_at=clock.now()))
    outbox.publish(
        session,
        TOPIC_RUN_FINISHED,
        {"run_id": row.id, "workspace_id": row.workspace_id, "kind": row.kind, "status": row.status},
        event_key=f"run_finished:{row.id}",
    )
    jobs.complete(session, job)
    session.commit()


def handle_run(session: Session, job: jobs.Job, llm: LLMClient, limits: Limits, pricing: Pricing) -> None:
    """Worker 执行一个研究任务。各种"停下来"的情况都在这里转成任务状态，只有可重试的错误往外抛。"""
    row = repo.get_any(session, job.payload["run_id"], for_update=True)
    if row is None or row.status in TERMINAL:
        jobs.complete(session, job)
        session.commit()
        return
    jobs.hold(session, job)
    if row.status != RunStatus.RUNNING:
        _transition(row, RunStatus.RUNNING, None)
        row.started_at = row.started_at or clock.now()
    session.commit()

    guard = RunGuard(session, row.id, job)
    steps = DbStepStore(session, row.id, job)
    client: LLMClient = (
        BudgetedLLM(llm, DbLedger(session, row, limits, pricing), pricing) if limits.model_enabled else DisabledLLM()
    )
    try:
        guard.check("start")
        profile = _profile_snapshot(session, row.workspace_id, row.profile_version)
        chunks = _material(session, row.workspace_id, list(row.observation_ids), list(row.document_ids))
        if row.kind == "question":
            parent = repo.report(session, row.parent_run_id)
            agent = FollowupAgent(
                client,
                steps,
                guard,
                report=Report.model_validate(parent.payload),
                chunks=chunks,
                profile=profile,
                tools=ChunkToolBox(chunks),
            )
            answer = agent.answer(row.question, row.id)
            _finish(session, row, job, RunStatus.SUCCEEDED, None, ("answer", answer.model_dump(mode="json")))
            return
        cache = DbExtractionCache(session, row.workspace_id if row.document_ids else None, row.workspace_id)
        pipeline = ResearchPipeline(client, steps, guard, PipelineConfig(mode=row.mode), cache=cache)
        report = pipeline.run(
            ResearchInputs(
                run_id=row.id, chunks=chunks, profile=profile, extraction_key=_extraction_key(chunks, row.mode)
            )
        )
        status = RunStatus.PARTIAL if report.coverage.extraction_issues or not chunks else RunStatus.SUCCEEDED
        _finish(session, row, job, status, None, ("report", report.model_dump(mode="json")))
    except PipelineStopped as stop:
        if stop.code == "lease_lost":
            raise jobs.LeaseLost(job.id) from None
        _finish(session, row, job, RunStatus.CANCELLED, stop.code, None)
    except CallOutcomeUnknown:
        _finish(session, row, job, RunStatus.WAITING_INPUT, "billing_unknown", None)
    except BudgetExceeded:
        _finish(session, row, job, RunStatus.WAITING_INPUT, "budget_exceeded", None)
    except ModelDisabled:
        _finish(session, row, job, RunStatus.FAILED, "model_disabled", None)
    except CallFailed:
        if job.attempts >= job.max_attempts:
            _finish(session, row, job, RunStatus.FAILED, "model_call_failed", None)
            return
        raise  # Worker 过一会儿重试；已完成的步骤会被复用
    except AppError as error:
        _finish(session, row, job, RunStatus.FAILED, error.code, None)

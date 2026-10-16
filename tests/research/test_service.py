"""研究模块：预估、发起、Worker 执行（基线和模型两种模式）、额度与不确定计费、取消、守卫、追问、隔离。

模型一律用 FakeLLM，不调真实模型（AGENTS.md：调真实模型会花钱）。
"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidradar.agent.llm import CallFailed, CallOutcomeUnknown, FakeLLM, Pricing
from bidradar.agent.schemas import Status
from bidradar.infra import jobs
from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError
from bidradar.infra.tables import OutboxEvent
from bidradar.modules.research import service
from bidradar.modules.research.models import JOB_RUN, MAX_OPEN_RUNS, Limits
from bidradar.modules.research.tables import LlmCallRow, RunStepRow
from bidradar.modules.workspace import service as workspace
from tests.agent.support import responder
from tests.factories import PROFILE, confirm_profile, import_notice, make_company, upload_and_parse

BASELINE = Limits()
AGENT = Limits(model_enabled=True)
PRICING = Pricing()


@dataclass
class World:
    wid: str
    users: dict
    notice_id: str
    document_id: str


@pytest.fixture
def world(db_session: Session, tmp_path) -> World:
    wid, users = make_company(db_session)
    confirm_profile(db_session, wid, users["admin"])
    notice = import_notice(db_session)
    blobs = BlobStore(tmp_path / "blobs")
    document_id = upload_and_parse(db_session, blobs, users["member"], wid, notice_id=notice["notice_id"])
    return World(wid, users, notice["notice_id"], document_id)


def start(session: Session, w: World, limits: Limits = BASELINE, key: str = "k1", actor: str = "member"):
    return service.create_analysis(
        session, w.users[actor].id, w.wid, w.notice_id, [w.document_id], key, limits, PRICING
    )


def work(session: Session, llm=None, limits: Limits = BASELINE) -> jobs.Job | None:
    """像 Worker 一样领一个研究任务并执行。"""
    job = jobs.claim(session, [JOB_RUN], "test-worker")
    if job is not None:
        service.handle_run(session, job, llm or FakeLLM(responder()), limits, PRICING)
    return job


def detail(session: Session, w: World, run_id: str):
    return service.get_run(session, w.users["viewer"].id, w.wid, run_id)


# ---- 预估和发起 ----


def test_estimate_lists_scope_and_costs_nothing_in_baseline(db_session: Session, world: World) -> None:
    args = (db_session, world.users["member"].id, world.wid, world.notice_id, [world.document_id])
    baseline = service.estimate_run(*args, BASELINE, PRICING)
    assert (baseline.mode, baseline.documents, baseline.model_calls, baseline.cost_micro) == ("baseline", 1, 0, 0)
    assert baseline.chunks > 10
    agent = service.estimate_run(*args, AGENT, PRICING)
    assert agent.model_calls > 0 and 0 < agent.cost_micro
    assert db_session.scalars(select(LlmCallRow)).all() == []  # 预估不调模型


def test_create_needs_analyze_right_and_a_confirmed_profile(db_session: Session, world: World) -> None:
    with pytest.raises(AppError, match="forbidden"):
        start(db_session, world, actor="viewer")
    wid, users = make_company(db_session, "b")
    with pytest.raises(AppError, match="profile_required"):
        service.create_analysis(db_session, users["admin"].id, wid, world.notice_id, [], "k", BASELINE, PRICING)


def test_create_freezes_inputs_and_is_idempotent(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    assert (run.status, run.mode, run.profile_version, run.document_ids) == (
        "queued",
        "baseline",
        1,
        [world.document_id],
    )
    assert run.observation_ids and run.config_version
    assert start(db_session, world).id == run.id
    with pytest.raises(AppError, match="idempotency"):
        service.create_analysis(
            db_session, world.users["member"].id, world.wid, world.notice_id, [], "k1", BASELINE, PRICING
        )


def test_other_companies_documents_cannot_be_used(db_session: Session, world: World, tmp_path) -> None:
    wid, users = make_company(db_session, "b")
    confirm_profile(db_session, wid, users["admin"])
    with pytest.raises(AppError) as error:
        service.create_analysis(
            db_session, users["member"].id, wid, world.notice_id, [world.document_id], "k", BASELINE, PRICING
        )
    assert error.value.status == 404


def test_open_runs_are_limited_per_company(db_session: Session, world: World) -> None:
    for i in range(MAX_OPEN_RUNS):
        start(db_session, world, key=f"k{i}")
    with pytest.raises(AppError, match="workspace_queue_limit"):
        start(db_session, world, key="one-more")


# ---- 执行 ----


def test_baseline_run_produces_a_report_without_model_calls(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    llm = FakeLLM(responder())
    work(db_session, llm)
    result = detail(db_session, world, run.id)
    assert result.run.status in ("succeeded", "partial") and result.report_kind == "report"
    report = result.report
    assert report["mode"] == "baseline" and llm.calls == []
    assert report["findings"] and {f["status"] for f in report["findings"]} == {Status.UNKNOWN.value}
    assert {f["label"] for f in report["facts"]} >= {"预算金额", "最高限价"}
    assert result.freshness == {"profile": "current", "catalog": "current"}
    assert result.cost["calls"] == 0
    [event] = db_session.scalars(select(OutboxEvent).where(OutboxEvent.topic == "research.run_finished"))
    assert event.payload == {
        "run_id": run.id,
        "workspace_id": world.wid,
        "kind": "analysis",
        "status": result.run.status,
    }
    assert jobs.claim(db_session, [JOB_RUN], "w") is None


def test_agent_run_records_every_call_in_the_ledger(db_session: Session, world: World) -> None:
    run = start(db_session, world, AGENT)
    llm = FakeLLM(responder())
    work(db_session, llm, AGENT)
    result = detail(db_session, world, run.id)
    assert result.report["mode"] == "agent"
    calls = db_session.scalars(select(LlmCallRow).where(LlmCallRow.run_id == run.id)).all()
    assert len(calls) == len(llm.calls) > 0 and {c.state for c in calls} == {"settled"}
    assert result.cost["charged"] == sum(c.charged for c in calls) > 0
    budget = service.budget_summary(db_session, world.users["viewer"].id, world.wid, AGENT, PRICING)
    assert (budget.workspace_used, budget.workspace_calls, budget.model_enabled) == (
        result.cost["charged"],
        len(calls),
        True,
    )


def test_retry_after_a_failed_call_reuses_finished_steps(db_session: Session, world: World) -> None:
    start(db_session, world, AGENT)
    count = {"n": 0}
    good = responder()

    def flaky(request):
        count["n"] += 1
        if count["n"] == 2:
            raise CallFailed("model_http_500")
        return good(request)

    job = jobs.claim(db_session, [JOB_RUN], "test-worker")
    with pytest.raises(CallFailed):
        service.handle_run(db_session, job, FakeLLM(flaky), AGENT, PRICING)
    assert db_session.scalars(select(RunStepRow)).all()  # 第一批已经落库
    retry = FakeLLM(responder())
    service.handle_run(db_session, job, retry, AGENT, PRICING)
    states = db_session.scalars(select(LlmCallRow.state)).all()
    # 失败的那次释放了预留、重试时重新预留；第一次已经付过钱的那次调用不再发。
    assert set(states) == {"settled"} and len(retry.calls) == len(states) - 1


def test_budget_exceeded_stops_before_sending(db_session: Session, world: World) -> None:
    tight = Limits(model_enabled=True, run_cap=1)
    run = start(db_session, world, tight)
    llm = FakeLLM(responder())
    work(db_session, llm, tight)
    result = detail(db_session, world, run.id)
    assert (result.run.status, result.run.reason) == ("waiting_input", "budget_exceeded")
    assert llm.calls == [] and result.report is None


def test_unknown_billing_waits_for_a_person_and_counts_as_spent(db_session: Session, world: World) -> None:
    run = start(db_session, world, AGENT)

    def timeout(request):
        raise CallOutcomeUnknown()

    work(db_session, FakeLLM(timeout), AGENT)
    result = detail(db_session, world, run.id)
    assert (result.run.status, result.run.reason) == ("waiting_input", "billing_unknown")
    assert result.cost["unknown"] == 1 and result.cost["reserved_open"] > 0
    budget = service.budget_summary(db_session, world.users["viewer"].id, world.wid, AGENT, PRICING)
    assert budget.workspace_unknown == 1 and budget.workspace_used == result.cost["reserved_open"]


def test_cancelled_run_is_not_executed(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    cancelled = service.cancel_run(db_session, world.users["member"].id, world.wid, run.id, "c1")
    assert (cancelled.status, cancelled.reason) == ("cancelled", "cancelled_by_user")
    assert service.cancel_run(db_session, world.users["member"].id, world.wid, run.id, "c1").status == "cancelled"
    assert jobs.claim(db_session, [JOB_RUN], "w") is None
    with pytest.raises(AppError, match="run_already_finished"):
        service.cancel_run(db_session, world.users["member"].id, world.wid, run.id, "c2")


def test_new_profile_version_stops_the_run(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    confirm_profile(db_session, world.wid, world.users["admin"], {**PROFILE, "staffing": "6名软件工程师"})
    work(db_session)
    result = detail(db_session, world, run.id)
    assert (result.run.status, result.run.reason, result.report) == ("cancelled", "profile_version_changed", None)


def test_revoked_access_stops_the_run(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    workspace.change_member(db_session, world.users["admin"].id, world.wid, world.users["member"].id, "viewer", True, 1)
    work(db_session)
    assert detail(db_session, world, run.id).run.reason == "access_revoked"


def test_report_freshness_shows_a_newer_profile(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    work(db_session)
    confirm_profile(db_session, world.wid, world.users["admin"], {**PROFILE, "staffing": "6名软件工程师"})
    result = detail(db_session, world, run.id)
    assert result.run.status in ("succeeded", "partial") and result.freshness["profile"] == "changed"


def test_runs_are_private_to_the_company(db_session: Session, world: World) -> None:
    run = start(db_session, world)
    wid, users = make_company(db_session, "b")
    with pytest.raises(AppError, match="not_found"):  # 不是成员：连这家公司存不存在都不透露
        service.get_run(db_session, users["admin"].id, world.wid, run.id)
    with pytest.raises(AppError, match="not_found"):
        service.get_run(db_session, users["admin"].id, wid, run.id)
    assert service.list_runs(db_session, users["admin"].id, wid) == []


# ---- 追问 ----


def finished_report(session: Session, w: World, limits: Limits = AGENT) -> str:
    run = start(session, w, limits)
    work(session, FakeLLM(responder()), limits)
    return run.id


def test_questions_need_the_model(db_session: Session, world: World) -> None:
    parent = finished_report(db_session, world, BASELINE)
    with pytest.raises(AppError, match="model_disabled"):
        service.create_question(db_session, world.users["member"].id, world.wid, parent, "驻场几天？", "q", BASELINE)


def test_question_run_answers_with_citations(db_session: Session, world: World) -> None:
    parent = finished_report(db_session, world)

    def respond(request):
        import json

        if request.purpose == "verify":
            return {"verdict": "supported"}
        history = json.loads(request.user)["history"]
        if not history:
            return {"action": "tool", "tool": "search_chunks", "arguments": {"query": "驻场"}}
        if len(history) == 1:
            return {"action": "tool", "tool": "read_chunk", "arguments": {"refs": [history[0]["result"][0]["ref"]]}}
        ref = history[1]["result"][0]["ref"]
        return {"action": "answer", "answer": "要求不少于2名工程师驻场。", "citations": [ref]}

    question = service.create_question(
        db_session, world.users["member"].id, world.wid, parent, "驻场要求是什么？", "q", AGENT
    )
    assert (question.kind, question.parent_run_id, question.mode) == ("question", parent, "agent")
    work(db_session, FakeLLM(respond), AGENT)
    result = detail(db_session, world, question.id)
    assert result.report_kind == "answer" and result.report["verified"] is True
    assert result.report["citations"] and result.report["answer"].startswith("要求不少于2名")
    # 回答引用的片段原文跟着回答一起存下，页面不用再去找。
    assert [c["id"] for c in result.report["chunks"]] == result.report["citations"]
    assert "驻场" in result.report["chunks"][0]["text"]

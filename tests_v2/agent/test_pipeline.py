"""流水线：端到端、续跑不重复调用、重做和降级、守卫、抽取缓存、基线模式。"""

import pytest

from bidradar.agent.llm import DisabledLLM, FakeLLM, LLMRequest
from bidradar.agent.pipeline import PipelineConfig, PipelineStopped, ResearchInputs, ResearchPipeline
from bidradar.agent.schemas import Finding, Kind, Status
from bidradar.agent.steps import match as match_step
from tests_v2.agent.support import MemorySteps, NullGuard, profile, responder, tender_chunks


def inputs(run_id: str = "run-1") -> ResearchInputs:
    return ResearchInputs(run_id=run_id, chunks=list(tender_chunks()), profile=profile())


def test_end_to_end_with_placeholder_match() -> None:
    llm = FakeLLM(responder())
    report = ResearchPipeline(llm, MemorySteps(), NullGuard()).run(inputs())
    obligations = [r for r in report.requirements if r.kind is Kind.OBLIGATION]
    assert len(report.findings) == len(obligations) >= 4
    # 占位匹配：全部未知，绝不会出现错误的满足或不满足。
    assert {f.status for f in report.findings} == {Status.UNKNOWN}
    assert {f.label for f in report.facts} >= {"预算金额", "最高限价", "截止时间"}
    assert report.checklist and report.summary.source == "template"
    assert report.coverage.skipped_sections == (("第六章 投标文件格式",),)
    cited = {c.id for c in report.chunks}
    assert all(cid in cited for r in report.requirements for cid in r.chunk_ids)
    assert report.versions["config_version"]


def test_resume_reuses_completed_steps_without_calling_the_model_again() -> None:
    steps = MemorySteps()
    first = FakeLLM(responder())
    ResearchPipeline(first, steps, NullGuard()).run(inputs())
    second = FakeLLM(responder())
    again = ResearchPipeline(second, steps, NullGuard()).run(inputs())
    assert len(first.calls) > 0 and second.calls == []
    assert again.findings


def test_crash_in_the_middle_resumes_from_the_last_step() -> None:
    steps = MemorySteps()

    class Crash(Exception):
        pass

    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 3:
            raise Crash()
        return responder()(request)

    with pytest.raises(Crash):
        ResearchPipeline(FakeLLM(flaky), steps, NullGuard()).run(inputs())
    assert len(steps.saved) == 2  # 第三次调用时崩了：前两批的结果已经存下
    second = FakeLLM(responder())
    ResearchPipeline(second, steps, NullGuard()).run(inputs())
    uninterrupted = FakeLLM(responder())
    ResearchPipeline(uninterrupted, MemorySteps(), NullGuard()).run(inputs())
    # 续跑只补做剩下的部分：比从头跑少了已经完成的两次调用。
    assert len(second.calls) == len(uninterrupted.calls) - 2


def test_bad_finding_is_redone_with_feedback_then_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    feedbacks = []

    def stubborn(requirement, chunks, profile, llm, *, call_key, feedback=None):
        feedbacks.append(feedback)
        return Finding(
            requirement_id=requirement.id,
            status=Status.UNMET,
            reason="企业不接受连续60天驻场",
            profile_fields=("delivery_constraints",),
            profile_quote="不接受连续60天驻场",
        )

    monkeypatch.setattr(match_step, "match", stubborn)
    report = ResearchPipeline(FakeLLM(responder()), MemorySteps(), NullGuard()).run(inputs())
    schedule = next(
        f
        for f in report.findings
        if "60日内完成系统开发" in {r.id: r for r in report.requirements}[f.requirement_id].quote
    )
    assert schedule.status is Status.UNKNOWN and schedule.verification.downgraded
    assert schedule.verification.attempts == 3
    assert feedbacks.count(None) == len(report.findings)  # 每条第一次没有意见，之后带着意见重做
    assert any(f and "profile_field_outside_dimension" in f for f in feedbacks)
    assert report.coverage.downgraded >= 1


def test_guard_stops_the_pipeline() -> None:
    class Stop:
        def check(self, step):
            if step.startswith("extract:"):
                raise PipelineStopped("cancelled")

    with pytest.raises(PipelineStopped, match="cancelled"):
        ResearchPipeline(FakeLLM(responder()), MemorySteps(), Stop()).run(inputs())


def test_extraction_cache_is_shared_across_runs() -> None:
    class Cache:
        def __init__(self):
            self.data = {}

        def get(self, key):
            return self.data.get(key)

        def put(self, key, value):
            self.data[key] = value

    cache = Cache()
    first = FakeLLM(responder())
    ResearchPipeline(first, MemorySteps(), NullGuard(), cache=cache).run(
        ResearchInputs(run_id="a", chunks=list(tender_chunks()), profile=profile(), extraction_key="docs-v1")
    )
    second = FakeLLM(responder())
    ResearchPipeline(second, MemorySteps(), NullGuard(), cache=cache).run(
        ResearchInputs(run_id="b", chunks=list(tender_chunks()), profile=profile(), extraction_key="docs-v1")
    )
    assert first.calls and not [c for c in second.calls if c.purpose.startswith("extract")]


def test_baseline_mode_never_calls_the_model() -> None:
    report = ResearchPipeline(DisabledLLM(), MemorySteps(), NullGuard(), PipelineConfig(mode="baseline")).run(inputs())
    assert report.mode == "baseline" and report.findings
    assert all(r.source == "rule" for r in report.requirements)


def test_config_version_changes_with_the_config() -> None:
    a = ResearchPipeline(DisabledLLM(), MemorySteps(), NullGuard(), PipelineConfig(mode="agent")).versions
    b = ResearchPipeline(DisabledLLM(), MemorySteps(), NullGuard(), PipelineConfig(mode="baseline")).versions
    assert a["config_version"] != b["config_version"]
    assert a["prompt.extract"].startswith("extract.v1:")


def test_baseline_turns_a_model_hungry_match_into_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """练习 2 写好的 match 会在规则判不了时问模型；基线模式下模型没开，这条只能是未知，任务不能因此失败。"""

    def asks_the_model(requirement, chunks, profile, llm, *, call_key, feedback=None):
        llm.complete(LLMRequest(purpose="match", call_key=call_key, system="s", user="u"))
        raise AssertionError("DisabledLLM 应该已经拒绝了")

    monkeypatch.setattr(match_step, "match", asks_the_model)
    config = PipelineConfig(mode="baseline")
    report = ResearchPipeline(DisabledLLM(), MemorySteps(), NullGuard(), config).run(inputs())
    assert report.findings and {(f.status, f.rule_id) for f in report.findings} == {(Status.UNKNOWN, "model_disabled")}
    assert all(f.gap for f in report.findings)

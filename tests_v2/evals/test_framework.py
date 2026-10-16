"""评测框架本身：标准答案前后一致、录制和重放、各步骤能跑、核验评测的造错方式有效。不调真实模型。"""

import pytest

from bidradar.agent.checks import quote_in
from bidradar.agent.dimensions import allowed_fields
from bidradar.agent.llm import FakeLLM, LLMRequest
from bidradar.agent.schemas import Kind, Status
from evals import metrics, run
from evals.cases import chunks_containing, document_chunks, load_cases
from evals.replay import MissingRecording, RecordingLLM
from tests_v2.agent.support import responder

CASES = load_cases(holdout=True)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_gold_data_is_consistent(case) -> None:
    """标准答案写错了，指标就全是错的。每次改 yaml 都靠这个测试兜底。"""
    chunks = document_chunks(case.document)
    for quote in [r.quote for r in case.document.requirements] + list(case.document.optional):
        assert chunks_containing(quote, chunks), f"原文里找不到：{quote}"
    for quote in case.document.not_requirements:
        assert chunks_containing(quote, chunks), f"原文里找不到：{quote}"
    obligations = {r.quote for r in case.document.requirements if r.kind is Kind.OBLIGATION}
    labeled = [f.quote for f in case.findings]
    assert len(labeled) == len(set(labeled)) and set(labeled) == obligations, "每条要求都要有且只有一条标准结论"
    for finding in case.findings:
        assert not any(finding.correct(s) for s in finding.must_not), f"既算对又算严重错误：{finding.quote}"
        if finding.profile_quote:
            dimension = case.gold_requirement(finding.quote).dimension
            fields = [case.profile.fields.get(name) or "" for name in allowed_fields(dimension)]
            assert quote_in(finding.profile_quote, fields), f"档案原话不在这个维度能看的字段里：{finding.quote}"


def test_recording_then_replay(tmp_path) -> None:
    path = tmp_path / "rec.jsonl"
    request = LLMRequest(purpose="verify", call_key="run-a:1", system="s", user="u")
    recorder = RecordingLLM(path, FakeLLM(lambda r: {"verdict": "supported"}))
    first = recorder.complete(request)
    # call_key 不同（另一次评测）也能重放：指纹只看请求内容。
    replay = RecordingLLM(path)
    again = replay.complete(request.model_copy(update={"call_key": "run-b:1"}))
    assert again.text == first.text and again.replayed and replay.replayed == 1
    with pytest.raises(MissingRecording):
        replay.complete(request.model_copy(update={"user": "改过的输入"}))


def test_live_mode_needs_explicit_consent(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run.make_setup("live", enable_model=False, max_cost_micro=1, recordings=tmp_path / "r.jsonl")


def test_verify_step_program_checks_catch_the_mechanical_errors() -> None:
    setup = run.make_setup("baseline", enable_model=False, max_cost_micro=0, recordings=None)
    for case in CASES:
        injected, results = run.collect_verify(case, setup)
        scores = metrics.verify_scores(injected, results)
        for kind in ("cross_dimension", "fake_profile_quote", "invented_number"):
            caught, total = scores.by_kind[kind]
            assert total and caught == total, kind
        # 没有模型核验时，"档案原话是真的、结论却反了"一条也拦不下：这正是模型核验存在的理由。
        assert scores.by_kind["flipped"][0] == 0
        assert scores.false_rejections == 0


def test_verify_step_with_a_judge() -> None:
    setup = run.Setup(FakeLLM(lambda r: {"verdict": "unsupported", "note": "对不上"}), "agent")
    case = next(c for c in CASES if c.id == "tender-x-beiyu")
    scores = metrics.verify_scores(*run.collect_verify(case, setup))
    assert scores.by_kind["flipped"][0] == scores.by_kind["flipped"][1] > 0
    assert scores.clean > 0 and scores.false_rejections == scores.clean  # 一律说"不支持"的核验会误杀全部正确结论


def test_steps_run_offline() -> None:
    setup = run.make_setup("baseline", enable_model=False, max_cost_micro=0, recordings=None)
    case = CASES[0]
    extracted = run.evaluate(case, "extract", setup)
    assert extracted["predicted"] > 0 and extracted["citation_validity"] == 1.0
    matched = run.collect_match(case, setup)
    assert len(matched.final) == len(case.findings)
    assert {f.status for f in matched.first} == {Status.UNKNOWN}  # 基线：规则判不了的只能未知
    report = run.collect_all(case, setup)
    assert report.mode == "baseline" and report.findings


def test_agent_mode_with_a_fake_model() -> None:
    fake = FakeLLM(responder())
    setup = run.Setup(fake, "agent")
    case = CASES[0]
    extracted = run.collect_extract(case, setup)
    assert extracted.requirements and fake.calls
    assert all(c.call_key.startswith(f"eval:{case.document.id}") for c in fake.calls)


def test_cli_prints_a_report(capsys) -> None:
    assert run.main(["--step", "verify"]) == 0
    out = capsys.readouterr().out
    assert "核验" in out and "拦截" in out

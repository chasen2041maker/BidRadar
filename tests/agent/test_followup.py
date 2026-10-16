"""追问 Agent：查到再答、引用必须读过、数字要有出处、调用上限、续跑不重复付费。"""

import json

from bidradar.agent.followup import FALLBACK_ANSWER, FORCE_ANSWER_AT, MAX_MODEL_CALLS, FollowupAgent
from bidradar.agent.llm import FakeLLM, LLMRequest
from bidradar.agent.pipeline import ResearchInputs, ResearchPipeline
from bidradar.agent.schemas import Chunk
from tests.agent.support import MemorySteps, NullGuard, profile, responder, tender_chunks

QUESTION = "驻场要求是什么？"


class KeywordTools:
    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def search_chunks(self, query: str, limit: int) -> list[Chunk]:
        return [c for c in self.chunks if query in c.text][:limit]


def make_agent(llm, steps=None) -> FollowupAgent:
    chunks = list(tender_chunks())
    report = ResearchPipeline(FakeLLM(responder()), MemorySteps(), NullGuard()).run(
        ResearchInputs(run_id="parent", chunks=chunks, profile=profile())
    )
    return FollowupAgent(
        llm,
        steps or MemorySteps(),
        NullGuard(),
        report=report,
        chunks=chunks,
        profile=profile(),
        tools=KeywordTools(chunks),
    )


def history(request: LLMRequest) -> list[dict]:
    return json.loads(request.user)["history"]


def researcher(answer: str = "需要不少于2名工程师驻场，驻场时间不少于30个工作日。", cite_unread: bool = False):
    """先搜"驻场"，再读第一条结果，然后引用它回答。"""

    def respond(request: LLMRequest):
        if request.purpose == "verify":
            return {"verdict": "supported"}
        done = history(request)
        if not done:
            return {"action": "tool", "tool": "search_chunks", "arguments": {"query": "驻场"}}
        if len(done) == 1:
            return {"action": "tool", "tool": "read_chunk", "arguments": {"refs": [done[0]["result"][0]["ref"]]}}
        ref = done[1]["result"][0]["ref"]
        return {"action": "answer", "answer": answer, "citations": ["c1" if cite_unread else ref]}

    return respond


def test_answers_after_searching_and_reading() -> None:
    llm = FakeLLM(researcher())
    answer = make_agent(llm).answer(QUESTION, "run-q")
    assert answer.verified and answer.stop_reason == "answered"
    [cited] = answer.citations
    assert "驻场" in next(c.text for c in tender_chunks() if c.id == cited)
    assert (answer.tool_calls, [r.purpose for r in llm.calls]) == (2, ["followup"] * 3 + ["verify"])


def test_citing_a_chunk_it_never_read_is_not_shown() -> None:
    answer = make_agent(FakeLLM(researcher(cite_unread=True))).answer(QUESTION, "run-q")
    assert (answer.answer, answer.verified) == (FALLBACK_ANSWER, False)
    assert "cited_unread_chunk" in answer.verification_note


def test_number_without_source_is_not_shown() -> None:
    answer = make_agent(FakeLLM(researcher(answer="需要5名工程师驻场。"))).answer(QUESTION, "run-q")
    assert answer.answer == FALLBACK_ANSWER and "unsupported_number" in answer.verification_note


def test_judge_rejection_falls_back() -> None:
    script = researcher()

    def respond(request):
        return {"verdict": "unsupported", "note": "原文没说"} if request.purpose == "verify" else script(request)

    answer = make_agent(FakeLLM(respond)).answer(QUESTION, "run-q")
    assert answer.answer == FALLBACK_ANSWER and "judge_unsupported" in answer.verification_note


def test_endless_tool_use_stops_at_the_limit() -> None:
    llm = FakeLLM(lambda r: {"action": "tool", "tool": "search_chunks", "arguments": {"query": "驻场"}})
    answer = make_agent(llm).answer(QUESTION, "run-q")
    assert answer.answer == FALLBACK_ANSWER and answer.stop_reason == "limit"
    assert len(llm.calls) == MAX_MODEL_CALLS
    # 到强制回答那一轮起，模型被告知必须回答，再调工具也不执行。
    assert json.loads(llm.calls[FORCE_ANSWER_AT - 1].user)["must_answer_now"] is True
    assert history(llm.calls[-1])[-1]["error"] == "tool_not_allowed_now"


def test_invalid_output_falls_back() -> None:
    answer = make_agent(FakeLLM(lambda r: "不是 JSON")).answer(QUESTION, "run-q")
    assert answer.answer == FALLBACK_ANSWER and answer.stop_reason == "invalid_output"


def test_resume_replays_rounds_and_restores_what_was_read() -> None:
    steps = MemorySteps()
    first = make_agent(FakeLLM(researcher()), steps).answer(QUESTION, "run-q")
    again_llm = FakeLLM(researcher())
    again = make_agent(again_llm, steps).answer(QUESTION, "run-q")
    # 续跑不再调模型；工具调用被重放，"读过哪个片段"恢复了，所以引用检查照样通过。
    assert again_llm.calls == [] and again == first


def test_unknown_tools_and_fields_are_refused() -> None:
    def respond(request):
        done = history(request)
        if not done:
            return {"action": "tool", "tool": "run_sql", "arguments": {"sql": "select 1"}}
        if len(done) == 1:
            return {"action": "tool", "tool": "get_profile", "arguments": {"fields": ["password", "staffing"]}}
        return {"action": "answer", "answer": "看不出来。", "citations": []}

    llm = FakeLLM(lambda r: {"verdict": "supported"} if r.purpose == "verify" else respond(r))
    make_agent(llm).answer(QUESTION, "run-q")
    steps = history(llm.calls[2])
    assert steps[0]["result"] == {"error": "unknown_tool"}
    assert list(steps[1]["result"]) == ["staffing"]

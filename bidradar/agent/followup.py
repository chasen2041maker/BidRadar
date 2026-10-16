"""追问 Agent：只在这里让模型自己决定"再查什么"（工具循环），调用次数有上限。

研究流水线的顺序是固定的，所以不用 Agent；追问不一样：用户的问题五花八门，事先不知道要查哪几段原文。

一轮循环：模型看到问题、报告概要和之前几轮的工具结果 → 输出一个动作：调一个工具，或者给出最终回答。
    工具：search_chunks（找片段）、read_chunk（读片段）、get_finding（读结论）、get_profile（读档案字段）
    上限：最多 6 次模型调用、12 次工具调用；到第 5 次调用时只允许回答，逼模型收尾
每一轮都是一个可续跑的步骤（followup:round:<n>），Worker 崩了从断点接着跑，付过钱的调用不再付。

工具都是只读的，参数由程序校验，只能读本次研究固定下来的片段和档案版本。模型拿不到 URL、SQL、文件路径。

最终回答要核验：
    程序检查：引用的片段必须本轮读过；回答里的数字要在引用的片段、读过的档案字段或结论里出现；没有联系方式、没有不允许的断言
    模型核验：回答里的事实是否都有依据（answer_check 提示词）
核验不通过，回答改为"基于现有材料无法确认"，并列出缺口。不会把没有依据的回答展示给用户。
"""

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from bidradar.agent import prompts
from bidradar.agent.checks import forbidden_claims, has_contact, redact, unsupported_numbers
from bidradar.agent.dimensions import PROFILE_FIELDS
from bidradar.agent.llm import InvalidModelOutput, LLMClient, LLMRequest, parse_json
from bidradar.agent.pipeline import Guard, StepStore, fingerprint
from bidradar.agent.schemas import Chunk, Frozen, ProfileSnapshot, Report

MAX_MODEL_CALLS = 6
MAX_TOOL_CALLS = 12
FORCE_ANSWER_AT = 5
SEARCH_LIMIT = 8
READ_LIMIT = 6
FALLBACK_ANSWER = "基于现有材料无法确认这个问题的答案。"


class ToolBox(Protocol):
    """research 模块实现：只能读本次研究固定下来的材料。"""

    def search_chunks(self, query: str, limit: int) -> list[Chunk]: ...


class FollowupAnswer(Frozen):
    answer: str
    citations: tuple[str, ...] = ()
    finding_refs: tuple[int, ...] = ()
    gaps: tuple[str, ...] = ()
    verified: bool = False
    verification_note: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    stop_reason: Literal["answered", "limit", "invalid_output", "unverified"] = "answered"


class _Action(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["tool", "answer"]
    tool: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    answer: str | None = None
    citations: list[str] = Field(default_factory=list)
    finding_refs: list[int] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)


class _Round(BaseModel):
    """一轮的记录：模型的动作和工具的结果。整轮存成一个步骤，续跑时原样复用。"""

    action: _Action | None
    result: Any = None
    error: str | None = None


class _Judgement(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: str
    note: str = ""


@dataclass
class _State:
    read: dict[str, Chunk]  # 读过的片段：别名 → 片段
    profile_read: dict[str, str | None]
    findings_read: list[int]
    tool_calls: int = 0


class FollowupAgent:
    def __init__(
        self,
        llm: LLMClient,
        steps: StepStore,
        guard: Guard,
        *,
        report: Report,
        chunks: list[Chunk],
        profile: ProfileSnapshot,
        tools: ToolBox,
    ) -> None:
        self.llm, self.steps, self.guard = llm, steps, guard
        self.report, self.profile, self.tools = report, profile, tools
        ordered = sorted(chunks, key=lambda c: (c.document_id, c.seq))
        self.alias_of = {c.id: f"c{i + 1}" for i, c in enumerate(ordered)}
        self.by_alias = {self.alias_of[c.id]: c for c in ordered}
        self.requirements = {r.id: r for r in report.requirements}

    # ---- 工具 ----

    def _view(self, chunk: Chunk) -> dict[str, Any]:
        return {
            "ref": self.alias_of[chunk.id],
            "page": chunk.page,
            "section": " / ".join(chunk.section_path),
            "text": redact(chunk.text),
        }

    def _run_tool(self, action: _Action, state: _State) -> Any:
        args = action.arguments
        if action.tool == "search_chunks":
            query = str(args.get("query", ""))[:100]
            limit = max(1, min(int(args.get("limit", 5) or 5), SEARCH_LIMIT))
            found = [c for c in self.tools.search_chunks(query, limit) if c.id in self.alias_of]
            # 搜索结果只给片段开头，读全文要再调 read_chunk：模型得明确决定读哪几段。
            return [{**self._view(c), "text": redact(c.text)[:120]} for c in found]
        if action.tool == "read_chunk":
            refs = [str(r) for r in (args.get("refs") or [])][:READ_LIMIT]
            result = []
            for ref in refs:
                chunk = self.by_alias.get(ref)
                if chunk is not None:
                    state.read[ref] = chunk
                    result.append(self._view(chunk))
            return result
        if action.tool == "get_finding":
            index = int(args.get("index", -1))
            if not 0 <= index < len(self.report.findings):
                return {"error": "no_such_finding"}
            finding = self.report.findings[index]
            requirement = self.requirements.get(finding.requirement_id)
            state.findings_read.append(index)
            for cid in requirement.chunk_ids if requirement else ():
                if cid in self.alias_of:
                    state.read[self.alias_of[cid]] = self.by_alias[self.alias_of[cid]]
            return {
                "index": index,
                "requirement": requirement.quote if requirement else None,
                "refs": [
                    self.alias_of[c] for c in (requirement.chunk_ids if requirement else ()) if c in self.alias_of
                ],
                "status": finding.status.value,
                "reason": finding.reason,
                "gap": finding.gap,
            }
        if action.tool == "get_profile":
            fields = [str(f) for f in (args.get("fields") or []) if f in PROFILE_FIELDS]
            for name in fields:
                state.profile_read[name] = self.profile.fields.get(name)
            return {name: redact(self.profile.fields.get(name) or "") or None for name in fields}
        return {"error": "unknown_tool"}

    # ---- 循环 ----

    def _request(self, question: str, history: list[dict], round_no: int, run_id: str) -> LLMRequest:
        payload = {
            "question": redact(question),
            "report": {
                "summary": self.report.summary.text,
                "findings": [
                    {
                        "index": i,
                        "requirement": self.requirements[f.requirement_id].quote[:120],
                        "status": f.status.value,
                    }
                    for i, f in enumerate(self.report.findings)
                    if f.requirement_id in self.requirements
                ],
            },
            "history": history,
            "must_answer_now": round_no >= FORCE_ANSWER_AT,
        }
        return LLMRequest(
            purpose="followup",
            call_key=f"{run_id}:followup:{round_no}",
            system=prompts.load("followup").text,
            user=json.dumps(payload, ensure_ascii=False),
            max_output_tokens=1500,
        )

    def answer(self, question: str, run_id: str) -> FollowupAnswer:
        state = _State(read={}, profile_read={}, findings_read=[])
        history: list[dict] = []
        for round_no in range(1, MAX_MODEL_CALLS + 1):
            request = self._request(question, history, round_no, run_id)
            name = f"followup:round:{round_no}"

            def play(request: LLMRequest = request, round_no: int = round_no) -> _Round:
                try:
                    action = parse_json(self.llm.complete(request), _Action)
                except InvalidModelOutput:
                    return _Round(action=None, error="invalid_output")
                if action.action == "tool":
                    if state.tool_calls >= MAX_TOOL_CALLS or round_no >= FORCE_ANSWER_AT:
                        return _Round(action=action, error="tool_not_allowed_now")
                    return _Round(action=action, result=self._run_tool(action, state))
                return _Round(action=action)

            record = self._replayed_or(name, fingerprint([question, history]), play, state)
            if record.action is None:
                return self._fallback("invalid_output", round_no, state)
            if record.action.action == "tool":
                state.tool_calls += 1
                history.append(
                    {
                        "tool": record.action.tool,
                        "arguments": record.action.arguments,
                        "result": record.result,
                        "error": record.error,
                    }
                )
                continue
            return self._finish(question, record.action, state, round_no, run_id)
        return self._fallback("limit", MAX_MODEL_CALLS, state)

    def _replayed_or(self, name: str, input_hash: str, play, state: _State) -> _Round:
        stored = self.steps.get(name, input_hash)
        if stored is not None:
            record = _Round.model_validate(stored)
            # 续跑时重放工具调用，把"读过哪些片段"恢复出来。工具都是只读的，重放没有副作用，也不花钱。
            if record.action and record.action.action == "tool" and record.error is None:
                self._run_tool(record.action, state)
            return record
        self.guard.check(name)
        record = play()
        self.steps.save(name, input_hash, record.model_dump(mode="json"))
        return record

    def _fallback(self, reason: str, calls: int, state: _State) -> FollowupAnswer:
        return FollowupAnswer(
            answer=FALLBACK_ANSWER,
            gaps=("这次追问没有在调用上限内得到有依据的回答，可以换个问法，或者补充材料后再问。",),
            model_calls=calls,
            tool_calls=state.tool_calls,
            stop_reason="limit" if reason == "limit" else "invalid_output",
        )

    def _finish(self, question: str, action: _Action, state: _State, calls: int, run_id: str) -> FollowupAnswer:
        text = (action.answer or "").strip()[:3000]
        cited = [ref for ref in action.citations if ref in state.read]
        problems = []
        if not text:
            problems.append("empty_answer")
        if len(cited) != len(action.citations):
            problems.append("cited_unread_chunk")
        sources = [state.read[ref].text for ref in cited]
        sources += [v for v in state.profile_read.values() if v]
        sources += [self.report.findings[i].reason for i in state.findings_read]
        if unsupported_numbers(text, sources):
            problems.append("unsupported_number")
        if has_contact(text) or forbidden_claims(text):
            problems.append("forbidden_content")
        note = None
        if not problems:
            verdict, note = self._judge(question, text, cited, state, run_id)
            if verdict != "supported":
                problems.append("judge_" + verdict)
        if problems:
            return FollowupAnswer(
                answer=FALLBACK_ANSWER,
                gaps=tuple(action.gaps[:5]) or ("回答没有通过核验，需要人工查看原文。",),
                verified=False,
                verification_note="、".join(problems) + (f"；{note}" if note else ""),
                model_calls=calls,
                tool_calls=state.tool_calls,
                stop_reason="unverified",
            )
        return FollowupAnswer(
            answer=text,
            citations=tuple(state.read[ref].id for ref in cited),
            finding_refs=tuple(i for i in action.finding_refs if 0 <= i < len(self.report.findings)),
            gaps=tuple(action.gaps[:5]),
            verified=True,
            verification_note=note,
            model_calls=calls + 1,
            tool_calls=state.tool_calls,
        )

    def _judge(self, question: str, text: str, cited: list[str], state: _State, run_id: str) -> tuple[str, str | None]:
        payload = {
            "question": redact(question),
            "answer": text,
            "chunks": [{"ref": ref, "text": redact(state.read[ref].text)} for ref in cited],
            "profile": {k: redact(v or "") or None for k, v in state.profile_read.items()},
        }
        request = LLMRequest(
            purpose="verify",
            call_key=f"{run_id}:followup:check",
            system=prompts.load("answer_check").text,
            user=json.dumps(payload, ensure_ascii=False),
            max_output_tokens=400,
        )
        stored = self.steps.get("followup:check", fingerprint(payload))
        if stored is None:
            self.guard.check("followup:check")
            try:
                result = parse_json(self.llm.complete(request), _Judgement)
                stored = {"verdict": result.verdict, "note": result.note}
            except InvalidModelOutput:
                stored = {"verdict": "uncertain", "note": "核验输出格式不对"}
            self.steps.save("followup:check", fingerprint(payload), stored)
        verdict = stored["verdict"] if stored["verdict"] in ("supported", "unsupported", "uncertain") else "uncertain"
        return verdict, stored.get("note") or None

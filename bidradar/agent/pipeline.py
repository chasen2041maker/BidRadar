"""编排研究流水线：解析 → 抽取 → 匹配 → 核验 → 汇总。

编排是一个普通的 Python 类，不用 LangGraph 一类的框架（决策 0008）：五个步骤的顺序是固定的，
不需要模型去决定"下一步做什么"，显式写出来最好读、最好测。

续跑：每一步的结果都通过 StepStore 存下来，步骤名精确到"一条要求的第几次匹配"：
    extract:<批次>   recall   match:<要求>:<第几次>   verify:<要求>:<第几次>   summary
Worker 崩在第 17 条要求，重启后前 16 条直接复用。步骤还带一个输入指纹（input_hash）：
输入变了（比如换了档案版本），旧结果不会被误用。

每一步之前调 guard.check()：任务有没有被取消、发起人还有没有权限、租约还在不在。
三样东西都从外面传进来（LLMClient、StepStore、Guard），所以流水线不连数据库也能单测和评测。
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel

from bidradar.agent import prompts
from bidradar.agent.dimensions import allowed_fields, table_fingerprint_source
from bidradar.agent.llm import LLMClient, ModelDisabled
from bidradar.agent.schemas import (
    Chunk,
    ExtractionIssue,
    ExtractResult,
    Finding,
    Kind,
    ProfileSlice,
    ProfileSnapshot,
    Report,
    Requirement,
    Status,
    VerifyResult,
)
from bidradar.agent.steps import compose as compose_step
from bidradar.agent.steps import extract as extract_step
from bidradar.agent.steps import match as match_step
from bidradar.agent.steps import verify as verify_step
from bidradar.agent.steps.parse import Material, prepare

PIPELINE_VERSION = "research-pipeline-v1"
NO_MODEL_REASON = "规则无法判断这条要求，模型没有开启，暂按未知处理。"
NO_MODEL_GAP = "需要人工对照企业档案判断这条要求是否满足。"

T = TypeVar("T", bound=BaseModel)


class StepStore(Protocol):
    def get(self, name: str, input_hash: str) -> dict | None: ...

    def save(self, name: str, input_hash: str, value: dict) -> None: ...


class Guard(Protocol):
    def check(self, step: str) -> None:
        """不能继续时抛 PipelineStopped。"""


class ExtractionCache(Protocol):
    """跨任务的抽取结果缓存：同一份公开文件、同一版抽取规则，只抽一次，各公司共用。"""

    def get(self, key: str) -> dict | None: ...

    def put(self, key: str, value: dict) -> None: ...


class PipelineStopped(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PipelineConfig:
    # agent：用模型；baseline：只用规则（没开模型时，或者用来对照）
    mode: Literal["agent", "baseline"] = "agent"
    # 一条要求最多生成几次结论：第一次 + 核验不通过时重做 2 次
    max_attempts: int = 3
    # unknown 结论要不要也做模型核验（设计文档第 14 节问题 3，默认不做）
    judge_unknown: bool = False
    recall_pass: bool = True
    model_summary: bool = False
    batch_chars: int = extract_step.BATCH_CHARS


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def versions(config: PipelineConfig) -> dict[str, str]:
    """这次研究用的全部规则的版本。任务创建时记下来，旧报告永远知道自己是按哪版规则生成的。"""
    prompt_texts = prompts.all_current()
    result = {
        "pipeline": PIPELINE_VERSION,
        "rules": extract_step.RULES_VERSION,
        "dimensions": fingerprint(table_fingerprint_source())[:12],
        "config": fingerprint(asdict(config))[:12],
    }
    result |= {
        f"prompt.{name}": f"{prompts.CURRENT[name]}:{fingerprint(text)[:8]}" for name, text in prompt_texts.items()
    }
    result["config_version"] = fingerprint(result)[:16]
    return result


@dataclass(frozen=True)
class ResearchInputs:
    run_id: str
    chunks: list[Chunk]
    profile: ProfileSnapshot
    # 抽取缓存的键：由文件内容和抽取规则版本决定（research 模块算好传进来）；None 表示不用缓存
    extraction_key: str | None = None


class ResearchPipeline:
    def __init__(
        self,
        llm: LLMClient,
        steps: StepStore,
        guard: Guard,
        config: PipelineConfig = PipelineConfig(),
        cache: ExtractionCache | None = None,
    ) -> None:
        self.llm, self.steps, self.guard, self.config, self.cache = llm, steps, guard, config, cache
        self.versions = versions(config)

    def step(self, name: str, inputs: object, fn: Callable[[], T], model: type[T]) -> T:
        input_hash = fingerprint([self.versions["config_version"], inputs])
        stored = self.steps.get(name, input_hash)
        if stored is not None:
            return model.model_validate(stored)  # 续跑：已完成的步骤直接复用
        self.guard.check(name)  # 取消、权限、租约
        result = fn()
        self.steps.save(name, input_hash, result.model_dump(mode="json"))
        return result

    # ---- ② 抽取 ----

    def extract(self, material: Material, inputs: ResearchInputs) -> ExtractResult:
        if self.cache is not None and inputs.extraction_key:
            cached = self.cache.get(inputs.extraction_key)
            if cached is not None:
                return ExtractResult.model_validate(cached)
        planned, skipped = extract_step.batches(material, self.config.batch_chars)
        if self.config.mode == "baseline":
            result = ExtractResult(
                requirements=tuple(extract_step.rule_extract(planned)),
                skipped_sections=tuple(skipped),
                batches=len(planned),
            )
        else:
            found: list[Requirement] = []
            issues: list[ExtractionIssue] = []
            for batch in planned:
                one = self.step(
                    f"extract:{batch.key}",
                    [c.id for c in batch.chunks],
                    lambda batch=batch: _BatchResult.of(
                        *extract_step.extract_batch(batch, material, self.llm, f"{inputs.run_id}:extract:{batch.key}")
                    ),
                    _BatchResult,
                )
                found += one.requirements
                issues += one.issues
            recall_added = 0
            if self.config.recall_pass:
                candidates = extract_step.recall_candidates(material, found, planned)
                recalled = self.step(
                    "recall",
                    [c.id for c in candidates],
                    lambda: _BatchResult.of(
                        extract_step.extract_recall(candidates, material, self.llm, f"{inputs.run_id}:recall"), []
                    ),
                    _BatchResult,
                )
                before = {r.id for r in found}
                found += recalled.requirements
                recall_added = len({r.id for r in recalled.requirements} - before)
            result = ExtractResult(
                requirements=tuple(extract_step.merge(found)),
                issues=tuple(issues),
                skipped_sections=tuple(skipped),
                batches=len(planned),
                recall_added=recall_added,
            )
        if self.cache is not None and inputs.extraction_key and not result.issues:
            # 只缓存完整的结果：有批次出错的结果下一次应该重新抽。
            self.cache.put(inputs.extraction_key, result.model_dump(mode="json"))
        return result

    # ---- ③ 匹配 + ④ 核验 ----

    def match_and_verify(
        self, requirement: Requirement, material: Material, profile: ProfileSnapshot, run_id: str
    ) -> Finding:
        chunks = [material.by_id[cid] for cid in requirement.chunk_ids if cid in material.by_id]
        sliced = ProfileSlice(
            version=profile.version,
            fields={name: profile.fields.get(name) for name in allowed_fields(requirement.dimension)},
        )
        feedback: str | None = None
        last: VerifyResult | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            base = [requirement.model_dump(mode="json"), [c.text for c in chunks], sliced.model_dump(), attempt]
            finding = self.step(
                f"match:{requirement.id}:{attempt}",
                base + [feedback],
                lambda attempt=attempt, feedback=feedback: self._match(
                    requirement, chunks, sliced, f"{run_id}:match:{requirement.id}:{attempt}", feedback
                ),
                Finding,
            )
            last = self.step(
                f"verify:{requirement.id}:{attempt}",
                base + [finding.model_dump(mode="json")],
                lambda attempt=attempt, finding=finding: verify_step.verify(
                    finding,
                    requirement,
                    chunks,
                    profile,
                    self.llm,
                    call_key=f"{run_id}:verify:{requirement.id}:{attempt}",
                    attempt=attempt,
                    judge_unknown=self.config.judge_unknown,
                ),
                VerifyResult,
            )
            if last.ok:
                return last.finding
            feedback = last.feedback
        return verify_step.downgrade(last, self.config.max_attempts)

    def _match(
        self,
        requirement: Requirement,
        chunks: list[Chunk],
        sliced: ProfileSlice,
        call_key: str,
        feedback: str | None,
    ) -> Finding:
        try:
            return match_step.match(requirement, chunks, sliced, self.llm, call_key=call_key, feedback=feedback)
        except ModelDisabled:
            # 基线模式下匹配想问模型：规则判不了、模型又没开，只能是未知。
            # 放在这里而不是让 match 自己处理，match 只管"先规则、再问模型"，不用关心现在是哪种模式。
            return Finding(
                requirement_id=requirement.id,
                status=Status.UNKNOWN,
                reason=NO_MODEL_REASON,
                gap=NO_MODEL_GAP,
                basis="rule",
                rule_id="model_disabled",
            )

    # ---- 整条流水线 ----

    def run(self, inputs: ResearchInputs) -> Report:
        material = prepare(inputs.chunks)
        extracted = self.extract(material, inputs)
        obligations = [r for r in extracted.requirements if r.kind is Kind.OBLIGATION]
        findings = [self.match_and_verify(r, material, inputs.profile, inputs.run_id) for r in obligations]
        summary_text = None
        if self.config.model_summary and self.config.mode == "agent" and findings:
            summary_text = self.step(
                "summary",
                [f.model_dump(mode="json") for f in findings],
                lambda: _Text(
                    text=compose_step.model_summary(
                        list(extracted.requirements), findings, self.llm, f"{inputs.run_id}:summary"
                    )
                ),
                _Text,
            ).text
        return compose_step.compose(
            mode=self.config.mode,
            extracted=extracted,
            findings=findings,
            material=material,
            versions=self.versions,
            summary_text=summary_text,
        )


class _BatchResult(BaseModel):
    requirements: list[Requirement]
    issues: list[ExtractionIssue]

    @classmethod
    def of(cls, requirements: list[Requirement], issues: list[ExtractionIssue]) -> "_BatchResult":
        return cls(requirements=requirements, issues=issues)


class _Text(BaseModel):
    text: str | None

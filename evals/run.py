"""评测入口：按步骤评，上游一律用标准答案，这样能定位到是哪一步错了（设计文档第 11 节）。

    uv run python -m evals.run --step extract     # 评抽取：召回率、精确率、维度准确率
    uv run python -m evals.run --step match       # 评匹配：输入是标准要求，不受抽取错误影响
    uv run python -m evals.run --step verify      # 评核验：故意喂错误的结论，看能拦下多少、会不会误杀
    uv run python -m evals.run --step all         # 端到端

模型怎么来（--llm）：
    baseline  默认。不调模型：抽取用规则，匹配里需要问模型的一律未知，核验只做程序检查。离线、免费
    replay    只用录下来的响应（evals/recordings/）。遇到没录过的请求就报错停下，不会偷偷去调模型
    live      没录过的请求真的发给 DeepSeek 并录下来。会花钱：必须同时写 --enable-model，
              费用上限 --max-cost（元，默认 2）。AGENTS.md：没有项目负责人明确同意不开启

其他：--case 只跑指定用例（可以写多个）；--holdout 连保留集一起跑（只在阶段结束时）；--json 把结果写进文件。
"""

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from bidradar.agent.dimensions import allowed_fields
from bidradar.agent.llm import BudgetedLLM, DeepSeekClient, DisabledLLM, LLMClient, ModelDisabled, Pricing
from bidradar.agent.pipeline import PipelineConfig, ResearchInputs, ResearchPipeline
from bidradar.agent.schemas import Chunk, ExtractResult, Finding, Kind, Report, Status, Verification, VerifyResult
from bidradar.agent.steps import verify as verify_step
from bidradar.agent.steps.parse import prepare
from evals import metrics
from evals.cases import Case, document_chunks, gold_requirement, load_cases
from evals.replay import RECORDINGS, MemoryLedger, RecordingLLM

STEPS = ("extract", "match", "verify", "all")


# ---- 运行环境 ----


class MemorySteps:
    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], dict] = {}

    def get(self, name: str, input_hash: str) -> dict | None:
        return self.saved.get((name, input_hash))

    def save(self, name: str, input_hash: str, value: dict) -> None:
        self.saved[(name, input_hash)] = value

    def first(self, name: str) -> dict | None:
        return next((value for (n, _), value in self.saved.items() if n == name), None)


class NullGuard:
    def check(self, step: str) -> None:
        pass


@dataclass
class Setup:
    llm: LLMClient
    mode: str  # agent / baseline
    recorder: RecordingLLM | None = None
    ledger: MemoryLedger | None = None

    def pipeline(self, steps: MemorySteps | None = None) -> ResearchPipeline:
        return ResearchPipeline(self.llm, steps or MemorySteps(), NullGuard(), PipelineConfig(mode=self.mode))


def make_setup(llm: str, *, enable_model: bool, max_cost_micro: int, recordings: Path) -> Setup:
    if llm == "baseline":
        return Setup(DisabledLLM(), "baseline")
    if llm == "replay":
        recorder = RecordingLLM(recordings)
        return Setup(recorder, "agent", recorder)
    if not enable_model:
        raise SystemExit("--llm live 会调用真实模型、会花钱：确认过费用上限后，再加上 --enable-model。")
    from bidradar.app.settings import Settings

    settings = Settings()
    key = settings.deepseek_api_key_file.read_text(encoding="utf-8").strip()
    ledger = MemoryLedger(max_cost_micro)
    # 重放在外层：录过的请求不进账本、不花钱；只有真的发出去的才预留和结算。
    recorder = RecordingLLM(
        recordings, BudgetedLLM(DeepSeekClient(key, model=settings.deepseek_model), ledger, Pricing())
    )
    return Setup(recorder, "agent", recorder, ledger)


def chunk_map(chunks: tuple[Chunk, ...]) -> dict[str, Chunk]:
    return {c.id: c for c in chunks}


# ---- 各步骤：收集预测 ----


def collect_extract(case: Case, setup: Setup) -> ExtractResult:
    chunks = list(document_chunks(case.document))
    inputs = ResearchInputs(run_id=f"eval:{case.document.id}", chunks=chunks, profile=case.profile)
    return setup.pipeline().extract(prepare(chunks), inputs)


@dataclass(frozen=True)
class MatchRun:
    first: list[Finding]  # 第一次匹配的原始输出（还没经过核验）
    final: list[Finding]  # 经过核验、重做、降级之后，报告里展示的结论


def collect_match(case: Case, setup: Setup) -> MatchRun:
    chunks = document_chunks(case.document)
    material = prepare(list(chunks))
    first, final = [], []
    for gold in case.findings:
        requirement = gold_requirement(case.gold_requirement(gold.quote), chunks)
        steps = MemorySteps()
        final.append(setup.pipeline(steps).match_and_verify(requirement, material, case.profile, f"eval:{case.id}"))
        first.append(Finding.model_validate(steps.first(f"match:{requirement.id}:1")))
    return MatchRun(first=first, final=final)


def _first_clause(text: str) -> str:
    return text.split("，")[0].split("；")[0][:20]


def inject(case: Case) -> list[metrics.Injected]:
    """为每条标准结论造几条错的（以及能造的话一条对的）结论。造法是固定的，结果可复现。

    cross_dimension     拿别的维度的档案字段下结论（旧版最典型的错）→ 程序检查应该拦下
    fake_profile_quote  满足，但引用的"档案原话"档案里没有 → 程序检查应该拦下
    invented_number     理由里出现原文和档案都没有的数字 → 程序检查应该拦下
    flipped             档案原话是真的，结论却和标准相反 → 只有模型核验能拦下
    clean               标准是 met / unmet 时，按标注的档案原话写一条正确的结论 → 不应该被拦下
    """
    chunks = document_chunks(case.document)
    fields = case.profile.fields
    items: list[metrics.Injected] = []
    for gold in case.findings:
        requirement = gold_requirement(case.gold_requirement(gold.quote), chunks)
        if requirement.kind is not Kind.OBLIGATION:
            continue
        allowed = [name for name in allowed_fields(requirement.dimension)]
        filled = [name for name in allowed if fields.get(name)]
        outside = [name for name in fields if name not in allowed and fields.get(name)]

        def add(kind: str, clean: bool = False, **values: Any) -> None:
            finding = Finding(requirement_id=requirement.id, basis="model", **values)
            items.append(metrics.Injected(kind=kind, requirement=requirement, finding=finding, clean=clean))

        if outside:
            quote = _first_clause(fields[outside[0]])
            add(
                "cross_dimension",
                status=Status.UNMET,
                reason=f"档案写明“{quote}”，不满足这条要求。",
                profile_fields=(outside[0],),
                profile_quote=quote,
            )
        if allowed:
            add(
                "fake_profile_quote",
                status=Status.MET,
                reason="档案声明具备完全满足这条要求的能力。",
                profile_fields=(allowed[0],),
                profile_quote="具备完全满足这条要求的能力和经验",
            )
        add(
            "invented_number",
            status=Status.UNKNOWN,
            reason="档案没有说明能否安排987名人员。",
            gap="需要补充人员安排。",
        )
        flipped = Status.UNMET if gold.status is Status.MET else Status.MET
        if filled and not gold.correct(flipped):
            quote = _first_clause(fields[filled[0]])
            verdict = "满足" if flipped is Status.MET else "不满足"
            add(
                "flipped",
                status=flipped,
                reason=f"档案写明“{quote}”，因此{verdict}这条要求。",
                profile_fields=(filled[0],),
                profile_quote=quote,
            )
        if gold.status in (Status.MET, Status.UNMET) and gold.profile_quote:
            owner = next((name for name in filled if gold.profile_quote in fields[name]), None)
            if owner is not None:
                add(
                    "clean",
                    clean=True,
                    status=gold.status,
                    reason=f"档案写明“{gold.profile_quote}”。",
                    profile_fields=(owner,),
                    profile_quote=gold.profile_quote,
                )
    return items


def collect_verify(case: Case, setup: Setup) -> tuple[list[metrics.Injected], list[VerifyResult]]:
    chunks = chunk_map(document_chunks(case.document))
    injected = inject(case)
    results = []
    for index, item in enumerate(injected):
        cited = [chunks[cid] for cid in item.requirement.chunk_ids]
        try:
            result = verify_step.verify(
                item.finding,
                item.requirement,
                cited,
                case.profile,
                setup.llm,
                call_key=f"eval:{case.id}:verify:{index}",
                attempt=1,
            )
        except ModelDisabled:
            # 基线：程序检查通过了，但没有模型去核。按"没拦下"算，这正是模型核验要补上的部分。
            result = VerifyResult(
                ok=True, finding=item.finding.model_copy(update={"verification": Verification(verdict="skipped")})
            )
        results.append(result)
    return injected, results


def collect_all(case: Case, setup: Setup) -> Report:
    chunks = list(document_chunks(case.document))
    return setup.pipeline().run(ResearchInputs(run_id=f"eval:{case.id}", chunks=chunks, profile=case.profile))


# ---- 各步骤：打分 ----


def _scored(fn: Callable[[], Any]) -> tuple[Any, str | None]:
    try:
        return fn(), None
    except NotImplementedError as todo:
        return None, str(todo)


def end_to_end_findings(case: Case, report: Report) -> list[Finding | None]:
    """端到端：标准结论 → 对上的那条预测要求 → 它的结论。没抽出来的要求，结论是 None。"""
    obligations = [r for r in report.requirements if r.kind is Kind.OBLIGATION]
    alignment = metrics.align([g.quote for g in case.findings], obligations, chunk_map(document_chunks(case.document)))
    by_requirement = {f.requirement_id: f for f in report.findings}
    matched = dict(alignment.pairs)
    return [by_requirement.get(obligations[matched[i]].id) if i in matched else None for i in range(len(case.findings))]


def evaluate(case: Case, step: str, setup: Setup) -> dict[str, Any]:
    chunks = chunk_map(document_chunks(case.document))
    started = time.monotonic()
    result: dict[str, Any] = {"case": case.id, "step": step, "mode": setup.mode}
    todo: list[str] = []
    if step == "extract":
        extracted = collect_extract(case, setup)
        result["predicted"] = len(extracted.requirements)
        result["citation_validity"] = metrics.citation_validity(extracted.requirements, chunks)
        result["scores"], note = _scored(
            lambda: metrics.extraction_scores(case.document, extracted.requirements, chunks)
        )
        todo.append(note)
    elif step == "match":
        run = collect_match(case, setup)
        result["first"], note = _scored(lambda: metrics.match_scores(case.findings, run.first))
        result["final"], _ = _scored(lambda: metrics.match_scores(case.findings, run.final))
        result["statuses"] = [f.status.value for f in run.final]
        todo.append(note)
    elif step == "verify":
        injected, results = collect_verify(case, setup)
        result["scores"] = metrics.verify_scores(injected, results)
    else:
        report = collect_all(case, setup)
        result["citation_validity"] = metrics.citation_validity(report.requirements, chunks)
        result["extract"], note = _scored(lambda: metrics.extraction_scores(case.document, report.requirements, chunks))
        todo.append(note)
        result["match"], note = _scored(lambda: metrics.match_scores(case.findings, end_to_end_findings(case, report)))
        todo.append(note)
        result["predicted"] = len(report.requirements)
        result["statuses"] = [f.status.value for f in report.findings]
        result["downgraded"] = report.coverage.downgraded
    result["seconds"] = round(time.monotonic() - started, 2)
    result["todo"] = sorted({t for t in todo if t})
    return result


# ---- 报表 ----


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _lines(result: dict[str, Any]) -> list[str]:
    step, lines = result["step"], []
    if step == "extract" or step == "all":
        scores: metrics.ExtractionScores | None = result.get("scores") if step == "extract" else result.get("extract")
        if scores is not None:
            lines.append(
                f"抽取  召回 {_pct(scores.recall)}  精确 {_pct(scores.precision)}  维度 {_pct(scores.dimension_accuracy)}"
                f"  事实/要求 {_pct(scores.kind_accuracy)}  误抽模板 {scores.forbidden}"
                f"  （标准 {scores.gold}，预测 {scores.predicted}，对上 {scores.aligned}）"
            )
            lines += [f"      漏：{quote}" for quote in scores.missed]
            lines += [f"      维度错：{q}（应为 {g}，标成 {p}）" for q, g, p in scores.wrong_dimension]
        lines.append(f"引用有效率 {_pct(result['citation_validity'])}")
    if step in ("match", "all"):
        pairs = (
            [("首次匹配", result.get("first")), ("核验之后", result.get("final"))]
            if step == "match"
            else [("匹配", result.get("match"))]
        )
        for label, scores in pairs:
            if scores is None:
                continue
            lines.append(
                f"{label}  状态准确 {_pct(scores.status_accuracy)}  严重错误 {scores.severe}（{_pct(scores.severe_rate)}）"
                f"  未知占比 {_pct(scores.unknown_rate)}  缺失 {scores.missing}"
            )
            lines += [f"      严重：{quote}" for quote in scores.severe_quotes]
    if step == "verify":
        scores: metrics.VerifyScores = result["scores"]
        kinds = "  ".join(f"{k} {c}/{t}" for k, (c, t) in scores.by_kind.items())
        lines.append(f"核验  拦截 {_pct(scores.catch_rate)}（{scores.caught}/{scores.injected}）  {kinds}")
        lines.append(f"      误杀 {_pct(scores.false_rejection_rate)}（{scores.false_rejections}/{scores.clean}）")
    lines += [f"（{note}：还没实现，先只给原始结果）" for note in result["todo"]]
    if result.get("predicted") is not None and result.get("scores" if step == "extract" else "extract") is None:
        lines.append(f"      抽出 {result['predicted']} 条")
    if result.get("statuses") is not None and result.get("first" if step == "match" else "match") is None:
        lines.append(
            "      结论：" + "、".join(f"{s}×{result['statuses'].count(s)}" for s in sorted(set(result["statuses"])))
        )
    return lines


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else "→".join(k)): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description="BidRadar 研究流水线评测")
    parser.add_argument("--step", choices=STEPS, required=True)
    parser.add_argument("--llm", choices=("baseline", "replay", "live"), default="baseline")
    parser.add_argument("--enable-model", action="store_true", help="允许 --llm live 调用真实模型（会花钱）")
    parser.add_argument("--max-cost", type=float, default=2.0, help="--llm live 的费用上限，单位元")
    parser.add_argument("--case", action="append", help="只跑这些用例")
    parser.add_argument("--holdout", action="store_true", help="连保留集一起跑")
    parser.add_argument("--recordings", type=Path, default=RECORDINGS / "deepseek-chat.jsonl")
    parser.add_argument("--json", type=Path, help="把结果写进这个文件")
    args = parser.parse_args(argv)

    setup = make_setup(
        args.llm,
        enable_model=args.enable_model,
        max_cost_micro=int(args.max_cost * 1_000_000),
        recordings=args.recordings,
    )
    if setup.ledger is not None:
        print(f"会调用真实模型，费用上限 {args.max_cost:.2f} 元。录过的请求直接重放，不计费。")
    results = []
    for case in load_cases(args.case, holdout=args.holdout):
        result = evaluate(case, args.step, setup)
        results.append(result)
        print(f"== {case.id} · {args.step} · {setup.mode} · {result['seconds']}s ==")
        for line in _lines(result):
            print("  " + line)
    if setup.recorder is not None:
        recorder = setup.recorder
        print(
            f"模型调用 {recorder.calls} 次，其中重放 {recorder.replayed} 次；"
            f"按录制时的用量折算约 {recorder.cost_micro / 1_000_000:.4f} 元"
        )
    if setup.ledger is not None:
        print(f"这次实际花费 {setup.ledger.spent / 1_000_000:.4f} 元")
    if args.json:
        args.json.write_text(json.dumps(_jsonable(results), ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

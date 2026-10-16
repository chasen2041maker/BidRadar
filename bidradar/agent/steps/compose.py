"""⑤ 汇总：把通过核验的结论拼成报告。程序为主，摘要可选用模型。

项目事实      kind=fact 的要求，按金额角色和日期分行，每行带原文
要求对照表    按严重程度排序：不满足 → 冲突 → 未知 → 满足 → 不适用
待核查清单    从"未知"结论的 gap 和"投标材料"维度的要求生成，程序去重。**不让模型写**：
              旧版模型写清单时常常改写、扩大原意
覆盖说明      读了哪些文件、多少片段；哪些章节作为模板跳过；抽取出过什么问题；几条结论被降级
摘要          默认用模板生成。开启模型摘要时，程序检查摘要里每个数字都出现在被引用的原文里，
              检查不过就退回模板摘要
"""

import json

from pydantic import BaseModel, ConfigDict

from bidradar.agent import prompts
from bidradar.agent.checks import forbidden_claims, squash, unsupported_numbers
from bidradar.agent.dimensions import Dimension, spec
from bidradar.agent.llm import InvalidModelOutput, LLMClient, LLMRequest, parse_json
from bidradar.agent.schemas import (
    ChecklistItem,
    CitedChunk,
    Coverage,
    ExtractResult,
    FactLine,
    Finding,
    Kind,
    Report,
    Requirement,
    Status,
    Summary,
)
from bidradar.agent.steps.extract import MONEY_LABEL_NAMES
from bidradar.agent.steps.parse import Material

SEVERITY = {Status.UNMET: 0, Status.CONFLICTING: 1, Status.UNKNOWN: 2, Status.MET: 3, Status.NOT_APPLICABLE: 4}
STATUS_NAMES = {
    Status.MET: "满足",
    Status.UNMET: "明确不满足",
    Status.UNKNOWN: "待核查",
    Status.CONFLICTING: "资料冲突",
    Status.NOT_APPLICABLE: "不适用",
}


def _fact_label(requirement: Requirement) -> str:
    if requirement.money_role is not None:
        return MONEY_LABEL_NAMES.get(requirement.money_role, "金额")
    if "截止" in requirement.quote:
        return "截止时间"
    if "获取" in requirement.quote:
        return "获取文件"
    return "项目信息"


def _checklist(requirements: list[Requirement], findings: list[Finding]) -> list[ChecklistItem]:
    items: dict[str, ChecklistItem] = {}
    for finding in findings:
        if finding.status is Status.UNKNOWN and finding.gap:
            key = squash(finding.gap)
            previous = items.get(key)
            ids = (previous.requirement_ids if previous else ()) + (finding.requirement_id,)
            items[key] = ChecklistItem(text=finding.gap, requirement_ids=ids, origin="gap")
    for requirement in requirements:
        if requirement.kind is Kind.OBLIGATION and requirement.dimension is Dimension.MATERIALS:
            text = "准备投标材料：" + requirement.quote
            items.setdefault(
                squash(text), ChecklistItem(text=text, requirement_ids=(requirement.id,), origin="materials")
            )
    # 同一个缺口对应的要求越多，越靠前。
    return sorted(items.values(), key=lambda item: -len(item.requirement_ids)) if items else []


def template_summary(requirements: list[Requirement], findings: list[Finding]) -> str:
    by_id = {r.id: r for r in requirements}
    counts = {status: sum(f.status is status for f in findings) for status in Status}
    parts = [f"共识别 {len(findings)} 条投标要求"]
    if counts[Status.UNMET]:
        unmet = [by_id[f.requirement_id] for f in findings if f.status is Status.UNMET]
        parts.append(f"明确不满足 {counts[Status.UNMET]} 条（{spec(unmet[0].dimension).label}等）")
    if counts[Status.CONFLICTING]:
        parts.append(f"资料冲突 {counts[Status.CONFLICTING]} 条")
    parts.append(f"待核查 {counts[Status.UNKNOWN]} 条，满足 {counts[Status.MET]} 条")
    return "，".join(parts) + "。结论只依据已读取的文件和企业档案，资格类要求需要结合企业证明逐项核对。"


class _Summary(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str


def model_summary(
    requirements: list[Requirement], findings: list[Finding], llm: LLMClient, call_key: str
) -> str | None:
    """让模型写摘要。不合格（数字没有出处、出现不允许的断言、太长）返回 None，由调用方退回模板。"""
    by_id = {r.id: r for r in requirements}
    payload = {
        "findings": [
            {"requirement": by_id[f.requirement_id].quote, "status": STATUS_NAMES[f.status], "reason": f.reason}
            for f in findings
        ]
    }
    request = LLMRequest(
        purpose="summary",
        call_key=call_key,
        system=prompts.load("summary").text,
        user=json.dumps(payload, ensure_ascii=False),
        max_output_tokens=800,
    )
    try:
        text = parse_json(llm.complete(request), _Summary).summary.strip()
    except InvalidModelOutput:
        return None
    sources = [by_id[f.requirement_id].quote for f in findings] + [f.reason for f in findings]
    if not text or len(text) > 600 or unsupported_numbers(text, sources) or forbidden_claims(text):
        return None
    return text


def compose(
    *,
    mode: str,
    extracted: ExtractResult,
    findings: list[Finding],
    material: Material,
    versions: dict[str, str],
    summary_text: str | None = None,
) -> Report:
    requirements = list(extracted.requirements)
    ordered = sorted(findings, key=lambda f: SEVERITY[f.status])
    facts = [
        FactLine(
            requirement_id=r.id, label=_fact_label(r), quote=r.quote, chunk_ids=r.chunk_ids, money_role=r.money_role
        )
        for r in requirements
        if r.kind is Kind.FACT
    ]
    cited = dict.fromkeys(cid for r in requirements for cid in r.chunk_ids)
    chunks = [
        CitedChunk(id=c.id, document_id=c.document_id, page=c.page, section_path=c.section_path, text=c.text)
        for cid in cited
        if (c := material.by_id.get(cid)) is not None
    ]
    coverage = Coverage(
        documents=tuple(dict.fromkeys(c.document_id for c in material.chunks)),
        chunk_count=len(material.chunks),
        batches=extracted.batches,
        skipped_sections=extracted.skipped_sections,
        extraction_issues=extracted.issues,
        recall_added=extracted.recall_added,
        downgraded=sum(1 for f in findings if f.verification and f.verification.downgraded),
    )
    summary = (
        Summary(text=summary_text, source="model")
        if summary_text
        else Summary(text=template_summary(requirements, ordered), source="template")
    )
    return Report(
        mode=mode,
        facts=tuple(facts),
        requirements=tuple(requirements),
        findings=tuple(ordered),
        checklist=tuple(_checklist(requirements, ordered)),
        summary=summary,
        coverage=coverage,
        chunks=tuple(chunks),
        versions=versions,
    )

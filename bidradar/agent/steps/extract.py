"""② 抽取要求：按章节分批，让模型找出要求和项目事实，每条都是原文摘录。

一次调用只看一个章节（太长再按字数切）。批次之间不共享上下文，所以可以并行，也可以单独重做。

程序检查每一条（不合格的打回）：
    - quote 规范空白后是所引片段的子串（模型改一个字都过不去）
    - 维度、类别、金额角色是合法的枚举值
    - 有金额角色时，摘录里真的有金额
不合格的条目带着原因再问一次（只问这一批、只附打回的条目），还不行就丢掉，记进覆盖说明。
不会因为一两条错误重抽整份文件。

漏检复查（召回率的安全网）：程序用规则找"像要求的句子"（含"须、应、不得……"、金额、日期）。
某个片段被规则命中、但没有任何要求引用它，就把这些片段收集起来，专门再问一次"有没有漏掉的"。

没有开启模型时（基线模式），rule_extract() 只用规则抽取，作为可以离线演示、可以对照的基线。
"""

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from bidradar.agent import prompts
from bidradar.agent.checks import MONEY_LABELS, money_roles, quote_in, squash
from bidradar.agent.dimensions import Dimension, describe_for_prompt
from bidradar.agent.llm import InvalidModelOutput, LLMClient, LLMRequest, parse_json
from bidradar.agent.schemas import Category, Chunk, ExtractionIssue, Kind, MoneyRole, Requirement
from bidradar.agent.steps.parse import Material, is_template

BATCH_CHARS = 6000
MAX_ITEMS_PER_BATCH = 40


@dataclass(frozen=True)
class Batch:
    section: tuple[str, ...]
    chunks: tuple[Chunk, ...]

    @property
    def key(self) -> str:
        """批次的稳定标识：由片段 ID 决定。同一份材料重跑，批次划分和标识都不变。"""
        return hashlib.sha256("\n".join(c.id for c in self.chunks).encode()).hexdigest()[:16]


def batches(material: Material, max_chars: int = BATCH_CHARS) -> tuple[list[Batch], list[tuple[str, ...]]]:
    """按章节（前两级标题）分批，模板章节跳过。返回 (批次, 跳过的章节)。"""
    groups: dict[tuple[str, ...], list[Chunk]] = {}
    skipped: list[tuple[str, ...]] = []
    for chunk in material.chunks:
        if chunk.kind == "heading":
            continue
        if is_template(chunk.section_path):
            if chunk.section_path[:2] not in skipped:
                skipped.append(chunk.section_path[:2])
            continue
        groups.setdefault(chunk.section_path[:2], []).append(chunk)
    result = []
    for section, chunks in groups.items():
        current: list[Chunk] = []
        size = 0
        for chunk in chunks:
            if current and size + len(chunk.text) > max_chars:
                result.append(Batch(section, tuple(current)))
                current, size = [], 0
            current.append(chunk)
            size += len(chunk.text)
        if current:
            result.append(Batch(section, tuple(current)))
    return result, skipped


# ---- 模型输出的结构（宽松接收，再逐条严格检查） ----


class _Item(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: str
    category: str
    dimension: str
    quote: str
    refs: list[str]
    money_role: str | None = None
    lot: str | None = None
    conditions: str | None = None


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[_Item]


def requirement_id(quote: str) -> str:
    """同一条要求（规范化后的摘录相同）不管被抽到几次，都是同一个 ID。"""
    return hashlib.sha256(squash(quote).encode()).hexdigest()[:32]


def _check_item(item: _Item, allowed: dict[str, Chunk], source: str) -> Requirement | str:
    """把一条模型输出变成 Requirement；不合格就返回错误码。"""
    chunks = [allowed[ref] for ref in item.refs if ref in allowed]
    if not item.refs or len(chunks) != len(item.refs) or len(chunks) > 3:
        return "unknown_ref"
    if not quote_in(item.quote, [c.text for c in chunks]):
        return "quote_not_in_chunk"
    try:
        kind, category, dimension = Kind(item.kind), Category(item.category), Dimension(item.dimension)
        role = MoneyRole(item.money_role) if item.money_role else None
    except ValueError:
        return "invalid_enum"
    if role is not None and not re.search(r"\d", item.quote):
        role = None  # 说是金额，摘录里却没有数字：去掉金额角色，不整条打回
    if kind is Kind.FACT:
        dimension = Dimension.FACT
    return Requirement(
        id=requirement_id(item.quote),
        kind=kind,
        category=category,
        dimension=dimension,
        quote=item.quote.strip(),
        chunk_ids=tuple(c.id for c in chunks),
        money_role=role,
        lot=item.lot,
        conditions=item.conditions,
        source=source,
    )


def _request(purpose: str, call_key: str, payload: dict) -> LLMRequest:
    prompt = prompts.load("extract_recall" if purpose == "extract_recall" else "extract")
    return LLMRequest(
        purpose=purpose,
        call_key=call_key,
        system=prompt.text,
        user=json.dumps(payload, ensure_ascii=False),
        max_output_tokens=4096,
    )


def extract_batch(
    batch: Batch, material: Material, llm: LLMClient, call_key: str
) -> tuple[list[Requirement], list[ExtractionIssue]]:
    """抽取一个批次。返回合格的要求和问题记录。"""
    allowed = {material.alias(c.id): c for c in batch.chunks}
    payload = {
        "section_path": list(batch.section),
        "chunks": [material.llm_view(c) for c in batch.chunks],
        "dimensions": describe_for_prompt(),
    }
    try:
        output = parse_json(llm.complete(_request("extract", call_key, payload)), _Output)
    except InvalidModelOutput:
        return [], [ExtractionIssue(section=batch.section, code="invalid_model_output")]
    good, bad = _sort(output.items[:MAX_ITEMS_PER_BATCH], allowed, "model")
    issues: list[ExtractionIssue] = []
    if bad:
        retry = {
            **payload,
            "rejected": [{"item": item.model_dump(), "reason": reason} for item, reason in bad],
            "instruction": "上面这些条目没有通过检查。请只修正它们（quote 必须原样复制自 refs 所指的片段），重新输出。",
        }
        try:
            fixed = parse_json(llm.complete(_request("extract_retry", call_key + ":retry", retry)), _Output)
            more, still_bad = _sort(fixed.items[: len(bad) * 2], allowed, "model")
            good += more
        except InvalidModelOutput:
            still_bad = bad
        issues = [
            ExtractionIssue(section=batch.section, code=reason, detail=item.quote[:200]) for item, reason in still_bad
        ]
    return good, issues


def _sort(
    items: list[_Item], allowed: dict[str, Chunk], source: str
) -> tuple[list[Requirement], list[tuple[_Item, str]]]:
    good, bad = [], []
    for item in items:
        checked = _check_item(item, allowed, source)
        if isinstance(checked, Requirement):
            good.append(checked)
        else:
            bad.append((item, checked))
    return good, bad


# ---- 规则：漏检复查的候选，以及基线模式的抽取 ----

# "应"要排除"供应商""响应""相应"这类词里的"应"，不然几乎每一句都会被当成要求。
MODAL = re.compile(r"必须|须|应当|(?<![供响相反适回对答])应(?![商答])|不得|不少于|不低于|不超过|至少|≥|≤")
DATE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}-\d{1,2}-\d{1,2}")
MONEY = re.compile(r"\d+(?:\.\d+)?\s*(?:万元|元)")


def looks_like_requirement(text: str) -> bool:
    """像不像要求：有"须、应、不得……"、金额、日期，或者任何一个维度的关键词（付款、驻场、工期……）。"""
    return bool(
        MODAL.search(text)
        or MONEY.search(text)
        or DATE.search(text)
        or any(pattern.search(text) for pattern, _, _ in KEYWORD_DIMENSIONS)
    )


def recall_candidates(
    material: Material, requirements: list[Requirement], extracted_batches: list[Batch]
) -> list[Chunk]:
    """被规则命中、但没有任何要求引用的片段。只看参与过抽取的片段（模板章节不算）。"""
    cited = {cid for r in requirements for cid in r.chunk_ids}
    return [
        c for batch in extracted_batches for c in batch.chunks if c.id not in cited and looks_like_requirement(c.text)
    ]


def extract_recall(chunks: list[Chunk], material: Material, llm: LLMClient, call_key: str) -> list[Requirement]:
    """漏检复查：只把漏掉的片段发给模型。不合格的条目直接丢掉，不再重问（这是补漏，不是主路径）。"""
    if not chunks:
        return []
    allowed = {material.alias(c.id): c for c in chunks}
    payload = {"chunks": [material.llm_view(c) for c in chunks], "dimensions": describe_for_prompt()}
    try:
        output = parse_json(llm.complete(_request("extract_recall", call_key, payload)), _Output)
    except InvalidModelOutput:
        return []
    good, _ = _sort(output.items[:MAX_ITEMS_PER_BATCH], allowed, "recall")
    return good


def merge(requirements: list[Requirement]) -> list[Requirement]:
    """同一条要求被不同批次抽到：合并成一条，引用片段取并集（最多 3 个）。保持第一次出现的顺序。"""
    merged: dict[str, Requirement] = {}
    for requirement in requirements:
        existing = merged.get(requirement.id)
        if existing is None:
            merged[requirement.id] = requirement
        else:
            ids = tuple(dict.fromkeys(existing.chunk_ids + requirement.chunk_ids))[:3]
            merged[requirement.id] = existing.model_copy(update={"chunk_ids": ids})
    return list(merged.values())


# 基线模式按关键词猜维度。顺序就是优先级：先认驻场，再认工期，避免"60日内完成"被当成驻场。
KEYWORD_DIMENSIONS: list[tuple[re.Pattern[str], Dimension, Category]] = [
    (re.compile(r"驻场|现场服务|常驻|派驻"), Dimension.ONSITE, Category.DELIVERY),
    (re.compile(r"(日|天)内完成|工期|交付期|完成.*(开发|部署|上线)"), Dimension.SCHEDULE, Category.DELIVERY),
    (re.compile(r"付款|支付|结算|履约保证金|质保期"), Dimension.COMMERCIAL, Category.COMMERCIAL),
    (re.compile(r"评分|得\s*\d+\s*分|分值"), Dimension.SCORING, Category.SCORING),
    (re.compile(r"业绩|类似项目|案例"), Dimension.EXPERIENCE, Category.QUALIFICATION),
    (re.compile(r"项目经理|工程师|人员|社保"), Dimension.STAFFING, Category.DELIVERY),
    (re.compile(r"服务地点|交付地点|实施地点"), Dimension.LOCATION, Category.DELIVERY),
    (re.compile(r"资格|资质|违法记录|民事责任|联合体|控股|信用"), Dimension.QUALIFICATION, Category.QUALIFICATION),
    (re.compile(r"须提供|提交.*(证明|复印件|副本)|加盖公章"), Dimension.MATERIALS, Category.MATERIALS),
]
SENTENCE = re.compile(r"[^。；;！!？?\n]+[。；;！!？?]?")


def _rule_requirement(sentence: str, chunk: Chunk) -> Requirement | None:
    text = sentence.strip()
    if len(text) < 6:
        return None
    roles = money_roles(text)
    if roles and MONEY.search(text):
        role = sorted(roles)[0] if len(roles) == 1 else None
        return Requirement(
            id=requirement_id(text),
            kind=Kind.FACT,
            category=Category.BUDGET,
            dimension=Dimension.FACT,
            quote=text,
            chunk_ids=(chunk.id,),
            money_role=role,
            source="rule",
        )
    if DATE.search(text) and re.search(r"截止|获取|开标|时间", text):
        return Requirement(
            id=requirement_id(text),
            kind=Kind.FACT,
            category=Category.DEADLINE,
            dimension=Dimension.FACT,
            quote=text,
            chunk_ids=(chunk.id,),
            source="rule",
        )
    for pattern, dimension, category in KEYWORD_DIMENSIONS:
        if pattern.search(text):
            break
    else:
        if not MODAL.search(text):
            return None  # 既没有"须、应、不得"，也没有任何维度的关键词：不是要求
        dimension, category = Dimension.CAPABILITY, Category.TECHNICAL
    return Requirement(
        id=requirement_id(text),
        kind=Kind.OBLIGATION,
        category=category,
        dimension=dimension,
        quote=text,
        chunk_ids=(chunk.id,),
        source="rule",
    )


def rule_extract(extracted_batches: list[Batch]) -> list[Requirement]:
    """基线模式：只用规则，一句一条。召回率高、精度低，维度靠关键词猜，用来离线演示和对照。"""
    found = []
    for batch in extracted_batches:
        for chunk in batch.chunks:
            # 表格行（"评分项：类似业绩；分值：15；……"）是一个整体，不按分号切。
            sentences = [chunk.text] if chunk.kind == "table_row" else SENTENCE.findall(chunk.text)
            for sentence in sentences:
                requirement = _rule_requirement(sentence, chunk)
                if requirement is not None:
                    found.append(requirement)
    return merge(found)


# 金额标签全集，给 compose 用来写"项目事实"的标签
MONEY_LABEL_NAMES = {role: label for label, role in reversed(list(MONEY_LABELS.items()))}

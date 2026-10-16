"""流水线的数据结构：Chunk（片段）、Requirement（一条要求）、Finding（一条结论）、Report（报告）等。

全部用 Pydantic 定义，一份定义三个用途：代码里的类型、步骤结果落库时的 JSON 结构、校验模型输出。
字段的含义见 docs/agent-design.md 第 2 节。所有对象都不可变（frozen），一步的输出不会被下一步悄悄改掉。
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from bidradar.agent.dimensions import Dimension


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Status(StrEnum):
    MET = "met"  # 原文有明确要求，档案有对应依据表明满足
    UNMET = "unmet"  # 原文有明确要求，档案明确表明不满足
    UNKNOWN = "unknown"  # 缺证据、证明未核验、可能不满足但不确定，都算这一类
    CONFLICTING = "conflicting"  # 资料对同一件事给出了互相矛盾的说法
    NOT_APPLICABLE = "not_applicable"  # 原文明确说某个条件不适用


class Kind(StrEnum):
    OBLIGATION = "obligation"  # 企业要满足的要求
    FACT = "fact"  # 项目事实：预算、截止时间……不存在满足与否


class Category(StrEnum):
    TECHNICAL = "technical"
    QUALIFICATION = "qualification"
    DELIVERY = "delivery"
    COMMERCIAL = "commercial"
    MATERIALS = "materials"
    SCORING = "scoring"
    BUDGET = "budget"
    DEADLINE = "deadline"
    OTHER = "other"


class MoneyRole(StrEnum):
    BUDGET = "budget"
    MAX_PRICE = "max_price"
    FILE_FEE = "file_fee"
    DEPOSIT = "deposit"
    PERFORMANCE_BOND = "performance_bond"
    UNIT_PRICE = "unit_price"
    PACKAGE_BUDGET = "package_budget"


class Chunk(Frozen):
    """一段原文（只做过空白规范化）。发给模型之前，联系方式会被替换掉（checks.redact），这里保存的仍是原文。"""

    id: str
    document_id: str
    seq: int
    page: int | None
    section_path: tuple[str, ...]
    kind: str
    text: str


class ProfileSnapshot(Frozen):
    """研究时固定下来的那一版企业档案。fields 里为 None 表示"还没填"，不表示"没有"。"""

    version: int
    fields: dict[str, str | None]
    proof_status: str = "not_provided"


class ProfileSlice(Frozen):
    """切给匹配步骤的档案：只含这条要求的维度允许看的字段。"""

    version: int
    fields: dict[str, str | None]

    @property
    def empty(self) -> bool:
        return all(not v for v in self.fields.values())


class Requirement(Frozen):
    id: str
    kind: Kind
    category: Category
    dimension: Dimension
    quote: str = Field(min_length=1, max_length=2000)
    chunk_ids: tuple[str, ...] = Field(min_length=1, max_length=3)
    money_role: MoneyRole | None = None
    lot: str | None = None
    conditions: str | None = None
    # 是谁抽出来的：模型、规则（模型没开时的基线）、漏检复查
    source: Literal["model", "rule", "recall"] = "model"


class Verification(Frozen):
    verdict: Literal["supported", "unsupported", "uncertain", "skipped"]
    checks_failed: tuple[str, ...] = ()
    judge_note: str | None = None
    attempts: int = 1
    downgraded: bool = False


class Finding(Frozen):
    requirement_id: str
    status: Status
    reason: str = Field(min_length=1, max_length=1200)
    profile_fields: tuple[str, ...] = ()
    # met / unmet 必填：档案里支撑这个判断的原话，程序会核对它真的是所列档案字段的子串
    profile_quote: str | None = None
    # unknown 必填：还缺什么才能判断。待核查清单就是从这里生成的
    gap: str | None = None
    basis: Literal["rule", "model", "placeholder"] = "model"
    rule_id: str | None = None
    verification: Verification | None = None


class FactLine(Frozen):
    requirement_id: str
    label: str
    quote: str
    chunk_ids: tuple[str, ...]
    money_role: MoneyRole | None = None


class ChecklistItem(Frozen):
    text: str
    requirement_ids: tuple[str, ...]
    origin: Literal["gap", "materials"]


class Summary(Frozen):
    text: str
    source: Literal["template", "model"]


class ExtractionIssue(Frozen):
    section: tuple[str, ...]
    code: str
    detail: str | None = None


class Coverage(Frozen):
    documents: tuple[str, ...]
    chunk_count: int
    batches: int
    skipped_sections: tuple[tuple[str, ...], ...] = ()
    extraction_issues: tuple[ExtractionIssue, ...] = ()
    recall_added: int = 0
    downgraded: int = 0


class CitedChunk(Frozen):
    """报告里引用到的片段原文。报告自带这些原文，以后文件重新解析了，报告照样看得懂。"""

    id: str
    document_id: str
    page: int | None
    section_path: tuple[str, ...]
    text: str


class Report(Frozen):
    mode: Literal["agent", "baseline"]
    facts: tuple[FactLine, ...]
    requirements: tuple[Requirement, ...]
    findings: tuple[Finding, ...]
    checklist: tuple[ChecklistItem, ...]
    summary: Summary
    coverage: Coverage
    chunks: tuple[CitedChunk, ...]
    versions: dict[str, str]


class ExtractResult(Frozen):
    requirements: tuple[Requirement, ...]
    issues: tuple[ExtractionIssue, ...] = ()
    skipped_sections: tuple[tuple[str, ...], ...] = ()
    batches: int = 0
    recall_added: int = 0


class VerifyResult(Frozen):
    ok: bool
    finding: Finding
    feedback: str | None = None

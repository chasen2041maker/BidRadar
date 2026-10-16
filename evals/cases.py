"""评测用例的读取：标准答案（gold）、虚构档案、招标文件的切片。

一个用例 = 一份招标文件的标准要求（documents/*.yaml，和公司无关）+ 一家虚构公司的档案（profiles/*.yaml）
+ 这家公司对每条要求应得的结论（cases/*.yaml）。同一份文件配不同公司，要求只标一次。
"""

import hashlib
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import yaml

from bidradar.agent.checks import squash
from bidradar.agent.dimensions import PROFILE_FIELDS, Dimension
from bidradar.agent.schemas import Category, Chunk, Kind, MoneyRole, ProfileSnapshot, Requirement, Status
from bidradar.agent.steps.extract import requirement_id
from bidradar.modules.documents.models import chunk_blocks, detect_type
from bidradar.modules.documents.parsers import parse

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent

# 标准答案里只标维度，类别按维度推出来（类别只影响报告里的分组，不影响匹配）。
CATEGORY_OF = {
    Dimension.QUALIFICATION: Category.QUALIFICATION,
    Dimension.CAPABILITY: Category.TECHNICAL,
    Dimension.EXPERIENCE: Category.QUALIFICATION,
    Dimension.SCHEDULE: Category.DELIVERY,
    Dimension.ONSITE: Category.DELIVERY,
    Dimension.LOCATION: Category.DELIVERY,
    Dimension.STAFFING: Category.QUALIFICATION,
    Dimension.COMMERCIAL: Category.COMMERCIAL,
    Dimension.MATERIALS: Category.MATERIALS,
    Dimension.SCORING: Category.SCORING,
    Dimension.FACT: Category.OTHER,
    Dimension.OTHER: Category.OTHER,
}


@dataclass(frozen=True)
class GoldRequirement:
    quote: str
    kind: Kind
    dimension: Dimension
    money_role: MoneyRole | None = None


@dataclass(frozen=True)
class GoldDocument:
    id: str
    files: tuple[Path, ...]
    requirements: tuple[GoldRequirement, ...]
    optional: tuple[str, ...] = ()
    not_requirements: tuple[str, ...] = ()


@dataclass(frozen=True)
class GoldFinding:
    quote: str  # 对应 GoldDocument.requirements 里的哪一条
    status: Status
    acceptable: frozenset[Status] = frozenset()
    must_not: frozenset[Status] = frozenset()
    profile_quote: str | None = None
    note: str | None = None

    def correct(self, status: Status) -> bool:
        return status is self.status or status in self.acceptable

    def severe(self, status: Status) -> bool:
        return status in self.must_not


@dataclass(frozen=True)
class Case:
    id: str
    document: GoldDocument
    profile: ProfileSnapshot
    findings: tuple[GoldFinding, ...]
    holdout: bool = False
    labeled_by: str = ""
    path: Path = field(default=HERE)

    def gold_requirement(self, quote: str) -> GoldRequirement:
        return next(r for r in self.document.requirements if r.quote == quote)


def default_must_not(status: Status, acceptable: frozenset[Status]) -> frozenset[Status]:
    """不写 must_not 时的严重错误：标准是未知却下了结论；标准是满足却判不满足，反过来也一样。"""
    if status is Status.UNKNOWN:
        severe = {Status.MET, Status.UNMET}
    elif status is Status.MET:
        severe = {Status.UNMET}
    elif status is Status.UNMET:
        severe = {Status.MET}
    else:
        severe = set()
    return frozenset(severe - acceptable)


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@cache
def load_document(path: Path) -> GoldDocument:
    data = _load_yaml(path)
    return GoldDocument(
        id=data["id"],
        files=tuple(REPO_ROOT / f for f in data["files"]),
        requirements=tuple(
            GoldRequirement(
                quote=item["quote"],
                kind=Kind(item["kind"]),
                dimension=Dimension(item["dimension"]),
                money_role=MoneyRole(item["money_role"]) if item.get("money_role") else None,
            )
            for item in data["requirements"]
        ),
        optional=tuple(data.get("optional") or ()),
        not_requirements=tuple(data.get("not_requirements") or ()),
    )


def load_profile(path: Path) -> ProfileSnapshot:
    data = _load_yaml(path)
    if set(data) != set(PROFILE_FIELDS):
        raise ValueError(f"{path.name}: 档案字段要和 dimensions.PROFILE_FIELDS 完全一致")
    return ProfileSnapshot(version=1, fields=data)


def load_case(path: Path) -> Case:
    data = _load_yaml(path)
    findings = []
    for item in data["findings"]:
        status = Status(item["status"])
        acceptable = frozenset(Status(s) for s in item.get("acceptable") or ())
        must_not = (
            frozenset(Status(s) for s in item["must_not"])
            if "must_not" in item
            else default_must_not(status, acceptable)
        )
        findings.append(
            GoldFinding(
                quote=item["quote"],
                status=status,
                acceptable=acceptable,
                must_not=must_not,
                profile_quote=item.get("profile_quote"),
                note=item.get("note"),
            )
        )
    return Case(
        id=data["id"],
        document=load_document(HERE / data["document"]),
        profile=load_profile(HERE / data["profile"]),
        findings=tuple(findings),
        holdout=bool(data.get("holdout")),
        labeled_by=data.get("labeled_by", ""),
        path=path,
    )


def load_cases(ids: list[str] | None = None, *, holdout: bool = False) -> list[Case]:
    """读全部用例。保留集（holdout: true）默认不读：只在阶段结束时跑，平时调提示词别看它。"""
    cases = [load_case(p) for p in sorted((HERE / "cases").glob("*.yaml"))]
    if ids:
        unknown = set(ids) - {c.id for c in cases}
        if unknown:
            raise ValueError(f"没有这些用例：{sorted(unknown)}")
        return [c for c in cases if c.id in ids]
    return [c for c in cases if holdout or not c.holdout]


# ---- 材料 ----


@cache
def document_chunks(document: GoldDocument) -> tuple[Chunk, ...]:
    """招标文件切片，和线上的 documents 模块用同一套解析和切片规则。"""
    chunks: list[Chunk] = []
    for path in document.files:
        data = path.read_bytes()
        drafts = chunk_blocks(parse(detect_type(path.name, data), data), hashlib.sha256(data).hexdigest())
        chunks += [
            Chunk(
                id=d.id,
                document_id=path.stem,
                seq=d.seq,
                page=d.page,
                section_path=d.section_path,
                kind=d.kind,
                text=d.text,
            )
            for d in drafts
        ]
    return tuple(chunks)


def chunks_containing(quote: str, chunks: tuple[Chunk, ...]) -> list[Chunk]:
    target = squash(quote)
    return [c for c in chunks if target in squash(c.text)]


def gold_requirement(gold: GoldRequirement, chunks: tuple[Chunk, ...]) -> Requirement:
    """把一条标准要求变成流水线里的 Requirement：评匹配和核验时，上游一律用标准答案。"""
    found = chunks_containing(gold.quote, chunks)
    if not found:
        raise ValueError(f"标准要求的原文在文件里找不到：{gold.quote}")
    return Requirement(
        id=requirement_id(gold.quote),
        kind=gold.kind,
        category=CATEGORY_OF[gold.dimension],
        dimension=gold.dimension,
        quote=gold.quote,
        chunk_ids=tuple(c.id for c in found[:1]),
        money_role=gold.money_role,
        source="model",
    )

"""research 的领域规则：任务状态、公告怎么变成研究材料、费用怎么预估。

纯 Python，不 import FastAPI 或 SQLAlchemy。
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from bidradar.agent.schemas import Chunk


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_INPUT = "waiting_input"


TERMINAL = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.WAITING_INPUT}
)
JOB_RUN = "research.run"
TOPIC_RUN_FINISHED = "research.run_finished"
MAX_QUESTION = 2000
MAX_DOCUMENTS = 10
# 一家公司同时排队和执行中的研究任务最多几个。防止误操作一下子排上几十个会花钱的任务。
MAX_OPEN_RUNS = 5


def notice_chunks(observation: dict[str, Any]) -> list[Chunk]:
    """把一条公告观察里的原文证据变成研究材料（片段）。

    规范化后的公告只有分散的原文证据（字段值、金额、获取条件、技术和资格要求的原文），没有完整正文。
    每条证据原样成为一个片段，document_id 用"notice:<观察 ID>"，和上传的文件区分开。同样的原文只收一次。
    """
    observation_id = observation["observation_id"]
    entries: list[tuple[str, str, str]] = []

    def add(evidence: list[dict[str, Any]] | None) -> None:
        for entry in evidence or []:
            item = (entry.get("label") or "原文", entry.get("text") or "", entry.get("locator") or "")
            if item[1].strip() and item not in entries:
                entries.append(item)

    for fact in observation.get("facts", {}).values():
        add(fact.get("evidence"))
    extra = observation.get("evidence_fields") or {}
    for claim in extra.get("money", []):
        add(claim.get("evidence"))
    add((extra.get("acquisition_window") or {}).get("evidence"))
    for condition in extra.get("access_conditions", []):
        add(condition.get("evidence"))
    for name in ("technical_evidence", "qualification_evidence", "delivery_evidence", "category_evidence"):
        add(extra.get(name))
    add(observation.get("material_reference_evidence"))

    document_id = "notice:" + observation_id
    chunks = [
        Chunk(
            id=hashlib.sha256(f"{observation_id}\n{locator}\n{text}".encode()).hexdigest(),
            document_id=document_id,
            seq=i,
            page=None,
            section_path=("公告", label),
            kind="text",
            text=text.strip(),
        )
        for i, (label, text, locator) in enumerate(entries)
    ]
    return chunks


@dataclass(frozen=True)
class Estimate:
    """发起研究前给用户看的范围和费用。费用是按"每条都要问模型"估的上限，实际通常更少。"""

    mode: str
    documents: int
    chunks: int
    characters: int
    model_calls: int
    cost_micro: int
    run_cap_micro: int


def estimate(mode: str, chunks: list[Chunk], *, documents: int, call_cost: int, run_cap: int) -> Estimate:
    """粗估：每 6000 字一批抽取，加一次漏检复查；大约每三个片段一条要求，每条要求最多匹配 3 次、核验 3 次。"""
    characters = sum(len(c.text) for c in chunks)
    if mode == "baseline":
        calls = 0
    else:
        batches = max(1, math.ceil(characters / 6000))
        requirements = math.ceil(len(chunks) / 3)
        calls = batches + 1 + requirements * 2
    return Estimate(
        mode=mode,
        documents=documents,
        chunks=len(chunks),
        characters=characters,
        model_calls=calls,
        cost_micro=calls * call_cost,
        run_cap_micro=run_cap,
    )


@dataclass(frozen=True)
class Run:
    id: str
    workspace_id: str
    actor_id: str
    kind: str
    mode: str
    notice_id: str
    parent_run_id: str | None
    question: str | None
    status: str
    reason: str | None
    profile_version: int
    catalog_snapshot: int
    observation_ids: list[str]
    document_ids: list[str]
    config_version: str
    estimate_micro: int
    version: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class RunDetail:
    run: Run
    title: str
    simulation: bool
    report: dict[str, Any] | None
    report_kind: str | None
    # 报告的输入现在还是不是最新的：档案、公告有没有新版本。报告本身永远不改。
    freshness: dict[str, str]
    cost: dict[str, int]


@dataclass(frozen=True)
class BudgetSummary:
    total_cap: int
    workspace_cap: int
    run_cap: int
    workspace_used: int
    workspace_calls: int
    workspace_unknown: int
    price_version: str
    model_enabled: bool


@dataclass(frozen=True)
class Limits:
    """研究的开关和额度，来自配置（app/settings.py）。金额单位 micro-CNY。"""

    model_enabled: bool = False
    total_cap: int = 20_000_000  # 全部公司合计 20 元
    workspace_cap: int = 10_000_000  # 每家公司 10 元
    run_cap: int = 2_000_000  # 每个任务 2 元（设计文档第 14 节问题 5）

    @property
    def mode(self) -> str:
        return "agent" if self.model_enabled else "baseline"

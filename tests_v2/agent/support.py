"""agent 测试共用的小工具：虚构招标文件的片段、虚构档案、内存里的步骤存储、假模型的应答脚本。全部不连数据库。"""

import json
from functools import cache

from bidradar.agent.llm import LLMRequest
from bidradar.agent.schemas import Chunk, ProfileSnapshot
from bidradar.modules.documents.models import FileType, chunk_blocks
from bidradar.modules.documents.parsers import parse
from tests_v2.factories import PROFILE, TENDER_PDF


@cache
def tender_chunks() -> tuple[Chunk, ...]:
    drafts = chunk_blocks(parse(FileType.PDF, TENDER_PDF.read_bytes()), "d" * 64)
    return tuple(
        Chunk(
            id=d.id, document_id="doc-1", seq=d.seq, page=d.page, section_path=d.section_path, kind=d.kind, text=d.text
        )
        for d in drafts
    )


def profile(**overrides) -> ProfileSnapshot:
    return ProfileSnapshot(version=1, fields={**PROFILE, **overrides})


class MemorySteps:
    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], dict] = {}

    def get(self, name: str, input_hash: str) -> dict | None:
        return self.saved.get((name, input_hash))

    def save(self, name: str, input_hash: str, value: dict) -> None:
        self.saved[(name, input_hash)] = value

    def names(self) -> list[str]:
        return [name for name, _ in self.saved]


class NullGuard:
    def __init__(self) -> None:
        self.checked: list[str] = []

    def check(self, step: str) -> None:
        self.checked.append(step)


# 假模型按"原文里出现了什么"决定抽出哪些要求，quote 都是原文里的原话。
KNOWN_REQUIREMENTS = [
    ("合同签订后60日内完成系统开发、部署和试运行", "obligation", "delivery", "schedule", None),
    ("中标人须安排不少于2名工程师驻场服务，驻场时间不少于30个工作日", "obligation", "delivery", "onsite", None),
    ("平台应支持私有化部署", "obligation", "technical", "capability", None),
    ("本项目不接受联合体投标", "obligation", "qualification", "qualification", None),
    ("预算金额：48万元", "fact", "budget", "fact", "budget"),
    ("最高限价：48万元", "fact", "budget", "fact", "max_price"),
    ("提交投标文件截止时间：2026年10月29日09:30（北京时间）", "fact", "deadline", "fact", None),
]


def extract_responder(request: LLMRequest) -> dict:
    payload = json.loads(request.user)
    items = []
    for chunk in payload.get("chunks", []):
        for quote, kind, category, dimension, role in KNOWN_REQUIREMENTS:
            if quote in chunk["text"]:
                items.append(
                    {
                        "kind": kind,
                        "category": category,
                        "dimension": dimension,
                        "quote": quote,
                        "refs": [chunk["ref"]],
                        "money_role": role,
                    }
                )
    return {"items": items}


def responder(**by_purpose):
    """按用途分派应答。没给的用途：抽取用 extract_responder，其余返回"supported"。"""

    def respond(request: LLMRequest):
        if request.purpose in by_purpose:
            value = by_purpose[request.purpose]
            return value(request) if callable(value) else value
        if request.purpose in ("extract", "extract_retry", "extract_recall"):
            return extract_responder(request) if request.purpose != "extract_recall" else {"items": []}
        return {"verdict": "supported", "note": "原文和档案能推出这个结论"}

    return respond

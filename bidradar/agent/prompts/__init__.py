"""提示词文件的读取。用哪个版本写在 CURRENT 里；改版本要跑评测（见 README.md）。"""

from dataclasses import dataclass
from functools import cache
from pathlib import Path

HERE = Path(__file__).parent

CURRENT = {
    "extract": "extract.v1",
    "extract_recall": "extract_recall.v1",
    "verify": "verify.v1",
    "summary": "summary.v1",
    "followup": "followup.v1",
    "answer_check": "answer_check.v1",
}


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    text: str


@cache
def load(name: str) -> Prompt:
    version = CURRENT[name]
    return Prompt(name=name, version=version, text=(HERE / f"{version}.md").read_text(encoding="utf-8"))


def all_current() -> dict[str, str]:
    """全部当前提示词的内容，参与计算配置版本。"""
    return {name: load(name).text for name in CURRENT}

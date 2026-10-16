"""① 解析：把 documents 切好的片段整理成"材料"（Material），供后面几步使用。

切片本身在 documents 模块里完成（PDF/Word → 带页码和章节路径的片段），这一步不用模型，只做三件事：
    - 给每个片段一个短别名（c1、c2……）。发给模型的是别名，程序再换回真实 ID。
      旧版要求模型逐字复制 64 位哈希 ID，经常被截短或抄错；短别名从根上消除这类错误，还省 token。
    - 发给模型之前替换掉联系方式（checks.redact）。保存的原文不变，界面上照样显示原文。
    - 标出模板章节（投标文件格式、承诺函格式……）。模板不是要求，抽取时整章跳过，覆盖说明里记一笔。
"""

import re
from dataclasses import dataclass, field

from bidradar.agent.checks import redact
from bidradar.agent.schemas import Chunk

# 章节标题里出现这些字样，就是投标文件模板，不是要求。
TEMPLATE_SECTION = re.compile(
    r"投标文件格式|响应文件格式|投标文件组成|格式[）)]?$|（格式）|\(格式\)|附件[：:]?\s*\d*\s*[^，。]*格式"
)


def is_template(section_path: tuple[str, ...]) -> bool:
    return any(TEMPLATE_SECTION.search(part) for part in section_path)


@dataclass(frozen=True)
class Material:
    chunks: tuple[Chunk, ...]
    by_id: dict[str, Chunk] = field(default_factory=dict)
    alias_of: dict[str, str] = field(default_factory=dict)
    by_alias: dict[str, Chunk] = field(default_factory=dict)

    def alias(self, chunk_id: str) -> str:
        return self.alias_of[chunk_id]

    def resolve(self, alias: str) -> Chunk | None:
        return self.by_alias.get(alias)

    def llm_view(self, chunk: Chunk) -> dict[str, object]:
        """发给模型的片段：别名、页码、去掉联系方式的原文。"""
        return {"ref": self.alias(chunk.id), "page": chunk.page, "text": redact(chunk.text)}

    def texts(self, chunk_ids: tuple[str, ...] | list[str]) -> list[str]:
        return [self.by_id[i].text for i in chunk_ids if i in self.by_id]


def prepare(chunks: list[Chunk]) -> Material:
    """按文档、序号排好，编上别名。同一批片段永远得到同样的别名，续跑时发给模型的内容不变。"""
    ordered = tuple(sorted(chunks, key=lambda c: (c.document_id, c.seq)))
    by_id = {c.id: c for c in ordered}
    alias_of = {c.id: f"c{i + 1}" for i, c in enumerate(ordered)}
    by_alias = {alias_of[c.id]: c for c in ordered}
    return Material(chunks=ordered, by_id=by_id, alias_of=alias_of, by_alias=by_alias)

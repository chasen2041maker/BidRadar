"""documents 的领域规则：能上传什么、怎么识别标题、怎么切片。

纯 Python，不 import FastAPI 或 SQLAlchemy。解析 PDF 和 Word 的代码在 parsers.py，
它把文件变成一串 Block（段落、标题、表格行）；这里把 Block 切成研究要用的 Chunk。

切片为什么重要：后面每一步的引用都指向切片。切得不好（一句话被切成两半、表格行丢了表头），
研究时摘录就对不上原文，或者一条要求的上下文缺了一半。所以切片是纯函数，规则写死、单独测试。
"""

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum

from bidradar.infra.errors import AppError

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 单个文件 20 MB，足够放下几百页的文字版招标文件
MAX_PAGES = 500
MAX_CHUNKS = 5000

# 单片的目标长度。太短，一条要求会被切散；太长，引用时定位不准，喂给模型也浪费。
CHUNK_TARGET = 600
CHUNK_MAX = 900
# 短于这个长度、又不是列表项的段落（"项目编号：XG-001""预算金额：48万元"），和相邻的短段落合成一片。
SHORT_PARAGRAPH = 40
LIST_ITEM = re.compile(r"^(\d+[.、．)）]|[（(]\d+[）)]|[（(][一二三四五六七八九十]+[）)])")
SENTENCE_END = "。；！？!?;"


class FileType(StrEnum):
    PDF = "pdf"
    DOCX = "docx"


MIME = {
    FileType.PDF: "application/pdf",
    FileType.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def detect_type(filename: str, data: bytes) -> FileType:
    """按文件头判断类型，扩展名只用来交叉核对。改了扩展名的可执行文件、HTML 错误页都会被挡住。

    旧版 Word（.doc）、扫描件转的图片不支持：前者要另装转换工具，后者要 OCR，都放到以后。
    """
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if data.startswith(b"%PDF-") and extension == "pdf":
        return FileType.PDF
    if data.startswith(b"PK\x03\x04") and extension == "docx":
        return FileType.DOCX
    raise AppError("unsupported_file_type", 415)


def clean_filename(value: str) -> str:
    """只保留文件名本身：去掉路径、控制字符，限制长度。文件名只用来显示，存储路径由 SHA-256 决定。"""
    name = re.split(r"[\\/]", value or "")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip()
    if not name or len(name) > 200:
        raise AppError("invalid_filename")
    return name


class BlockKind(StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE_ROW = "table_row"


@dataclass(frozen=True)
class Block:
    """解析器的输出：文件里的一个段落、一个标题或一行表格。"""

    kind: BlockKind
    text: str
    page: int | None = None
    level: int | None = None  # 标题层级，1 最高
    header: tuple[str, ...] = ()  # 表格行的表头


@dataclass(frozen=True)
class ChunkDraft:
    seq: int
    kind: str
    text: str
    page: int | None
    section_path: tuple[str, ...]
    id: str = ""

    @property
    def text_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


# 中文招标文件常见的标题写法，按层级从高到低。PDF 里没有"标题样式"，只能看文字长相。
HEADING_PATTERNS = [
    (1, re.compile(r"^第[一二三四五六七八九十百零〇\d]+[章部篇](\s|$|[^，。；])")),
    (2, re.compile(r"^[一二三四五六七八九十]+[、．.]")),
    (3, re.compile(r"^[（(][一二三四五六七八九十]+[）)]")),
]
HEADING_MAX_LENGTH = 40


def heading_level(text: str) -> int | None:
    """像标题就返回层级，不像返回 None。

    标题要短、不以句号结尾、中间没有逗号。"（一）满足……规定，具有……能力。"是正文；
    PDF 里它折行后的第一行也不能当成标题，逗号这条规则就是为它加的。
    """
    if len(text) > HEADING_MAX_LENGTH or text.endswith(("。", "；", ";")) or "，" in text or "," in text:
        return None
    for level, pattern in HEADING_PATTERNS:
        if pattern.match(text):
            return level
    return None


def normalize_space(text: str) -> str:
    """合并连续空白。中文排版里的换行多半是 PDF 版面造成的，不是原文的分段。"""
    return re.sub(r"\s+", " ", text).strip()


def _split_long(text: str) -> list[str]:
    """把过长的段落按句子切开，每片不超过 CHUNK_MAX。一句话本身就超长时，才在句子中间硬切。"""
    if len(text) <= CHUNK_MAX:
        return [text]
    sentences = re.findall(rf"[^{SENTENCE_END}]+[{SENTENCE_END}]?", text)
    pieces, current = [], ""
    for sentence in sentences:
        while len(sentence) > CHUNK_MAX:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:CHUNK_MAX])
            sentence = sentence[CHUNK_MAX:]
        if len(current) + len(sentence) > CHUNK_TARGET and current:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces


def _row_text(block: Block) -> str:
    """表格行带上表头写成一句话："评分项：类似业绩；分值：15；评分标准：……"。单独拿出来也看得懂。"""
    cells = [normalize_space(c) for c in block.text.split("\t")]
    if block.header and len(block.header) == len(cells):
        return "；".join(f"{h}：{c}" for h, c in zip(block.header, cells) if c)
    return "；".join(c for c in cells if c)


@dataclass
class _Section:
    path: list[str] = field(default_factory=list)
    levels: list[int] = field(default_factory=list)

    def enter(self, level: int, title: str) -> None:
        while self.levels and self.levels[-1] >= level:
            self.levels.pop()
            self.path.pop()
        self.levels.append(level)
        self.path.append(title)


def chunk_blocks(blocks: list[Block], document_sha256: str) -> list[ChunkDraft]:
    """把解析结果切成片。

    规则：
    - 标题单独成片，同时进入后面各片的 section_path（["第三章 采购需求", "二、交付要求"]）。
    - 一个段落一片。列表项（"1. ……""（一）……"）往往各是一条要求，一定单独成片。
    - 同一章节、同一页里相邻的短段落（项目编号、预算这类一行一项的信息）合成一片，用换行分隔；
      不跨章节、不跨页合并，这样每片都有确定的页码和章节。
    - 过长的段落按句子切开。
    - 表格每行一片，带上表头。
    - 片 ID = sha256(文档 SHA-256、序号、文本 SHA-256)：同一文件、同一版解析器，永远得到同样的 ID。
    """
    section = _Section()
    drafts: list[ChunkDraft] = []
    buffer: list[str] = []
    buffer_page: int | None = None

    def emit(kind: str, text: str, page: int | None) -> None:
        if not text:
            return
        draft = ChunkDraft(seq=len(drafts), kind=kind, text=text, page=page, section_path=tuple(section.path))
        identity = hashlib.sha256(f"{document_sha256}\n{draft.seq}\n{draft.text_hash}".encode()).hexdigest()
        drafts.append(ChunkDraft(**{**draft.__dict__, "id": identity}))
        if len(drafts) > MAX_CHUNKS:
            raise AppError("document_too_large", 413)

    def flush() -> None:
        nonlocal buffer, buffer_page
        if buffer:
            emit("text", "\n".join(buffer), buffer_page)
        buffer, buffer_page = [], None

    for block in blocks:
        text = normalize_space(block.text) if block.kind is not BlockKind.TABLE_ROW else block.text
        if not text:
            continue
        if block.kind is BlockKind.HEADING:
            flush()
            section.enter(block.level or 1, text)
            # 标题片的 section_path 已经包含它自己。
            emit("heading", text, block.page)
        elif block.kind is BlockKind.TABLE_ROW:
            flush()
            emit("table_row", _row_text(block), block.page)
        elif len(text) < SHORT_PARAGRAPH and not LIST_ITEM.match(text):
            if buffer and (block.page != buffer_page or sum(map(len, buffer)) + len(text) > CHUNK_TARGET):
                flush()
            if not buffer:
                buffer_page = block.page
            buffer.append(text)
        else:
            flush()
            for piece in _split_long(text):
                emit("text", piece, block.page)
    flush()
    return drafts


@dataclass(frozen=True)
class Document:
    id: str
    workspace_id: str | None
    notice_id: str | None
    filename: str
    file_type: str
    size_bytes: int
    sha256: str
    status: str
    error_code: str | None
    warning: str | None
    page_count: int | None
    chunk_count: int
    uploaded_by: str | None
    created_at: object
    parsed_at: object | None


@dataclass(frozen=True)
class Chunk:
    """给别的模块用的切片。研究流水线引用的就是 id。"""

    id: str
    document_id: str
    seq: int
    page: int | None
    section_path: tuple[str, ...]
    kind: str
    text: str


# 解析出零片，多半是扫描件（整页是图片）。不算失败，但要提醒用户。
WARNING_NO_TEXT = "no_text_extracted"
TOPIC_DOCUMENT_PARSED = "documents.parsed"
JOB_PARSE = "documents.parse"

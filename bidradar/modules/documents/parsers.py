"""把 PDF 和 Word 文件解析成一串 Block（段落、标题、表格行），交给 models.chunk_blocks 切片。

用的库都是宽松许可：pdfplumber（MIT）和 python-docx（MIT）。PyMuPDF 是 AGPL，公开仓库不用。

上传的文件不可信，解析前先挡几类问题：页数过多、Word 压缩包解压后过大（压缩炸弹）、文件损坏。
扫描件（整页是图片）解析不出文字，结果会是零片，由 service 标记"可能是扫描件"。OCR 以后再做。
"""

import io
import re
import zipfile

from bidradar.infra.errors import AppError
from bidradar.modules.documents.models import (
    LIST_ITEM,
    MAX_PAGES,
    SENTENCE_END,
    Block,
    BlockKind,
    FileType,
    heading_level,
)

MAX_DOCX_UNCOMPRESSED = 100 * 1024 * 1024
# "项目编号：……""预算金额：……"这类一行一项的写法。PDF 里它们各占一行，但行尾没有句号。
LABEL_LINE = re.compile(r"^[一-鿿A-Za-z（）()]{2,16}[：:]")
# 表格出现在页面顶部这个比例以内，并且列数和上一页最后一张表相同，就当作跨页续表。
CONTINUATION_TOP = 0.15


def parse(file_type: FileType, data: bytes) -> list[Block]:
    try:
        return parse_pdf(data) if file_type is FileType.PDF else parse_docx(data)
    except AppError:
        raise
    except Exception:  # 第三方解析库在坏文件上可能抛各种异常，统一当作"文件无法解析"
        raise AppError("document_unreadable", 422) from None


# ---- PDF ----


def _cell(value: str | None) -> str:
    # 单元格里的换行是版面折行，中文直接拼接。
    return re.sub(r"\s*\n\s*", "", value or "").strip()


def _starts_new_paragraph(previous: str, line: str) -> bool:
    return (
        previous.endswith(tuple(SENTENCE_END + "：:"))
        # 上一段本身就是一行标题（"第一章 投标邀请"），标题后面的文字一定是新段落。
        or heading_level(previous) is not None
        or heading_level(line) is not None
        or LIST_ITEM.match(line) is not None
        or LABEL_LINE.match(line) is not None
    )


def _lines_to_blocks(lines: list[str], page: int) -> list[Block]:
    """把 PDF 的一行行文字拼回段落。PDF 没有"段落"的概念，只能根据行尾标点和行首格式猜。"""
    blocks, current = [], ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if current and _starts_new_paragraph(current, line):
            blocks.append(current)
            current = line
        else:
            current += line
    if current:
        blocks.append(current)
    result = []
    for text in blocks:
        level = heading_level(text)
        result.append(
            Block(BlockKind.HEADING, text, page=page, level=level)
            if level
            else Block(BlockKind.PARAGRAPH, text, page=page)
        )
    return result


def parse_pdf(data: bytes) -> list[Block]:
    import pdfplumber

    blocks: list[Block] = []
    header: tuple[str, ...] = ()
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        if len(pdf.pages) > MAX_PAGES:
            raise AppError("document_too_large", 413)
        for number, page in enumerate(pdf.pages, start=1):
            tables = page.find_tables()
            boxes = [table.bbox for table in tables]

            def outside(obj, boxes=boxes) -> bool:
                return not any(
                    obj.get("x0", 0) >= x0 - 1
                    and obj.get("x1", 0) <= x1 + 1
                    and obj.get("top", 0) >= top - 1
                    and obj.get("bottom", 0) <= bottom + 1
                    for x0, top, x1, bottom in boxes
                )

            text_page = page.filter(outside) if boxes else page
            # 文字行和表格按纵坐标排好序，保持原文的先后顺序。
            items = [(line["top"], "line", line["text"]) for line in text_page.extract_text_lines()]
            items += [(table.bbox[1], "table", table) for table in tables]
            items.sort(key=lambda item: item[0])

            pending: list[str] = []
            for top, kind, value in items:
                if kind == "line":
                    pending.append(value)
                    continue
                blocks.extend(_lines_to_blocks(pending, number))
                pending = []
                rows = [[_cell(c) for c in row] for row in value.extract()]
                if not rows:
                    continue
                continued = (
                    top < page.height * CONTINUATION_TOP
                    and header
                    and len(rows[0]) == len(header)
                    and blocks
                    and blocks[-1].kind is BlockKind.TABLE_ROW
                )
                if not continued:
                    header, rows = tuple(rows[0]), rows[1:]
                elif rows and not rows[0][0]:
                    # 续表的第一行第一格是空的：这是上一页最后一行被拆到这一页的部分，拼回去。
                    previous = blocks[-1].text.split("\t")
                    merged = [p + c for p, c in zip(previous, rows[0])]
                    blocks[-1] = Block(BlockKind.TABLE_ROW, "\t".join(merged), page=blocks[-1].page, header=header)
                    rows = rows[1:]
                for row in rows:
                    blocks.append(Block(BlockKind.TABLE_ROW, "\t".join(row), page=number, header=header))
            blocks.extend(_lines_to_blocks(pending, number))
    return blocks


# ---- Word ----


def _docx_heading_level(style_name: str, text: str) -> int | None:
    match = re.match(r"(?:Heading|标题)\s*(\d)", style_name or "")
    if match:
        return int(match.group(1))
    return heading_level(text)


def parse_docx(data: bytes) -> list[Block]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    # .docx 是一个 zip 包。先看解压后的总大小，挡住"几 KB 的文件解压出几 GB"的压缩炸弹。
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_DOCX_UNCOMPRESSED:
            raise AppError("document_too_large", 413)
    document = Document(io.BytesIO(data))
    blocks: list[Block] = []
    for element in document.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            if not text:
                continue
            level = _docx_heading_level(paragraph.style.name if paragraph.style is not None else "", text)
            blocks.append(Block(BlockKind.HEADING, text, level=level) if level else Block(BlockKind.PARAGRAPH, text))
        elif tag == "tbl":
            table = Table(element, document)
            rows = [[_cell(cell.text) for cell in row.cells] for row in table.rows]
            if not rows:
                continue
            header = tuple(rows[0])
            for row in rows[1:]:
                blocks.append(Block(BlockKind.TABLE_ROW, "\t".join(row), header=header))
    return blocks

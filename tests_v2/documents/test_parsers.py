"""用虚构招标文件验证 PDF 和 Word 解析：标题、页码、表格、跨页续表。"""

import io
import zipfile

import pytest

from bidradar.infra.errors import AppError
from bidradar.modules.documents import parsers
from bidradar.modules.documents.models import FileType, chunk_blocks
from tests_v2.factories import TENDER_DOCX, TENDER_PDF


def chunks_of(path, file_type):
    return chunk_blocks(parsers.parse(file_type, path.read_bytes()), "a" * 64)


@pytest.mark.parametrize(("path", "file_type"), [(TENDER_PDF, FileType.PDF), (TENDER_DOCX, FileType.DOCX)])
def test_key_requirements_are_separate_chunks(path, file_type) -> None:
    texts = {c.text: c for c in chunks_of(path, file_type)}
    schedule = texts["1. 合同签订后60日内完成系统开发、部署和试运行。"]
    assert schedule.section_path == ("第三章 采购需求", "二、交付要求")
    assert any(t.startswith("2. 项目实施期间，中标人须安排不少于2名工程师驻场服务") for t in texts)
    assert any("预算金额：48万元" in t and "最高限价：48万元" in t for t in texts)


def test_pdf_chunks_carry_page_numbers() -> None:
    chunks = chunks_of(TENDER_PDF, FileType.PDF)
    pages = {c.text[:12]: c.page for c in chunks}
    assert pages["项目编号：XG-2026-0815"[:12]] == 1
    assert max(c.page for c in chunks) == 3


def test_pdf_table_split_across_pages_is_stitched() -> None:
    rows = [c.text for c in chunks_of(TENDER_PDF, FileType.PDF) if c.kind == "table_row"]
    assert "评分项：类似业绩；分值：15；评分标准：近三年每提供1个知识库或智能问答类项目合同得5分，最多15分" in rows
    assert len(rows) == 4


def test_docx_and_pdf_agree_on_requirement_text() -> None:
    pdf = {c.text for c in chunks_of(TENDER_PDF, FileType.PDF) if c.kind != "heading"}
    docx = {c.text for c in chunks_of(TENDER_DOCX, FileType.DOCX) if c.kind != "heading"}
    # 第六章模板的折行差异不影响要求本身；前五章的正文两边一致。
    assert len(pdf & docx) >= len(docx) - 3


def test_corrupt_file_is_unreadable() -> None:
    with pytest.raises(AppError, match="document_unreadable"):
        parsers.parse(FileType.PDF, b"%PDF-1.7 this is not really a pdf")


def test_zip_bomb_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(parsers, "MAX_DOCX_UNCOMPRESSED", 1000)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 100_000)
    with pytest.raises(AppError, match="document_too_large"):
        parsers.parse(FileType.DOCX, buffer.getvalue())

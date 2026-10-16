"""切片规则：标题、章节路径、短段落合并、列表项单独成片、长段落按句切、表格行带表头。"""

import pytest

from bidradar.infra.errors import AppError
from bidradar.modules.documents.models import (
    CHUNK_MAX,
    Block,
    BlockKind,
    FileType,
    chunk_blocks,
    clean_filename,
    detect_type,
    heading_level,
)

P, H, T = BlockKind.PARAGRAPH, BlockKind.HEADING, BlockKind.TABLE_ROW
SHA = "a" * 64


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("第三章 采购需求", 1),
        ("一、技术要求", 2),
        ("（二）投标保证金", 3),
        ("（一）满足《政府采购法》第二十二条规定，具有独立承担民事责任的能力。", None),
        ("（一）满足《政府采购法》第二十二条规定，具有独立承担民事责任的能", None),  # PDF 折行后的半行
        ("本项目不收取投标保证金。", None),
    ],
)
def test_heading_level(text: str, level: int | None) -> None:
    assert heading_level(text) == level


def test_section_path_follows_heading_levels() -> None:
    chunks = chunk_blocks(
        [
            Block(H, "第三章 采购需求", level=1),
            Block(H, "一、技术要求", level=2),
            Block(P, "1. 平台应支持私有化部署，数据存储在指定环境内。"),
            Block(H, "二、交付要求", level=2),
            Block(P, "1. 合同签订后60日内完成系统开发。"),
            Block(H, "第四章 合同主要条款", level=1),
            Block(P, "付款方式：验收合格后支付70%。"),
        ],
        SHA,
    )
    paths = {c.text: c.section_path for c in chunks if c.kind == "text"}
    assert paths["1. 平台应支持私有化部署，数据存储在指定环境内。"] == ("第三章 采购需求", "一、技术要求")
    assert paths["1. 合同签订后60日内完成系统开发。"] == ("第三章 采购需求", "二、交付要求")
    assert paths["付款方式：验收合格后支付70%。"] == ("第四章 合同主要条款",)


def test_short_lines_merge_but_list_items_stay_separate() -> None:
    chunks = chunk_blocks(
        [
            Block(P, "项目编号：XG-001", page=1),
            Block(P, "预算金额：48万元", page=1),
            Block(P, "1. 合同签订后60日内完成。", page=1),
            Block(P, "2. 驻场不少于30个工作日。", page=1),
        ],
        SHA,
    )
    assert [c.text for c in chunks] == [
        "项目编号：XG-001\n预算金额：48万元",
        "1. 合同签订后60日内完成。",
        "2. 驻场不少于30个工作日。",
    ]


def test_short_lines_do_not_merge_across_pages() -> None:
    chunks = chunk_blocks([Block(P, "项目编号：XG-001", page=1), Block(P, "预算金额：48万元", page=2)], SHA)
    assert [(c.page, c.text) for c in chunks] == [(1, "项目编号：XG-001"), (2, "预算金额：48万元")]


def test_long_paragraph_splits_on_sentences() -> None:
    sentence = "平台应支持文档批量导入和自动解析。" * 80
    chunks = chunk_blocks([Block(P, sentence)], SHA)
    assert len(chunks) > 1
    assert all(len(c.text) <= CHUNK_MAX for c in chunks)
    assert all(c.text.endswith("。") for c in chunks)
    assert "".join(c.text for c in chunks) == sentence


def test_table_rows_carry_their_header() -> None:
    chunks = chunk_blocks([Block(T, "类似业绩\t15\t每个项目5分", header=("评分项", "分值", "评分标准"))], SHA)
    assert chunks[0].text == "评分项：类似业绩；分值：15；评分标准：每个项目5分"


def test_chunk_ids_are_stable_and_unique() -> None:
    blocks = [Block(P, "1. 第一条要求。"), Block(P, "2. 第二条要求。")]
    first, again = chunk_blocks(blocks, SHA), chunk_blocks(blocks, SHA)
    assert [c.id for c in first] == [c.id for c in again]
    assert len({c.id for c in first}) == 2
    assert chunk_blocks(blocks, "b" * 64)[0].id != first[0].id


def test_type_detection_uses_magic_bytes() -> None:
    assert detect_type("a.pdf", b"%PDF-1.7 ...") is FileType.PDF
    assert detect_type("a.docx", b"PK\x03\x04...") is FileType.DOCX
    for name, data in (("a.pdf", b"<html>"), ("a.exe", b"%PDF-"), ("a.doc", b"\xd0\xcf\x11\xe0")):
        with pytest.raises(AppError, match="unsupported_file_type"):
            detect_type(name, data)


def test_filename_keeps_only_the_name() -> None:
    assert clean_filename("C:\\Users\\x\\招标文件.pdf") == "招标文件.pdf"
    assert clean_filename("../../etc/passwd.pdf") == "passwd.pdf"
    with pytest.raises(AppError):
        clean_filename("")

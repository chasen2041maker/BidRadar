"""公告类型判断：全国统一模板的结果公告、意向公开要认出来，认不准的保持"未知"。

所有标题、编号、单位都是虚构的。
"""

from copy import deepcopy

import pytest

from bidradar.modules.ingestion.normalize import VERSION, normalize_bundle
from tests.ingestion.support import Catalog
from tests.ingestion.test_public_evidence_fields import rehash, sample


def notice_type(title: str) -> str:
    return normalize_bundle(sample(title=title))["observations"][0]["notice_type"]


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # 结果公告：统一模板的写法、半角括号、括号里换个顺序、标题后面带"（二次）"。
        ("虚构平台项目中标（成交）结果公告", "award"),
        ("虚构平台项目中标(成交)结果公告", "award"),
        ("虚构平台项目成交（中标）结果公告", "award"),
        ("虚构平台项目中标（成交）结果公告（二次）", "award"),
        ("虚构平台项目成交结果公告", "award"),
        # 意向公开的两种常见标题。
        ("虚构单位2026年第四季度政府采购意向公开", "intention"),
        ("虚构单位2026年10月至12月政府采购意向", "intention"),
        # 对结果公告的更正仍是更正公告。
        ("虚构平台项目中标（成交）结果更正公告", "correction"),
        # 这几类说不准是不是结果，保持未知，不去猜。
        ("虚构平台项目中标候选人公示", "unknown"),
        ("虚构平台项目单一来源采购公示", "unknown"),
        ("虚构平台项目合同公告", "unknown"),
    ],
)
def test_title_templates(title: str, expected: str) -> None:
    assert notice_type(title) == expected


def test_template_award_is_not_shown_as_open_and_links_to_its_procurement() -> None:
    catalog = Catalog()
    procurement = normalize_bundle(
        sample(title="虚构平台项目竞争性磋商公告", key="https://www.ccgp.gov.cn/cggg/zygg/jzxcs/202610/t1.htm")
    )
    award = normalize_bundle(
        sample(title="虚构平台项目中标（成交）结果公告", key="https://www.ccgp.gov.cn/cggg/zygg/zbgg/202610/t2.htm")
    )
    catalog.import_bundle(procurement)
    catalog.import_bundle(award)

    detail = catalog.detail(award["observations"][0]["notice_id"], as_of="2026-10-22T00:00:00Z")

    assert detail["currentness"]["status"] == "not_opening_notice"
    relation = detail["relationships"][0]
    assert relation["status"] == "candidate"
    assert relation["target_notice_id"] == procurement["observations"][0]["notice_id"]


def test_old_classification_imported_later_does_not_replace_the_new_one() -> None:
    """同一次抓取，先导入 v4 的解释，再导入一份旧的 v3 解释：当前版本仍是 v4，v3 留在历史里。"""
    catalog = Catalog()
    newest = normalize_bundle(sample(title="虚构平台项目中标（成交）结果公告"))
    old = deepcopy(newest)
    old["normalizer_version"] = "procurement-facts-v3"
    for item in old["observations"]:
        item.update(normalizer_version="procurement-facts-v3", notice_type="unknown")
    rehash(old)

    catalog.import_bundle(newest)
    catalog.import_bundle(old)

    detail = catalog.detail(newest["observations"][0]["notice_id"])
    assert VERSION == "procurement-facts-v4"
    assert detail["current"] == newest["observations"][0]
    assert detail["history"] == [newest["observations"][0], old["observations"][0]]

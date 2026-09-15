"""CCGP小片离线回归；全部HTML为手写虚构样本，不证明真实站点已接通。"""
import unittest
from hashlib import sha256
from unittest.mock import patch

from services.ingestion.sources.ccgp import (
    MAX_HTML_BYTES, ParseStatus, SEARCH_URL, build_search_params, parse_search_page,
)

URL = "http://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260915_99999999.htm"
META = "2026-09-15 09:00:00 | 采购人：虚构软件研究所 | 代理机构：虚构代理 | 公开招标公告 | 虚构地区"


def row(url=URL, meta=META, title="虚构<em>AI</em>系统"):
    return f'<li><a href="{url}">{title}</a><p>仅为测试摘要</p><span>{meta}</span></li>'


def page(rows, suffix=""):
    return f'<html><body><ul class="vT-srch-result-list-bid">{rows}</ul>{suffix}</body></html>'


class CCGPSourceTests(unittest.TestCase):
    def test_upstream_request_mapping(self):
        params = build_search_params(" AI系统 ", 2, start_date="2026-09-01", end_date="2026-09-15")
        self.assertEqual(SEARCH_URL, "http://search.ccgp.gov.cn/bxsearch")
        self.assertEqual((params["kw"], params["page_index"], params["start_time"]), ("AI系统", "2", "2026:09:01"))
        self.assertEqual(params["end_time"], "2026:09:15")
        self.assertEqual(params["bidType"], "0")  # 包含历史/更正，不冒充只有在招项目。

    def test_missing_dates_remain_missing(self):
        params = build_search_params("软件")
        self.assertEqual((params["start_time"], params["end_time"]), ("", ""))

    def test_invalid_keywords(self):
        for keyword in (None, 1, "", "  ", "x" * 201):
            with self.subTest(keyword=keyword), self.assertRaises(ValueError):
                build_search_params(keyword)

    def test_invalid_page(self):
        for value in (0, -1, True, 1.0, "1"):
            with self.subTest(page=value), self.assertRaises(ValueError):
                build_search_params("软件", value)

    def test_invalid_date(self):
        for value in ("2026-02-30", "20260915", "2026-9-15", "", 1):
            with self.subTest(date=value), self.assertRaises(ValueError):
                build_search_params("软件", start_date=value)

    def test_reversed_dates(self):
        with self.assertRaises(ValueError):
            build_search_params("软件", start_date="2026-09-15", end_date="2026-09-01")

    def test_preserves_raw_evidence_and_locator(self):
        html = page(row())
        result = parse_search_page(html)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.input_sha256, sha256(html.encode()).hexdigest())
        item = result.items[0]
        self.assertEqual((item.title, item.url, item.metadata_text), ("虚构AI系统", URL, META))
        self.assertEqual(item.published_text, "2026-09-15 09:00:00")
        self.assertEqual(item.purchaser, "虚构软件研究所")
        self.assertEqual(item.agency, "虚构代理")
        self.assertEqual(item.locator, "ul.vT-srch-result-list-bid > li:nth-of-type(1)")

    def test_highlight_span_is_not_metadata(self):
        result = parse_search_page(page(row(title='虚构<span>AI</span>项目')))
        self.assertEqual(result.items[0].metadata_text, META)

    def test_missing_fields_are_unknown(self):
        item = parse_search_page(page(row(meta="日期待核实 | 未提供采购方"))).items[0]
        self.assertIsNone(item.published_text)
        self.assertIsNone(item.purchaser)
        self.assertIsNone(item.agency)

    def test_known_empty_template(self):
        self.assertEqual(parse_search_page(page("", "共 0 条")).status, ParseStatus.EMPTY)

    def test_dom_drift_is_not_empty(self):
        for html in ("", "<html>共0条</html>", page(""), '<ul class="new-template"></ul>'):
            with self.subTest(html=html):
                self.assertEqual(parse_search_page(html).status, ParseStatus.PARSE_ERROR)

    def test_block_page_is_not_empty(self):
        for message in ("访问受限", "访问过于频繁", "请输入验证码"):
            self.assertEqual(parse_search_page(f"<h1>{message}</h1>").status, ParseStatus.BLOCKED)

    def test_challenge_shell_is_not_empty(self):
        result = parse_search_page(page("", "共0条<h1>请输入验证码</h1>"))
        self.assertEqual(result.status, ParseStatus.BLOCKED)
        self.assertEqual(result.items, ())
        self.assertEqual(result.issues, ("access_challenge",))

    def test_challenge_shell_with_residual_rows_is_not_ok(self):
        result = parse_search_page(page(row(), "<h1>访问过于频繁</h1>"))
        self.assertEqual(result.status, ParseStatus.BLOCKED)
        self.assertEqual(result.items, ())  # 不能把限制页残留的结果当作一次成功采集。

    def test_captcha_procurement_title_is_not_a_challenge(self):
        result = parse_search_page(page(row(title="虚构验证码系统采购")))
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.items[0].title, "虚构验证码系统采购")

    def test_invalid_row_preserves_partial_status(self):
        result = parse_search_page(page(row() + "<li>损坏条目</li>"))
        self.assertEqual(result.status, ParseStatus.PARTIAL)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.issues, ("row_2:invalid_title_url_or_metadata",))

    def test_no_valid_rows_is_parse_error(self):
        self.assertEqual(parse_search_page(page("<li>损坏条目</li>")).status, ParseStatus.PARSE_ERROR)

    def test_unsafe_or_unregistered_urls_rejected(self):
        unsafe = ("javascript:alert(1)", "http://127.0.0.1/a", "https://www.ccgp.gov.cn.evil.example/a",
                  URL.replace("www.ccgp.gov.cn", "user@www.ccgp.gov.cn"),
                  URL.replace("www.ccgp.gov.cn", "www.ccgp.gov.cn:8080"),
                  "https://www.ccgp.gov.cn/../../private", "/cggg/zygg/a.htm",
                  "https://www.ccgp.gov.cn/%2e%2e/a.htm")
        for url in unsafe:
            with self.subTest(url=url):
                self.assertEqual(parse_search_page(page(row(url=url))).status, ParseStatus.PARSE_ERROR)

    def test_https_and_protocol_relative_official_links(self):
        for url in (URL.replace("http:", "https:"), URL.removeprefix("http:")):
            self.assertEqual(parse_search_page(page(row(url=url))).status, ParseStatus.OK)

    def test_multiple_containers_require_review(self):
        self.assertEqual(parse_search_page(page(row()) + page(row())).status, ParseStatus.PARSE_ERROR)

    def test_malformed_declaration_is_parse_error(self):
        self.assertEqual(parse_search_page("<![not-a-declaration]>").status, ParseStatus.PARSE_ERROR)

    def test_unclosed_rows_are_not_silently_merged(self):
        html = page(row().removesuffix("</li>") + row())
        self.assertEqual(parse_search_page(html).status, ParseStatus.PARSE_ERROR)

    def test_changed_html_changes_input_fingerprint(self):
        first = parse_search_page(page(row()))
        second = parse_search_page(page(row(title="更正后的虚构项目")))
        self.assertNotEqual(first.input_sha256, second.input_sha256)

    def test_input_type_and_size(self):
        with self.assertRaises(TypeError):
            parse_search_page(b"html")
        with self.assertRaises(ValueError):
            parse_search_page("x" * (MAX_HTML_BYTES + 1))

    def test_no_network_or_script_execution(self):
        with patch("socket.socket", side_effect=AssertionError("离线片段不得联网")):
            build_search_params("软件")
            result = parse_search_page(page(row() + "<script>throw new Error('not executed')</script>"))
        self.assertEqual(result.status, ParseStatus.OK)


if __name__ == "__main__":
    unittest.main()

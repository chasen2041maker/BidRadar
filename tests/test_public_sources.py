"""只用合成模板验证来源边界；真实含联系方式原件留在忽略目录，不进入测试库。"""
from hashlib import sha256
import unittest

from services.ingestion.sources.ccgp import MAX_HTML_BYTES, ListingResult, ParseStatus, parse_notice_page
from services.ingestion.sources.public_notices import (
    build_listing_url, canonical_notice_url, listing_identity, parse_listing, parse_notice,
)

CCGP = "cn_ccgp"
HAINAN = "cn_hainan"
C_LIST = "https://www.ccgp.gov.cn/cggg/zygg/jzxcs/"
C_NOTICE = C_LIST + "202609/t20260930_100001.htm"
H_LIST = "https://ggzy.hainan.gov.cn/ggzy/ggzy/cggg/index.jhtml"
H_NOTICE = "https://ggzy.hainan.gov.cn/ggzy/ggzy/cggg/100001.jhtml"


def ccgp_row(href="./202609/t20260930_100001.htm", title="信息系统开发采购", date="2026-09-30 09:00"):
    return (f'<li><a href="{href}" title="{title}">{title[:4]}...</a>'
            f'发布时间：<em>{date}</em> 地域：<em>测试地区</em> 采购人：<em>测试采购单位</em></li>')


def hainan_row(href="http://ggzy.hainan.gov.cn:80/ggzy/ggzy/cggg/100001.jhtml", date="2026-09-30"):
    return (f'<tr><td>1</td><td>测试地区</td><td><a href="{href}" title="合成信息系统开发采购">'
            f'合成信息...</a></td><td>{date}</td></tr>')


def hainan_notice(body, title="合成软件采购", extra=""):
    return (f'<a href="/unrelated.pdf">站点下载专区</a><div class="newsTex"><h1>{title}</h1>'
            f'<div class="msgbar">来源：测试中心</div><div class="summary">{title}</div>'
            f'<div class="newsCon">{body}</div></div>{extra}')


def ccgp_notice(body, extra=""):
    return ('<div class="vF_detail_main"><div class="vF_detail_header"><h2 class="tc">合成软件采购</h2></div>'
            f'{extra}<div class="vF_detail_content">{body}</div></div>')


class PublicUrlTests(unittest.TestCase):
    def test_verified_page_conventions_round_trip(self):
        for source, category in ((CCGP, "zygg/jzxcs"), (CCGP, "dfgg/gkzb"),
                                 (HAINAN, "cggg"), (HAINAN, "zfcgqtgg"), (HAINAN, "cgzbgg")):
            for page in (1, 2, 25):
                with self.subTest(source=source, category=category, page=page):
                    self.assertEqual(listing_identity(source, build_listing_url(source, category, page)), (category, page))
        self.assertEqual(build_listing_url(CCGP, "zygg/jzxcs", 2), C_LIST + "index_1.htm")
        self.assertEqual(build_listing_url(HAINAN, "cggg", 2), H_LIST.replace("index.jhtml", "index_2.jhtml"))
        self.assertEqual(listing_identity(CCGP, C_LIST + "index.htm"), ("zygg/jzxcs", 1))

    def test_builder_rejects_unbounded_or_injected_inputs(self):
        for source, category, page in (("other", "cggg", 1), (HAINAN, "../cggg", 1),
                                       (CCGP, "zygg/unknown", 1), (CCGP, "zygg/jzxcs", True),
                                       (HAINAN, "cggg", 0), (HAINAN, "cggg", 100001)):
            with self.subTest(source=source, category=category, page=page), self.assertRaises(ValueError):
                build_listing_url(source, category, page)

    def test_listing_identity_rejects_url_escape_hatches(self):
        for source, good in ((CCGP, C_LIST), (HAINAN, H_LIST)):
            bad = [good + "?", good + "#", good + "?page=2", good + "#anchor",
                   good.replace("https://", "https://user@"), good.replace(".gov.cn", ".gov.cn.evil.test"),
                   good.replace(".gov.cn", ".gov.cn:8443"), good.replace("https://", "file://"),
                   good.replace("/ggzy/", "/%67gzy/") if source == HAINAN else good.replace("/cggg/", "/%63ggg/"),
                   good + "\n", good.replace(".gov.cn/", ".gov.cn\\/")]
            for url in bad:
                with self.subTest(url=url):
                    self.assertIsNone(listing_identity(source, url))
        self.assertIsNone(listing_identity(HAINAN, H_LIST.replace("index", "index_1")))
        self.assertIsNone(listing_identity(CCGP, C_LIST + "index_0.htm"))

    def test_hainan_canonical_matches_verified_official_https_redirect(self):
        for url in (H_NOTICE, H_NOTICE.replace("https://", "http://"),
                    H_NOTICE.replace("https://", "http://").replace(".cn/", ".cn:80/"),
                    H_NOTICE.replace(".cn/", ".cn:443/")):
            self.assertEqual(canonical_notice_url(HAINAN, url), H_NOTICE)
        for url in (H_NOTICE + "?a=1", H_NOTICE + "#x", H_NOTICE.replace("100001", "index"),
                    H_NOTICE.replace(".cn/", ".cn:444/"), H_NOTICE.replace("cggg", "jycx")):
            self.assertIsNone(canonical_notice_url(HAINAN, url))

    def test_legacy_ccgp_identity_keeps_protocol_query_and_drops_fragment(self):
        old = C_NOTICE.replace("https://", "http://") + "?version=2#fragment"
        self.assertEqual(canonical_notice_url(CCGP, old), old.split("#")[0])
        self.assertIsNone(canonical_notice_url(CCGP, old.replace(".cn/", ".cn:80/")))
        self.assertIsNone(canonical_notice_url("other", C_NOTICE))


class PublicListingTests(unittest.TestCase):
    def test_ccgp_static_rows_deduplicate_and_keep_full_title_and_metadata(self):
        html = '<ul class="c_list_bid">' + ccgp_row() * 2 + '</ul>'
        result = parse_listing(html, C_LIST, CCGP)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(len(result.items), 1)
        item = result.items[0]
        self.assertEqual(item.title, "信息系统开发采购")
        self.assertEqual(item.published_text, "2026-09-30 09:00")
        self.assertEqual(item.purchaser, "测试采购单位")
        self.assertEqual(item.source_id, CCGP)
        self.assertEqual(result.input_sha256, sha256(html.encode()).hexdigest())
        self.assertIsNone(result.next_url)  # 无分页证据时不凭固定每页条数猜下一页。

    def test_ccgp_pager_is_read_as_data_without_executing_script(self):
        html = '<ul class="c_list_bid">' + ccgp_row() + '</ul>'
        pager = "<script>Pager({size:3, current:0, prefix:'index',suffix:'htm'});</script>"
        result = parse_listing(html + pager, C_LIST, CCGP)
        self.assertEqual(result.next_url, C_LIST + "index_1.htm")
        wrong = parse_listing(html + pager.replace("current:0", "current:1"), C_LIST, CCGP)
        self.assertEqual(wrong.status, ParseStatus.PARTIAL)
        self.assertIn("pagination_mismatch", wrong.issues)
        self.assertIsNone(wrong.next_url)
        # 不执行 appended JavaScript，也不从混入任意代码的语句里抽取跳转。
        appended = pager.replace(";</script>", ";fetch('https://evil.test');</script>")
        self.assertIsNone(parse_listing(html + appended, C_LIST, CCGP).next_url)
        end = pager.replace("size:3", "size:1")
        self.assertIsNone(parse_listing(html + end, C_LIST, CCGP).next_url)

    def test_hainan_table_footer_and_inert_next_link(self):
        footer = ('<tr><td colspan="4">共30条记录 1/3页 '
                  '<a href="#" onclick="location.href=encodeURI(\'index_2.jhtml\');">下一页</a></td></tr>')
        html = '<table class="newtable"><tr><th>序号</th><th>地区</th><th>标题</th><th>日期</th></tr>' + hainan_row() + footer + '</table>'
        result = parse_listing(html, H_LIST, HAINAN)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(len(result.items), 1)
        self.assertEqual(result.items[0].url, H_NOTICE)
        self.assertEqual(result.items[0].title, "合成信息系统开发采购")
        self.assertEqual(result.items[0].published_text, "2026-09-30")
        self.assertEqual(result.items[0].source_id, HAINAN)
        self.assertIsNone(result.items[0].purchaser)
        self.assertEqual(result.next_url, H_LIST.replace("index.jhtml", "index_2.jhtml"))

    def test_pagination_cannot_change_source_category_or_skip_page(self):
        base = '<table class="newtable">' + hainan_row() + '</table>'
        for href in ("https://evil.test/index_2.jhtml", "../cgzbgg/index_2.jhtml", "index_3.jhtml", "index_2.jhtml?extra=1"):
            with self.subTest(href=href):
                r = parse_listing(base + f'<a href="{href}">下一页</a>', H_LIST, HAINAN)
                self.assertIsNone(r.next_url)
                self.assertIn("invalid_next_page", r.issues)
        disabled = parse_listing(base + '<a disabled>下一页</a>', H_LIST, HAINAN)
        self.assertEqual(disabled.status, ParseStatus.OK)
        self.assertIsNone(disabled.next_url)

    def test_bad_rows_do_not_hide_good_rows_or_become_empty(self):
        for bad in ("https://evil.test/data.htm", "http://[broken", "javascript:alert(1)",
                    "/cggg/dfgg/jzxcs/202609/t20260930_100002.htm"):
            with self.subTest(bad=bad):
                html = '<ul class="c_list_bid">' + ccgp_row() + ccgp_row(bad) + '</ul>'
                result = parse_listing(html, C_LIST, CCGP)
                self.assertEqual(result.status, ParseStatus.PARTIAL)
                self.assertEqual(len(result.items), 1)
                self.assertIn("row_2:invalid_title_or_url", result.issues)
        bad_only = parse_listing('<ul class="c_list_bid">' + ccgp_row("https://evil.test/x.htm") + '</ul>', C_LIST, CCGP)
        self.assertEqual(bad_only.status, ParseStatus.PARSE_ERROR)

    def test_missing_date_is_unknown_and_marked_partial(self):
        html = '<table class="newtable">' + hainan_row(date="暂无") + '</table>'
        result = parse_listing(html, H_LIST, HAINAN)
        self.assertEqual(result.status, ParseStatus.PARTIAL)
        self.assertIsNone(result.items[0].published_text)

    def test_empty_requires_matching_template_and_explicit_zero(self):
        for html, status in (('<ul class="c_list_bid"></ul>共0条', ParseStatus.EMPTY),
                             ('<ul class="c_list_bid"></ul>', ParseStatus.PARSE_ERROR),
                             ('<div>共0条</div>', ParseStatus.PARSE_ERROR)):
            self.assertEqual(parse_listing(html, C_LIST, CCGP).status, status)

    def test_challenge_page_blocks_even_with_stale_rows_but_product_title_is_valid(self):
        html = '<ul class="c_list_bid">' + ccgp_row(title="验证码系统采购") + '</ul>'
        self.assertEqual(parse_listing(html, C_LIST, CCGP).status, ParseStatus.OK)
        blocked = parse_listing(html + '<div>访问过于频繁，请输入验证码</div>', C_LIST, CCGP)
        self.assertEqual(blocked.status, ParseStatus.BLOCKED)
        self.assertEqual(blocked.items, ())

    def test_nested_or_ambiguous_template_is_rejected(self):
        html = '<ul class="c_list_bid"><li>' + ccgp_row() + '</li></ul>'
        self.assertEqual(parse_listing(html, C_LIST, CCGP).status, ParseStatus.PARSE_ERROR)
        doubled = '<ul class="c_list_bid">' + ccgp_row() + '</ul>'
        self.assertEqual(parse_listing(doubled * 2, C_LIST, CCGP).status, ParseStatus.PARSE_ERROR)


class PublicNoticeTests(unittest.TestCase):
    def test_hainan_preserves_div_segments_inline_spans_and_table_cells(self):
        body = ('<div class="t5">一、基本情况<div class="t9">预算：<span>未知</span></div></div>'
                '<div>获取文件截止：2026-10-10</div><table><tr><td>项目编号</td><td>DEMO-1</td></tr></table>'
                '<script>不可执行内容</script>')
        result = parse_notice(hainan_notice(body), H_NOTICE, HAINAN)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual([s["text"] for s in result.segments],
                         ["一、基本情况", "预算：未知", "获取文件截止：2026-10-10", "项目编号 | DEMO-1"])
        self.assertNotIn("不可执行内容", result.text)
        self.assertNotIn("下载专区", result.text)
        self.assertEqual(result.metadata, ())  # 正文截止不能替代不存在的发布时间。
        self.assertEqual(result.attachments, ())

    def test_nested_table_in_paragraph_keeps_budget_heading_and_value_row(self):
        body = ('<p>需求<table><tr><td>数量</td><td>采购预算（元人民币）</td></tr>'
                '<tr><td>1</td><td>1000000.00</td></tr></table>系统备注</p>')
        result = parse_notice(ccgp_notice(body), C_NOTICE, CCGP)
        self.assertEqual([s["text"] for s in result.segments],
                         ["需求", "数量 | 采购预算（元人民币）", "1 | 1000000.00", "系统备注"])
        self.assertIn("tr:nth-child(2)", result.segments[2]["locator"])
        # 同一修复也覆盖原 v1 调用，避免两入口产生不同证据段落。
        self.assertEqual(result.segments, parse_notice_page(ccgp_notice(body), C_NOTICE).segments)

    def test_ccgp_summary_attachment_empty_href_is_explicitly_missing(self):
        html = ccgp_notice('<p>采购正文</p>', '<table><tr><td>附件</td><td><a href="">合成公告.docx</a></td></tr></table>')
        result = parse_notice(html, C_NOTICE, CCGP)
        self.assertEqual(result.status, ParseStatus.PARTIAL)
        self.assertEqual(result.attachments, ())
        self.assertIn("attachment_link_missing", result.issues)

    def test_public_attachment_keeps_protocol_encodes_chinese_and_deduplicates(self):
        href = "http://ggzy.hainan.gov.cn/jcfw/upload6/20260930/demo.pdf?attname=合成公告.pdf&amp;v=%20"
        body = f'<div>材料<a href="{href}">合成公告.pdf</a><a href="{href}#page=2">重复引用</a></div>'
        result = parse_notice(hainan_notice(body), H_NOTICE, HAINAN)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(len(result.attachments), 1)
        attachment = result.attachments[0]
        self.assertTrue(attachment["url"].isascii())
        self.assertTrue(attachment["url"].startswith("http://"))
        self.assertIn("attname=%E5%90%88%E6%88%90", attachment["url"])
        self.assertIn("&v=%20", attachment["url"])
        self.assertEqual(attachment["name"], "合成公告.pdf")
        self.assertEqual(attachment["status"], "not_fetched")
        self.assertIn("div.newsCon", attachment["locator"])

    def test_malformed_attachment_links_never_become_fetch_targets(self):
        for href in ("javascript:alert(1)", "file:///secret.pdf", "http://[broken/x.pdf",
                     "https://user:pass@ggzy.hainan.gov.cn/x.pdf", "https://ggzy.hainan.gov.cn:9000/x.pdf"):
            with self.subTest(href=href):
                result = parse_notice(hainan_notice(f'<div>正文<a href="{href}">合成.pdf</a></div>'), H_NOTICE, HAINAN)
                self.assertEqual(result.attachments, ())
                self.assertIn("malformed_attachment_link", result.issues)

    def test_linked_notice_keeps_reference_without_asserting_relationship(self):
        original = H_NOTICE.replace("100001", "100002")
        result = parse_notice(hainan_notice(f'<p>原公告<a href="{original}">原公告</a></p>'), H_NOTICE, HAINAN)
        self.assertEqual(result.links[0]["url"], original)
        self.assertEqual(result.attachments, ())
        self.assertNotIn("relation", result.links[0])

    def test_hainan_challenge_does_not_confuse_security_project_title(self):
        good = hainan_notice('<div>验证码模块开发</div>', title="验证码系统采购")
        self.assertEqual(parse_notice(good, H_NOTICE, HAINAN).status, ParseStatus.OK)
        stale = parse_notice(good + '<aside>请输入验证码</aside>', H_NOTICE, HAINAN)
        self.assertEqual(stale.status, ParseStatus.BLOCKED)
        self.assertIsNone(stale.text)
        title = hainan_notice('<div>残留旧正文</div>', title="请输入验证码")
        self.assertEqual(parse_notice(title, H_NOTICE, HAINAN).status, ParseStatus.BLOCKED)

    def test_unknown_template_or_missing_body_is_not_a_notice(self):
        for html in ('<title>普通公告</title><p>模板未知</p>', '<div class="newsTex"><h1>标题</h1></div>',
                     '<div class="newsTex"><h1>标题</h1></div><div class="newsCon">不在正文容器内</div>'):
            result = parse_notice(html, H_NOTICE, HAINAN)
            self.assertEqual(result.status, ParseStatus.PARSE_ERROR)
            self.assertIsNone(result.text)


class PublicResourceTests(unittest.TestCase):
    def test_shared_result_remains_four_positional_arguments_compatible(self):
        self.assertIsNone(ListingResult(ParseStatus.EMPTY, (), (), "hash").next_url)

    def test_invalid_url_fails_before_creating_business_items(self):
        self.assertEqual(parse_listing('<p>x</p>', 'https://evil.test/', HAINAN).issues, ("invalid_listing_url",))
        self.assertEqual(parse_notice('<p>x</p>', 'https://evil.test/', HAINAN).issues, ("invalid_notice_url",))

    def test_html_depth_nodes_size_and_type_are_bounded(self):
        for parse, url in ((parse_listing, H_LIST), (parse_notice, H_NOTICE)):
            with self.subTest(parser=parse.__name__):
                with self.assertRaises(TypeError):
                    parse(b"html", url, HAINAN)
                with self.assertRaises(ValueError):
                    parse("x" * (MAX_HTML_BYTES + 1), url, HAINAN)
                deep = parse("<div>" * 130, url, HAINAN)
                self.assertEqual(deep.status, ParseStatus.PARSE_ERROR)
                self.assertIn("html_depth_limit", deep.issues)
                many = parse("<br>" * 10001, url, HAINAN)
                self.assertEqual(many.status, ParseStatus.PARSE_ERROR)
                self.assertIn("html_node_limit", many.issues)
                malformed = parse("<![invalid declaration]>", url, HAINAN)
                self.assertEqual(malformed.status, ParseStatus.PARSE_ERROR)
                # 标准库版本可能把未知声明忽略或报错，两种都不能产出业务正文/条目。
                self.assertTrue(malformed.issues)


if __name__ == "__main__":
    unittest.main()

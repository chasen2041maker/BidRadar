"""正文模板回归，全部为虚构HTML；真正站点模板尚需获准后的回归样本。"""
import unittest

from services.ingestion.sources.ccgp import ParseStatus, parse_notice_page

URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_99990001.htm"


class NoticeTests(unittest.TestCase):
    def page(self, content):
        return '<h2 class="tc">虚构采购</h2><div class="vF_detail_content">' + content + '</div>'

    def test_title_content_relative_attachment_and_no_invented_fields(self):
        result = parse_notice_page(self.page('<p>范围待核实</p><a href="file.pdf">文件</a>'), URL)
        self.assertEqual(result.status, ParseStatus.OK)
        self.assertEqual(result.title, "虚构采购")
        self.assertIn("范围待核实", result.text)
        self.assertEqual(result.attachments[0]["url"], URL.rsplit("/", 1)[0] + "/file.pdf")
        self.assertEqual(result.attachments[0]["status"], "not_fetched")

    def test_unknown_template_not_a_successful_notice(self):
        self.assertEqual(parse_notice_page("<title>公告</title><body>导航</body>", URL).status, ParseStatus.PARSE_ERROR)

    def test_challenge_shell_precedes_residual_notice(self):
        result = parse_notice_page(self.page("残留正文") + "<h1>请输入验证码</h1>", URL)
        self.assertEqual(result.status, ParseStatus.BLOCKED)
        self.assertIsNone(result.text)

    def test_captcha_system_in_notice_body_is_not_a_challenge(self):
        self.assertEqual(parse_notice_page(self.page("验证码系统采购"), URL).status, ParseStatus.OK)

    def test_captcha_system_in_notice_title_is_not_a_challenge(self):
        for heading in ('<h2 class="tc">验证码系统采购</h2>', '<h1>验证码系统采购</h1>'):
            with self.subTest(heading=heading):
                page = '<head><title>验证码系统采购</title></head>' + heading + '<div class="vF_detail_content">正常正文</div>'
                result = parse_notice_page(page, URL)
                self.assertEqual(result.status, ParseStatus.OK)
                self.assertEqual(result.title, "验证码系统采购")
                self.assertEqual(result.text, "正常正文")

    def test_residual_captcha_project_title_does_not_hide_external_challenge(self):
        page = self.page("残留正文").replace("虚构采购", "验证码系统采购")
        result = parse_notice_page(page + '<aside>请输入验证码后继续访问</aside>', URL)
        self.assertEqual(result.status, ParseStatus.BLOCKED)
        self.assertIsNone(result.text)

    def test_explicit_challenge_heading_precedes_residual_body(self):
        for heading in ('<h1>请输入验证码</h1>', '<h2 class="tc">访问受限</h2>',
                        '<h2 class="title">访问过于频繁，请稍后再试</h2>', '<h1>安全验证</h1>'):
            with self.subTest(heading=heading):
                result = parse_notice_page(heading + '<div class="vF_detail_content">残留正文</div>', URL)
                self.assertEqual(result.status, ParseStatus.BLOCKED)
                self.assertIsNone(result.text)

    def test_script_not_executed_or_treated_as_text(self):
        result = parse_notice_page(self.page("范围<script>secret()</script>"), URL)
        self.assertNotIn("secret", result.text)

    def test_missing_title_partial_and_malformed_link_diagnosed(self):
        result = parse_notice_page('<div class="vF_detail_content">正文<a href="http://[">文件</a></div>', URL)
        self.assertEqual(result.status, ParseStatus.PARTIAL)
        self.assertIn("missing_notice_title", result.issues)
        self.assertIn("malformed_attachment_link", result.issues)

    def test_duplicate_attachment_is_one_reference(self):
        result = parse_notice_page(self.page('<a href="a.pdf">一</a><a href="a.pdf">二</a>'), URL)
        self.assertEqual(len(result.attachments), 1)

    def test_depth_limit_applies_to_notice(self):
        result = parse_notice_page("<div>" * 129, URL)
        self.assertEqual(result.issues, ("html_depth_limit",))


if __name__ == "__main__":
    unittest.main()

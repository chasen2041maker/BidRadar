"""两来源新增请求到归档/重放/导出的真实本地链；页面与HTTP均为合成数据。"""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.ingestion.__main__ import main
from services.ingestion.archive import Store
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute, public_request_spec, replay
from services.ingestion.transport import FetchResult

CCGP = "https://www.ccgp.gov.cn/cggg/zygg/jzxcs/202609/t20260930_999991.htm"
HAINAN = "https://ggzy.hainan.gov.cn/ggzy/ggzy/cggg/999991.jhtml"


class FakePublicTransport:
    """不绕过生产校验来声称网络通过；这里只替换获取以验证持久流程。"""
    def __init__(self, source):
        self.source, self.calls = source, []
        self.interrupt_after = None

    def fetch(self, url, *, kind, checkpoint, **_):
        checkpoint()
        if len(self.calls) == self.interrupt_after:
            raise KeyboardInterrupt()
        self.calls.append((kind, url))
        if kind == "listing":
            row = '<li><a href="' + CCGP + '">虚构软件平台</a>发布时间：<em>2026-09-30 12:00</em> 采购人：<em>虚构采购单位</em></li>'
            other = '<li><a href="' + CCGP.replace("999991", "999992") + '">虚构道路工程</a>发布时间：<em>2026-09-30 12:00</em> 采购人：<em>虚构采购单位</em></li>'
            html = '<ul class="c_list_bid">' + row + other + '</ul>'
            if self.source == "cn_hainan":
                html = ('<table class="newtable"><tr><td>1</td><td>测试地区</td><td><a href="' + HAINAN
                        + '" title="虚构软件平台">虚构软件平台</a></td><td>2026-09-29</td></tr></table>')
        else:
            body = '<p>项目编号：SYNTHETIC-001</p><p>采购人：虚构采购单位</p><p>预算金额：10万元</p>'
            html = ('<h2 class="tc">虚构软件竞争性磋商公告</h2><div class="vF_detail_content">' + body + '</div>'
                    if self.source == "cn_ccgp" else '<div class="newsTex"><h1>虚构软件公开招标公告</h1><div class="newsCon">' + body + '</div></div>')
        return FetchResult(url, url, 200, {"content-type": "text/html; charset=utf-8"},
                           html.encode(), "2026-09-30T00:00:00Z", 1)


class PublicPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "ingestion"
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_public_listing_duplicate_pages_title_filter_and_resume(self):
        request = public_request_spec(category="zygg/jzxcs", pages=2, title_terms=["软件"])
        request["simulation"] = True
        run, _ = self.store.create_run(request, "new-public")
        transport = FakePublicTransport("cn_ccgp")
        transport.interrupt_after = 1
        with patch("socket.socket", side_effect=AssertionError("禁止联网")):
            with self.assertRaises(KeyboardInterrupt):
                execute(self.store, run, transport)
            self.assertEqual("queued", self.store.run(run)["status"])
            resumed = FakePublicTransport("cn_ccgp")
            report = execute(self.store, run, resumed)
            execute(self.store, run, resumed)
        # 一页已提交不重取；两页同一条公告只下载一次，其他标题保留筛除计数。
        self.assertEqual(["listing", "notice"], [kind for kind, _ in resumed.calls])
        self.assertEqual(1, self.store.task_count(run, "notice"))
        self.assertEqual(1, report["captures"][0]["parsed"]["notices_outside_title_filter"])
        self.assertIn(report["run"]["status"], ("succeeded", "partial"))

    def test_hainan_source_identity_export_and_replay_without_network(self):
        request = public_request_spec(source_id="cn_hainan", notice_urls=[HAINAN])
        request["simulation"] = True
        run, _ = self.store.create_run(request, "hainan")
        report = execute(self.store, run, FakePublicTransport("cn_hainan"))
        self.assertEqual("succeeded", report["run"]["status"])
        capture = report["captures"][0]
        with patch("socket.socket", side_effect=AssertionError("重放禁止联网")):
            bundle = export_bundle(self.store, run)
            result = replay(self.store, capture["id"])
        self.assertEqual("cn_hainan", bundle["source_id"])
        self.assertEqual(1, len(bundle["documents"]))
        self.assertEqual(capture["sha256"], result["parsed"]["raw_sha256"])
        self.assertEqual(capture["parsed"], self.store.capture(capture["id"])["parsed"])

    def test_new_cli_requires_network_opt_in_and_persists_failure(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("socket.socket", side_effect=AssertionError("缺授权禁止联网")):
            code = main(["--store", str(self.root), "collect-public", "--source", "hainan",
                         "--notice-url", HAINAN, "--key", "disabled"])
        report = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(3, code)
        self.assertEqual("network_opt_in_required", report["run"]["error_code"])
        self.assertEqual([], export_bundle(self.store, report["run"]["id"])["documents"])

    def test_hainan_publication_date_points_to_archived_listing_not_fetch_time(self):
        request = public_request_spec(source_id="cn_hainan", category="cggg")
        request["simulation"] = True
        run, _ = self.store.create_run(request, "dated-listing")
        report = execute(self.store, run, FakePublicTransport("cn_hainan"))
        listing = next(c for c in report["captures"] if c["kind"] == "listing")
        content = export_bundle(self.store, run)["documents"][0]["content"]
        date = next(m for m in content["metadata"] if m["label"] == "公告发布时间")
        self.assertEqual("2026-09-29", date["text"])
        self.assertIn(listing["id"], date["locator"])
        self.assertIn(listing["sha256"], date["locator"])
        self.assertEqual(listing["url"], content["discovery_evidence"][0]["url"])

    def test_requests_reject_cross_source_unbounded_and_mode_changes(self):
        for kwargs in ({"source_id": "cn_hainan", "notice_urls": [CCGP]},
                       {"category": "zygg/jzxcs", "pages": 6},
                       {"category": "zygg/jzxcs", "title_terms": [""]},
                       {"notice_urls": [CCGP], "title_terms": ["软件"]},
                       {"source_id": "other", "notice_urls": [CCGP]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                public_request_spec(**kwargs)
        request = public_request_spec(notice_urls=[CCGP], dns_mode="google-doh")
        run, _ = self.store.create_run(request, "doh")
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--store", str(self.root), "resume", run, "--dns-mode", "system"])
        self.assertEqual(4, code)
        self.assertEqual("dns_mode_must_match_run", json.loads(output.getvalue())["code"])


if __name__ == "__main__":
    unittest.main()

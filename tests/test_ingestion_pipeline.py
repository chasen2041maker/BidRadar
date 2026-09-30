"""阶段验收：真实本地账本和归档配虚构传输，绝不把模拟结果称为站点接通。"""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from services.ingestion.__main__ import main
from services.ingestion.archive import Store
from services.ingestion.demo import ATTACHMENT_URL, NOTICE_URL, DemoTransport, demo_request
from services.ingestion.pipeline import execute, parse_capture, replay, request_spec
from services.ingestion.transport import FetchError, FetchResult, SourcePolicy, Transport


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "archive"
        self.store = Store(self.root)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def register(self, request=None, key="test"):
        return self.store.create_run(request or demo_request(), key)[0]

    def test_complete_pagination_notice_attachment_and_duplicate_run(self):
        run_id = self.register()
        transport = DemoTransport()
        with patch("socket.socket", side_effect=AssertionError("模拟不得联网")):
            result = execute(self.store, run_id, transport)
            second = execute(self.store, run_id, transport)
        self.assertEqual(result["run"]["status"], "succeeded")
        self.assertEqual(len(transport.calls), 4)  # 两页重复链接只生成一个正文任务。
        self.assertEqual(len(result["captures"]), 4)
        self.assertEqual(len(second["captures"]), 4)
        self.assertEqual(len(list(self.store.blobs.glob("*.bin"))), 3)
        notice = next(item for item in result["captures"] if item["kind"] == "notice")
        self.assertEqual(notice["parsed"]["attachments"][0]["fetch_status"], "fetched")
        for capture in result["captures"]:
            self.assertEqual(sha256(self.store.read_blob(capture["sha256"])).hexdigest(), capture["sha256"])

    def test_replay_reads_archive_not_network_and_keeps_original(self):
        report = execute(self.store, self.register(), DemoTransport())
        capture = next(item for item in report["captures"] if item["kind"] == "notice")
        before = self.store.capture(capture["id"])
        with patch("socket.socket", side_effect=AssertionError("重放不得联网")):
            result = replay(self.store, capture["id"])
        self.assertEqual(result["parsed"]["raw_sha256"], capture["sha256"])
        self.assertEqual(self.store.capture(capture["id"]), before)

    def test_process_interruption_resumes_without_repeating_committed_page(self):
        run_id = self.register()
        class Interrupted(DemoTransport):
            def fetch(self, url, **kwargs):
                if len(self.calls) == 1:
                    raise KeyboardInterrupt
                return super().fetch(url, **kwargs)
        with self.assertRaises(KeyboardInterrupt):
            execute(self.store, run_id, Interrupted())
        self.assertEqual(self.store.run(run_id)["status"], "queued")
        transport = DemoTransport()
        result = execute(self.store, run_id, transport)
        self.assertEqual(result["run"]["status"], "succeeded")
        self.assertEqual(len(transport.calls), 3)

    def test_cancel_during_fetch_cannot_publish_or_schedule_following(self):
        run_id = self.register()
        store = self.store
        class Cancelled(DemoTransport):
            def fetch(self, url, **kwargs):
                result = super().fetch(url, **kwargs)
                store.cancel(run_id)
                return result
        result = execute(self.store, run_id, Cancelled())
        self.assertEqual(result["run"]["status"], "cancelled")
        self.assertEqual(result["captures"], [])
        self.assertEqual(self.store.task_count(run_id, "notice"), 0)

    def test_blocked_stops_remaining_pages_and_never_reports_empty(self):
        class Blocked(DemoTransport):
            def fetch(self, url, **kwargs):
                self.calls.append((url, kwargs["kind"]))
                raise FetchError("access_blocked", 429, 1)
        transport = Blocked()
        report = execute(self.store, self.register(), transport)
        self.assertEqual(report["run"]["status"], "blocked")
        self.assertEqual(len(transport.calls), 1)
        self.assertGreater(report["pending"], 0)
        self.assertEqual(report["captures"][0]["http_status"], 429)

    def test_captcha_html_is_archived_and_blocks_run(self):
        class Captcha(DemoTransport):
            def fetch(self, url, **kwargs):
                return FetchResult(url, url, 200, {"content-type": "text/html"},
                                   "<h1>请输入验证码</h1>".encode(), "2026-09-30T00:00:00Z", 1)
        report = execute(self.store, self.register(), Captcha())
        self.assertEqual(report["run"]["status"], "blocked")
        self.assertEqual(report["captures"][0]["parsed"]["status"], "blocked")
        self.assertTrue(report["captures"][0]["sha256"])

    def test_failed_attachment_keeps_successful_notice_and_gap(self):
        class FailedAttachment(DemoTransport):
            def fetch(self, url, **kwargs):
                if kwargs["kind"] == "attachment":
                    raise FetchError("timeout", None, 2)
                return super().fetch(url, **kwargs)
        report = execute(self.store, self.register(), FailedAttachment())
        self.assertEqual(report["run"]["status"], "partial")
        notice = next(row for row in report["captures"] if row["kind"] == "notice")
        self.assertEqual(notice["parsed"]["status"], "ok")
        self.assertEqual(notice["parsed"]["attachments"][0]["fetch_status"], "failed")

    def test_attachment_not_requested_is_explicit_and_not_downloaded(self):
        request = request_spec(notice_urls=[NOTICE_URL])
        transport = DemoTransport()
        report = execute(self.store, self.register(request), transport)
        self.assertEqual(report["run"]["status"], "partial")
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(report["captures"][0]["parsed"]["attachments"][0]["status"], "not_requested")

    def test_denied_attachment_is_a_gap_not_an_arbitrary_fetch(self):
        class Deny:
            def authorize(self, url, kind):
                raise FetchError("url_not_authorized")
        transport = DemoTransport()
        transport.policy = Deny()
        report = execute(self.store, self.register(), transport)
        self.assertEqual(report["run"]["status"], "partial")
        self.assertFalse(any(kind == "attachment" for _, kind in transport.calls))
        notice = next(row for row in report["captures"] if row["kind"] == "notice")
        self.assertEqual(notice["parsed"]["attachments"][0]["status"], "not_authorized")

    def test_notice_limit_preserves_unfetched_count(self):
        class TwoNotices(DemoTransport):
            def fetch(self, url, **kwargs):
                r = super().fetch(url, **kwargs)
                if kwargs["kind"] == "listing":
                    row = ('<li><a href="' + NOTICE_URL.replace("99990001", "99990002")
                           + '">另一虚构项目</a><span>日期未知</span></li>').encode()
                    return FetchResult(r.url, r.final_url, 200, r.headers, r.body.replace(b"</ul>", row + b"</ul>"), r.fetched_at, 1)
                return r
        request = request_spec(keyword="软件", max_notices=1, attachments=True)
        report = execute(self.store, self.register(request), TwoNotices())
        self.assertEqual(report["run"]["status"], "partial")
        self.assertEqual(report["captures"][0]["parsed"]["notices_outside_run_limit"], 1)

    def test_decode_failure_preserves_original_bytes(self):
        class Undecodable(DemoTransport):
            def fetch(self, url, **kwargs):
                return FetchResult(url, url, 200, {"content-type": "text/html; charset=utf-8"},
                                   b"\xff\xfe", "2026-09-30T00:00:00Z", 1)
        report = execute(self.store, self.register(request_spec(keyword="软件")), Undecodable())
        self.assertEqual(report["run"]["status"], "failed")
        capture = report["captures"][0]
        self.assertEqual(capture["parsed"]["issues"], ["invalid_html_encoding"])
        self.assertEqual(self.store.read_blob(capture["sha256"]), b"\xff\xfe")

    def test_gb18030_keeps_raw_and_decoded_hash_separate(self):
        body = '<ul class="vT-srch-result-list-bid"></ul>共0条'.encode("gb18030")
        result = parse_capture("listing", body, {"content-type": "text/html; charset=gb18030"}, NOTICE_URL)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["raw_sha256"], sha256(body).hexdigest())
        self.assertNotEqual(result["raw_sha256"], result["decoded_input_sha256"])

    def test_html_download_not_reported_as_pdf(self):
        parsed = parse_capture("attachment", b"<html>Login</html>", {"content-type": "text/html"}, ATTACHMENT_URL)
        self.assertEqual(parsed["status"], "parse_error")
        self.assertEqual(parsed["issues"], ["attachment_is_html"])

    def test_request_contract_rejects_unknown_and_unbounded_inputs(self):
        for kwargs in ({}, {"keyword": "软件", "pages": True}, {"keyword": "软件", "pages": 6},
                       {"notice_urls": ["http://127.0.0.1/a.htm"]}, {"keyword": "软件", "notice_urls": [NOTICE_URL]},
                       {"notice_urls": [NOTICE_URL], "pages": 2}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                request_spec(**kwargs)

    def test_cli_demo_show_verify_replay_and_idempotency(self):
        self.store.close()
        def call(*args):
            output = io.StringIO()
            with redirect_stdout(output), patch("socket.socket", side_effect=AssertionError("离线CLI不得联网")):
                code = main(["--store", str(self.root), *args])
            return code, [json.loads(line) for line in output.getvalue().splitlines()]
        try:
            code, events = call("demo")
            self.assertEqual(code, 0)
            run_id = events[0]["run_id"]
            self.assertTrue(events[0]["simulation"])
            self.assertFalse(call("demo")[1][0]["created"])
            self.assertEqual(call("show", run_id)[1][0]["run"]["status"], "succeeded")
            self.assertEqual(call("verify")[1][0]["blob_count"], 3)
            self.assertEqual(call("replay", events[1]["captures"][0]["id"])[0], 0)
        finally:
            self.store = Store(self.root)

    def test_full_transport_policy_robots_archive_parse_chain(self):
        # 真实准入/robots/DNS判断和编排/SQLite贯通，仅最底层HTTP响应由虚构样本替代。
        now = datetime.now(timezone.utc)
        policy = SourcePolicy.from_dict({"schema_version": 1, "approved": True,
            "purpose": "synthetic integration test", "reviewed_at": (now - timedelta(hours=1)).isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(), "evidence": ["synthetic-only"],
            "attachments_allowed": True, "allowed": [
                {"host": "search.ccgp.gov.cn", "path_prefix": "/bxsearch", "kinds": ["listing"]},
                {"host": "www.ccgp.gov.cn", "path_prefix": "/cggg/", "kinds": ["notice", "attachment"]}]})
        fixture = DemoTransport()
        calls = []
        def exchange(url, ip, timeout, limit):
            calls.append(url)
            if url.endswith("/robots.txt"):
                return 200, {"content-type": "text/plain"}, b"User-agent: *\nAllow: /\n"
            kind = "listing" if "/bxsearch?" in url else ("attachment" if url.endswith(".pdf") else "notice")
            result = fixture.fetch(url, max_bytes=limit, kind=kind)
            return result.http_status, result.headers, result.body
        transport = Transport(policy, exchange=exchange, resolver=lambda host, port: ["8.8.8.8"], min_interval=0)
        with patch("socket.socket", side_effect=AssertionError("集成模拟不得联网")):
            report = execute(self.store, self.register(), transport)
        self.assertEqual(report["run"]["status"], "succeeded")
        self.assertEqual(sum(c["attempts"] for c in report["captures"]), 6)
        self.assertEqual(sum(url.endswith("/robots.txt") for url in calls), 2)

    def test_cli_missing_policy_persists_blocked_without_network(self):
        self.store.close()
        try:
            out = io.StringIO()
            with redirect_stdout(out), patch("socket.socket", side_effect=AssertionError("缺准入不得联网")):
                code = main(["--store", str(self.root), "collect", "--keyword", "软件", "--key", "missing-policy"])
            events = [json.loads(line) for line in out.getvalue().splitlines()]
            self.assertEqual(code, 3)
            self.assertEqual(events[0]["event"], "run_registered")
            self.assertEqual(events[-1]["run"]["status"], "blocked")
            self.assertEqual(events[-1]["captures"][0]["error_code"], "policy_required")
        finally:
            self.store = Store(self.root)

    def test_cli_database_failure_is_an_error_not_an_empty_success(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("services.ingestion.__main__.Store", side_effect=sqlite3.DatabaseError("fixture")):
            code = main(["--store", str(self.root), "verify"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(output.getvalue()), {"event": "error", "code": "storage_database_error"})


if __name__ == "__main__":
    unittest.main()

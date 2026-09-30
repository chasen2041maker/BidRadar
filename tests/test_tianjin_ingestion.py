"""天津接入负例与恢复验收：虚构令牌/响应，只验证安全边界和本地链路。"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from services.ingestion import tianjin as tj
from services.ingestion.archive import Store
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import replay
from services.ingestion.transport import FetchError, FetchResult
from services.processing.normalize import normalize_bundle
from services.catalog.store import Catalog


def response_body(*, rows=None, total=1):
    return json.dumps({"code": 200, "columnNames": ["公告标题", "项目编号", "采购人名称", "预算（万元）"],
                       "list": rows if rows is not None else [["虚构软件竞争性谈判公告", "DEMO-002", "虚构单位", "8"]],
                       "totalCount": total}, ensure_ascii=False).encode()


class FakeTianjin:
    def __init__(self, body=None):
        self.calls = []
        self.body = body if body is not None else response_body()

    def fetch_page(self, page_number, page_size, *, checkpoint):
        checkpoint()
        self.calls.append(page_number)
        url = tj.page_url(page_number, page_size)
        return FetchResult(url, url, 200, {"content-type": "application/json"}, self.body, "2026-09-30T10:00:00Z", 1)


class TianjinTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token_path = Path(self.temp.name) / "token.txt"
        self.fake_token = "fixture-token-no-real-permission"
        self.token_path.write_text(self.fake_token, encoding="utf-8")
        self.urls = []
        self.clock = patch.object(tj, "utc_now", return_value="2026-09-30T12:00:00Z")
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def exchange(self, url, ip, timeout, max_bytes):
        self.urls.append(url)
        return 200, {"content-type": "application/json", "set-cookie": "do-not-keep"}, response_body()

    def transport(self, **kwargs):
        return tj.TianjinTransport(allow_network=True, token_file=self.token_path,
                                  exchange=kwargs.get("exchange", self.exchange),
                                  resolver=kwargs.get("resolver", lambda *args: ["1.1.1.1"]))

    def test_official_page_parameter_order_and_no_credentials_in_result(self):
        result = self.transport().fetch_page(3, 10, checkpoint=lambda: None)
        query = parse_qs(urlsplit(self.urls[0]).query)
        self.assertEqual(query["page"], ["10"])
        self.assertEqual(query["pageNum"], ["3"])
        self.assertEqual(query["authToken"], [self.fake_token])
        self.assertNotIn(self.fake_token, repr(result))
        self.assertNotIn("authToken", result.url)
        self.assertNotIn("set-cookie", result.headers)

    def test_disabled_or_missing_credential_makes_no_request(self):
        disabled = tj.TianjinTransport(exchange=self.exchange)
        with self.assertRaises(FetchError) as error:
            disabled.fetch_page(1, 10, checkpoint=lambda: None)
        self.assertEqual(error.exception.code, "network_opt_in_required")
        self.token_path.write_text("", encoding="utf-8")
        with self.assertRaises(FetchError) as error:
            self.transport().fetch_page(1, 10, checkpoint=lambda: None)
        self.assertEqual(error.exception.code, "credential_missing_or_invalid")
        self.assertEqual(self.urls, [])

    def test_private_dns_and_expired_review_do_not_send_token(self):
        for addresses in (["127.0.0.1"], ["1.1.1.1", "10.0.0.1"], [], ["224.0.0.1"]):
            with self.subTest(addresses=addresses), self.assertRaises(FetchError):
                self.transport(resolver=lambda *args: addresses).fetch_page(1, 10, checkpoint=lambda: None)
        with patch.object(tj, "utc_now", return_value="2026-10-30T12:00:00Z"), self.assertRaises(FetchError) as error:
            self.transport().fetch_page(1, 10, checkpoint=lambda: None)
        self.assertEqual(error.exception.code, "source_terms_review_required")
        self.assertEqual(self.urls, [])

    def test_redirect_denial_and_error_are_single_attempt_and_never_leak(self):
        for status in (302, 401, 403, 429, 500):
            calls = []
            def exchange(*args):
                calls.append(args[0])
                return status, {"location": "https://untrusted.example"}, b""
            with self.subTest(status=status), self.assertRaises(FetchError) as error:
                self.transport(exchange=exchange).fetch_page(1, 10, checkpoint=lambda: None)
            self.assertEqual(len(calls), 1)
            self.assertEqual(error.exception.attempts, 1)
            self.assertNotIn(self.fake_token, str(error.exception))
        def error_exchange(*args):
            raise RuntimeError(args[0])
        with self.assertRaises(FetchError) as error:
            self.transport(exchange=error_exchange).fetch_page(1, 10, checkpoint=lambda: None)
        self.assertNotIn(self.fake_token, str(error.exception))

    def test_credential_echo_and_html_never_reach_archive(self):
        for headers, body in (({"content-type": "application/json"}, self.fake_token.encode()),
                              ({"content-type": "application/json"}, b'{"authToken":"hidden"}'),
                              ({"content-type": "text/html"}, b"login")):
            with self.assertRaises(FetchError):
                self.transport(exchange=lambda *args: (200, headers, body)).fetch_page(1, 10, checkpoint=lambda: None)

    def test_cancel_after_dns_stops_request(self):
        count = 0
        def checkpoint():
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.transport().fetch_page(1, 10, checkpoint=checkpoint)
        self.assertEqual(self.urls, [])


class TianjinParsingTests(unittest.TestCase):
    def test_zero_result_differs_from_business_error_and_preview(self):
        self.assertEqual(tj.parse_page(response_body(rows=[], total=0), 10)["status"], "empty")
        self.assertEqual(tj.parse_page(b'{"code":401,"msg":"denied"}', 10)["status"], "blocked")
        value = json.loads(response_body())
        value["previewCount"] = 20
        self.assertEqual(tj.parse_page(json.dumps(value).encode(), 10)["status"], "parse_error")

    def test_unknown_shape_short_row_duplicate_columns_and_placeholder_rejected(self):
        sample = json.loads(response_body())
        for modified in ({"code": 200, "data": []},
                         {**sample, "list": [["虚构", "short"]]},
                         {**sample, "columnNames": ["a", "a", "b", "c"]},
                         {**sample, "list": [["该字段不支持预览", "x", "y", "z"]]},
                         {**sample, "totalCount": True}):
            with self.subTest(value=modified):
                self.assertEqual(tj.parse_page(json.dumps(modified).encode(), 10)["status"], "parse_error")

    def test_limited_parameters_reject_bool_and_overflow(self):
        for args in ({"pages": True}, {"page_size": 5000}, {"start_page": 0}, {"start_page": 10000, "pages": 2}):
            with self.assertRaises(ValueError):
                tj.request_spec(**args)


class TianjinPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "ingestion")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def run_id(self, key="test", **kwargs):
        # 显式虚构标记由测试添加；真实CLI不提供把模拟伪装为真采集的开关。
        return self.store.create_run({**tj.request_spec(**kwargs), "simulation": True}, key)[0]

    def test_full_local_chain_preserves_api_snapshot_and_attribution(self):
        run = self.run_id(pages=2, page_size=1)
        fake = FakeTianjin(response_body(total=2))
        with patch("socket.socket", side_effect=AssertionError("不应联网")):
            report = tj.execute(self.store, run, fake)
            exported = export_bundle(self.store, run)
            bundle = normalize_bundle(exported)
        self.assertEqual(fake.calls, [1, 2])
        self.assertEqual(report["run"]["status"], "succeeded")
        self.assertEqual(len(bundle["observations"]), 2)
        first = bundle["observations"][0]
        self.assertEqual(first["identity_kind"], "content_snapshot")
        self.assertEqual(first["material_status"], "not_provided_by_api")
        self.assertEqual(first["attribution"], tj.ATTRIBUTION)
        self.assertEqual(first["notice_id"], bundle["observations"][1]["notice_id"])
        self.assertNotEqual(first["observation_id"], bundle["observations"][1]["observation_id"])
        sources = [r[0] for r in self.store.db.execute("SELECT DISTINCT source_id FROM versions")]
        self.assertEqual(sources, [tj.SOURCE_ID])
        catalog = Catalog(Path(self.temp.name) / "catalog")
        try:
            catalog.import_bundle(bundle)
            self.assertEqual(catalog.query(simulation=True)["total"], 1)
        finally:
            catalog.close()

    def test_repeat_run_and_offline_replay_keep_original_capture(self):
        run = self.run_id()
        fake = FakeTianjin()
        report = tj.execute(self.store, run, fake)
        tj.execute(self.store, run, fake)
        self.assertEqual(fake.calls, [1])
        before = deepcopy(report["captures"][0])
        with patch("socket.socket", side_effect=AssertionError("重放不能联网")):
            replayed = replay(self.store, before["id"])
        self.assertEqual(replayed["parser_version"], tj.PARSER_VERSION)
        self.assertEqual(self.store.capture(before["id"]), before)

    def test_interrupt_resumes_only_uncommitted_page(self):
        run = self.run_id(pages=2, page_size=1)
        class Interrupt(FakeTianjin):
            def fetch_page(self, page_number, page_size, **kwargs):
                if page_number == 2:
                    raise KeyboardInterrupt
                return super().fetch_page(page_number, page_size, **kwargs)
        with self.assertRaises(KeyboardInterrupt):
            tj.execute(self.store, run, Interrupt(response_body(total=2)))
        self.assertEqual(self.store.run(run)["status"], "queued")
        resumed = FakeTianjin(response_body(total=2))
        tj.execute(self.store, run, resumed)
        self.assertEqual(resumed.calls, [2])
        self.assertEqual(len(self.store.report(run)["captures"]), 2)

    def test_cancel_in_flight_prevents_commit_and_next_page(self):
        run = self.run_id(pages=2, page_size=1)
        store = self.store
        class Cancel(FakeTianjin):
            def fetch_page(self, *args, **kwargs):
                result = super().fetch_page(*args, **kwargs)
                store.cancel(run)
                return result
        fake = Cancel()
        result = tj.execute(store, run, fake)
        self.assertEqual(result["run"]["status"], "cancelled")
        self.assertEqual(result["captures"], [])
        tj.execute(store, run, FakeTianjin())
        self.assertEqual(fake.calls, [1])

    def test_schema_error_preserves_raw_and_zero_result_has_no_failure(self):
        failed = tj.execute(self.store, self.run_id(), FakeTianjin(b'{"code":200,"data":[]}'))
        self.assertEqual(failed["run"]["status"], "failed")
        self.assertTrue(failed["captures"][0]["sha256"])
        self.assertEqual(len(export_bundle(self.store, failed["run"]["id"])["failures"]), 1)
        empty = tj.execute(self.store, self.run_id("empty"), FakeTianjin(response_body(rows=[], total=0)))
        self.assertEqual(empty["run"]["status"], "succeeded")
        self.assertEqual(export_bundle(self.store, empty["run"]["id"])["documents"], [])

    def test_empty_page_with_remaining_total_is_failure_not_zero_result(self):
        report = tj.execute(self.store, self.run_id(), FakeTianjin(response_body(rows=[], total=10)))
        self.assertEqual(report["run"]["status"], "failed")
        self.assertEqual(report["run"]["error_code"], "inconsistent_pagination")

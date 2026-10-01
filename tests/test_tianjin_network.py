"""真实环境发现fake-IP后补的解析负例；仅虚构DNS/接口，不使用账号或公网。"""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from services.ingestion import tianjin as tj
from services.ingestion.__main__ import main
from services.ingestion.archive import Store
from services.ingestion.transport import FetchError, FetchResult


def dns_answer():
    return {"Status": 0, "TC": False, "CD": False,
            "Question": [{"name": tj.HOST + ".", "type": 1}],
            "Answer": [{"name": tj.HOST + ".", "type": 1, "TTL": 60, "data": "1.1.1.1"}]}


class PublicDnsTests(unittest.TestCase):
    def resolve(self, value):
        return tj.resolve_public_dns(tj.HOST, 443, 10, exchange=lambda *args:
                                     (200, {"content-type": "application/json"}, json.dumps(value).encode()))

    def test_fixed_https_resolver_receives_only_official_public_hostname(self):
        calls = []
        def exchange(*args):
            calls.append(args)
            return 200, {"content-type": "application/json; charset=UTF-8"}, json.dumps(dns_answer()).encode()
        self.assertEqual(tj.resolve_public_dns(tj.HOST, 443, 30, exchange=exchange), ["1.1.1.1"])
        url, ip, timeout, max_bytes = calls[0]
        self.assertEqual((urlsplit(url).scheme, urlsplit(url).hostname, ip), ("https", "dns.google", "8.8.8.8"))
        self.assertEqual(parse_qs(urlsplit(url).query),
                         {"name": [tj.HOST], "type": ["A"], "edns_client_subnet": ["0.0.0.0/0"]})
        self.assertEqual((timeout, max_bytes), (10, 65536))

    def test_dns_question_and_response_flags_are_not_assumed(self):
        variants = [{"Status": 2}, {"Status": False}, {"TC": True}, {"CD": True},
                    {"Question": []}, {"Question": [{"name": "other.example.", "type": 1}]},
                    {"Question": [{"name": tj.HOST + ".", "type": True}]}, {"Answer": []}]
        for change in variants:
            with self.subTest(change=change), self.assertRaises(FetchError) as error:
                self.resolve({**dns_answer(), **change})
            self.assertEqual(error.exception.code, "doh_response_rejected")

    def test_every_answer_must_be_same_name_public_a_record_with_positive_ttl(self):
        variants = [{"data": "198.18.1.97"}, {"data": "127.0.0.1"}, {"data": "224.0.0.1"},
                    {"data": "2606:4700:4700::1111"}, {"name": "other.example."},
                    {"type": 5, "data": "alias.example."}, {"TTL": 0}, {"TTL": True}]
        for change in variants:
            value = dns_answer()
            # 即使首个IP安全，第二个地址不安全也必须拒绝整份响应。
            value["Answer"].append({**deepcopy(value["Answer"][0]), **change})
            with self.subTest(change=change), self.assertRaises(FetchError):
                self.resolve(value)

    def test_http_redirect_nonjson_oversize_duplicate_keys_and_network_failure_stop(self):
        sample = json.dumps(dns_answer()).encode()
        for status, headers, body in [(302, {}, b""), (403, {}, b""),
                (200, {"content-type": "text/html"}, b"login"),
                (200, {"content-type": "application/json"}, b" " * 65537),
                (200, {"content-type": "application/json"}, b'{"Status":0,' + sample[1:])]:
            calls = []
            def exchange(*args):
                calls.append(args)
                return status, headers, body
            with self.assertRaises(FetchError):
                tj.resolve_public_dns(tj.HOST, 443, 10, exchange=exchange)
            self.assertEqual(len(calls), 1)
        def fail(*args):
            raise OSError("private diagnostic that must not escape")
        with self.assertRaises(FetchError) as error:
            tj.resolve_public_dns(tj.HOST, 443, 10, exchange=fail)
        self.assertEqual(str(error.exception), "doh_transport_error")

    def test_no_arbitrary_target_or_implicit_network_fallback(self):
        def forbidden(*args):
            raise AssertionError("不能触发网络")
        with self.assertRaises(FetchError) as error:
            tj.resolve_public_dns("other.example", 443, 10, exchange=forbidden)
        self.assertEqual(error.exception.code, "dns_target_not_allowed")
        with patch.object(tj, "resolve_public_dns", side_effect=forbidden):
            transport = tj.TianjinTransport(dns_mode="google-doh")
            with self.assertRaises(FetchError) as error:
                transport.fetch_page(1, 1, checkpoint=lambda: None)
        self.assertEqual(error.exception.code, "network_opt_in_required")
        self.assertNotIn("dns_mode", tj.request_spec())
        self.assertEqual(tj.request_spec(dns_mode="google-doh")["dns_mode"], "google-doh")
        with self.assertRaises(ValueError):
            tj.request_spec(dns_mode="arbitrary-resolver")

    def test_resume_inherits_saved_dns_and_refuses_silent_mode_change(self):
        class EmptyTransport:
            source_id = tj.SOURCE_ID
            def fetch_page(self, page_number, page_size, *, checkpoint):
                checkpoint()
                url = tj.page_url(page_number, page_size)
                body = b'{"code":200,"columnNames":["title"],"list":[],"totalCount":0}'
                return FetchResult(url, url, 200, {"content-type": "application/json"}, body,
                                   "2026-09-30T12:00:00Z", 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "ingestion"
            for mode in ("system", "google-doh"):
                store = Store(root)
                try:
                    request = {**tj.request_spec(dns_mode=mode), "simulation": True}
                    run = store.create_run(request, "resume-" + mode)[0]
                finally:
                    store.close()
                with patch.object(tj, "TianjinTransport", return_value=EmptyTransport()) as factory:
                    with redirect_stdout(io.StringIO()):
                        result = main(["--store", str(root), "resume", run, "--allow-network"])
                    self.assertEqual(result, 0)
                    self.assertEqual(factory.call_args.kwargs["dns_mode"], mode)
                with patch.object(tj, "TianjinTransport", side_effect=AssertionError("不能换模式发请求")):
                    with redirect_stdout(io.StringIO()) as output:
                        result = main(["--store", str(root), "resume", run, "--dns-mode",
                                       "system" if mode == "google-doh" else "google-doh"])
                    self.assertEqual(result, 4)
                    self.assertIn("dns_mode_must_match_run", output.getvalue())


if __name__ == "__main__":
    unittest.main()

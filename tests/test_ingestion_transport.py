"""虚构响应与本地socket替身验证边界；任何测试均不访问真实CCGP。"""
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import patch

from services.ingestion.transport import (
    FetchError, SourcePolicy, Transport, _exchange, _parse_robots,
)

NOW = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
NOTICE = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_123.htm"
OTHER = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_456.htm"
ROBOTS = "https://www.ccgp.gov.cn/robots.txt"
ALLOW = (200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow:\n")
PAGE = (200, {"content-type": "text/html"}, b"<html>sample</html>")


def policy_data():
    return {
        "schema_version": 1, "approved": True, "purpose": "虚构离线测试",
        "reviewed_at": "2026-09-30T09:00:00Z", "expires_at": "2026-10-01T09:00:00Z",
        "evidence": ["虚构规则，禁止据此访问真实来源"], "attachments_allowed": False,
        "allowed": [
            {"host": "www.ccgp.gov.cn", "path_prefix": "/cggg/", "kinds": ["notice", "attachment"]},
            {"host": "search.ccgp.gov.cn", "path_prefix": "/bxsearch", "kinds": ["listing"]},
        ],
    }


class Clock:
    def __init__(self):
        self.value = 0
        self.waits = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.value += seconds

    def utcnow(self):
        return NOW + timedelta(seconds=self.value)


class Exchange:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class TransportTests(unittest.TestCase):
    def transport(self, responses, *, policy=None, resolver=None, **kwargs):
        self.clock = Clock()
        self.exchange = Exchange(responses)
        return Transport(policy or SourcePolicy.from_dict(policy_data()), exchange=self.exchange,
                         resolver=resolver or (lambda host, port: ["8.8.8.8"]),
                         clock=self.clock, sleep=self.clock.sleep, utcnow=self.clock.utcnow,
                         **kwargs)

    def fetch(self, transport, url=NOTICE, **kwargs):
        return transport.fetch(url, max_bytes=kwargs.get("max_bytes", 1024), kind=kwargs.get("kind", "notice"))

    def assert_error(self, code, transport, url=NOTICE, **kwargs):
        with self.assertRaises(FetchError) as caught:
            self.fetch(transport, url, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception

    def test_policy_missing_never_calls_network(self):
        transport = Transport(None)
        self.assert_error("policy_required", transport)
        self.assertEqual(0, transport.requests)

    def test_policy_file_roundtrip_and_reject_unapproved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy_data()), encoding="utf-8")
            self.assertFalse(SourcePolicy.from_file(path).attachments_allowed)
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(FetchError):
                SourcePolicy.from_file(path)

    def test_invalid_policy_fields(self):
        for key, value in (("approved", False), ("schema_version", True), ("purpose", ""),
                           ("evidence", []), ("attachments_allowed", 1),
                           ("expires_at", "2026-09-01T00:00:00Z"), ("reviewed_at", "2026-09-30")):
            with self.subTest(key=key):
                data = policy_data()
                data[key] = value
                with self.assertRaises(FetchError):
                    SourcePolicy.from_dict(data)

    def test_policy_host_and_prefix_reject_escape(self):
        for host, prefix in (("evil.test", "/"), ("www.ccgp.gov.cn", "/cggg/../"),
                             ("www.ccgp.gov.cn", "/cggg/%2f")):
            data = policy_data()
            data["allowed"][0].update(host=host, path_prefix=prefix)
            with self.assertRaises(FetchError):
                SourcePolicy.from_dict(data)

    def test_expired_policy_never_fetches_robots(self):
        data = policy_data()
        data["expires_at"] = "2026-09-30T09:30:00Z"
        transport = self.transport([], policy=SourcePolicy.from_dict(data))
        self.assert_error("policy_expired_or_not_yet_valid", transport)
        self.assertEqual([], self.exchange.calls)

    def test_success_preserves_bytes_and_counts_robots(self):
        transport = self.transport([ALLOW, PAGE, PAGE])
        first = self.fetch(transport)
        second = self.fetch(transport, OTHER)
        self.assertEqual(PAGE[2], first.body)
        self.assertEqual((2, 1), (first.attempts, second.attempts))
        self.assertEqual("2026-09-30T10:00:02Z", first.fetched_at)
        self.assertEqual([ROBOTS, NOTICE, OTHER], [call[0] for call in self.exchange.calls])
        self.assertEqual(["8.8.8.8"] * 3, [call[1] for call in self.exchange.calls])

    def test_url_scope_and_ambiguous_encoding_never_fetch(self):
        bad = ["https://evil.test/cggg/a.htm", "https://www.ccgp.gov.cn:443/cggg/a.htm",
               "https://user@www.ccgp.gov.cn/cggg/a.htm", NOTICE + "#x",
               NOTICE.replace("/cggg/", "/%2fcggg/"), NOTICE.replace("/cggg/", "/%252ecggg/"),
               NOTICE.replace("/cggg/", "/a/../cggg/"), "file:///etc/passwd", NOTICE + "\n",
               "http://[bad", None, 123]
        for url in bad:
            with self.subTest(url=url):
                transport = self.transport([])
                self.assert_error("url_not_allowed", transport, url)
                self.assertEqual([], self.exchange.calls)

    def test_listing_exact_path_not_prefix(self):
        transport = self.transport([])
        self.assert_error("url_not_allowed", transport, "https://search.ccgp.gov.cn/bxsearch-other", kind="listing")

    def test_attachments_require_separate_permission(self):
        transport = self.transport([])
        self.assert_error("attachment_not_approved", transport, NOTICE.replace(".htm", ".pdf"), kind="attachment")
        data = policy_data()
        data["attachments_allowed"] = True
        transport = self.transport([ALLOW, (200, {}, b"%PDF-test")], policy=SourcePolicy.from_dict(data))
        self.assertEqual(b"%PDF-test", self.fetch(transport, NOTICE.replace(".htm", ".pdf"), kind="attachment").body)

    def test_private_mixed_and_empty_dns_answers_fail_closed(self):
        for addresses in (["127.0.0.1"], ["8.8.8.8", "10.0.0.2"], [], ["::1"], ["169.254.169.254"], ["bad"]):
            transport = self.transport([], resolver=lambda h, p: addresses)
            # robots本身也执行公网DNS规则；错误保守归类为规则不可取得。
            error = self.assert_error("robots_unavailable", transport)
            self.assertEqual(0, error.attempts)
            self.assertEqual([], self.exchange.calls)

    def test_dns_rebinding_after_robots_is_rejected(self):
        answers = iter([["8.8.8.8"], ["127.0.0.1"]])
        transport = self.transport([ALLOW], resolver=lambda h, p: next(answers))
        error = self.assert_error("dns_not_public", transport)
        self.assertEqual(1, error.attempts)

    def test_non_unicast_dns_is_rejected_even_if_is_global_is_true(self):
        # 224.0.0.1/ff0e::1的is_global为True，仍不允许把采购HTTP请求发向多播组。
        blocked = ["224.0.0.1", "239.255.255.250", "ff0e::1", "0.0.0.0", "::",
                   "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "240.0.0.1",
                   "100.64.0.1", "192.0.2.1", "fec0::1", "2001:db8::1",
                   "::ffff:8.8.8.8", "2002:0808:0808::1", "2606:4700:4700::1111%eth0"]
        for address in blocked:
            with self.subTest(address=address):
                answers = iter([["8.8.8.8"], ["8.8.8.8", address]])
                transport = self.transport([ALLOW], resolver=lambda h, p: next(answers))
                error = self.assert_error("dns_not_public", transport)
                self.assertEqual(1, error.attempts)
                self.assertEqual([ROBOTS], [call[0] for call in self.exchange.calls])

    def test_public_ipv6_unicast_can_be_pinned(self):
        transport = self.transport([ALLOW, PAGE], resolver=lambda h, p: ["2606:4700:4700::1111"])
        self.assertEqual(PAGE[2], self.fetch(transport).body)
        self.assertEqual(["2606:4700:4700::1111"] * 2, [call[1] for call in self.exchange.calls])

    def test_robots_unavailable_does_not_send_notice(self):
        for response in ((404, {}, b""), (200, {}, b""), (200, {}, b"<html>challenge</html>"),
                         (200, {}, b"invalid rules"), (200, {}, b"User-agent: *\nCrawl-delay: -1")):
            with self.subTest(response=response):
                transport = self.transport([response])
                self.assert_error("robots_unavailable", transport)
                self.assertEqual(1, len(self.exchange.calls))

    def test_robots_disallow_applies_to_encoded_unreserved_path(self):
        transport = self.transport([(200, {}, b"User-agent: *\nDisallow: /cggg/")])
        self.assert_error("robots_denied", transport, NOTICE.replace("/cggg", "/%63ggg"))
        self.assertEqual(1, len(self.exchange.calls))

    def test_robots_specific_group_wildcards_and_allow_precedence(self):
        robots = _parse_robots(b"User-agent: *\nDisallow: /\n\nUser-agent: BidRadar\nDisallow: /cggg/*\nAllow: /cggg/ok.htm$\n")
        self.assertTrue(robots.permits("https://www.ccgp.gov.cn/cggg/ok.htm"))
        self.assertFalse(robots.permits("https://www.ccgp.gov.cn/cggg/ok.htm?x=1"))
        self.assertFalse(robots.permits(NOTICE))

    def test_robots_many_wildcards_do_not_backtrack_exponentially(self):
        robots = _parse_robots(b"User-agent: *\nDisallow: /" + b"*a" * 60 + b"b$")
        self.assertTrue(robots.permits("https://www.ccgp.gov.cn/" + "a" * 2000))

    def test_robots_resource_limit_fails_closed(self):
        with self.assertRaises(FetchError):
            _parse_robots(b"User-agent: *\n" + b"Disallow: /x\n" * 257)

    def test_robots_crawl_delay_and_request_rate_are_respected(self):
        transport = self.transport([(200, {}, b"User-agent: *\nCrawl-delay: 3\nRequest-rate: 1/5\nDisallow:"), PAGE, PAGE])
        self.fetch(transport)
        self.fetch(transport, OTHER)
        self.assertEqual([5, 5], self.clock.waits)

    def test_robots_403_blocks_host_no_second_request(self):
        transport = self.transport([(403, {}, b"")])
        error = self.assert_error("blocked", transport)
        self.assertEqual((403, 1), (error.http_status, error.attempts))
        self.assert_error("blocked", transport)
        self.assertEqual(1, transport.requests)

    def test_403_429_never_retry_or_follow_later(self):
        for status in (403, 429):
            transport = self.transport([ALLOW, (status, {"retry-after": "30"}, b"blocked")])
            error = self.assert_error("blocked", transport)
            self.assertEqual((status, 2), (error.http_status, error.attempts))
            self.assertEqual(0, self.assert_error("blocked", transport, OTHER).attempts)
            self.assertEqual(2, len(self.exchange.calls))

    def test_503_retry_after_and_network_retry_are_bounded(self):
        transport = self.transport([ALLOW, (503, {"Retry-After": "7"}, b""), PAGE])
        self.assertEqual(3, self.fetch(transport).attempts)
        self.assertIn(7, self.clock.waits)
        transport = self.transport([ALLOW, TimeoutError(), TimeoutError()])
        self.assertEqual(3, self.assert_error("network_error", transport).attempts)

    def test_retry_after_http_date_and_long_delay(self):
        transport = self.transport([ALLOW, (503, {"retry-after": "Wed, 30 Sep 2026 10:00:12 GMT"}, b""), PAGE])
        self.fetch(transport)
        self.assertIn(10, self.clock.waits)
        transport = self.transport([ALLOW, (503, {"retry-after": "3600"}, b"")])
        self.assert_error("retry_after_out_of_bounds", transport)
        self.assertEqual(2, len(self.exchange.calls))

    def test_tls_error_not_retried(self):
        transport = self.transport([ALLOW, ssl.SSLError("certificate")])
        self.assertEqual(2, self.assert_error("tls_error", transport).attempts)

    def test_redirect_revalidates_url_dns_and_robots(self):
        transport = self.transport([ALLOW, (302, {"location": OTHER}, b""), PAGE])
        result = self.fetch(transport)
        self.assertEqual((NOTICE, OTHER, 3), (result.url, result.final_url, result.attempts))

    def test_cross_host_redirect_requires_destination_robots(self):
        data = policy_data()
        data["allowed"].append({"host": "ccgp.gov.cn", "path_prefix": "/cggg/", "kinds": ["notice"]})
        destination = NOTICE.replace("www.ccgp", "ccgp")
        transport = self.transport([ALLOW, (302, {"location": destination}, b""),
                                    (200, {}, b"User-agent: *\nDisallow: /")], policy=SourcePolicy.from_dict(data))
        self.assert_error("robots_denied", transport)
        self.assertEqual("https://ccgp.gov.cn/robots.txt", self.exchange.calls[-1][0])

    def test_redirect_external_downgrade_and_loop_rejected(self):
        for destination, code in (("https://evil.test/x.htm", "url_not_allowed"),
                                  (NOTICE.replace("https:", "http:"), "redirect_not_allowed")):
            transport = self.transport([ALLOW, (302, {"location": destination}, b"")])
            self.assert_error(code, transport)
            self.assertEqual(2, len(self.exchange.calls))
        transport = self.transport([ALLOW] + [(302, {"location": NOTICE}, b"")] * 4)
        self.assertEqual(5, self.assert_error("redirect_limit", transport).attempts)

    def test_body_limit_and_compression_rejected(self):
        transport = self.transport([ALLOW, (200, {}, b"x" * 1025)])
        self.assert_error("body_too_large", transport)
        transport = self.transport([ALLOW, (200, {"Content-Encoding": "gzip"}, b"compressed")])
        self.assert_error("content_encoding_not_supported", transport)

    def test_request_budget_counts_robots_and_redirects(self):
        transport = self.transport([ALLOW, (302, {"location": OTHER}, b"")], max_requests=2)
        self.assertEqual(2, self.assert_error("request_budget_exceeded", transport).attempts)

    def test_time_budget_and_policy_expiry_rechecked_after_wait(self):
        transport = self.transport([ALLOW], max_seconds=1)
        self.assert_error("time_budget_exceeded", transport)
        data = policy_data()
        data["expires_at"] = "2026-09-30T10:00:01Z"
        transport = self.transport([ALLOW], policy=SourcePolicy.from_dict(data))
        self.assert_error("policy_expired_or_not_yet_valid", transport)
        self.assertEqual(1, transport.requests)

    def test_failed_downloads_consume_total_byte_budget(self):
        transport = self.transport([ALLOW, TimeoutError(), TimeoutError()])
        with patch("services.ingestion.transport.MAX_TOTAL_BYTES", 2100):
            self.assert_error("network_error", transport)
            self.assertGreaterEqual(transport.bytes_received, 2048)

    def test_invalid_constructor_and_body_limits(self):
        for key, value in (("timeout", True), ("timeout", 31), ("max_attempts", 4),
                           ("max_requests", 101), ("max_seconds", 601), ("min_interval", float("nan"))):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    Transport(None, **{key: value})
        transport = self.transport([])
        for value in (0, True, 17 * 1024 * 1024):
            self.assert_error("invalid_byte_limit", transport, max_bytes=value)


class FakeSocket:
    def __init__(self, payload):
        self.payload = payload
        self.sent = b""
        self.connected = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.connected = address

    def sendall(self, data):
        self.sent += data

    def makefile(self, mode):
        return BytesIO(self.payload)

    def close(self):
        pass

    def shutdown(self, how):
        pass


class SocketBoundaryTests(unittest.TestCase):
    def test_actual_http_adapter_pins_ip_keeps_host_and_tls_hostname(self):
        sock = FakeSocket(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        with patch("services.ingestion.transport.socket.socket", return_value=sock), \
                patch("services.ingestion.transport.ssl.create_default_context") as context, \
                patch("services.ingestion.transport.socket.getaddrinfo", side_effect=AssertionError("must not resolve twice")):
            context.return_value.wrap_socket.return_value = sock
            self.assertEqual(b"ok", _exchange(NOTICE, "8.8.8.8", 5, 1024)[2])
            context.return_value.wrap_socket.assert_called_once_with(sock, server_hostname="www.ccgp.gov.cn")
        self.assertEqual(("8.8.8.8", 443), sock.connected)
        self.assertIn(b"Host: www.ccgp.gov.cn\r\n", sock.sent)
        self.assertIn(b"Accept-Encoding: identity\r\n", sock.sent)

    def test_actual_reader_rejects_oversize_and_truncated_bodies(self):
        for payload, limit, code in (
                (b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx", 10, "body_too_large"),
                (b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx", 20, "truncated_body"),
                (b"HTTP/1.1 200 OK\r\n\r\n123456", 5, "body_too_large")):
            sock = FakeSocket(payload)
            with patch("services.ingestion.transport.socket.socket", return_value=sock):
                with self.assertRaises(FetchError) as caught:
                    _exchange(NOTICE.replace("https:", "http:"), "8.8.8.8", 5, limit)
                self.assertEqual(code, caught.exception.code)

    def test_actual_reader_returns_403_before_error_body(self):
        sock = FakeSocket(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 999999\r\nContent-Encoding: gzip\r\n\r\nx")
        with patch("services.ingestion.transport.socket.socket", return_value=sock):
            status, _, body = _exchange(NOTICE.replace("https:", "http:"), "8.8.8.8", 5, 10)
            self.assertEqual((403, b""), (status, body))


if __name__ == "__main__":
    unittest.main()

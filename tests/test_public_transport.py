"""公开来源新增门控与DNS负例；仅注入虚构响应，绝不访问外网。"""
from datetime import datetime, timezone
import json
import unittest

from services.ingestion.public_dns import resolve_public_dns
from services.ingestion.transport import FetchError, SourcePolicy, Transport

NOW = datetime(2026, 9, 30, tzinfo=timezone.utc)
NOTICE = "https://ggzy.hainan.gov.cn/ggzy/ggzy/cggg/293740.jhtml"


def policy(**overrides):
    value = {"schema_version": 2, "approved": True, "purpose": "虚构离线测试",
        "reviewed_at": "2026-09-29T00:00:00Z", "expires_at": "2026-10-01T00:00:00Z",
        "evidence": ["虚构政策，不得据此访问网站"], "attachments_allowed": False,
        "robots_missing": "allow_404", "allowed": [{"host": "ggzy.hainan.gov.cn",
            "path_prefix": "/ggzy/ggzy/cggg/", "kinds": ["notice", "listing"]}]}
    value.update(overrides)
    return SourcePolicy.from_dict(value)


class PublicTransportTests(unittest.TestCase):
    def transport(self, statuses, **options):
        self.calls = []
        def exchange(url, *_):
            self.calls.append(url)
            status = statuses.pop(0)
            return status, {"content-type": "text/html"}, b"<html>sample</html>" if status == 200 else b""
        return Transport(policy(), exchange=exchange, resolver=lambda *_: ["8.8.8.8"],
                         utcnow=lambda: NOW, min_interval=0, max_attempts=1, **options)

    def test_missing_robots_only_explicit_404_is_allowed(self):
        transport = self.transport([404, 200])
        result = transport.fetch(NOTICE, kind="notice", max_bytes=1024)
        self.assertEqual(200, result.http_status)
        self.assertEqual(2, len(self.calls))
        transport = self.transport([404])
        transport.policy = policy(robots_missing="deny")
        with self.assertRaises(FetchError) as caught:
            transport.fetch(NOTICE, kind="notice", max_bytes=1024)
        self.assertEqual("robots_unavailable", caught.exception.code)

    def test_denial_and_server_failure_do_not_inherit_404_permission(self):
        for status in (401, 403, 429, 500, 503):
            with self.subTest(status=status):
                transport = self.transport([status])
                with self.assertRaises(FetchError):
                    transport.fetch(NOTICE, kind="notice", max_bytes=1024)
                self.assertEqual(1, len(self.calls))

    def test_opt_in_and_policy_scope_before_network(self):
        transport = self.transport([], allow_network=False)
        with self.assertRaises(FetchError) as caught:
            transport.fetch(NOTICE, kind="notice", max_bytes=1024)
        self.assertEqual("network_opt_in_required", caught.exception.code)
        self.assertEqual([], self.calls)
        for url, kind in (("https://ggzy.hainan.gov.cn/login", "notice"),
                          ("https://ggzy.hainan.gov.cn/ggzy/ggzy/cggg/a.pdf", "attachment"),
                          ("https://evil.example/ggzy/ggzy/cggg/123.jhtml", "notice")):
            with self.subTest(url=url), self.assertRaises(FetchError):
                policy().authorize(url, kind)

    def test_old_policy_cannot_opt_in_missing_robots_or_new_host(self):
        with self.assertRaises(FetchError):
            policy(schema_version=1)

    def test_hainan_default_port_redirect_keeps_scope_and_rejects_other_port(self):
        # 官网真实重定向带:443；仅允许此来源的协议默认端口，不开放任意端口。
        value = policy()
        value._authorize(NOTICE.replace(".cn/", ".cn:443/"), "notice", NOW)
        with self.assertRaises(FetchError):
            value._authorize(NOTICE.replace(".cn/", ".cn:444/"), "notice", NOW)


class PublicDnsTests(unittest.TestCase):
    def payload(self, answers=None):
        return {"Status": 0, "TC": False, "CD": False,
            "Question": [{"name": "www.ccgp.gov.cn.", "type": 1}], "Answer": answers or [
                {"name": "www.ccgp.gov.cn.", "type": 5, "TTL": 100, "data": "cdn.example."},
                {"name": "cdn.example.", "type": 1, "TTL": 100, "data": "8.8.4.4"}]}

    def resolve(self, payload):
        self.calls = []
        def exchange(*args):
            self.calls.append(args)
            return 200, {"content-type": "application/json"}, json.dumps(payload).encode()
        return resolve_public_dns("www.ccgp.gov.cn", 443, 7, exchange=exchange)

    def test_finite_connected_cname_chain_pins_dns_provider(self):
        self.assertEqual(["8.8.4.4"], self.resolve(self.payload()))
        self.assertEqual(("8.8.8.8", 7, 65536), self.calls[0][1:])
        self.assertIn("name=www.ccgp.gov.cn", self.calls[0][0])

    def test_unconnected_private_loop_ambiguous_and_truncated_answers_rejected(self):
        changes = [
            lambda p: p["Answer"].append({"name": "elsewhere.example.", "type": 1, "TTL": 10, "data": "8.8.8.8"}),
            lambda p: p["Answer"][1].update(data="198.18.1.2"),
            lambda p: p["Answer"][1].update(data=134744072),
            lambda p: p["Answer"][1].update(type=5, data="www.ccgp.gov.cn."),
            lambda p: p["Answer"].append({"name": "www.ccgp.gov.cn.", "type": 5, "TTL": 10, "data": "other.example."}),
            lambda p: p.update(TC=True),
            lambda p: p["Question"][0].update(name="other.example."),
        ]
        for change in changes:
            with self.subTest(change=change):
                payload = self.payload()
                change(payload)
                with self.assertRaises(FetchError) as caught:
                    self.resolve(payload)
                self.assertEqual("doh_response_rejected", caught.exception.code)

    def test_external_target_rejected_before_dns(self):
        with self.assertRaises(FetchError) as caught:
            resolve_public_dns("unapproved.example", 443)
        self.assertEqual("dns_target_not_allowed", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

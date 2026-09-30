"""供应商协议测试仅用合成数据与本机HTTP服务；不使用密钥文件、不连接收费API。"""
from contextlib import contextmanager
from copy import deepcopy
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading
import time
import unittest
from unittest.mock import patch

from services.research.evidence import canonical
from services.research.provider import (DeepSeekProvider, ProviderError, prepare_egress,
    estimate_tokens, estimate_reservation, usage_cost, MAX_RESPONSE_BYTES, REQUESTED_MODEL)


def reply(**changes):
    return {"id": "synthetic-request", "model": "deepseek-v4.1-flash",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "prompt_cache_hit_tokens": 30,
                      "prompt_cache_miss_tokens": 70, "total_tokens": 120}, **changes}


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


@contextmanager
def local_exchange(handler):
    """只在测试替换连接构造器：同时断言产品连接仍固定官方主机且保留TLS验证。"""
    requests = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            try:
                handler(self)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    def connection(host, port, *, timeout, context):
        assert host == "api.deepseek.com" and port == 443
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
        return HTTPConnection("127.0.0.1", server.server_port, timeout=timeout)
    try:
        with patch("services.research.provider.HTTPSConnection", side_effect=connection):
            yield requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(1)


def send(handler, status=200, body=None, headers=None):
    body = encoded(reply()) if body is None else body
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for key, value in (headers or {}).items():
        handler.send_header(key, value)
    handler.end_headers()
    handler.wfile.write(body)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{"role": "system", "content": "只返回JSON。"}, {"role": "user", "content": "核查软件条件。"}]

    def test_loopback_protocol_preserves_requested_model_disables_thinking_and_normalizes_usage(self):
        with local_exchange(send) as requests, patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:1"}):
            provider = DeepSeekProvider("synthetic-key-only", max_output_tokens=512)
            result = provider.complete(self.messages, [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/chat/completions")
        payload = json.loads(requests[0]["body"])
        self.assertEqual(payload["model"], REQUESTED_MODEL)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], provider.max_output_tokens)
        self.assertEqual(result["usage"], {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 30})
        self.assertEqual(result["model"], "deepseek-v4.1-flash")
        self.assertEqual(result["requested_model"], REQUESTED_MODEL)

    def test_price_rounding_cache_and_reservation_are_conservative_estimates(self):
        self.assertEqual(usage_cost({"input_tokens": 100, "output_tokens": 20, "cached_tokens": 30}), 302)
        self.assertEqual(usage_cost({"input_tokens": 100, "output_tokens": 20, "cached_tokens": 30}, peak=False), 151)
        self.assertEqual(usage_cost({"input_tokens": 1, "output_tokens": 0, "cached_tokens": 1}), 1)
        for invalid in (None, {}, {"input_tokens": True, "output_tokens": 0, "cached_tokens": 0},
                        {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 1}):
            self.assertIsNone(usage_cost(invalid))
        self.assertGreater(estimate_tokens(self.messages, []), len(encoded(self.messages)))
        self.assertEqual(estimate_reservation(self.messages, [], 512),
                         estimate_tokens(self.messages, []) * 2 + 512 * 8)

    def test_public_egress_covers_history_tool_arguments_report_and_is_idempotent(self):
        eid = "1" * 64
        messages = [
            {"role": "system", "content": "联系方式不可推断。用户输入联系人：测试人；预算100万元。"},
            {"role": "user", "content": "请联系test@example.test；截止2026-10-08。"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function",
             "function": {"name": "finish_report", "arguments": canonical({"report": {"summary": "联系人：测试人；软件服务", "evidence_ids": [eid]}})}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": canonical({"question": "电话13800138000", "budget": "13800138000元", "history": {"answer": "邮箱test@example.test"}, "evidence_id": eid})}]
        original = deepcopy(messages)
        tools = [{"type": "function", "function": {"name": "test", "description": "项目联系人：测试人；软件需求"}}]
        clean, clean_tools = prepare_egress(messages, tools)
        self.assertEqual(messages, original)
        self.assertEqual(prepare_egress(clean, clean_tools), (clean, clean_tools))
        text = canonical(clean)
        self.assertNotIn("测试人", text)
        self.assertNotIn("example.test", text)
        self.assertIn("联系方式不可推断", text)
        self.assertIn("2026-10-08", text)
        self.assertIn("13800138000元", text)
        args = json.loads(clean[2]["tool_calls"][0]["function"]["arguments"])
        self.assertEqual(args["report"]["evidence_ids"], [eid])
        result = json.loads(clean[3]["content"])
        self.assertEqual(result["evidence_id"], eid)
        self.assertEqual(result["question"], "电话[联系方式已省略]")

    def test_provider_reapplies_egress_before_any_exchange(self):
        seen = []
        def exchange(body, timeout):
            seen.append(body)
            return 200, {"content-type": "application/json"}, encoded(reply())
        provider = DeepSeekProvider("synthetic-key-only", exchange=exchange)
        provider.complete([{"role": "user", "content": "test@example.test"}], [])
        self.assertNotIn(b"example.test", seen[0])
        invalid = [{"role": "assistant", "tool_calls": [{"function": {"arguments": '{"x":1,"x":2}'}}]}]
        with self.assertRaises(ProviderError) as error:
            provider.complete(invalid, [])
        self.assertEqual(error.exception.code, "invalid_model_request")
        self.assertFalse(error.exception.usage_unknown)
        self.assertEqual(len(seen), 1)

    def test_contact_json_keys_and_common_phone_formats_are_removed_before_egress(self):
        eid = "a" * 64
        source = {"contact@example.test": "采购要求", "13800138000": "采购要求",
                  "notes": ["电话：(010)12345678", "手机：138 0013 8000", "(021)87654321", "138 0013 8000"],
                  "amount": "预算13800138000.00元，金额100万元", "deadline": "2026-10-10 09:30",
                  "evidence_id": eid}
        # 两个联系方式键归一后冲突，必须拒绝而不是丢弃任一字段继续发送。
        with self.assertRaisesRegex(ProviderError, "invalid_model_request"):
            prepare_egress([{"role": "user", "content": canonical(source)}], [])
        del source["13800138000"]
        clean, tools = prepare_egress([{"role": "user", "content": canonical(source)}], [])
        value = json.loads(clean[0]["content"])
        self.assertEqual(value["[联系方式已省略]"], "采购要求")
        self.assertNotIn("contact@example.test", canonical(clean))
        self.assertEqual(value["notes"], ["[联系方式已省略]"] * 4)
        self.assertEqual(value["amount"], source["amount"])
        self.assertEqual(value["deadline"], source["deadline"])
        self.assertEqual(value["evidence_id"], eid)
        self.assertEqual(prepare_egress(clean, tools), (clean, tools))
        direct, _ = prepare_egress([{"role": "user", "content": '{"13800138000":"采购要求"}'}], [])
        self.assertEqual(json.loads(direct[0]["content"]), {"[联系方式已省略]": "采购要求"})

    def test_redirect_is_never_followed_and_no_retry(self):
        with local_exchange(lambda h: send(h, 307, b'{}', {"Location": "http://127.0.0.1:1/secret"})) as requests:
            with self.assertRaises(ProviderError) as error:
                DeepSeekProvider("synthetic-key-only").complete(self.messages, [])
        self.assertEqual(len(requests), 1)
        self.assertEqual(error.exception.code, "provider_unavailable")
        self.assertTrue(error.exception.usage_unknown)

    def test_explicit_rejections_have_stable_codes_and_do_not_copy_provider_body(self):
        for status in (400, 401, 402, 403, 404, 429, 503):
            calls = []
            def exchange(body, timeout):
                calls.append(1)
                return status, {}, b"secret-token-upstream-debug"
            with self.subTest(status=status), self.assertRaises(ProviderError) as error:
                DeepSeekProvider("synthetic-key-only", exchange=exchange).complete(self.messages, [])
            self.assertEqual(calls, [1])
            self.assertEqual(error.exception.usage_unknown, status == 503)
            self.assertNotIn("secret", str(error.exception))

    def test_total_timeout_bounds_keepalive_blank_lines_and_marks_unknown(self):
        def dribble(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Connection", "close")
            handler.end_headers()
            for _ in range(20):
                handler.wfile.write(b"\n")
                handler.wfile.flush()
                time.sleep(.025)
        started = time.monotonic()
        with local_exchange(dribble) as requests:
            with self.assertRaises(ProviderError) as error:
                DeepSeekProvider("synthetic-key-only", timeout=.13).complete(self.messages, [])
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(requests), 1)
        self.assertTrue(error.exception.usage_unknown)

    def test_incomplete_and_duplicate_headers_fail_closed(self):
        def incomplete(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", "30")
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(b'{}')
        for handler in (incomplete, lambda h: send(h, headers={"content-length": "2"})):
            with local_exchange(handler), self.assertRaises(ProviderError) as error:
                DeepSeekProvider("synthetic-key-only").complete(self.messages, [])
            self.assertTrue(error.exception.usage_unknown)
            self.assertIn(error.exception.code, ("provider_incomplete_response", "invalid_provider_headers"))

    def test_malformed_responses_model_substitution_and_unexpected_thinking_rejected(self):
        values = [b'{"choices":[],"choices":[]}', b'{"value":NaN}', b"[]", encoded(reply(model="another-model")),
                  encoded(reply(choices=[{"finish_reason": "stop", "message": {"role": "assistant", "content": "{}", "reasoning_content": "not-requested"}}]))]
        for body in values:
            with self.subTest(body=body[:50]), self.assertRaises(ProviderError) as error:
                DeepSeekProvider("synthetic-key-only", exchange=lambda *_: (200, {"content-type": "application/json"}, body)).complete(self.messages, [])
            self.assertTrue(error.exception.usage_unknown)

    def test_missing_usage_is_not_zero_and_output_limit_not_overridden(self):
        provider = DeepSeekProvider("synthetic-key-only", exchange=lambda *_: (200, {"content-type": "application/json"}, encoded(reply(usage={}))))
        self.assertIsNone(provider.complete(self.messages, [])["usage"])
        for kwargs in ({"max_output_tokens": 4097}, {"timeout": float("nan")}, {"requested_model": "deepseek-pro"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderError):
                DeepSeekProvider("synthetic-key-only", **kwargs)
        provider = DeepSeekProvider("synthetic-key-only", exchange=lambda *_: (200, {"content-type": "application/json"}, b" " * (MAX_RESPONSE_BYTES + 1)))
        with self.assertRaises(ProviderError) as error:
            provider.complete(self.messages, [])
        self.assertTrue(error.exception.usage_unknown)


if __name__ == "__main__":
    unittest.main()

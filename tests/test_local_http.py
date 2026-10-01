"""公共传输层只检验身份/边界，不冒充租户业务授权。"""
from contextlib import closing
from http.client import HTTPConnection
from email.message import Message
import io
import json
import secrets
import threading
import time
import unittest
from unittest.mock import patch

from services.common import local_http
from services.common.local_http import LocalClient, LocalHTTPError, create_server


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {"workspace": secrets.token_urlsafe(32), "tracking": secrets.token_urlsafe(32)}
        self.dispatched = []
        def dispatch(method, path, body, identity):
            self.dispatched.append((method, path, body, identity))
            if path == "/fail":
                raise LocalHTTPError("version_conflict", 409)
            return 202, {"caller": identity, "body": body}
        self.server = create_server(dispatch, self.tokens)
        self.server.response_headers = threading.Event()
        self.server.handler_done = threading.Event()
        original = self.server.RequestHandlerClass
        class ObservedHandler(original):
            def end_headers(self):
                super().end_headers()
                self.server.response_headers.set()

            def _handle(self):
                try:
                    super()._handle()
                finally:
                    self.server.handler_done.set()

            do_GET = do_POST = _handle
        self.server.RequestHandlerClass = ObservedHandler
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def test_distinct_service_identity_and_stable_errors(self):
        for identity, token in self.tokens.items():
            client = LocalClient(self.base, token)
            self.assertEqual(client.request("POST", "/x", {"hello": "中文"}), {"caller": identity, "body": {"hello": "中文"}})
            with self.assertRaises(LocalHTTPError) as error:
                client.request("GET", "/fail")
            self.assertEqual((error.exception.code, error.exception.status), ("version_conflict", 409))
        with self.assertRaises(LocalHTTPError) as error:
            LocalClient(self.base, secrets.token_urlsafe(32)).request("GET", "/x")
        self.assertEqual(error.exception.status, 401)

    def test_invalid_host_json_and_framing_are_rejected(self):
        cases = [({"Host": "evil.invalid"}, b'{}', 400), ({}, b'{"x":1,"x":2}', 400),
                 ({}, b'{"x":NaN}', 400), ({"Forwarded": "host=evil.invalid"}, b'{}', 400),
                 ({"Content-Type": "text/plain"}, b'{}', 415)]
        for extra, raw, expected in cases:
            with self.subTest(extra=extra, raw=raw), closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)) as connection:
                headers = {"Authorization": "Bearer " + self.tokens["workspace"], "Content-Type": "application/json", **extra}
                connection.request("POST", "/x", raw, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                response.read()

    def _headers(self, changes=None):
        headers = {"Host": [self.server.authority], "Authorization": ["Bearer " + self.tokens["workspace"]],
                   "Content-Type": ["application/json"], "Content-Length": ["2"], "Connection": ["close"]}
        headers.update(changes or {})
        return headers

    def test_early_rejections_receive_delayed_body_without_reset_or_dispatch(self):
        # 等待服务端确已写响应头，再迟20ms发body，保证测试触发早拒绝而非一次性发送碰运气。
        cases = [("host", {"Host": ["evil.invalid"]}, "POST", 400, "invalid_host"),
                 ("forwarded", {"Forwarded": ["host=evil.invalid"]}, "POST", 400, "forwarded_request_rejected"),
                 ("content_type", {"Content-Type": ["text/plain"]}, "POST", 415, "json_required"),
                 ("token", {"Authorization": ["Bearer " + "x" * 32]}, "POST", 401, "unauthorized"),
                 ("duplicate_token", {"Authorization": ["Bearer " + self.tokens["workspace"]] * 2}, "POST", 401, "unauthorized"),
                 ("method", {}, "PUT", 405, "method_not_allowed"),
                 ("get_body", {}, "GET", 400, "unexpected_body"),
                 ("transfer_encoding", {"Transfer-Encoding": ["chunked"]}, "POST", 400, "invalid_framing"),
                 ("duplicate_length", {"Content-Length": ["2", "2"]}, "POST", 400, "invalid_length")]
        for name, changes, method, status, code in cases:
            with self.subTest(case=name), closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as connection:
                self.server.response_headers.clear()
                self.server.handler_done.clear()
                connection.putrequest(method, "/x", skip_host=True, skip_accept_encoding=True)
                for key, values in self._headers(changes).items():
                    for value in values:
                        connection.putheader(key, value)
                connection.endheaders()  # 内部send只发送头；body等服务端明确早拒绝后再单独send。
                self.assertTrue(self.server.response_headers.wait(1))
                time.sleep(.02)
                connection.send(b"{}")
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                self.assertEqual(json.loads(response.read())["error"]["code"], code)
                self.assertEqual(response.getheader("Connection"), "close")
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                connection.close()
                self.assertTrue(self.server.handler_done.wait(1))
        self.assertEqual(self.dispatched, [])  # 消耗拒绝体不意味着认证或业务获得第二次机会。

    def test_normal_and_invalid_json_split_bodies_keep_original_results(self):
        for raw, expected in ((b'{"x":1}', 202), (b'{"x":NaN}', 400), (b'{"x":1,"x":2}', 400)):
            with self.subTest(raw=raw), closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as connection:
                connection.putrequest("POST", "/x")
                connection.putheader("Authorization", "Bearer " + self.tokens["workspace"])
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", str(len(raw)))
                connection.endheaders()
                connection.send(raw[:1])
                time.sleep(.02)
                connection.send(raw[1:])
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertIsInstance(json.loads(response.read()), dict)
        self.assertEqual(len(self.dispatched), 1)

    def test_slow_rejected_body_has_total_deadline_and_valid_response(self):
        # 在响应后继续慢滴答，每个间隔短于单次socket超时，但不能累计延长清理截止。
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as connection:
            connection.putrequest("POST", "/x", skip_host=True)
            connection.putheader("Host", "evil.invalid")
            connection.putheader("Content-Length", "100")
            connection.endheaders()
            self.assertTrue(self.server.response_headers.wait(1))
            start = time.monotonic()
            for _ in range(3):
                time.sleep(.04)
                connection.send(b"x")
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["error"]["code"], "invalid_host")
            self.assertTrue(self.server.handler_done.wait(.6))
            self.assertLess(time.monotonic() - start, .65)
        self.assertEqual(self.dispatched, [])

    def test_rejection_discard_byte_limit_and_ambiguous_framing_never_parse_body(self):
        class Connection:
            def settimeout(self, value):
                self.timeout = value
        for lengths, transfer in ((["9999999"], None), (["2", "2"], None), (["2"], "chunked")):
            with self.subTest(lengths=lengths, transfer=transfer):
                handler = object.__new__(local_http._Handler)
                handler.headers = Message()
                for value in lengths:
                    handler.headers["Content-Length"] = value
                if transfer:
                    handler.headers["Transfer-Encoding"] = transfer
                handler.server, handler.connection, handler._body_received = self.server, Connection(), 0
                handler.rfile = io.BytesIO(b"x" * (local_http.REJECT_DRAIN_BYTES + 100))
                handler._discard_unread_body()
                self.assertEqual(handler.rfile.tell(), local_http.REJECT_DRAIN_BYTES)
                self.assertLessEqual(handler.connection.timeout, local_http.REJECT_DRAIN_TIMEOUT)

    def test_normal_body_total_deadline_rejects_slow_stream_without_dispatch(self):
        with patch.object(local_http, "BODY_TIMEOUT", .4), closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as connection:
            connection.putrequest("POST", "/x")
            connection.putheader("Authorization", "Bearer " + self.tokens["workspace"])
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "100")
            connection.endheaders()
            start = time.monotonic()
            for _ in range(3):
                connection.send(b" ")
                time.sleep(.035)
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(response.read())["error"]["code"], "service_unavailable")
            self.assertTrue(self.server.handler_done.wait(.6))
            self.assertLess(time.monotonic() - start, .9)
        self.assertEqual(self.dispatched, [])

    def test_client_never_accepts_non_loopback_or_arbitrary_url(self):
        for url in ("http://localhost:9000", "https://example.org", self.base + "/api", "http://user@127.0.0.1:99"):
            with self.assertRaises(ValueError):
                LocalClient(url, self.tokens["workspace"])
        client = LocalClient(self.base, self.tokens["workspace"])
        for path in ("https://example.org/", "//example.org/x"):
            with self.assertRaises(LocalHTTPError):
                client.request("GET", path)

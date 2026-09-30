"""公共传输层只检验身份/边界，不冒充租户业务授权。"""
from contextlib import closing
from http.client import HTTPConnection
import secrets
import threading
import unittest

from services.common.local_http import LocalClient, LocalHTTPError, create_server


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tokens = {"workspace": secrets.token_urlsafe(32), "tracking": secrets.token_urlsafe(32)}
        def dispatch(method, path, body, identity):
            if path == "/fail":
                raise LocalHTTPError("version_conflict", 409)
            return 202, {"caller": identity, "body": body}
        self.server = create_server(dispatch, self.tokens)
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
            with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)) as connection:
                headers = {"Authorization": "Bearer " + self.tokens["workspace"], "Content-Type": "application/json", **extra}
                connection.request("POST", "/x", raw, headers)
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
                response.read()

    def test_client_never_accepts_non_loopback_or_arbitrary_url(self):
        for url in ("http://localhost:9000", "https://example.org", self.base + "/api", "http://user@127.0.0.1:99"):
            with self.assertRaises(ValueError):
                LocalClient(url, self.tokens["workspace"])
        client = LocalClient(self.base, self.tokens["workspace"])
        for path in ("https://example.org/", "//example.org/x"):
            with self.assertRaises(LocalHTTPError):
                client.request("GET", path)

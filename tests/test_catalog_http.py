"""真实 loopback HTTP 的契约/安全测试；采购数据由虚构 ingestion→processing 链生成。"""
from contextlib import closing, contextmanager
from dataclasses import replace
from http.client import HTTPConnection, HTTPResponse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from services.catalog.http_api import create_server, main
from services.catalog.store import Catalog
from services.ingestion.archive import Store
from services.ingestion.demo import DemoTransport, demo_request, NOTICE_URL
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute
from services.processing.normalize import normalize_bundle
from services.workspace.catalog_client import CatalogClient, CatalogError


class _FixtureTransport(DemoTransport):
    def __init__(self, number, observed):
        super().__init__()
        self.url = NOTICE_URL.replace("99990001", f"{number:08d}")
        self.observed = observed

    def fetch(self, url, **kwargs):
        result = super().fetch(NOTICE_URL if kwargs["kind"] == "notice" else url, **kwargs)
        return replace(result, url=url, final_url=url, fetched_at=self.observed,
                       body=result.body.replace(NOTICE_URL.encode(), self.url.encode()))


@contextmanager
def _running(server):
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _bad_upstream(body, *, status=200, extra_headers=None, delay=0):
    """可控坏服务只接本地请求，用真实线缆响应检验客户端 fail closed。"""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.calls += 1
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra_headers or []:
                self.send_header(key, value)
            self.end_headers()
            time.sleep(delay)
            try:
                self.wfile.write(body)
            except OSError:
                pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.calls = 0
    with _running(server):
        yield server


class CatalogHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog_path = self.root / "catalog"
        self.ingestion = Store(self.root / "ingestion")
        self.catalog = Catalog(self.catalog_path)
        self.counter = 0
        self.one = self.add(1, "2026-09-30T12:00:00Z")
        self.two = self.add(2, "2026-09-30T11:00:00Z")
        self.token = secrets.token_urlsafe(32)
        self.server = create_server(self.catalog_path, token=self.token)
        self.running = _running(self.server)
        self.running.__enter__()
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.client = CatalogClient(self.address, self.token)

    def tearDown(self):
        self.running.__exit__(None, None, None)
        self.catalog.close()
        self.ingestion.close()
        self.temp.cleanup()

    def add(self, number, observed):
        self.counter += 1
        request = {**demo_request(), "simulation": True, "pages": 1, "attachments": False}
        run, _ = self.ingestion.create_run(request, f"http-fixture-{self.counter}")
        execute(self.ingestion, run, _FixtureTransport(number, observed))
        bundle = normalize_bundle(export_bundle(self.ingestion, run))
        self.catalog.import_bundle(bundle)
        return bundle["observations"][0]

    def request(self, target="/v1/notices", *, method="GET", headers=None, body=None):
        with closing(HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as connection:
            values = {"Authorization": "Bearer " + self.token}
            values.update(headers or {})
            connection.request(method, target, body=body, headers=values)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), json.loads(raw) if raw else None

    def assert_catalog_error(self, code, status, call):
        with self.assertRaises(CatalogError) as raised:
            call()
        self.assertEqual((raised.exception.code, raised.exception.status), (code, status))

    def test_health_and_queries_are_json_with_no_browser_cors(self):
        status, headers, body = self.request("/health", headers={"Authorization": ""})
        self.assertEqual((status, body), (200, {"ok": True, "version": "catalog-http-v1"}))
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(self.client.query()["total"], 0)  # 演示不能混进默认真实目录。
        result = self.client.query(simulation=True, keyword="软件", kind="procurement")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["items"][0]["observation_id"], self.one["observation_id"])
        self.assertEqual(self.client.detail(self.one["notice_id"])["current"], self.one)

    def test_exact_host_and_service_token_required(self):
        for host in ("localhost", "localhost:" + str(self.server.server_port), "127.0.0.1",
                     "evil.test:" + str(self.server.server_port), "127.0.0.1:1"):
            self.assertEqual(self.request(headers={"Host": host})[0], 403)
        for token in ("", "Bearer wrong", "Basic wrong"):
            status, _, body = self.request(headers={"Authorization": token})
            self.assertEqual(status, 401)
            self.assertEqual(body, {"error": {"code": "unauthorized", "message": "unauthorized"}})
            self.assertNotIn(self.token, json.dumps(body))

    def test_duplicate_host_or_auth_is_rejected(self):
        for duplicate in (f"Host: 127.0.0.1:{self.server.server_port}", "Authorization: Bearer " + self.token):
            raw = (f"GET /v1/notices HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\n"
                   f"Authorization: Bearer {self.token}\r\n{duplicate}\r\n\r\n").encode()
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2) as sock:
                sock.sendall(raw)
                response = HTTPResponse(sock)
                response.begin()
                self.assertIn(response.status, (401, 403))
                self.assertIn("error", json.loads(response.read()))

    def test_read_only_routes_methods_and_body(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD", "TRACE", "BREW"):
            self.assertEqual(self.request(method=method)[0], 405)
        self.assertEqual(self.request(method="POST", body="never read")[0], 405)
        self.assertEqual(self.request(method="BREW", headers={"Authorization": ""})[0], 401)
        for path in ("/import", "/raw/file", "/v1/notices/../../secret", "/v1/notices/" + "a" * 63,
                     "/v1/notices/" + self.one["notice_id"] + "?download=true"):
            status, headers, result = self.request(path)
            self.assertEqual(status, 404)
            self.assertIn("application/json", headers["Content-Type"])
            self.assertNotIn(path, json.dumps(result))
        self.assertEqual(self.request(body="unaccepted body")[0], 400)
        self.assertEqual(self.request(headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.request(headers={"Expect": "100-continue", "Content-Length": "100"})[0], 400)

    def test_request_target_and_query_limits(self):
        bad = ("?unknown=x", "?keyword=x&keyword=y", "?simulation=1", "?page_size=0", "?page_size=101",
               "?kind=made_up", "?keyword=%FF", "?keyword=%GG", "?keyword=%00", "?keyword=" + "a" * 201,
               "?keyword", "?cursor=" + "a" * 5001, "?page_size=1&simulation=true&cursor=bogus")
        for query in bad:
            with self.subTest(query=query[:60]):
                self.assertEqual(self.request("/v1/notices" + query)[0], 400)
        self.assertEqual(self.request("/" + "a" * 8200)[0], 414)
        self.assertEqual(self.request("//evil.test/path")[0], 400)
        self.assertEqual(self.request(self.address + "/v1/notices")[0], 400)

    def test_snapshot_pagination_survives_new_import_and_version(self):
        first = self.client.query(simulation=True, page_size=1)
        self.add(3, "2026-10-01T12:00:00Z")
        self.add(2, "2026-10-01T13:00:00Z")
        second = self.client.query(simulation=True, page_size=1, cursor=first["next_cursor"])
        self.assertEqual((first["total"], second["total"]), (2, 2))
        self.assertEqual(first["as_of"], second["as_of"])
        self.assertEqual(second["items"][0]["observation_id"], self.two["observation_id"])
        self.assert_catalog_error("cursor_query_mismatch", 400,
            lambda: self.client.query(simulation=True, page_size=2, cursor=first["next_cursor"]))

    def test_selection_freezes_only_confirmed_fields_and_detects_new_current_version(self):
        selected = [{"notice_id": self.one["notice_id"], "observation_id": self.one["observation_id"]}]
        snapshots = self.client.validate_selection(selected)
        self.assertEqual(snapshots, [{k: self.one[k] for k in ("notice_id", "observation_id", "title", "source_url", "simulation")}])
        changed = self.add(1, "2026-10-01T12:00:00Z")
        self.assertNotEqual(changed["observation_id"], self.one["observation_id"])
        self.assert_catalog_error("catalog_version_changed", 409, lambda: self.client.validate_selection(selected))
        self.assertEqual(len(self.client.detail(self.one["notice_id"])["history"]), 2)
        self.assertEqual(snapshots[0]["observation_id"], self.one["observation_id"])

    def test_empty_duplicate_invalid_or_extra_selection_never_means_all(self):
        item = {"notice_id": self.one["notice_id"], "observation_id": self.one["observation_id"]}
        for items in (None, [], [item] * 21, [item, item], [{**item, "title": "untrusted"}],
                      [{**item, "notice_id": "../file"}], [{"notice_id": self.one["notice_id"]}]):
            self.assert_catalog_error("invalid_selection", 400, lambda: self.client.validate_selection(items))
        self.assert_catalog_error("catalog_version_changed", 409,
            lambda: self.client.validate_selection([{"notice_id": "a" * 64, "observation_id": "b" * 64}]))

    def test_each_request_has_its_own_read_only_connection(self):
        # 同时从多个真实 socket 查询，不能复用主线程 SQLite 连接。
        errors, results = [], []
        def query():
            try:
                results.append(self.client.query(simulation=True)["total"])
            except Exception as exc:
                errors.append(type(exc).__name__)
        threads = [threading.Thread(target=query) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(results, [2] * 5)
        with closing(Catalog(self.catalog_path, read_only=True)) as read_only:
            with self.assertRaises(sqlite3.OperationalError):
                read_only.db.execute("DELETE FROM observations")

    def test_missing_or_corrupt_store_fails_instead_of_silent_empty_catalog(self):
        missing = self.root / "missing"
        with self.assertRaises(ValueError):
            create_server(missing, token=self.token)
        self.assertFalse(missing.exists())
        with patch("services.catalog.http_api.Catalog", side_effect=sqlite3.OperationalError("private failure")):
            self.assert_catalog_error("catalog_unavailable", 503, self.client.query)

    def test_client_ignores_environment_proxy(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
            self.assertEqual(self.client.query(simulation=True)["total"], 2)

    def test_client_rejects_wrong_identity_and_unsafe_source_reference_in_observation(self):
        for key, value in (("source_url", "javascript:alert(1)"), ("notice_id", "b" * 64), ("title", [])):
            payload = self.catalog.detail(self.one["notice_id"])
            payload["current"][key] = value
            payload["history"][0][key] = value
            with _bad_upstream(json.dumps(payload).encode()) as server:
                client = CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token)
                self.assert_catalog_error("catalog_unavailable", 503, lambda: client.detail(self.one["notice_id"]))

    def test_service_token_and_bind_errors_never_echo_secret(self):
        for host in ("0.0.0.0", "localhost", "127.0.0.2", "::1"):
            with self.assertRaises(ValueError):
                create_server(self.catalog_path, host=host, token=self.token)
        token_file = self.root / "token.txt"
        secret = "not-valid secret file"
        token_file.write_text(secret)
        output = io.StringIO()
        with patch("sys.stdout", output):
            self.assertEqual(main(["--store", str(self.catalog_path), "--port", "0", "--token-file", str(token_file)]), 4)
        self.assertNotIn(secret, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "catalog_start_failed")


class CatalogClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.token = secrets.token_urlsafe(32)

    def fail_query(self, client):
        with self.assertRaises(CatalogError) as raised:
            client.query()
        self.assertEqual((raised.exception.code, raised.exception.status), ("catalog_unavailable", 503))

    def test_only_literal_fixed_loopback_base_is_allowed(self):
        for url in ("http://localhost:8766", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
                    "http://127.1:8766", "http://2130706433:8766", "https://127.0.0.1:8766", "http://127.0.0.1:8766/",
                    "http://user@127.0.0.1:8766", "http://127.0.0.1:8766?x=1", "http://127.0.0.1:8766#x",
                    "http://127.0.0.1.evil.test:8766", "http://[::1]:8766"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                CatalogClient(url, self.token)

    def test_json_shape_duplicate_keys_nan_infinity_and_response_size_fail_closed(self):
        for body in (b'[]', b'{"items":[]}', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                     b'{"x":1e999}', b'not json', b'{"x":' + b'[' * 1000 + b'0' + b']' * 1000 + b'}'):
            with self.subTest(body=body[:40]), _bad_upstream(body) as server:
                self.fail_query(CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token))
        with _bad_upstream(b'{"padding":"' + b'x' * 500 + b'"}') as server:
            self.fail_query(CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token, max_bytes=100))

    def test_redirect_is_not_followed_even_to_another_loopback_server(self):
        with _bad_upstream(b'{}') as target:
            with _bad_upstream(b'{}', status=302, extra_headers=[("Location", f"http://127.0.0.1:{target.server_port}/v1/notices")]) as source:
                self.fail_query(CatalogClient(f"http://127.0.0.1:{source.server_port}", self.token))
            self.assertEqual(target.calls, 0)

    def test_wrong_auth_and_closed_service_are_503_not_empty_items(self):
        with _bad_upstream(b'{"error":{"code":"unauthorized","message":"unauthorized"}}', status=401) as server:
            self.fail_query(CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token))
        client = CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token, timeout=0.1)
        self.fail_query(client)

    def test_duplicate_content_length_is_rejected(self):
        with _bad_upstream(b'{}', extra_headers=[("Content-Length", "2")]) as server:
            self.fail_query(CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token))

    def test_slow_body_timeout_is_503_and_bounded(self):
        with _bad_upstream(b'{}', delay=0.3) as server:
            start = time.monotonic()
            self.fail_query(CatalogClient(f"http://127.0.0.1:{server.server_port}", self.token, timeout=0.05))
            self.assertLess(time.monotonic() - start, 1)


if __name__ == "__main__":
    unittest.main()

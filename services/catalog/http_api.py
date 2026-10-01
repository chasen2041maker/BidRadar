"""公共目录的本地只读 HTTP v1 边界，不是生产网关或公司身份入口。

GET /health 返回 {ok, version}；其它 GET 必须携带服务 Bearer token：
/v1/notices?keyword=&kind=&page_size=20&simulation=false&cursor=...
/v1/notices/{64位小写hex}。成功沿用 Catalog 的 JSON 契约；错误统一
{error:{code,message}}。只绑定 127.0.0.1，无 CORS、导入、原件下载或模型调用。
每请求独立只读连接；游标快照由 catalog 拥有，workspace 不读取其数据库。
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qsl, urlsplit

from .store import Catalog, KINDS

VERSION = "catalog-http-v1"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_TARGET = 8192
TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class _RequestError(ValueError):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address

    def handle_error(self, request, client_address):
        # 不让默认 traceback/请求日志泄漏查询参数、原文或服务凭据。
        pass


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BidRadarCatalog/1"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _json(self, status, value):
        payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > MAX_RESPONSE_BYTES:
            status = 503
            payload = b'{"error":{"code":"catalog_response_too_large","message":"catalog_response_too_large"}}'
        self.close_connection = True  # 不复用含未读/畸形 body 的连接，消除请求边界歧义。
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _error(self, code, status=400):
        self._json(status, {"error": {"code": code, "message": code}})

    def send_error(self, code, message=None, explain=None):
        # BaseHTTPRequestHandler 默认错误页是 HTML 且可能回显输入，统一替换。
        status = 405 if code == 501 else code
        self._error("method_not_allowed" if status == 405 else "invalid_http_request", status)

    def parse_request(self):
        if not super().parse_request():
            return False
        raw_target = self.raw_requestline.split()[1]
        if (len(raw_target) > MAX_TARGET or not raw_target.isascii()
                or not raw_target.startswith(b"/") or raw_target.startswith(b"//")
                or b"#" in raw_target or b"\\" in raw_target):
            self._error("invalid_request_target", 414 if len(raw_target) > MAX_TARGET else 400)
            return False
        if len(self.headers) > 64 or sum(len(k) + len(v) for k, v in self.headers.items()) > 16384:
            self._error("headers_too_large", 431)
            return False
        return True

    def handle_expect_100(self):
        self._error("request_body_not_allowed", 400)
        return False

    def _gate(self, *, check_body=True):
        # 固定 Host+端口阻止 DNS rebinding，Bearer 是服务间凭据，不是前端用户登录。
        if self.headers.get_all("Host", []) != [f"127.0.0.1:{self.server.server_port}"]:
            raise _RequestError("invalid_host", 403)
        lengths = self.headers.get_all("Content-Length", [])
        if check_body and (self.headers.get_all("Transfer-Encoding", []) or lengths not in ([], ["0"])):
            raise _RequestError("request_body_not_allowed")
        if self.path != "/health":
            expected = "Bearer " + self.server.service_token
            values = self.headers.get_all("Authorization", [])
            if len(values) != 1 or not hmac.compare_digest(values[0].encode("utf-8"), expected.encode("ascii")):
                raise _RequestError("unauthorized", 401)

    def _filters(self, query):
        if len(query) > 7000 or re.search(r"%(?![0-9a-fA-F]{2})", query):
            raise _RequestError("invalid_query")
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                              encoding="utf-8", errors="strict", max_num_fields=5)
        except (ValueError, UnicodeError):
            raise _RequestError("invalid_query") from None
        values = dict(pairs)
        if (len(values) != len(pairs) or set(values) - {"keyword", "kind", "page_size", "simulation", "cursor"}
                or any(any(ord(c) < 32 or ord(c) == 127 for c in key + value) for key, value in pairs)):
            raise _RequestError("invalid_query")
        keyword, kind = values.get("keyword", ""), values.get("kind") or None
        page_size, simulation = values.get("page_size", "20"), values.get("simulation", "false")
        cursor = values.get("cursor") or None
        if (len(keyword) > 200 or kind is not None and kind not in KINDS
                or not re.fullmatch(r"[1-9][0-9]{0,2}", page_size) or int(page_size) > 100
                or simulation not in ("true", "false") or cursor is not None and len(cursor) > 5000):
            raise _RequestError("invalid_query")
        return dict(keyword=keyword, kind=kind, page_size=int(page_size), simulation=simulation == "true", cursor=cursor)

    def do_GET(self):
        try:
            self._gate()
            path = urlsplit(self.path)
            if path.path == "/health" and not path.query and self.path == "/health":
                self._json(200, {"ok": True, "version": VERSION})
                return
            if path.path == "/v1/notices":
                filters = self._filters(path.query)
                operation, argument = "query", filters
            elif re.fullmatch(r"/v1/notices/[0-9a-f]{64}", path.path) and "?" not in self.path:
                operation, argument = "detail", {"notice_id": path.path.rsplit("/", 1)[1]}
            elif re.fullmatch(r"/v1/observations/[0-9a-f]{64}/[0-9a-f]{64}", path.path) and "?" not in self.path:
                _, _, _, nid, oid = path.path.split("/")
                operation, argument = "observation", {"notice_id": nid, "observation_id": oid}
            elif re.fullmatch(r"/v1/bundles/[0-9a-f]{64}", path.path):
                operation = "bundle"
                argument = {"notice_id": path.path.rsplit("/", 1)[1], **self._numbers(path.query, {"snapshot"})}
            elif path.path == "/v1/changes":
                operation, argument = "changes", self._numbers(path.query, {"after", "limit"})
            else:
                raise _RequestError("route_not_found", 404)
            with closing(Catalog(self.server.store_path, read_only=True)) as catalog:
                try:
                    result = getattr(catalog, operation)(**argument)
                except ValueError as exc:
                    code = str(exc)
                    if code in ("notice_not_found", "observation_not_found"):
                        raise _RequestError(code, 404) from None
                    if code in ("invalid_cursor", "cursor_query_mismatch", "invalid_snapshot", "invalid_query", "cursor_ahead"):
                        raise _RequestError(code) from None
                    raise
            self._json(200, result)
        except _RequestError as exc:
            self._error(exc.code, exc.status)
        except (OSError, sqlite3.DatabaseError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            self._error("catalog_unavailable", 503)

    def _numbers(self, query, allowed):
        """增量与历史读接口只接受明确的十进制整数，不借重复键更换快照。"""
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
            values = dict(pairs)
            if len(values) != len(pairs) or set(values) - allowed:
                raise ValueError()
            if any(not re.fullmatch(r"0|[1-9][0-9]{0,15}", v) for v in values.values()):
                raise ValueError()
            return {k: int(v) for k, v in values.items()}
        except ValueError:
            raise _RequestError("invalid_query") from None

    def _unsupported(self):
        try:
            self._gate(check_body=False)
            self._error("method_not_allowed", 405)
        except _RequestError as exc:
            self._error(exc.code, exc.status)

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _unsupported

    def __getattr__(self, name):
        # 未知 HTTP 方法也走 Host/令牌校验，最后统一 405，而非标准库默认 HTML 501。
        if name.startswith("do_"):
            return self._unsupported
        raise AttributeError(name)


def create_server(store, host="127.0.0.1", port=0, *, token):
    """创建未启动的服务器供 CLI/本地集成测试使用；store 是已初始化目录路径。"""
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("invalid_bind_address")
    if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
        raise ValueError("invalid_service_token")
    store_path = Path(store).absolute()
    with closing(Catalog(store_path, read_only=True)):
        pass
    server = _Server((host, port), _Handler)
    server.store_path, server.service_token = store_path, token
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="仅本机公共目录只读 HTTP 服务")
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with args.token_file.open("rb") as stream:
            raw = stream.read(257)
        if len(raw) > 256:
            raise ValueError("token_file_too_large")
        token = raw.decode("ascii").strip()
        with create_server(args.store, port=args.port, token=token) as server:
            print(json.dumps({"listening": f"http://127.0.0.1:{server.server_port}", "version": VERSION}), flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (ValueError, TypeError, IndexError, OSError, sqlite3.DatabaseError):
        print(json.dumps({"error": {"code": "catalog_start_failed", "message": "catalog_start_failed"}}), flush=True)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

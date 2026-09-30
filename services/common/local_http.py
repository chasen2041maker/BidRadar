"""本地服务的纯HTTP边界，不能包含业务存储、租户推断或授权默认值。

服务令牌只识别受信调用者，所属服务仍必须向workspace核查当前业务权限。
固定回环地址、无代理/重定向、严格JSON和总体截止；日志不输出请求/凭据。
"""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import time
from urllib.parse import urlsplit


class LocalHTTPError(Exception):
    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_json")
            result[key] = value
        return result
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
    pending, count = [(result, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if depth > 64 or count > 200000:
            raise ValueError("json_limit")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    if not isinstance(result, dict):
        raise ValueError("object_required")
    return result


def token_valid(token):
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is not None


class LocalClient:
    def __init__(self, base_url, token, timeout=5, max_bytes=8 * 1024 * 1024):
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", base_url)
        if not match or not 1 <= int(match[1]) <= 65535 or not token_valid(token):
            raise ValueError("invalid_local_service")
        if type(timeout) not in (float, int) or not 0 < timeout <= 60 or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("invalid_limits")
        self.port, self.token, self.timeout, self.max_bytes = int(match[1]), token, timeout, max_bytes

    def request(self, method, path, payload=None):
        parts = urlsplit(path)
        if method not in ("GET", "POST") or not path.startswith("/") or parts.scheme or parts.netloc or parts.fragment or len(path) > 8192:
            raise LocalHTTPError("invalid_route", 400)
        if method == "GET" and payload is not None:
            raise LocalHTTPError("unexpected_body", 400)
        raw = encode(payload) if payload is not None else None
        if raw and len(raw) > self.max_bytes:
            raise LocalHTTPError("request_too_large", 413)
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json", "Connection": "close"}
        if raw is not None:
            headers["Content-Type"] = "application/json"
        connection = HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        response = None
        deadline = time.monotonic() + self.timeout
        try:
            connection.request(method, path, raw, headers)
            sock = connection.sock
            def remaining():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError()
                sock.settimeout(left)
            remaining()
            response = connection.getresponse()
            lengths = response.headers.get_all("Content-Length", [])
            if (response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"
                    or response.getheader("Content-Encoding") or len(lengths) != 1
                    or not lengths[0].isdigit() or int(lengths[0]) > self.max_bytes
                    or response.getheader("Transfer-Encoding") or 300 <= response.status < 400):
                raise ValueError("invalid_response")
            expected, body = int(lengths[0]), bytearray()
            while len(body) < expected:
                remaining()
                chunk = response.read1(min(65536, expected - len(body)))
                if not chunk:
                    raise ValueError("truncated_response")
                body.extend(chunk)
            value = decode(body)
            if response.status not in (200, 201, 202):
                error = value.get("error", {})
                code = error.get("code") if isinstance(error, dict) else None
                if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                    raise ValueError("invalid_error")
                raise LocalHTTPError(code, response.status)
            return value
        except (OSError, HTTPException, ValueError, TypeError, RecursionError):
            raise LocalHTTPError("service_unavailable", 503) from None
        finally:
            if response is not None:
                response.close()
            connection.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class _Handler(BaseHTTPRequestHandler):
    server_version = "BidRadarInternal/1"

    def log_message(self, *_):
        pass

    def _send(self, status, value):
        raw = encode(value)
        if len(raw) > self.server.max_bytes:
            status, raw = 503, encode({"error": {"code": "response_too_large", "message": "response_too_large"}})
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def _handle(self):
        try:
            if self.headers.get_all("Host") != [self.server.authority] or len(self.path) > 8192:
                raise LocalHTTPError("invalid_host", 400)
            if any(self.headers.get(name) is not None for name in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto")):
                raise LocalHTTPError("forwarded_request_rejected", 400)
            parts = urlsplit(self.path)
            if not self.path.startswith("/") or parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
                raise LocalHTTPError("invalid_route", 400)
            tokens = self.headers.get_all("Authorization", [])
            identity = next((name for name, token in self.server.tokens.items()
                             if len(tokens) == 1 and hmac.compare_digest(tokens[0].encode(), ("Bearer " + token).encode())), None)
            if identity is None:
                raise LocalHTTPError("unauthorized", 401)
            if self.command not in ("GET", "POST"):
                raise LocalHTTPError("method_not_allowed", 405)
            if self.headers.get("Transfer-Encoding") is not None:
                raise LocalHTTPError("invalid_framing", 400)
            lengths = self.headers.get_all("Content-Length", [])
            data = None
            if self.command == "POST":
                if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= self.server.max_bytes:
                    raise LocalHTTPError("invalid_length", 400)
                if self.headers.get_all("Content-Type") != ["application/json"]:
                    raise LocalHTTPError("json_required", 415)
                raw = self.rfile.read(int(lengths[0]))
                if len(raw) != int(lengths[0]):
                    raise LocalHTTPError("incomplete_body", 400)
                data = decode(raw)
            elif lengths and lengths != ["0"]:
                raise LocalHTTPError("unexpected_body", 400)
            # 多调用者模式把服务身份交给拥有者，拥有者限制路由/任务种类，不能用
            # tracking令牌通过人工分析入口绕过watch委托。
            result = (self.server.dispatch(self.command, self.path, data, identity)
                      if self.server.multiple_tokens else self.server.dispatch(self.command, self.path, data))
            status, value = result if isinstance(result, tuple) else (200, result)
            self._send(status, value)
        except LocalHTTPError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.code}})
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            self._send(400, {"error": {"code": "invalid_request", "message": "invalid_request"}})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._send(503, {"error": {"code": "service_unavailable", "message": "service_unavailable"}})

    do_GET = do_POST = _handle

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._handle
        raise AttributeError(name)


def create_server(dispatch, token, *, port=0, host="127.0.0.1", max_bytes=8 * 1024 * 1024):
    multiple = isinstance(token, dict)
    tokens = token if multiple else {"service": token}
    if (host != "127.0.0.1" or not tokens or not all(isinstance(k, str) and token_valid(v) for k, v in tokens.items())
            or len(set(tokens.values())) != len(tokens) or type(port) is not int or not 0 <= port <= 65535
            or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024):
        raise ValueError("invalid_local_server")
    server = _Server((host, port), _Handler)
    server.dispatch, server.tokens, server.max_bytes = dispatch, tokens, max_bytes
    server.multiple_tokens = multiple
    server.authority = f"127.0.0.1:{server.server_port}"
    return server

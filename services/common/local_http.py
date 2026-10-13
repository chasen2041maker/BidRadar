"""本地服务之间通信用的 HTTP 客户端和服务器（只处理传输，不含任何业务逻辑）。

research、tracking 的内部接口都用这里的 create_server 起服务，用 LocalClient 互相调用。

几条规则：
    - 只连本机 127.0.0.1，不走代理，不跟随重定向。
    - 请求和响应都是严格的 JSON（不允许重复键、NaN），有大小上限和总超时。
    - 服务令牌只说明"调用方是哪个服务"，不代表任何用户权限；
      用户权限仍由各服务向 workspace 查询。
    - 不打印访问日志，免得请求内容或令牌进日志。

v2 计划换成 FastAPI，这一层基本可以删掉（见 docs/v2-blueprint.md）。
"""

from __future__ import annotations

from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import re
import socket
import time
from urllib.parse import urlsplit

BODY_TIMEOUT = 10.0  # 读取请求体的总时限（秒）
REJECT_DRAIN_TIMEOUT = 0.25  # 拒绝请求后，最多再等多久来读掉没读完的请求体
REJECT_DRAIN_BYTES = 64 * 1024  # 拒绝请求后，最多读掉多少字节


class LocalHTTPError(Exception):
    """服务间调用出错。code 是错误码，status 是 HTTP 状态码。"""

    def __init__(self, code, status=503):
        super().__init__(code)
        self.code, self.status = code, status


def encode(value):
    """对象编码成 UTF-8 的 JSON 字节（键排序、无多余空格）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def decode(raw):
    """严格解析 JSON 字节：不允许重复键和 NaN，限制嵌套深度和节点数，顶层必须是对象。"""

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_json")
            result[key] = value
        return result

    result = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")),
    )
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
    """服务令牌格式是否合法：32–128 位的字母、数字、下划线或连字符。"""
    return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is not None


class LocalClient:
    """调用另一个本地服务的客户端。base_url 必须是 http://127.0.0.1:端口。"""

    def __init__(self, base_url, token, timeout=5, max_bytes=8 * 1024 * 1024):
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", base_url)
        if not match or not 1 <= int(match[1]) <= 65535 or not token_valid(token):
            raise ValueError("invalid_local_service")
        if (
            type(timeout) not in (float, int)
            or not 0 < timeout <= 60
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 32 * 1024 * 1024
        ):
            raise ValueError("invalid_limits")
        self.port, self.token, self.timeout, self.max_bytes = int(match[1]), token, timeout, max_bytes

    def request(self, method, path, payload=None):
        """发一个请求并返回解析后的 JSON。

        对方返回错误时抛 LocalHTTPError(对方的错误码, 状态码)；
        网络问题、格式不对、超时，一律抛 LocalHTTPError("service_unavailable", 503)。
        """
        parts = urlsplit(path)
        if (
            method not in ("GET", "POST")
            or not path.startswith("/")
            or parts.scheme
            or parts.netloc
            or parts.fragment
            or len(path) > 8192
        ):
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
            if (
                response.getheader("Content-Type", "").split(";", 1)[0] != "application/json"
                or response.getheader("Content-Encoding")
                or len(lengths) != 1
                or not lengths[0].isdigit()
                or int(lengths[0]) > self.max_bytes
                or response.getheader("Transfer-Encoding")
                or 300 <= response.status < 400
            ):
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
    """多线程服务器。每个连接 10 秒超时。"""

    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class _Handler(BaseHTTPRequestHandler):
    """处理一个内部请求：检查 Host、令牌、请求体格式，然后交给 dispatch 函数。"""

    server_version = "BidRadarInternal/1"

    def log_message(self, *_):
        pass

    def _read_body(self, expected):
        """读取请求体，总共最多等 BODY_TIMEOUT 秒。

        超时是按总时间算的：对方每次只发几个字节、一直拖着，也不会让等待时间无限延长。
        """
        deadline, body = time.monotonic() + BODY_TIMEOUT, bytearray()
        while len(body) < expected:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self.connection.settimeout(remaining)
            chunk = self.rfile.read1(min(65536, expected - len(body)))
            if not chunk:
                raise LocalHTTPError("incomplete_body", 400)
            body.extend(chunk)
            self._body_received += len(chunk)
        return bytes(body)

    def _discard_unread_body(self):
        """提前拒绝了请求之后，把对方还没发完的请求体读掉一部分再关闭连接。

        为什么要这样：在 Windows 上，如果服务器还有没读的数据就关闭连接，系统会发 RST，
        客户端可能因此收不到我们已经发出的错误响应。
        读掉的内容直接丢弃，不会被解析。最多读 64 KB 或 250 毫秒，超过就直接关闭。
        """
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths and self.headers.get("Transfer-Encoding") is None:
            return
        limit = min(REJECT_DRAIN_BYTES, self.server.max_bytes)
        if (
            self.headers.get("Transfer-Encoding") is None
            and len(lengths) == 1
            and re.fullmatch(r"[0-9]{1,10}", lengths[0])
        ):
            limit = min(limit, max(0, int(lengths[0]) - self._body_received))
        deadline = time.monotonic() + REJECT_DRAIN_TIMEOUT
        try:
            while limit:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.rfile.read1(min(8192, limit))
                if not chunk:
                    break
                limit -= len(chunk)
        except OSError:
            # 超时或对方断开：清理到此为止，已经发出的响应不受影响。
            pass

    def _send(self, status, value):
        """发送 JSON 响应，然后关闭连接。"""
        raw = encode(value)
        if len(raw) > self.server.max_bytes:
            status, raw = 503, encode({"error": {"code": "response_too_large", "message": "response_too_large"}})
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.wfile.flush()
        self.close_connection = True
        # 先关闭写方向（响应已经完整发出），再读掉对方没发完的数据，最后才完全关闭。
        try:
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return
        self._discard_unread_body()

    def _handle(self):
        """处理请求：检查 Host、转发头、路径、令牌、方法、请求体，然后调用 dispatch。"""
        self._body_received = 0
        try:
            if self.headers.get_all("Host") != [self.server.authority] or len(self.path) > 8192:
                raise LocalHTTPError("invalid_host", 400)
            if any(
                self.headers.get(name) is not None for name in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto")
            ):
                raise LocalHTTPError("forwarded_request_rejected", 400)
            parts = urlsplit(self.path)
            if not self.path.startswith("/") or parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
                raise LocalHTTPError("invalid_route", 400)
            tokens = self.headers.get_all("Authorization", [])
            identity = next(
                (
                    name
                    for name, token in self.server.tokens.items()
                    if len(tokens) == 1 and hmac.compare_digest(tokens[0].encode(), ("Bearer " + token).encode())
                ),
                None,
            )
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
                raw = self._read_body(int(lengths[0]))
                data = decode(raw)
            elif lengths and lengths != ["0"]:
                raise LocalHTTPError("unexpected_body", 400)
            # 有多个调用方时，把调用方的名字一起传给 dispatch，由服务自己决定谁能用哪个接口。
            # 例如 tracking 不能借用户发起分析的接口，绕过自动复核的委托检查。
            result = (
                self.server.dispatch(self.command, self.path, data, identity)
                if self.server.multiple_tokens
                else self.server.dispatch(self.command, self.path, data)
            )
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
    """创建内部服务器（未启动）。

    dispatch(method, path, body[, identity]) 处理业务，返回结果或 (状态码, 结果)。
    token 可以是一个令牌，也可以是 {调用方名字: 令牌}；后者会把调用方名字传给 dispatch。
    """
    multiple = isinstance(token, dict)
    tokens = token if multiple else {"service": token}
    if (
        host != "127.0.0.1"
        or not tokens
        or not all(isinstance(k, str) and token_valid(v) for k, v in tokens.items())
        or len(set(tokens.values())) != len(tokens)
        or type(port) is not int
        or not 0 <= port <= 65535
        or type(max_bytes) is not int
        or not 1 <= max_bytes <= 32 * 1024 * 1024
    ):
        raise ValueError("invalid_local_server")
    server = _Server((host, port), _Handler)
    server.dispatch, server.tokens, server.max_bytes = dispatch, tokens, max_bytes
    server.multiple_tokens = multiple
    server.authority = f"127.0.0.1:{server.server_port}"
    return server

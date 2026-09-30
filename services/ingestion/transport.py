"""单进程、限量公开资料传输：准入 → robots → 公网DNS → 固定IP连接 → 字节上限。

准入文件是经过人工核验的本地记录，不是网站许可证明；本模块不替人批准用途。
不读取环境代理、Cookie、账号或关闭TLS的选项，不自动取得外站附件。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import http.client
import ipaddress
import json
import math
from pathlib import Path
import queue
import re
import socket
import ssl
import threading
import time
from urllib.parse import unquote, urljoin, urlsplit

HOSTS = frozenset({"search.ccgp.gov.cn", "www.ccgp.gov.cn", "ccgp.gov.cn"})
KINDS = frozenset({"listing", "notice", "attachment", "robots"})
USER_AGENT = "BidRadar/0.1"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_ROBOTS_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


class FetchError(Exception):
    """可持久化的非敏感故障；attempts含本次fetch的robots、重定向和失败请求。"""

    def __init__(self, code: str, http_status: int | None = None, attempts: int = 0):
        self.code, self.http_status, self.attempts = code, http_status, attempts
        super().__init__(code)


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    http_status: int
    headers: dict[str, str]
    body: bytes
    fetched_at: str
    attempts: int


def _utcnow():
    return datetime.now(timezone.utc)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed.astimezone(timezone.utc)


def _url_parts(url):
    """拒绝URL解析器/服务器解释不同的路径，不修补外部网页给出的可疑链接。"""
    try:
        if (not isinstance(url, str) or not 1 <= len(url) <= 4096
                or any(ord(c) < 33 or ord(c) > 126 for c in url) or "\\" in url):
            raise ValueError()
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or parts.hostname not in HOSTS
                or parts.username is not None or parts.password is not None
                or parts.port is not None or parts.fragment or not parts.path.startswith("/")):
            raise ValueError()
        path = unquote(parts.path, errors="strict")
        if (any(c in path for c in "\\%?#") or any(ord(c) < 32 for c in path)
                or any(segment in (".", "..") for segment in path.split("/"))
                or re.search(r"%(?:2f|5c)", parts.path, re.I)):
            raise ValueError()
        return parts, path
    except (ValueError, UnicodeError):
        raise FetchError("url_not_allowed") from None


@dataclass(frozen=True)
class SourcePolicy:
    """固定域内再缩小到批准用途/路径；过期在每次动作前检查，不以缓存续期。"""

    purpose: str
    reviewed_at: datetime
    expires_at: datetime
    evidence: tuple[str, ...]
    attachments_allowed: bool
    allowed: tuple[tuple[str, str, tuple[str, ...]], ...]

    @classmethod
    def from_file(cls, path):
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise ValueError()
            return cls.from_dict(json.loads(raw.decode("utf-8")))
        except (OSError, UnicodeError, ValueError, TypeError):
            raise FetchError("policy_invalid") from None

    @classmethod
    def from_dict(cls, data):
        try:
            if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1
                    or data.get("approved") is not True):
                raise ValueError()
            purpose = data["purpose"]
            evidence = data["evidence"]
            attachments = data["attachments_allowed"]
            reviewed, expires = _timestamp(data["reviewed_at"]), _timestamp(data["expires_at"])
            if (not isinstance(purpose, str) or not purpose.strip()
                    or not isinstance(evidence, list) or not evidence
                    or any(not isinstance(e, str) or not e.strip() for e in evidence)
                    or type(attachments) is not bool or reviewed >= expires):
                raise ValueError()
            rules = []
            if not isinstance(data["allowed"], list) or not 1 <= len(data["allowed"]) <= 32:
                raise ValueError()
            for rule in data["allowed"]:
                host, prefix, kinds = rule["host"], rule["path_prefix"], rule["kinds"]
                if (host not in HOSTS or not isinstance(prefix, str) or not prefix.startswith("/")
                        or any(c in prefix for c in "\\%?#") or any(ord(c) < 33 or ord(c) > 126 for c in prefix)
                        or any(s in (".", "..") for s in prefix.split("/"))
                        or not isinstance(kinds, list) or not kinds
                        or any(k not in KINDS - {"robots"} for k in kinds)):
                    raise ValueError()
                rules.append((host, prefix, tuple(kinds)))
            return cls(purpose, reviewed, expires, tuple(evidence), attachments, tuple(rules))
        except (KeyError, ValueError, TypeError):
            raise FetchError("policy_invalid") from None

    def authorize(self, url, kind):
        self._authorize(url, kind, _utcnow())

    def _authorize(self, url, kind, now):
        if not self.reviewed_at <= now < self.expires_at:
            raise FetchError("policy_expired_or_not_yet_valid")
        parts, path = _url_parts(url)
        if not isinstance(kind, str) or kind not in KINDS:
            raise FetchError("invalid_kind")
        if kind == "robots":
            if path == "/robots.txt" and not parts.query and any(r[0] == parts.hostname for r in self.allowed):
                return
            raise FetchError("url_not_allowed")
        if kind == "listing" and (parts.hostname != "search.ccgp.gov.cn" or path != "/bxsearch"):
            raise FetchError("url_not_allowed")
        if kind == "notice" and (parts.hostname == "search.ccgp.gov.cn" or not re.fullmatch(
                r"/cggg/(?:zygg|dfgg)/[A-Za-z0-9_/-]+\.html?", path)):
            raise FetchError("url_not_allowed")
        if kind == "attachment" and not self.attachments_allowed:
            raise FetchError("attachment_not_approved")
        for host, prefix, kinds in self.allowed:
            # 目录前缀必须以/结束；否则只匹配精确路径，防止/bxsearch-evil混入。
            if (host == parts.hostname and kind in kinds
                    and (path.startswith(prefix) if prefix.endswith("/") else path == prefix)):
                return
        raise FetchError("url_not_allowed")


@dataclass(frozen=True)
class _Robots:
    rules: tuple[tuple[bool, str], ...]
    delay: float

    def permits(self, url):
        parts = urlsplit(url)
        target = _robot_normalize(parts.path + ("?" + parts.query if parts.query else ""))
        matches = []
        for allow, pattern in self.rules:
            if not pattern:
                continue
            pattern = _robot_normalize(pattern)
            end = pattern.endswith("$")
            value = pattern[:-1] if end else pattern
            if _robots_match(value, target, end):
                matches.append((len(value.replace("*", "")), allow))
        return max(matches)[1] if matches else True


def _robot_normalize(value):
    # robots匹配需把%63和c视为同一非保留字符，避免编码路径绕过Disallow。
    def replace(match):
        char = chr(int(match.group()[1:], 16))
        return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group().upper()
    return re.sub(r"%[0-9a-fA-F]{2}", replace, value)


def _robots_match(pattern, target, end):
    """按*分段顺序查找，避免把外部robots编成可能指数回溯的正则表达式。"""
    segments = pattern.split("*")
    if not target.startswith(segments[0]):
        return False
    if len(segments) == 1:
        return not end or target == pattern
    cursor = len(segments[0])
    for segment in segments[1:-1]:
        found = target.find(segment, cursor)
        if found < 0:
            return False
        cursor = found + len(segment)
    last = segments[-1]
    if end:
        return target.endswith(last) and len(target) - len(last) >= cursor
    return target.find(last, cursor) >= 0


def _parse_robots(body):
    """只接受可识别UTF-8规则，HTML挑战页/空页/未知语法失败关闭。支持*和$路径。"""
    try:
        if len(body) > MAX_ROBOTS_BYTES:
            raise ValueError()
        text = body.decode("utf-8-sig")
        if "<" in text or "\x00" in text or len(text.splitlines()) > 1024:
            raise ValueError()
        groups, agents, rules, delay, started = [], [], [], 0.0, False
        for original in text.splitlines() + [""]:
            line = original.split("#", 1)[0].strip()
            if not line:
                if agents and started:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, started = [], [], 0.0, False
                continue
            field, separator, value = line.partition(":")
            if not separator:
                raise ValueError()
            field, value = field.strip().lower(), value.strip()
            if field == "user-agent":
                if started:
                    groups.append((agents, rules, delay))
                    agents, rules, delay, started = [], [], 0.0, False
                if not re.fullmatch(r"[A-Za-z0-9_*.-]+", value):
                    raise ValueError()
                agents.append(value.lower())
            elif field == "sitemap":
                continue  # 仅记录规则，不取站点地图或递归扩展来源。
            elif field in ("allow", "disallow", "crawl-delay", "request-rate") and agents:
                started = True
                if field in ("allow", "disallow"):
                    if value and (not value.startswith("/") or len(value) > 2048
                                  or any(ord(c) < 33 or ord(c) > 126 for c in value)):
                        raise ValueError()
                    rules.append((field == "allow", value))
                elif field == "crawl-delay":
                    requested = float(value)
                    if not math.isfinite(requested) or requested < 0:
                        raise ValueError()
                    delay = max(delay, requested)
                else:
                    count, period = value.split("/")
                    count, period = float(count), float(period)
                    if not math.isfinite(count) or not math.isfinite(period) or min(count, period) <= 0:
                        raise ValueError()
                    delay = max(delay, period / count)
                if not math.isfinite(delay) or not 0 <= delay <= 60:
                    raise ValueError()
            else:
                raise ValueError()
        if agents:
            groups.append((agents, rules, delay))
        if not groups:
            raise ValueError()
        scored = [(max((len(a) for a in agents if a != "*" and a in "bidradar"), default=0), agents, rules, delay)
                  for agents, rules, delay in groups]
        best = max(s[0] for s in scored)
        chosen = [s for s in scored if (s[0] == best if best else "*" in s[1])]
        selected_rules = tuple(rule for s in chosen for rule in s[2])
        if len(selected_rules) > 256:
            raise ValueError()
        return _Robots(selected_rules, max((s[3] for s in chosen), default=0))
    except (ValueError, ZeroDivisionError, UnicodeError):
        raise FetchError("robots_invalid") from None


def _resolve(host, port, timeout):
    """系统DNS也受运行期限约束；超时的只读解析线程为daemon，不继续网络请求。"""
    result = queue.Queue(maxsize=1)
    def work():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)
    threading.Thread(target=work, daemon=True).start()
    try:
        records = result.get(timeout=timeout)
    except queue.Empty:
        raise FetchError("dns_timeout") from None
    if isinstance(records, OSError):
        raise FetchError("dns_error") from None
    return [record[4][0] for record in records]


def _public_unicast(address):
    """is_global单独不足以表示公网服务器：多播/旧IPv6站点本地也可能返回True。"""
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.is_link_local or address.is_reserved
            or address.is_private):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # 不将带接口作用域或隧道/映射地址交给OS另作路由解释；首源只需普通公网单播。
        return not (address.is_site_local or address.scope_id is not None
                    or address.ipv4_mapped is not None or address.sixtofour is not None
                    or address.teredo is not None)
    return True


class _PinnedConnection(http.client.HTTPConnection):
    """连接核验后的数字IP；HTTPS证书仍针对原域名验证，杜绝第二次DNS解析。"""

    def __init__(self, host, ip, secure, timeout):
        self.default_port = 443 if secure else 80
        super().__init__(host, self.default_port, timeout=timeout)
        self.ip, self.secure = ip, secure
        self.active_socket = None

    def connect(self):
        family = socket.AF_INET6 if ipaddress.ip_address(self.ip).version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock = sock
        self.active_socket = sock
        sock.settimeout(self.timeout)
        sock.connect((self.ip, self.port))
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            self.active_socket = self.sock


def _exchange(url, ip, timeout, max_bytes):
    """返回(status,headers,body)。不自动重定向；原始字节不解压、不执行HTML。"""
    parts = urlsplit(url)
    connection = _PinnedConnection(parts.hostname, ip, parts.scheme == "https", timeout)
    def expire():
        if connection.active_socket is not None:
            try:
                connection.active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.active_socket.close()
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    started = time.monotonic()
    try:
        target = parts.path + ("?" + parts.query if parts.query else "")
        connection.request("GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"})
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        # 拒绝/重定向/临时故障响应不用下载正文，更不能因错误页体积遮掉403/429。
        if response.status != 200:
            return response.status, headers, b""
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise FetchError("content_encoding_not_supported", response.status)
        if "content-length" in headers:
            try:
                length = int(headers["content-length"])
                if length < 0 or length > max_bytes:
                    raise FetchError("body_too_large", response.status)
            except ValueError:
                raise FetchError("invalid_content_length", response.status) from None
        chunks, received = [], 0
        while True:
            if time.monotonic() - started >= timeout:
                raise TimeoutError()
            chunk = response.read1(min(65536, max_bytes - received + 1))
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise FetchError("body_too_large", response.status)
            chunks.append(chunk)
        if "content-length" in headers and received != int(headers["content-length"]):
            raise FetchError("truncated_body", response.status)
        return response.status, headers, b"".join(chunks)
    finally:
        timer.cancel()
        connection.close()


class Transport:
    """一次CLI运行复用同一实例以共享robots、限频、请求/字节/时长预算。

    exchange(url,ip,timeout,max_bytes)和resolver(host,port)仅供可信代码注入模拟；
    clock为单调秒，utcnow为带时区datetime。不是可从外部JSON指定的插件入口。
    """

    def __init__(self, policy, *, timeout=10, max_attempts=2, min_interval=2,
                 max_requests=50, max_seconds=180, exchange=None, resolver=None,
                 clock=None, sleep=None, utcnow=None):
        if (type(timeout) not in (int, float) or not 0 < timeout <= 30
                or type(max_attempts) is not int or not 1 <= max_attempts <= 3
                or type(min_interval) not in (int, float) or not 0 <= min_interval <= 60
                or type(max_requests) is not int or not 1 <= max_requests <= 100
                or type(max_seconds) not in (int, float) or not 0 < max_seconds <= 600
                or (exchange is None and min_interval < 2)):
            raise ValueError("invalid transport limits")
        self.policy, self.timeout, self.max_attempts = policy, timeout, max_attempts
        self.min_interval, self.max_requests, self.max_seconds = min_interval, max_requests, max_seconds
        self.exchange, self.resolver = exchange or _exchange, resolver
        self.clock, self.sleep, self.utcnow = clock or time.monotonic, sleep or time.sleep, utcnow or _utcnow
        self.started, self.last_request = self.clock(), None
        self.requests, self.bytes_received = 0, 0
        self.robots, self.blocked_hosts = {}, set()

    def _remaining(self):
        remaining = self.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise FetchError("time_budget_exceeded")
        return remaining

    def _wait(self, delay):
        if delay >= self._remaining():
            raise FetchError("time_budget_exceeded")
        if delay > 0:
            self.sleep(delay)
        self._remaining()

    def _authorize(self, url, kind):
        if not isinstance(self.policy, SourcePolicy):
            raise FetchError("policy_required")
        self.policy._authorize(url, kind, self.utcnow())
        if urlsplit(url).hostname in self.blocked_hosts:
            raise FetchError("blocked")

    def _request(self, url, kind, max_bytes, delay):
        self._authorize(url, kind)
        if self.requests >= self.max_requests:
            raise FetchError("request_budget_exceeded")
        if self.bytes_received >= MAX_TOTAL_BYTES:
            raise FetchError("byte_budget_exceeded")
        if self.last_request is not None:
            self._wait(max(0, max(self.min_interval, delay) - (self.clock() - self.last_request)))
        self._authorize(url, kind)  # 限频等待后可能已过期，不能沿用等待前的批准。
        parts = urlsplit(url)
        port = 443 if parts.scheme == "https" else 80
        addresses = (self.resolver(parts.hostname, port) if self.resolver else
                     _resolve(parts.hostname, port, min(self.timeout, self._remaining())))
        try:
            addresses = tuple(ipaddress.ip_address(value) for value in addresses)
            if not addresses or any(not _public_unicast(address) for address in addresses):
                raise ValueError()
        except ValueError:
            raise FetchError("dns_not_public") from None
        self._authorize(url, kind)
        timeout = min(self.timeout, self._remaining())
        self.last_request = self.clock()
        self.requests += 1  # 真正调用exchange前计数；超时和失败请求也消耗预算。
        allowance = min(max_bytes, MAX_TOTAL_BYTES - self.bytes_received)
        try:
            status, headers, body = self.exchange(url, str(addresses[0]), timeout, allowance)
        except (FetchError, OSError, http.client.HTTPException):
            # 无完整响应时无法得知已读字节，按本次上限保守扣预算，防失败重试绕过累计上限。
            self.bytes_received += allowance
            raise
        self._remaining()
        headers = {k.lower(): v for k, v in headers.items()}
        self.bytes_received += len(body)
        if status in (401, 403, 429):
            self.blocked_hosts.add(parts.hostname)
            raise FetchError("blocked", status)
        if len(body) > allowance or self.bytes_received > MAX_TOTAL_BYTES:
            raise FetchError("body_too_large", status)
        if headers.get("content-encoding", "identity").lower() != "identity":
            raise FetchError("content_encoding_not_supported", status)
        return status, headers, body

    def _robots_for(self, url):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            try:
                _, status, _, body = self._read(origin + "/robots.txt", "robots", MAX_ROBOTS_BYTES)
                self.robots[origin] = _parse_robots(body)
            except FetchError as exc:
                if exc.code in ("blocked", "policy_expired_or_not_yet_valid", "request_budget_exceeded", "time_budget_exceeded"):
                    raise
                raise FetchError("robots_unavailable", exc.http_status) from None
        return self.robots[origin]

    def _retry_delay(self, headers, attempt):
        raw = headers.get("retry-after")
        if raw is None:
            return min(2 ** attempt, 10)
        try:
            delay = float(raw) if raw.isdigit() else (parsedate_to_datetime(raw) - self.utcnow()).total_seconds()
            delay = max(0, delay)
            if not math.isfinite(delay) or delay > 60:
                raise ValueError()
            return delay
        except (ValueError, TypeError, OverflowError):
            raise FetchError("retry_after_out_of_bounds") from None

    def _read(self, url, kind, max_bytes):
        self._authorize(url, kind)
        initial_host = urlsplit(url).hostname
        for redirects in range(4):
            self._authorize(url, kind)
            robots = self._robots_for(url) if kind != "robots" else _Robots((), 0)
            if not robots.permits(url):
                raise FetchError("robots_denied")
            for attempt in range(self.max_attempts):
                try:
                    status, headers, body = self._request(url, kind, max_bytes, robots.delay)
                except ssl.SSLError:
                    raise FetchError("tls_error") from None
                except (OSError, http.client.HTTPException):
                    if attempt + 1 == self.max_attempts:
                        raise FetchError("network_error") from None
                    self._wait(min(2 ** attempt, 10))
                    continue
                if status in (401, 403, 429):
                    self.blocked_hosts.add(urlsplit(url).hostname)
                    raise FetchError("blocked", status)
                if status in (500, 502, 503, 504):
                    if attempt + 1 == self.max_attempts:
                        raise FetchError("http_error", status)
                    self._wait(self._retry_delay(headers, attempt))
                    continue
                break
            if status in (301, 302, 303, 307, 308):
                if redirects == 3:
                    raise FetchError("redirect_limit", status)
                try:
                    target = urljoin(url, headers.get("location", ""))
                except ValueError:
                    raise FetchError("redirect_not_allowed", status) from None
                if (not headers.get("location") or (urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https")
                        or (kind == "robots" and urlsplit(target).hostname != initial_host)):
                    raise FetchError("redirect_not_allowed", status)
                self._authorize(target, kind)
                url = target
                continue
            if status != 200:
                raise FetchError("http_error", status)
            return url, status, headers, body
        raise FetchError("redirect_limit")

    def fetch(self, url, *, max_bytes, kind):
        """只交付完整成功响应；失败不给半截原件。robots未知时不发送资料请求。"""
        before = self.requests
        try:
            if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BODY_BYTES:
                raise FetchError("invalid_byte_limit")
            final_url, status, headers, body = self._read(url, kind, max_bytes)
            return FetchResult(url, final_url, status, headers, body,
                               self.utcnow().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                               self.requests - before)
        except FetchError as exc:
            exc.attempts = self.requests - before
            raise

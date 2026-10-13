"""抓取网页的网络层。每个请求都按顺序过这几道关：

    1. 来源准入：URL 必须在准入文件允许的域名和路径范围内，准入文件也不能过期。
    2. robots.txt：网站禁止抓取的路径不抓。
    3. DNS：只连公网地址，内网、回环、组播地址一律拒绝。
    4. 固定 IP 连接：用解析出的 IP 直接连，HTTPS 证书仍按原域名验证，避免两次 DNS 解析结果不同。
    5. 限额：请求次数、下载字节数、总时长都有上限，请求之间有最小间隔。

准入文件是人工核验后写的本地记录，不是网站给的许可证明，这个模块也不替人做批准。
不读取系统代理、不带 Cookie 或账号、不关闭 TLS 校验，不会自动去别的网站取附件。
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

HOSTS = frozenset({"search.ccgp.gov.cn", "www.ccgp.gov.cn", "ccgp.gov.cn", "ggzy.hainan.gov.cn"})
KINDS = frozenset({"listing", "notice", "attachment", "robots"})
USER_AGENT = "BidRadar/0.1"
MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_ROBOTS_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024


class FetchError(Exception):
    """抓取失败。code 是错误码；attempts 是这次抓取一共发了几个请求（含 robots、重定向和失败的请求）。"""

    def __init__(self, code: str, http_status: int | None = None, attempts: int = 0):
        self.code, self.http_status, self.attempts = code, http_status, attempts
        super().__init__(code)


class _CheckpointFailure(Exception):
    """包装检查回调（checkpoint）抛出的异常，让它穿过内部的重试逻辑，在 fetch 出口处再还原成原异常。"""

    def __init__(self, error):
        self.error, self.traceback = error, error.__traceback__


def _checkpoint(callback):
    """调用检查回调；回调抛出的异常包装成 _CheckpointFailure。"""
    if callback is not None:
        try:
            callback()
        except Exception as error:
            # 回调抛出的是"任务被取消"之类的控制信号，不能被当成网络故障去重试。
            raise _CheckpointFailure(error) from None


@dataclass(frozen=True)
class FetchResult:
    """一次成功抓取的结果：请求 URL、最终 URL、状态码、响应头、正文、时间、请求次数。"""

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
    """拆分并检查 URL。

    拒绝那些不同程序可能理解成不同地址的写法（反斜杠、奇怪的转义等）。
    网页给出的可疑链接直接拒绝，不尝试修补。
    """
    try:
        if (
            not isinstance(url, str)
            or not 1 <= len(url) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in url)
            or "\\" in url
        ):
            raise ValueError()
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or parts.hostname not in HOSTS
            or parts.username is not None
            or parts.password is not None
            or (
                parts.port is not None
                and not (
                    parts.hostname == "ggzy.hainan.gov.cn" and parts.port == (443 if parts.scheme == "https" else 80)
                )
            )
            or parts.fragment
            or not parts.path.startswith("/")
        ):
            raise ValueError()
        path = unquote(parts.path, errors="strict")
        if (
            any(c in path for c in "\\%?#")
            or any(ord(c) < 32 for c in path)
            or any(segment in (".", "..") for segment in path.split("/"))
            or re.search(r"%(?:2f|5c)", parts.path, re.I)
        ):
            raise ValueError()
        return parts, path
    except (ValueError, UnicodeError):
        raise FetchError("url_not_allowed") from None


@dataclass(frozen=True)
class SourcePolicy:
    """来源准入规则：允许访问哪些域名、哪些路径、哪类内容（列表、公告、附件），有效期到什么时候。

    每次请求前都检查一遍有效期，过期了就拒绝，不会因为之前检查过就放行。
    """

    purpose: str
    reviewed_at: datetime
    expires_at: datetime
    evidence: tuple[str, ...]
    attachments_allowed: bool
    allowed: tuple[tuple[str, str, tuple[str, ...]], ...]
    schema_version: int = 1
    robots_missing: str = "deny"

    @classmethod
    def from_file(cls, path):
        """从 JSON 文件读取准入规则。"""
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
        """从字典构造准入规则，并检查格式。"""
        try:
            if (
                not isinstance(data, dict)
                or type(data.get("schema_version")) is not int
                or data.get("schema_version") not in (1, 2)
                or data.get("approved") is not True
            ):
                raise ValueError()
            version = data["schema_version"]
            missing = data.get("robots_missing", "deny")
            if missing not in ("deny", "allow_404") or (version == 1 and missing != "deny"):
                raise ValueError()
            purpose = data["purpose"]
            evidence = data["evidence"]
            attachments = data["attachments_allowed"]
            reviewed, expires = _timestamp(data["reviewed_at"]), _timestamp(data["expires_at"])
            if (
                not isinstance(purpose, str)
                or not purpose.strip()
                or not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(e, str) or not e.strip() for e in evidence)
                or type(attachments) is not bool
                or reviewed >= expires
            ):
                raise ValueError()
            rules = []
            if not isinstance(data["allowed"], list) or not 1 <= len(data["allowed"]) <= 32:
                raise ValueError()
            for rule in data["allowed"]:
                host, prefix, kinds = rule["host"], rule["path_prefix"], rule["kinds"]
                if (
                    host not in HOSTS
                    or (version == 1 and host == "ggzy.hainan.gov.cn")
                    or not isinstance(prefix, str)
                    or not prefix.startswith("/")
                    or any(c in prefix for c in "\\%?#")
                    or any(ord(c) < 33 or ord(c) > 126 for c in prefix)
                    or any(s in (".", "..") for s in prefix.split("/"))
                    or not isinstance(kinds, list)
                    or not kinds
                    or any(k not in KINDS - {"robots"} for k in kinds)
                ):
                    raise ValueError()
                rules.append((host, prefix, tuple(kinds)))
            return cls(purpose, reviewed, expires, tuple(evidence), attachments, tuple(rules), version, missing)
        except (KeyError, ValueError, TypeError):
            raise FetchError("policy_invalid") from None

    def authorize(self, url, kind):
        """检查这个 URL 和内容类型是否在准入范围内，不在就抛 FetchError。"""
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
        if kind in ("listing", "notice"):
            legacy = (
                (parts.hostname == "search.ccgp.gov.cn" and path == "/bxsearch")
                if kind == "listing"
                else (
                    parts.hostname in ("www.ccgp.gov.cn", "ccgp.gov.cn")
                    and bool(re.fullmatch(r"/cggg/(?:zygg|dfgg)/[A-Za-z0-9_/-]+\.html?", path))
                )
            )
            public = False
            if self.schema_version == 2 and not parts.query:
                from services.ingestion.sources.public_notices import canonical_notice_url, listing_identity

                source = "cn_hainan" if parts.hostname == "ggzy.hainan.gov.cn" else "cn_ccgp"
                public = (
                    canonical_notice_url(source, url) is not None
                    if kind == "notice"
                    else listing_identity(source, url) is not None
                )
            if not legacy and not public:
                raise FetchError("url_not_allowed")
        if kind == "attachment" and not self.attachments_allowed:
            raise FetchError("attachment_not_approved")
        for host, prefix, kinds in self.allowed:
            # 路径前缀必须以 / 结尾，否则只做精确匹配。不然 /bxsearch 会把 /bxsearch-evil 也放进来。
            if (
                host == parts.hostname
                and kind in kinds
                and (path.startswith(prefix) if prefix.endswith("/") else path == prefix)
            ):
                return
        raise FetchError("url_not_allowed")


@dataclass(frozen=True)
class _Robots:
    """解析好的 robots.txt 规则。"""

    rules: tuple[tuple[bool, str], ...]
    delay: float

    def permits(self, url):
        """按 robots 规则判断这个 URL 能不能抓：最长匹配的规则说了算。"""
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
    """把路径里的百分号编码统一一下，方便和 robots 规则比较。"""

    # 例如 %63 和 c 是同一个字符，不统一的话，编码过的路径就能绕过 Disallow 规则。
    def replace(match):
        char = chr(int(match.group()[1:], 16))
        return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group().upper()

    return re.sub(r"%[0-9a-fA-F]{2}", replace, value)


def _robots_match(pattern, target, end):
    """判断路径是否匹配一条 robots 规则（支持 * 通配和结尾的 $）。

    用分段查找实现，而不是把规则转成正则：外部写的规则转成正则可能非常慢。
    """
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
    """解析 robots.txt。

    只接受能识别的 UTF-8 规则；拿到的是 HTML 验证页、空页面或看不懂的语法时，按"禁止抓取"处理。
    """
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
                continue  # 只读规则，不去抓 Sitemap 里列出的地址。
            elif field in ("allow", "disallow", "crawl-delay", "request-rate") and agents:
                started = True
                if field in ("allow", "disallow"):
                    if value and (
                        not value.startswith("/")
                        or len(value) > 2048
                        or any(ord(c) < 33 or ord(c) > 126 for c in value)
                    ):
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
        scored = [
            (max((len(a) for a in agents if a != "*" and a in "bidradar"), default=0), agents, rules, delay)
            for agents, rules, delay in groups
        ]
        best = max(s[0] for s in scored)
        chosen = [s for s in scored if (s[0] == best if best else "*" in s[1])]
        selected_rules = tuple(rule for s in chosen for rule in s[2])
        if len(selected_rules) > 256:
            raise ValueError()
        return _Robots(selected_rules, max((s[3] for s in chosen), default=0))
    except (ValueError, ZeroDivisionError, UnicodeError):
        raise FetchError("robots_invalid") from None


def _resolve(host, port, timeout):
    """用系统 DNS 解析主机名，并且受超时限制。

    系统 DNS 调用本身不能中途取消，所以放在后台线程里跑；超时就不再等它，它也不会再发起别的请求。
    """
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
    """这个 IP 是不是普通的公网单播地址。

    只看 is_global 不够：组播地址、旧的 IPv6 站点本地地址也可能被判成 global。
    """
    if (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_private
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        # 带接口作用域的地址、隧道地址、IPv4 映射地址也拒绝：操作系统可能把它们路由到别处。
        return not (
            address.is_site_local
            or address.scope_id is not None
            or address.ipv4_mapped is not None
            or address.sixtofour is not None
            or address.teredo is not None
        )
    return True


class _PinnedConnection(http.client.HTTPConnection):
    """直接连接已经核验过的 IP。HTTPS 证书仍按原来的域名验证。

    这样就不会发生"检查时解析到公网地址、真正连接时又解析到内网地址"的情况。
    """

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
    """发一个 GET 请求，返回 (状态码, 响应头, 正文)。不跟随重定向；正文按原始字节返回，不解压。"""
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
        connection.request(
            "GET", target, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Connection": "close"}
        )
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        # 状态码不是 200 时不下载正文。否则一个很大的错误页可能触发大小超限，把真正的 403/429 掩盖掉。
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
    """抓取器。一次命令行运行只用一个实例，这样 robots 缓存、限频和各项限额在整个运行中共享。

    exchange、resolver、clock、utcnow 这几个参数只给测试用，用来替换网络、DNS 和时钟。
    """

    def __init__(
        self,
        policy,
        *,
        timeout=10,
        max_attempts=2,
        min_interval=2,
        max_requests=50,
        max_seconds=180,
        exchange=None,
        resolver=None,
        clock=None,
        sleep=None,
        utcnow=None,
        dns_mode="system",
        allow_network=True,
    ):
        if (
            type(timeout) not in (int, float)
            or not 0 < timeout <= 30
            or type(max_attempts) is not int
            or not 1 <= max_attempts <= 3
            or type(min_interval) not in (int, float)
            or not 0 <= min_interval <= 60
            or type(max_requests) is not int
            or not 1 <= max_requests <= 100
            or type(max_seconds) not in (int, float)
            or not 0 < max_seconds <= 600
            or (exchange is None and min_interval < 2)
        ):
            raise ValueError("invalid transport limits")
        if dns_mode not in ("system", "google-doh") or type(allow_network) is not bool:
            raise ValueError("invalid transport options")
        self.policy, self.timeout, self.max_attempts = policy, timeout, max_attempts
        self.min_interval, self.max_requests, self.max_seconds = min_interval, max_requests, max_seconds
        self.exchange, self.resolver = exchange or _exchange, resolver
        self.clock, self.sleep, self.utcnow = clock or time.monotonic, sleep or time.sleep, utcnow or _utcnow
        self.started, self.last_request = self.clock(), None
        self.requests, self.bytes_received = 0, 0
        self.robots, self.blocked_hosts = {}, set()
        self.dns_mode, self.allow_network = dns_mode, allow_network
        self.dns_requests = 0

    def _remaining(self):
        remaining = self.max_seconds - (self.clock() - self.started)
        if remaining <= 0:
            raise FetchError("time_budget_exceeded")
        return remaining

    def _wait(self, delay, checkpoint):
        _checkpoint(checkpoint)
        if delay >= self._remaining():
            raise FetchError("time_budget_exceeded")
        if delay > 0:
            self.sleep(delay)
        _checkpoint(checkpoint)
        self._remaining()

    def _authorize(self, url, kind):
        if not self.allow_network:
            raise FetchError("network_opt_in_required")
        if not isinstance(self.policy, SourcePolicy):
            raise FetchError("policy_required")
        self.policy._authorize(url, kind, self.utcnow())
        if urlsplit(url).hostname in self.blocked_hosts:
            raise FetchError("blocked")

    def _request(self, url, kind, max_bytes, delay, checkpoint):
        _checkpoint(checkpoint)
        self._authorize(url, kind)
        if self.requests >= self.max_requests:
            raise FetchError("request_budget_exceeded")
        if self.bytes_received >= MAX_TOTAL_BYTES:
            raise FetchError("byte_budget_exceeded")
        if self.last_request is not None:
            self._wait(max(0, max(self.min_interval, delay) - (self.clock() - self.last_request)), checkpoint)
        _checkpoint(checkpoint)
        self._authorize(url, kind)  # 等待限频间隔期间准入可能过期了，所以等完再检查一次。
        parts = urlsplit(url)
        port = 443 if parts.scheme == "https" else 80
        try:
            timeout = min(self.timeout, self._remaining())
            if self.resolver:
                addresses = self.resolver(parts.hostname, port)
            elif self.dns_mode == "google-doh":
                from services.ingestion.public_dns import resolve_public_dns

                self.dns_requests += 1
                addresses = resolve_public_dns(parts.hostname, port, timeout)
            else:
                addresses = _resolve(parts.hostname, port, timeout)
        finally:
            _checkpoint(checkpoint)
        try:
            addresses = tuple(ipaddress.ip_address(value) for value in addresses)
            if not addresses or any(not _public_unicast(address) for address in addresses):
                raise ValueError()
        except ValueError:
            raise FetchError("dns_not_public") from None
        # 等待和 DNS 解析期间运行可能被取消，所以发请求前再检查一次。
        _checkpoint(checkpoint)
        self._authorize(url, kind)
        timeout = min(self.timeout, self._remaining())
        self.last_request = self.clock()
        self.requests += 1  # 发请求前就计数：超时和失败的请求也算次数。
        allowance = min(max_bytes, MAX_TOTAL_BYTES - self.bytes_received)
        try:
            status, headers, body = self.exchange(url, str(addresses[0]), timeout, allowance)
        except (FetchError, OSError, http.client.HTTPException):
            # 请求失败时不知道读了多少字节，按这次的上限扣，免得靠反复失败重试绕过总字节限额。
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

    def _robots_for(self, url, checkpoint):
        _checkpoint(checkpoint)
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.robots:
            try:
                _, status, _, body = self._read(origin + "/robots.txt", "robots", MAX_ROBOTS_BYTES, checkpoint)
                self.robots[origin] = _parse_robots(body)
            except FetchError as exc:
                # 按 RFC 9309，robots.txt 返回 404 可以视为"没有限制"（但这不等于获得了使用许可）。
                # 只有准入文件明确开启这个选项才这样处理；403、429、5xx 和网络错误一律按"禁止"处理。
                if exc.code == "http_error" and exc.http_status == 404 and self.policy.robots_missing == "allow_404":
                    self.robots[origin] = _Robots((), 0)
                    return self.robots[origin]
                if exc.code in (
                    "blocked",
                    "policy_expired_or_not_yet_valid",
                    "request_budget_exceeded",
                    "time_budget_exceeded",
                ):
                    raise
                raise FetchError("robots_unavailable", exc.http_status) from None
        return self.robots[origin]

    def _retry_delay(self, headers, attempt):
        raw = headers.get("retry-after")
        if raw is None:
            return min(2**attempt, 10)
        try:
            delay = float(raw) if raw.isdigit() else (parsedate_to_datetime(raw) - self.utcnow()).total_seconds()
            delay = max(0, delay)
            if not math.isfinite(delay) or delay > 60:
                raise ValueError()
            return delay
        except (ValueError, TypeError, OverflowError):
            raise FetchError("retry_after_out_of_bounds") from None

    def _read(self, url, kind, max_bytes, checkpoint):
        _checkpoint(checkpoint)
        self._authorize(url, kind)
        initial_host = urlsplit(url).hostname
        for redirects in range(4):
            _checkpoint(checkpoint)
            self._authorize(url, kind)
            robots = self._robots_for(url, checkpoint) if kind != "robots" else _Robots((), 0)
            if not robots.permits(url):
                raise FetchError("robots_denied")
            for attempt in range(self.max_attempts):
                try:
                    status, headers, body = self._request(url, kind, max_bytes, robots.delay, checkpoint)
                except ssl.SSLError:
                    _checkpoint(checkpoint)
                    raise FetchError("tls_error") from None
                except (OSError, http.client.HTTPException):
                    _checkpoint(checkpoint)
                    if attempt + 1 == self.max_attempts:
                        raise FetchError("network_error") from None
                    self._wait(min(2**attempt, 10), checkpoint)
                    continue
                _checkpoint(checkpoint)
                if status in (401, 403, 429):
                    self.blocked_hosts.add(urlsplit(url).hostname)
                    raise FetchError("blocked", status)
                if status in (500, 502, 503, 504):
                    if attempt + 1 == self.max_attempts:
                        raise FetchError("http_error", status)
                    self._wait(self._retry_delay(headers, attempt), checkpoint)
                    continue
                break
            if status in (301, 302, 303, 307, 308):
                if redirects == 3:
                    raise FetchError("redirect_limit", status)
                try:
                    target = urljoin(url, headers.get("location", ""))
                except ValueError:
                    raise FetchError("redirect_not_allowed", status) from None
                if (
                    not headers.get("location")
                    or (urlsplit(url).scheme == "https" and urlsplit(target).scheme != "https")
                    or (kind == "robots" and urlsplit(target).hostname != initial_host)
                ):
                    raise FetchError("redirect_not_allowed", status)
                self._authorize(target, kind)
                url = target
                continue
            if status != 200:
                raise FetchError("http_error", status)
            return url, status, headers, body
        raise FetchError("redirect_limit")

    def fetch(self, url, *, max_bytes, kind, checkpoint=None):
        """抓取一个 URL，返回 FetchResult；失败抛 FetchError。

        会自动处理 robots、有限次数的重试和重定向（重定向目标也要通过准入检查）。
        checkpoint 是调用方传入的检查函数，在每次网络动作之前调用，用来检查运行是否被取消。
        它抛出的异常会原样传出。已经发出的请求不会被强行中断，但之后不会再发新的。
        """
        before = self.requests
        try:
            if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_BODY_BYTES:
                raise FetchError("invalid_byte_limit")
            final_url, status, headers, body = self._read(url, kind, max_bytes, checkpoint)
            return FetchResult(
                url,
                final_url,
                status,
                headers,
                body,
                self.utcnow().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                self.requests - before,
            )
        except _CheckpointFailure as exc:
            raise exc.error.with_traceback(exc.traceback) from None
        except FetchError as exc:
            exc.attempts = self.requests - before
            raise

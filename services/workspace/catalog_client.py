"""workspace 到 catalog 的只读 HTTP 客户端；不 import catalog ORM、不跨库读取。

只有固定 loopback 服务与固定路由可访问；无代理、重定向、原件下载或模型调用。
选择校验返回当前观察的最小冻结快照；不是两个服务的原子事务，后续消费仍须
按工作流重验当前权限/版本，不承诺保存之后目录永不变化。
"""
from __future__ import annotations

from http.client import HTTPConnection, HTTPException
import json
import math
import re
import time
from urllib.parse import urlencode, urlsplit

_HEX = re.compile(r"[0-9a-f]{64}")
_KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}


class CatalogError(Exception):
    """仅携带稳定错误码/HTTP 状态，不把令牌、上游原文或网络地址拼进异常。"""
    def __init__(self, code: str, status: int = 503):
        super().__init__(code)
        self.code, self.status = code, status


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non_finite_json")


def _decode(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    pending, nodes = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 64 or nodes > 200_000 or isinstance(item, float) and not math.isfinite(item):
            raise ValueError("json_resource_limit")
        if isinstance(item, dict):
            pending.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, depth + 1) for v in item)
    if not isinstance(value, dict):
        raise ValueError("invalid_json_shape")
    return value


def _observation(value):
    required = {"observation_id", "notice_id", "source_id", "simulation", "source_record_key", "identity_kind",
                "source_url", "capture_id", "raw_sha256", "parser_version", "normalizer_version", "observed_at",
                "title", "notice_type", "facts", "references", "material_status", "attribution", "run_id"}
    if (not isinstance(value, dict) or any(not isinstance(value.get(k), str) or not _HEX.fullmatch(value[k])
            for k in ("notice_id", "observation_id"))
            or not required.issubset(value) or value.get("notice_type") not in _KINDS
            or not isinstance(value.get("title"), str) or len(value["title"]) > 2000
            or not isinstance(value.get("source_url"), str) or len(value["source_url"]) > 4096
            or type(value.get("simulation")) is not bool or not isinstance(value.get("facts"), dict)):
        raise ValueError("invalid_observation")
    parts = urlsplit(value["source_url"])
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None
            or parts.password is not None or any(ord(c) <= 32 or ord(c) == 127 for c in value["source_url"])
            or "\\" in value["source_url"]):
        raise ValueError("invalid_source_reference")


class CatalogClient:
    def __init__(self, base_url, token, *, timeout=5, max_bytes=8 * 1024 * 1024):
        # 用完整字符串文法拒绝 localhost、userinfo、路径/查询、替代 IP 表示法与任意 URL。
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", base_url) if isinstance(base_url, str) else None
        if match is None or not 1 <= int(match[1]) <= 65535:
            raise ValueError("invalid_catalog_address")
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token) is None:
            raise ValueError("invalid_service_token")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30
                or type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024):
            raise ValueError("invalid_client_limits")
        self._port, self._token, self._timeout, self._max_bytes = int(match[1]), token, timeout, max_bytes

    def _request(self, target):
        if len(target) > 8192:
            raise CatalogError("invalid_query", 400)
        connection = HTTPConnection("127.0.0.1", self._port, timeout=self._timeout)
        deadline = time.monotonic() + self._timeout
        response = None
        try:
            connection.request("GET", target, headers={"Authorization": "Bearer " + self._token,
                                                       "Accept": "application/json", "Connection": "close"})
            sock = connection.sock
            def remaining():
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    raise TimeoutError()
                sock.settimeout(seconds)
            remaining()
            response = connection.getresponse()
            # http.client 从不自动跟随 3xx；仅接受无压缩的 JSON，有界读取且有总时限。
            if (response.status in range(300, 400) or response.getheader("Content-Encoding") is not None
                    or response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json"):
                raise ValueError("unexpected_catalog_response")
            lengths = response.headers.get_all("Content-Length", [])
            if (len(lengths) > 1 or lengths and response.getheader("Transfer-Encoding") is not None
                    or lengths and (not lengths[0].isdigit() or int(lengths[0]) > self._max_bytes)):
                raise ValueError("response_size_limit")
            raw = bytearray()
            while not (lengths and len(raw) == int(lengths[0])):
                remaining()
                chunk = response.read1(min(65536, self._max_bytes + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > self._max_bytes:
                    raise ValueError("response_size_limit")
            if lengths and len(raw) != int(lengths[0]):
                raise ValueError("incomplete_response")
            payload = _decode(raw)
            if response.status != 200:
                error = payload.get("error")
                if (set(payload) != {"error"} or not isinstance(error, dict)
                        or set(error) != {"code", "message"} or not isinstance(error["code"], str)
                        or not isinstance(error["message"], str)):
                    raise ValueError("invalid_error_shape")
                accepted = {400: {"invalid_query", "invalid_cursor", "cursor_query_mismatch"},
                            404: {"notice_not_found"}}
                if error["code"] in accepted.get(response.status, set()):
                    raise CatalogError(error["code"], response.status)
                raise CatalogError("catalog_unavailable", 503)
            return payload
        except (OSError, HTTPException, ValueError, TypeError, RecursionError, UnicodeError):
            raise CatalogError("catalog_unavailable", 503) from None
        finally:
            # Connection: close 时响应流仍可能持有 socket 引用；超时/拒绝响应也必须释放。
            if response is not None:
                response.close()
            connection.close()

    def query(self, *, keyword="", kind=None, page_size=20, simulation=False, cursor=None):
        """默认真实目录；显式 simulation=True 才查询演示。游标须沿用同一组过滤条件。"""
        if (not isinstance(keyword, str) or len(keyword) > 200 or type(page_size) is not int
                or not 1 <= page_size <= 100 or type(simulation) is not bool
                or kind is not None and (not isinstance(kind, str) or kind not in _KINDS)
                or cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 5000)
                or any(ord(c) < 32 or ord(c) == 127 for c in keyword + (cursor or ""))):
            raise CatalogError("invalid_query", 400)
        filters = {"keyword": keyword, "page_size": page_size, "simulation": str(simulation).lower()}
        if kind is not None:
            filters["kind"] = kind
        if cursor is not None:
            filters["cursor"] = cursor
        result = self._request("/v1/notices?" + urlencode(filters))
        try:
            if (set(result) != {"items", "total", "next_cursor", "as_of", "snapshot", "simulation", "recent_runs"}
                    or not isinstance(result["items"], list) or len(result["items"]) > page_size
                    or type(result["total"]) is not int or result["total"] < len(result["items"])
                    or type(result["snapshot"]) is not int or result["snapshot"] < 0
                    or type(result["simulation"]) is not bool or result["simulation"] != simulation
                    or not isinstance(result["as_of"], str) or len(result["as_of"]) > 40
                    or not isinstance(result["recent_runs"], list) or len(result["recent_runs"]) > 10
                    or result["next_cursor"] is not None and (not isinstance(result["next_cursor"], str)
                        or not 1 <= len(result["next_cursor"]) <= 5000)):
                raise ValueError()
            ids = set()
            for item in result["items"]:
                _observation(item)
                if item["simulation"] != simulation or item["notice_id"] in ids or not isinstance(item.get("currentness"), dict):
                    raise ValueError()
                ids.add(item["notice_id"])
        except (ValueError, KeyError, TypeError):
            raise CatalogError("catalog_unavailable", 503) from None
        return result

    def detail(self, notice_id):
        if not isinstance(notice_id, str) or not _HEX.fullmatch(notice_id):
            raise CatalogError("invalid_notice_id", 400)
        result = self._request("/v1/notices/" + notice_id)
        try:
            if (set(result) != {"current", "history", "currentness", "relationships", "snapshot"}
                    or not isinstance(result["history"], list) or not result["history"]
                    or not isinstance(result["relationships"], list) or not isinstance(result["currentness"], dict)
                    or type(result["snapshot"]) is not int or result["snapshot"] < 0
                    or result["current"] != result["history"][0]):
                raise ValueError()
            for observation in result["history"]:
                _observation(observation)
                if observation["notice_id"] != notice_id:
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise CatalogError("catalog_unavailable", 503) from None
        return result

    def validate_selection(self, items):
        """只接受 1..20 个唯一公告的固定版本；全部核验成功才交付最小快照。"""
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise CatalogError("invalid_selection", 400)
        seen, versions = set(), set()
        for item in items:
            if (not isinstance(item, dict) or set(item) != {"notice_id", "observation_id"}
                    or any(not isinstance(item[k], str) or not _HEX.fullmatch(item[k]) for k in item)
                    or item["notice_id"] in seen or item["observation_id"] in versions):
                raise CatalogError("invalid_selection", 400)
            seen.add(item["notice_id"])
            versions.add(item["observation_id"])
        snapshots = []
        for item in items:
            try:
                current = self.detail(item["notice_id"])["current"]
            except CatalogError as exc:
                if exc.code == "notice_not_found":
                    raise CatalogError("catalog_version_changed", 409) from None
                raise
            if current["observation_id"] != item["observation_id"]:
                raise CatalogError("catalog_version_changed", 409)
            snapshots.append({k: current[k] for k in ("notice_id", "observation_id", "title", "source_url", "simulation")})
        return snapshots

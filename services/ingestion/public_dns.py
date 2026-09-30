"""固定公开采购域名的显式 DoH；只解决本机 DNS 兼容，不改变访问权限。

解析结果可含有界 CNAME 链；只接受由问题域名连通到的公网 A 记录。连接仍由
transport 固定数字 IP 并按原网站域名校验证书，不能由网页指定解析器或目标。
"""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlencode

from services.ingestion.transport import FetchError, _exchange, _public_unicast

HOSTS = frozenset({"www.ccgp.gov.cn", "ccgp.gov.cn", "search.ccgp.gov.cn", "ggzy.hainan.gov.cn"})


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_dns_key")
        result[key] = value
    return result


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?", value):
        raise ValueError("invalid_dns_name")
    if len(value) > 254:
        raise ValueError("invalid_dns_name")
    return value.lower().rstrip(".")


def resolve_public_dns(host, port, timeout=10, *, exchange=None):
    """一次有界查询，不缓存、不跳转、不自动切换；失败返回固定错误码。"""
    if host not in HOSTS or port not in (80, 443):
        raise FetchError("dns_target_not_allowed")
    url = "https://dns.google/resolve?" + urlencode(
        {"name": host, "type": "A", "edns_client_subnet": "0.0.0.0/0"})
    try:
        status, headers, body = (exchange or _exchange)(url, "8.8.8.8", min(timeout, 10), 65536)
    except Exception:
        raise FetchError("doh_transport_error") from None
    if (status != 200 or len(body) > 65536 or
            headers.get("content-type", "").split(";", 1)[0].strip().lower() not in
            ("application/json", "application/x-javascript")):
        raise FetchError("doh_response_rejected")
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        if (not isinstance(payload, dict) or type(payload.get("Status")) is not int
                or payload["Status"] != 0 or payload.get("TC") is not False or payload.get("CD") is not False):
            raise ValueError()
        question, answers = payload.get("Question"), payload.get("Answer")
        if (not isinstance(question, list) or len(question) != 1 or
                _name(question[0]["name"]) != host or type(question[0]["type"]) is not int or
                question[0]["type"] != 1 or not isinstance(answers, list) or not 1 <= len(answers) <= 24):
            raise ValueError()
        records = {}
        for record in answers:
            if (not isinstance(record, dict) or type(record.get("type")) is not int or record["type"] not in (1, 5)
                    or type(record.get("TTL")) is not int or record["TTL"] <= 0
                    or not isinstance(record.get("data"), str)):
                raise ValueError()
            owner = _name(record["name"])
            if record["type"] == 1:
                address = ipaddress.ip_address(record["data"])
                if address.version != 4 or not _public_unicast(address):
                    raise ValueError()
                value = str(address)
            else:
                value = _name(record["data"])
            records.setdefault(owner, []).append((record["type"], value))
        # 不接受额外不连通记录、循环、CNAME/A混搭或多个别名目标。
        current, visited = host, set()
        for _ in range(9):
            if current in visited or current not in records:
                raise ValueError()
            visited.add(current)
            rows = records[current]
            if all(kind == 1 for kind, _ in rows):
                if visited != set(records):
                    raise ValueError()
                return sorted({value for _, value in rows})
            if len(rows) != 1 or rows[0][0] != 5:
                raise ValueError()
            current = rows[0][1]
        raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise FetchError("doh_response_rejected") from None

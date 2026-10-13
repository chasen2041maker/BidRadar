"""通过 DNS-over-HTTPS（Google 公共 DNS）解析采购网站的域名。

为什么需要：有些本机网络环境的 DNS 会返回假地址（比如某些代理软件的 fake-IP），导致连不上。
这只是换一种方式查 IP，不改变任何访问权限。

只解析固定的几个采购网站域名；允许有限长度的 CNAME 链，最终只接受公网 IPv4 地址。
连接时仍按原域名校验 HTTPS 证书。
"""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import urlencode

from services.ingestion.transport import FetchError, _exchange, _public_unicast

HOSTS = frozenset({"www.ccgp.gov.cn", "ccgp.gov.cn", "search.ccgp.gov.cn", "ggzy.hainan.gov.cn"})


def _unique(pairs):
    """json.loads 的钩子：遇到重复键就报错。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_dns_key")
        result[key] = value
    return result


def _name(value):
    """检查并规范化 DNS 名称（转小写、去掉结尾的点）。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\.?", value):
        raise ValueError("invalid_dns_name")
    if len(value) > 254:
        raise ValueError("invalid_dns_name")
    return value.lower().rstrip(".")


def resolve_public_dns(host, port, timeout=10, *, exchange=None):
    """查询一个采购网站域名的 IPv4 地址列表。

    只查一次，不缓存，不自动换别的 DNS；失败抛 FetchError（固定错误码）。
    """
    if host not in HOSTS or port not in (80, 443):
        raise FetchError("dns_target_not_allowed")
    url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A", "edns_client_subnet": "0.0.0.0/0"})
    try:
        status, headers, body = (exchange or _exchange)(url, "8.8.8.8", min(timeout, 10), 65536)
    except Exception:
        raise FetchError("doh_transport_error") from None
    if (
        status != 200
        or len(body) > 65536
        or headers.get("content-type", "").split(";", 1)[0].strip().lower()
        not in ("application/json", "application/x-javascript")
    ):
        raise FetchError("doh_response_rejected")
    try:
        payload = json.loads(body.decode("utf-8"), object_pairs_hook=_unique)
        if (
            not isinstance(payload, dict)
            or type(payload.get("Status")) is not int
            or payload["Status"] != 0
            or payload.get("TC") is not False
            or payload.get("CD") is not False
        ):
            raise ValueError()
        question, answers = payload.get("Question"), payload.get("Answer")
        if (
            not isinstance(question, list)
            or len(question) != 1
            or _name(question[0]["name"]) != host
            or type(question[0]["type"]) is not int
            or question[0]["type"] != 1
            or not isinstance(answers, list)
            or not 1 <= len(answers) <= 24
        ):
            raise ValueError()
        records = {}
        for record in answers:
            if (
                not isinstance(record, dict)
                or type(record.get("type")) is not int
                or record["type"] not in (1, 5)
                or type(record.get("TTL")) is not int
                or record["TTL"] <= 0
                or not isinstance(record.get("data"), str)
            ):
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
        # 沿着 CNAME 链走到 A 记录。出现循环、无关记录、CNAME 和 A 混在一起、或指向多个别名，都拒绝。
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

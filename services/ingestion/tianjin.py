"""天津政府采购开放接口的采集适配器。

几个要点：
    - 只访问三个固定的官方资源（谈判、磋商、更正），默认不联网，必须显式开启。
    - 开发者令牌从本地文件读取，只在发请求时短暂放在内存里，不写进日志、URL 记录或错误信息。
    - 接口参数里 page 是每页条数，pageNum 才是页码。接口没有关键词和排序参数，
      所以只能取指定的几页，软件相关的筛选放在目录端做。这不是全量数据。
    - 目前令牌仍被平台拒绝，响应格式还没用真实账号验证过。
"""

from __future__ import annotations

from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlencode, unquote

from services.ingestion.archive import StoreError, utc_now, encode_json
from services.ingestion.transport import FetchError, FetchResult, _exchange, _resolve, _public_unicast

SOURCE_ID = "tj_procurement_negotiation"
RESOURCE_ID = "addbe9f2805346c28d4b639311e6e68a"
# 允许访问的官方资源。接口地址是写死的，不会因为响应内容而访问别的地址。
RESOURCE_KEYS = {
    "negotiation": SOURCE_ID,
    "consultation": "tj_procurement_consultation",
    "correction": "tj_procurement_correction",
}
SOURCE_RESOURCES = {
    SOURCE_ID: RESOURCE_ID,
    "tj_procurement_consultation": "cafe018abce346a7a34682a33fb6c4c2",
    "tj_procurement_correction": "67393436a7cd44f5a896b3e98153f7a7",
}
MATERIAL_FIELDS = {
    SOURCE_ID: (),
    "tj_procurement_consultation": ("其他附件文件下载链接",),
    "tj_procurement_correction": ("更正附件链接",),
}
HOST = "open.data.tj.gov.cn"
ENDPOINT = f"https://{HOST}/api/invoke/{RESOURCE_ID}"
RESOURCE_URL = f"https://{HOST}/sjjk/{RESOURCE_ID}.htm"
PARSER_VERSION = "tj-open-data-v2"
ATTRIBUTION = "天津市信息资源统一开放平台"
DEFAULT_TOKEN_FILE = Path.home() / ".bidradar" / "credentials" / "tianjin-token.txt"
MAX_BYTES = 4 * 1024 * 1024


def _unique_object(pairs):
    """json.loads 的钩子：遇到重复键就报错。

    否则前一个值会被后一个覆盖，如果前一个值里藏着令牌，就检查不到了。
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def resolve_public_dns(host, port, timeout, *, exchange=None):
    """用 DNS-over-HTTPS 解析天津接口的域名（应对本机 DNS 返回假地址的情况）。

    只接受直接的 A 记录，不跟踪 CNAME，不缓存。这个请求里绝不会带上 API 令牌。
    """
    if host != HOST or port != 443:
        raise FetchError("dns_target_not_allowed")
    url = "https://dns.google/resolve?" + urlencode({"name": HOST, "type": "A", "edns_client_subnet": "0.0.0.0/0"})
    try:
        status, headers, body = (exchange or _exchange)(url, "8.8.8.8", min(timeout, 10), 65536)
    except Exception:
        raise FetchError("doh_transport_error") from None
    if status != 200 or len(body) > 65536:
        raise FetchError("doh_response_rejected")
    if headers.get("content-type", "").split(";", 1)[0].strip().lower() not in (
        "application/json",
        "application/x-javascript",
    ):
        raise FetchError("doh_response_rejected")
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or type(value.get("Status")) is not int
            or value["Status"] != 0
            or value.get("TC") is not False
            or value.get("CD") is not False
        ):
            raise ValueError()
        questions, answers = value.get("Question"), value.get("Answer")
        if (
            not isinstance(questions, list)
            or len(questions) != 1
            or not isinstance(questions[0], dict)
            or questions[0].get("name", "").lower() != HOST + "."
            or type(questions[0].get("type")) is not int
            or questions[0]["type"] != 1
            or not isinstance(answers, list)
            or not 1 <= len(answers) <= 16
        ):
            raise ValueError()
        addresses = []
        for answer in answers:
            if (
                not isinstance(answer, dict)
                or answer.get("name", "").lower() != HOST + "."
                or type(answer.get("type")) is not int
                or answer["type"] != 1
                or type(answer.get("TTL")) is not int
                or answer["TTL"] <= 0
                or not isinstance(answer.get("data"), str)
            ):
                raise ValueError()
            address = ipaddress.ip_address(answer["data"])
            if address.version != 4 or not _public_unicast(address):
                raise ValueError()
            addresses.append(str(address))
        return addresses
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        raise FetchError("doh_response_rejected") from None


def request_spec(*, start_page=1, pages=1, page_size=10, dns_mode="system", source_id=SOURCE_ID):
    """生成天津接口的采集请求：哪个资源、从第几页开始、取几页、每页几条。"""
    for name, value, maximum in (("start_page", start_page, 10000), ("pages", pages, 5), ("page_size", page_size, 20)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"invalid_{name}")
    if start_page + pages - 1 > 10000:
        raise ValueError("page_range_exceeded")
    if dns_mode not in ("system", "google-doh"):
        raise ValueError("invalid_dns_mode")
    if not isinstance(source_id, str) or source_id not in SOURCE_RESOURCES:
        raise ValueError("invalid_tianjin_source")
    request = {
        "schema_version": 1,
        "source_id": source_id,
        "start_page": start_page,
        "pages": pages,
        "page_size": page_size,
        "simulation": False,
    }
    # 用默认 DNS 时不写入 dns_mode，保持和旧版本请求的幂等内容一致；用 DoH 时才写入。
    if dns_mode != "system":
        request["dns_mode"] = dns_mode
    return request


def page_url(page_number, page_size, source_id=SOURCE_ID):
    """生成某一页的 URL（不含令牌）。令牌在真正发请求时才加上，所以不会出现在任何记录里。"""
    request_spec(start_page=page_number, page_size=page_size, source_id=source_id)
    endpoint = f"https://{HOST}/api/invoke/{SOURCE_RESOURCES[source_id]}"
    return endpoint + "?" + urlencode({"page": page_size, "pageNum": page_number})


class TianjinTransport:
    """天津接口的抓取器。

    2026-09-30 核对过开放和免费使用条款，目前只用于本地开发。访问依据是接口自己的调用说明，
    不是网站的 robots.txt。需要自己注册开发者账号。
    默认不联网；不跟随重定向、不走代理，认证失败不会自动重试。
    """

    def __init__(
        self,
        *,
        allow_network=False,
        token_file=DEFAULT_TOKEN_FILE,
        exchange=None,
        resolver=None,
        dns_mode="system",
        source_id=SOURCE_ID,
    ):
        if dns_mode not in ("system", "google-doh"):
            raise ValueError("invalid_dns_mode")
        request_spec(source_id=source_id)
        self.source_id = source_id
        self.allow_network = allow_network
        self.token_file = Path(token_file)
        self.exchange = exchange or _exchange
        self.resolver = resolver or (resolve_public_dns if dns_mode == "google-doh" else _resolve)
        self.calls, self.started = 0, time.monotonic()
        self.last_request = None

    def fetch_page(self, page_number, page_size, *, checkpoint):
        """抓取一页数据，返回 FetchResult。"""
        request_spec(start_page=page_number, page_size=page_size)
        checkpoint()
        if not self.allow_network:
            raise FetchError("network_opt_in_required")
        # 条款核对有有效期，过期了就要重新核对，不能拿旧的核对结果继续访问。
        if not "2026-09-30" <= utc_now()[:10] < "2026-10-30":
            raise FetchError("source_terms_review_required")
        if self.calls >= 5 or time.monotonic() - self.started > 180:
            raise FetchError("request_budget_exceeded")
        try:
            with self.token_file.open("rb") as stream:
                raw = stream.read(4097)
            token = raw.decode("utf-8-sig").strip()
            if not 16 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
                raise ValueError()
        except (OSError, UnicodeError, ValueError):
            raise FetchError("credential_missing_or_invalid") from None
        # 解析出的地址都必须是公网地址；连接时用这个 IP，证书仍按官方域名校验。
        addresses = self.resolver(HOST, 443, 10)
        try:
            if not addresses or any(not _public_unicast(ipaddress.ip_address(ip)) for ip in addresses):
                raise ValueError()
        except ValueError:
            raise FetchError("dns_not_public") from None
        checkpoint()
        if self.last_request is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        checkpoint()
        request_started = time.monotonic()
        if request_started - self.started > 180:
            raise FetchError("request_budget_exceeded")
        safe_url = page_url(page_number, page_size, self.source_id)
        self.calls += 1
        self.last_request = request_started
        try:
            status, headers, body = self.exchange(
                safe_url + "&" + urlencode({"authToken": token}), addresses[0], 20, MAX_BYTES
            )
        except FetchError:
            # 异常信息里可能带着完整 URL（含令牌），所以只返回固定的错误码。
            raise FetchError("api_transport_error", attempts=1) from None
        except Exception:
            raise FetchError("api_transport_error", attempts=1) from None
        if status != 200:
            raise FetchError("api_access_denied" if status in (401, 403, 429) else "api_http_error", status, 1)
        if len(body) > MAX_BYTES:
            raise FetchError("body_too_large", status, 1)
        media = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media not in ("application/json", "text/json"):
            raise FetchError("api_not_json", status, 1)
        # 错误页或服务器回显里可能带着令牌。只要发现令牌，就宁可丢掉这个响应，也不把它存进原件库。
        forbidden = (
            token.encode(),
            urlencode({"authToken": token}).split("=", 1)[1].encode(),
            json.dumps(token)[1:-1].encode(),
        )
        if any(secret in body for secret in forbidden) or b'"authToken"' in body:
            raise FetchError("credential_echo_rejected", status, 1)
        try:
            decoded = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_unique_object)
        except (UnicodeError, ValueError, RecursionError):
            raise FetchError("api_invalid_json", status, 1) from None
        # 令牌可能被 JSON 的 \u 转义藏起来，直接搜字节找不到，所以要解码后再逐个字符串检查一遍。
        pending = [decoded]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                if any(key.lower() == "authtoken" for key in value):
                    raise FetchError("credential_echo_rejected", status, 1)
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str) and (token in value or token in unquote(value)):
                raise FetchError("credential_echo_rejected", status, 1)
        return FetchResult(safe_url, safe_url, status, {"content-type": media}, body, utc_now(), 1)


def parse_page(body: bytes, page_size: int) -> dict:
    """解析接口返回的一页数据。

    期望的格式是 columnNames（列名）加 list（每行的值），每行列数必须一致。
    零条数据、接口业务拒绝、预览截断、格式看不懂，会分别返回不同的状态。
    这个格式来自公开的预览页面；真实接口如果不一样，就保留原件、报错，不去猜字段。
    接口返回的 msg 不写进日志，里面可能有个人信息。
    """
    try:
        value = json.loads(body.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError, RecursionError):
        return {"status": "parse_error", "issues": ["invalid_json"]}
    if not isinstance(value, dict) or type(value.get("code")) is not int:
        return {"status": "parse_error", "issues": ["api_schema_unverified"]}
    if value["code"] != 200:
        # 只识别实测过的错误类型。不认识的 500 不会被猜成"令牌失效"。
        issue = (
            "api_credential_rejected"
            if value["code"] == 500 and value.get("msg") == "令牌不合法【失效或被禁用】"
            else "api_business_error"
        )
        return {"status": "blocked", "issues": [issue]}
    columns, rows, total = value.get("columnNames"), value.get("list"), value.get("totalCount")
    if (
        not isinstance(columns, list)
        or not 1 <= len(columns) <= 100
        or any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in columns)
        or len(set(columns)) != len(columns)
        or not isinstance(rows, list)
        or len(rows) > page_size
        or type(total) is not int
        or total < len(rows)
        or "previewCount" in value
    ):
        return {"status": "parse_error", "issues": ["api_schema_or_preview_rejected"]}
    result = []
    for index, row in enumerate(rows):
        if (
            not isinstance(row, list)
            or len(row) != len(columns)
            or any(item is not None and not isinstance(item, str) for item in row)
            or any(isinstance(item, str) and (len(item) > 200000 or "该字段不支持预览" in item) for item in row)
        ):
            return {"status": "parse_error", "issues": ["incomplete_or_invalid_row"]}
        fields = dict(zip(columns, row))
        # 接口没有唯一的记录编号，只能用内容哈希标识这条记录的这个版本；内容一变，ID 也会变。
        try:
            digest = sha256(encode_json(fields).encode()).hexdigest()
        except UnicodeError:
            return {"status": "parse_error", "issues": ["invalid_unicode_row"]}
        result.append({"fields": fields, "row_index": index, "record_sha256": digest})
    return {
        "status": "ok" if rows else "empty",
        "rows": result,
        "total_count": total,
        "issues": [],
        "attribution": ATTRIBUTION,
    }


def execute(store, run_id, transport):
    """执行一次天津接口采集。

    每一页的原件和下一页任务在同一个事务里登记，中断后可以接着跑；失败或取消的运行要重新发起。
    接口没有快照机制，翻页期间数据可能移动（比如有新记录插到前面），所以不保证不重不漏。
    """
    run = store.run(run_id)
    source_id = run["request"].get("source_id")
    if not isinstance(source_id, str) or source_id not in SOURCE_RESOURCES:
        raise StoreError("wrong_source_executor")
    if run["status"] in ("succeeded", "partial", "failed", "blocked", "cancelled"):
        return store.report(run_id)
    # 同一个网站的不同资源（谈判、更正）要分开，不能把谈判的数据记到更正的运行里。
    if getattr(transport, "source_id", None) != source_id:
        raise StoreError("transport_source_mismatch")
    token = store.acquire(run_id)
    req = run["request"]

    def task(page):
        return {"kind": "api_page", "url": page_url(page, req["page_size"], source_id), "payload": {"page": page}}

    try:
        store.enqueue(run_id, token, [task(req["start_page"])])
        while item := store.next_task(run_id):
            checkpoint = lambda: store.heartbeat(run_id, token)
            try:
                response = transport.fetch_page(item["payload"]["page"], req["page_size"], checkpoint=checkpoint)
            except FetchError as exc:
                store.record(run_id, token, item, error=exc.code, http_status=exc.http_status, attempts=exc.attempts)
                store.finish(run_id, token, "blocked", exc.code)
                return store.report(run_id)
            checkpoint()
            parsed = parse_page(store.read_blob(store.put_blob(response.body)), req["page_size"])
            page = item["payload"]["page"]
            if (
                parsed["status"] in ("ok", "empty")
                and len(parsed["rows"]) < req["page_size"]
                and (page - 1) * req["page_size"] + len(parsed["rows"]) < parsed["total_count"]
            ):
                # 总数说还有记录，这一页却是空的或不满：这是异常，不能当"已经取完"，也不往后多抓来掩盖。
                parsed.update(status="parse_error", issues=["inconsistent_pagination"])
            more = parsed["status"] == "ok" and page * req["page_size"] < parsed["total_count"]
            within_limit = page + 1 < req["start_page"] + req["pages"]
            parsed["page_number"] = page
            parsed["more_outside_run_limit"] = more and not within_limit
            error = parsed["issues"][0] if parsed["status"] in ("parse_error", "blocked") else None
            store.record(
                run_id,
                token,
                item,
                response=response,
                parsed=parsed,
                parser_version=PARSER_VERSION,
                error=error,
                following=[task(page + 1)] if more and within_limit else [],
            )
            if error:
                store.finish(run_id, token, "blocked" if parsed["status"] == "blocked" else "failed", error)
                return store.report(run_id)
        store.finish(run_id, token, "succeeded")
        return store.report(run_id)
    except BaseException:
        store.release(run_id, token)
        if store.run(run_id)["status"] == "cancelled":
            return store.report(run_id)
        raise

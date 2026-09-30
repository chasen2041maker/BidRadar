"""天津官方开放接口：固定资源、显式联网、令牌只在请求内存中短暂使用。

page是每页条数，pageNum才是页码；官方接口没有已确认的关键词/排序参数。
仅采样指定的有限页，软件关键词在目录端筛选，不能声称全量或最新覆盖。
响应结构尚须真实账号验收；预览的截断行绝不补空后冒充完整数据。
"""
from __future__ import annotations

from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import time
from urllib.parse import urlencode

from services.ingestion.archive import StoreError, utc_now, encode_json
from services.ingestion.transport import FetchError, FetchResult, _exchange, _resolve, _public_unicast

SOURCE_ID = "tj_procurement_negotiation"
RESOURCE_ID = "addbe9f2805346c28d4b639311e6e68a"
HOST = "open.data.tj.gov.cn"
ENDPOINT = f"https://{HOST}/api/invoke/{RESOURCE_ID}"
RESOURCE_URL = f"https://{HOST}/sjjk/{RESOURCE_ID}.htm"
PARSER_VERSION = "tj-open-data-v1"
ATTRIBUTION = "天津市信息资源统一开放平台"
DEFAULT_TOKEN_FILE = Path.home() / ".bidradar" / "credentials" / "tianjin-token.txt"
MAX_BYTES = 4 * 1024 * 1024


def request_spec(*, start_page=1, pages=1, page_size=10):
    for name, value, maximum in (("start_page", start_page, 10000), ("pages", pages, 5),
                                  ("page_size", page_size, 20)):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"invalid_{name}")
    if start_page + pages - 1 > 10000:
        raise ValueError("page_range_exceeded")
    return {"schema_version": 1, "source_id": SOURCE_ID, "start_page": start_page,
            "pages": pages, "page_size": page_size, "simulation": False}


def page_url(page_number, page_size):
    """账本只存不含令牌的定位；实际认证参数不能进入URL日志、幂等键或异常。"""
    return ENDPOINT + "?" + urlencode({"page": page_size, "pageNum": page_number})


class TianjinTransport:
    """2026-09-30已核对无条件开放/免费使用条款，当前用途限本地开发。

    固定资源API的明确调用契约为访问依据，不把通用网页robots许可冒充API授权。
    用户仍须自行注册开发者。联网默认关闭；不跳转、不代理、不自动重试认证失败。
    """
    def __init__(self, *, allow_network=False, token_file=DEFAULT_TOKEN_FILE,
                 exchange=None, resolver=None):
        self.allow_network = allow_network
        self.token_file = Path(token_file)
        self.exchange, self.resolver = exchange or _exchange, resolver or _resolve
        self.calls, self.started = 0, time.monotonic()
        self.last_request = None

    def fetch_page(self, page_number, page_size, *, checkpoint):
        request_spec(start_page=page_number, page_size=page_size)
        checkpoint()
        if not self.allow_network:
            raise FetchError("network_opt_in_required")
        # 有效期只作为重新核查提醒；过期不能凭旧条款和旧令牌继续自动访问。
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
        # 所有DNS地址都必须为公网；TLS仍校验官方域名，连接固定已验证IP。
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
        safe_url = page_url(page_number, page_size)
        self.calls += 1
        self.last_request = time.monotonic()
        try:
            status, headers, body = self.exchange(safe_url + "&" + urlencode({"authToken": token}),
                                                   addresses[0], 20, MAX_BYTES)
        except FetchError:
            # 不传播可注入URL的异常消息；固定本地错误码，调用者不得打印认证请求。
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
        # 错误页或服务端回显可能包含凭据。宁可拒绝保留此响应，也不污染原件库。
        forbidden = (token.encode(), urlencode({"authToken": token}).split("=", 1)[1].encode(),
                     json.dumps(token)[1:-1].encode())
        if any(secret in body for secret in forbidden) or b'"authToken"' in body:
            raise FetchError("credential_echo_rejected", status, 1)
        return FetchResult(safe_url, safe_url, status, {"content-type": media}, body, utc_now(), 1)


def parse_page(body: bytes, page_size: int) -> dict:
    """只接受完整等宽表；零行、业务拒绝、预览截断和未知契约明确区分。

    columnNames/list形状来自公开预览。真实API若形状不同，保留原件、拒绝猜字段，
    待核实真实契约后升级解析版本。msg不进入日志，避免错误消息夹带私人数据。
    """
    try:
        value = json.loads(body.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        return {"status": "parse_error", "issues": ["invalid_json"]}
    if not isinstance(value, dict) or type(value.get("code")) is not int:
        return {"status": "parse_error", "issues": ["api_schema_unverified"]}
    if value["code"] != 200:
        return {"status": "blocked", "issues": ["api_business_error"]}
    columns, rows, total = value.get("columnNames"), value.get("list"), value.get("totalCount")
    if (not isinstance(columns, list) or not 1 <= len(columns) <= 100
            or any(not isinstance(c, str) or not c.strip() or len(c) > 100 for c in columns)
            or len(set(columns)) != len(columns) or not isinstance(rows, list)
            or len(rows) > page_size or type(total) is not int or total < len(rows)
            or "previewCount" in value):
        return {"status": "parse_error", "issues": ["api_schema_or_preview_rejected"]}
    result = []
    for index, row in enumerate(rows):
        if (not isinstance(row, list) or len(row) != len(columns)
                or any(item is not None and not isinstance(item, str) for item in row)
                or any(isinstance(item, str) and (len(item) > 200000 or "该字段不支持预览" in item) for item in row)):
            return {"status": "parse_error", "issues": ["incomplete_or_invalid_row"]}
        fields = dict(zip(columns, row))
        # 没有官方唯一记录号：内容哈希只标识一个快照，不伪造跨变更稳定的公告ID。
        digest = sha256(encode_json(fields).encode()).hexdigest()
        result.append({"fields": fields, "row_index": index, "record_sha256": digest})
    return {"status": "ok" if rows else "empty", "rows": result, "total_count": total,
            "issues": [], "attribution": ATTRIBUTION}


def execute(store, run_id, transport):
    """每页原件和后继页同事务登记；中断可接管，失败/取消必须显式新任务。

    单次取样的页顺序由来源决定，没有快照令牌时不承诺翻页期间数据不移动。
    """
    run = store.run(run_id)
    if run["request"].get("source_id") != SOURCE_ID:
        raise StoreError("wrong_source_executor")
    if run["status"] in ("succeeded", "partial", "failed", "blocked", "cancelled"):
        return store.report(run_id)
    token = store.acquire(run_id)
    req = run["request"]
    def task(page):
        return {"kind": "api_page", "url": page_url(page, req["page_size"]), "payload": {"page": page}}
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
            if (parsed["status"] in ("ok", "empty") and len(parsed["rows"]) < req["page_size"]
                    and (page - 1) * req["page_size"] + len(parsed["rows"]) < parsed["total_count"]):
                # “总数还有记录但本页提前空/短”不是成功零结果；不盲目向后补抓掩盖问题。
                parsed.update(status="parse_error", issues=["inconsistent_pagination"])
            more = (parsed["status"] == "ok" and page * req["page_size"] < parsed["total_count"])
            within_limit = page + 1 < req["start_page"] + req["pages"]
            parsed["page_number"] = page
            parsed["more_outside_run_limit"] = more and not within_limit
            error = parsed["status"] if parsed["status"] in ("parse_error", "blocked") else None
            store.record(run_id, token, item, response=response, parsed=parsed,
                         parser_version=PARSER_VERSION, error=error,
                         following=[task(page + 1)] if more and within_limit else [])
            if error:
                store.finish(run_id, token, "blocked" if error == "blocked" else "failed", parsed["issues"][0])
                return store.report(run_id)
        store.finish(run_id, token, "succeeded")
        return store.report(run_id)
    except BaseException:
        store.release(run_id, token)
        if store.run(run_id)["status"] == "cancelled":
            return store.report(run_id)
        raise

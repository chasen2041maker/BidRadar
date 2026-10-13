"""采集流程：登记运行 → 按队列抓取 → 保存原始文件 → 解析 → 把解析出的新链接加入队列 → 结束。

    列表页 → 解析出公告链接 → 公告页 → 解析出附件链接 → 附件
每一步的数量都有上限（页数、公告数、附件数），不会无限扩展。

只处理公开资料，不调模型，也不对项目做任何判断。
能不能真的联网抓取，取决于来源准入文件和网站的 robots.txt；离线测试通过不代表来源已获准。
"""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from services.ingestion.archive import Store, StoreError
from services.ingestion.sources.ccgp import (
    MAX_HTML_BYTES,
    SEARCH_URL,
    _notice_url,
    build_search_params,
    parse_notice_page,
    parse_search_page,
)
from services.ingestion.transport import FetchError

PARSER_VERSION = "public-acquisition-v3"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
BLOCKING_ERRORS = frozenset(
    {
        "blocked",
        "access_blocked",
        "policy_required",
        "policy_invalid",
        "policy_expired_or_not_yet_valid",
        "attachment_not_approved",
        "url_not_allowed",
        "dns_not_public",
        "robots_denied",
        "robots_unavailable",
        "robots_invalid",
        "redirect_not_allowed",
        "request_budget_exceeded",
        "byte_budget_exceeded",
        "time_budget_exceeded",
        "retry_after_out_of_bounds",
        "network_opt_in_required",
        "dns_target_not_allowed",
        "doh_response_rejected",
        "doh_transport_error",
    }
)


def _canonical_notice_url(url: str) -> str:
    """把公告 URL 规范化：主机名和协议转小写、去掉 # 后面的片段。

    同一条公告在列表里可能出现多个写法，规范化后再去重，免得重复的链接占掉公告数量上限。
    HTTP 和 HTTPS、不同的查询参数仍视为不同的地址，不合并。
    """
    accepted = _notice_url(url) if isinstance(url, str) else None
    if accepted is None:
        raise ValueError("公告须为CCGP已登记路径")
    parts = urlsplit(accepted)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def request_spec(
    *,
    keyword: str | None = None,
    notice_urls=(),
    pages: int = 1,
    max_notices: int = 10,
    max_attachments: int = 5,
    attachments: bool = False,
    start_date=None,
    end_date=None,
) -> dict:
    """生成 CCGP 搜索采集的请求：按关键词搜索，或者直接给出公告 URL，二选一。

    所有参数都会参与幂等比较：同一个幂等键配不同参数会报冲突。
    """
    for name, value, limit in (
        ("pages", pages, 5),
        ("max_notices", max_notices, 20),
        ("max_attachments", max_attachments, 10),
    ):
        if type(value) is not int or not 1 <= value <= limit:
            raise ValueError(f"{name}须为1到{limit}的整数")
    if type(attachments) is not bool or bool(keyword) == bool(notice_urls):
        raise ValueError("指定关键词或公告链接之一")
    if not isinstance(notice_urls, (list, tuple)) or len(notice_urls) > max_notices:
        raise ValueError("公告数量超出本次上限")
    urls = []
    for url in notice_urls:
        canonical = _canonical_notice_url(url)
        if canonical not in urls:
            urls.append(canonical)
    if keyword is not None:
        build_search_params(keyword, start_date=start_date, end_date=end_date)
        keyword = keyword.strip()
    elif start_date is not None or end_date is not None or pages != 1:
        raise ValueError("明确公告入口不接受搜索日期/分页参数")
    return {
        "schema_version": 1,
        "source_id": "cn_ccgp",
        "keyword": keyword,
        "notice_urls": urls,
        "pages": pages,
        "max_notices": max_notices,
        "max_attachments": max_attachments,
        "attachments": attachments,
        "start_date": start_date,
        "end_date": end_date,
    }


def public_request_spec(
    *,
    source_id="cn_ccgp",
    category=None,
    notice_urls=(),
    start_page=1,
    pages=1,
    max_notices=10,
    max_attachments=5,
    attachments=False,
    title_terms=(),
    dns_mode="system",
):
    """生成按栏目采集的请求（CCGP 或海南）：指定栏目和页码范围，或者直接给出公告 URL。

    title_terms 是标题筛选词：只抓标题里含这些词的公告。它只是缩小抓取范围，
    不代表"技术相关"之类的判断，也不是全网搜索。
    DNS 模式和筛选词也参与幂等比较，恢复运行时不能悄悄换来源、扩大页数或改网络模式。
    """
    from services.ingestion.sources.public_notices import build_listing_url, canonical_notice_url

    if source_id not in ("cn_ccgp", "cn_hainan") or dns_mode not in ("system", "google-doh"):
        raise ValueError("invalid_public_source_or_dns")
    for name, value, maximum in (
        ("start_page", start_page, 10000),
        ("pages", pages, 5),
        ("max_notices", max_notices, 20),
        ("max_attachments", max_attachments, 10),
    ):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("invalid_" + name)
    if start_page + pages - 1 > 10000:
        raise ValueError("page_range_exceeded")
    if (
        type(attachments) is not bool
        or bool(category) == bool(notice_urls)
        or not isinstance(notice_urls, (tuple, list))
        or len(notice_urls) > max_notices
        or not isinstance(title_terms, (tuple, list))
        or len(title_terms) > 20
        or any(not isinstance(t, str) or not t.strip() or len(t) > 100 for t in title_terms)
    ):
        raise ValueError("invalid_public_request")
    urls = []
    for value in notice_urls:
        url = canonical_notice_url(source_id, value)
        if url is None:
            raise ValueError("notice_url_outside_source")
        if url not in urls:
            urls.append(url)
    if category:
        for page in range(start_page, start_page + pages):
            build_listing_url(source_id, category, page)
    elif start_page != 1 or pages != 1 or title_terms:
        raise ValueError("explicit_notices_reject_listing_options")
    return {
        "schema_version": 2,
        "discovery": "public_category",
        "source_id": source_id,
        "category": category,
        "notice_urls": urls,
        "start_page": start_page,
        "pages": pages,
        "max_notices": max_notices,
        "max_attachments": max_attachments,
        "attachments": attachments,
        "title_terms": list(dict.fromkeys(t.strip() for t in title_terms)),
        "dns_mode": dns_mode,
    }


def decode_html(body: bytes, headers: dict[str, str]) -> tuple[str, str]:
    """按 BOM 或页面声明的编码解码 HTML，返回 (文本, 编码)。

    解码失败就报错，不用替换字符硬解：乱码不能当作原文证据。
    """
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if content_type and content_type.split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
        raise ValueError("unexpected_html_content_type")
    if body.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
        if not match:
            match = re.search(
                r"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)", body[:4096].decode("ascii", errors="ignore"), re.I
            )
        encoding = match.group(1).lower() if match else "utf-8"
    if encoding not in ("utf-8", "utf8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
        raise ValueError("unsupported_charset")
    try:
        return body.decode(encoding, errors="strict"), encoding
    except UnicodeError as exc:
        raise ValueError("invalid_html_encoding") from exc


def parse_capture(kind: str, body: bytes, headers: dict, url: str, source_id="cn_ccgp") -> dict:
    """按任务类型（列表、公告、附件）解析一份抓取到的内容。

    原始字节的哈希和解码后文本的哈希分开记录，不混为一谈。
    """
    if kind == "attachment":
        media = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        # 网站常把 HTML 错误页伪装成文件下载，这种情况不能记为"已取得附件"。
        if "html" in media.lower() or body.lstrip()[:32].lower().startswith((b"<!doctype html", b"<html")):
            return {"status": "parse_error", "issues": ["attachment_is_html"], "raw_sha256": sha256(body).hexdigest()}
        extension = urlsplit(url).path.lower().rsplit(".", 1)[-1]
        magic_ok = (
            (extension == "pdf" and body.startswith(b"%PDF-"))
            or (extension in ("docx", "xlsx", "zip") and body.startswith(b"PK\x03\x04"))
            or (extension in ("doc", "xls") and body.startswith(bytes.fromhex("d0cf11e0a1b11ae1")))
            or (extension == "rar" and body.startswith(b"Rar!"))
            or (extension == "7z" and body.startswith(b"7z\xbc\xaf\x27\x1c"))
        )
        return {
            "status": "ok" if magic_ok else "parse_error",
            "kind": "attachment",
            "issues": [] if magic_ok else ["attachment_signature_mismatch"],
            "raw_sha256": sha256(body).hexdigest(),
            "text_extracted": False,
        }
    html, encoding = decode_html(body, headers)
    if source_id not in ("cn_ccgp", "cn_hainan"):
        raise ValueError("invalid_public_source")
    if kind not in ("listing", "notice"):
        raise ValueError("invalid_capture_kind")
    from services.ingestion.sources.public_notices import listing_identity

    if source_id == "cn_ccgp" and kind == "listing" and listing_identity(source_id, url) is None:
        parsed = asdict(parse_search_page(html))
    else:
        from services.ingestion.sources.public_notices import parse_listing, parse_notice

        parsed = asdict(
            parse_listing(html, url, source_id) if kind == "listing" else parse_notice(html, url, source_id)
        )
    parsed["status"] = parsed["status"].value
    parsed["decoded_input_sha256"] = parsed.pop("input_sha256")
    parsed["raw_sha256"] = sha256(body).hexdigest()
    parsed["encoding"] = encoding
    if kind == "notice":
        from services.ingestion.sources.public_notices import canonical_notice_url

        canonical = canonical_notice_url(source_id, url)
        if canonical is None:
            raise ValueError("notice_url_outside_source")
        parsed["notice_id"] = sha256((source_id + "\n" + canonical).encode()).hexdigest()
    return parsed


def _initial_tasks(request: dict) -> list[dict]:
    """根据请求生成最初的抓取任务（列表页，或者直接给出的公告页）。"""
    if request.get("discovery") == "public_category":
        from services.ingestion.sources.public_notices import build_listing_url

        if request["notice_urls"]:
            return [{"kind": "notice", "url": url} for url in request["notice_urls"]]
        return [
            {"kind": "listing", "url": build_listing_url(request["source_id"], request["category"], page)}
            for page in range(request["start_page"], request["start_page"] + request["pages"])
        ]
    if request["keyword"] is None:
        return [{"kind": "notice", "url": url} for url in request["notice_urls"]]
    return [
        {
            "kind": "listing",
            "url": SEARCH_URL
            + "?"
            + urlencode(
                build_search_params(
                    request["keyword"], page, start_date=request["start_date"], end_date=request["end_date"]
                )
            ),
        }
        for page in range(1, request["pages"] + 1)
    ]


def _following(store: Store, run_id: str, request: dict, task: dict, parsed: dict, transport) -> list[dict]:
    """根据解析结果决定接下来要抓什么：列表页 → 公告，公告 → 附件。不超过请求里的数量上限。"""
    tasks = []
    if task["kind"] == "listing":
        known = store.task_urls(run_id, "notice")
        skipped = 0
        filtered = 0
        for item in parsed.get("items", ()):
            if request.get("title_terms") and not any(
                term.casefold() in item["title"].casefold() for term in request["title_terms"]
            ):
                filtered += 1
                continue
            if request.get("discovery") == "public_category":
                from services.ingestion.sources.public_notices import canonical_notice_url

                url = canonical_notice_url(request["source_id"], item["url"])
                if url is None:
                    parsed["issues"] = [*parsed.get("issues", ()), "notice_url_outside_source"]
                    parsed["status"] = "partial"
                    continue
            else:
                url = _canonical_notice_url(item["url"])
            if url in known:
                continue
            if len(known) >= request["max_notices"]:
                skipped += 1
                continue
            known.add(url)
            tasks.append({"kind": "notice", "url": url, "parent_url": task["url"], "payload": item})
        parsed["notices_outside_run_limit"] = skipped
        parsed["notices_outside_title_filter"] = filtered
    if task["kind"] == "notice":
        known = store.task_urls(run_id, "attachment")
        for link in parsed.get("attachments", ()):
            if not request["attachments"]:
                link["status"] = "not_requested"
                continue
            if link["url"] in known:
                link["status"] = "already_scheduled"
                continue
            if len(known) >= request["max_attachments"]:
                link["status"] = "run_limit"
                continue
            try:
                transport.policy.authorize(link["url"], "attachment")
            except (FetchError, AttributeError) as exc:
                link["status"] = "not_authorized"
                link["reason"] = getattr(exc, "code", "policy_required")
                continue
            known.add(link["url"])
            link["status"] = "scheduled"
            tasks.append({"kind": "attachment", "url": link["url"], "parent_url": task["url"], "payload": link})
    return tasks


def execute(store: Store, run_id: str, transport) -> dict:
    """执行（或恢复执行）一次采集运行，返回运行汇总。

    已经抓过的页面不会重新联网。遇到访问受限（来源未获准、robots 禁止、被网站拒绝）就停止整个运行，
    不会换着法子去绕。
    """
    run = store.run(run_id)
    if run["status"] in ("succeeded", "partial", "blocked", "failed", "cancelled"):
        return store.report(run_id)
    token = store.acquire(run_id)
    request = run["request"]
    try:
        store.enqueue(run_id, token, _initial_tasks(request))
        while task := store.next_task(run_id):
            store.heartbeat(run_id, token)
            try:
                # 每发一个 HTTP 请求（包括读 robots、重试、重定向）之前，都检查运行有没有被取消、租约是否还在。
                # 已经发出的请求收不回来，但之后不会再发新的。
                response = transport.fetch(
                    task["url"],
                    max_bytes=(MAX_ATTACHMENT_BYTES if task["kind"] == "attachment" else MAX_HTML_BYTES),
                    kind=task["kind"],
                    checkpoint=lambda: store.heartbeat(run_id, token),
                )
            except FetchError as exc:
                store.record(run_id, token, task, error=exc.code, http_status=exc.http_status, attempts=exc.attempts)
                # 访问受限：整个运行停下，剩下的页面也不再尝试。
                if exc.code in BLOCKING_ERRORS:
                    store.finish(run_id, token, "blocked", exc.code)
                    return store.report(run_id)
                continue
            try:
                # 先把原始文件存盘，再解析。这样每个解析结果都能追溯到同一份原始字节。
                # 如果在这里崩溃，恢复时可能重新下载一次，但相同的内容不会重复存储。
                store.heartbeat(run_id, token)
                archived_body = store.read_blob(store.put_blob(response.body))
                parsed = parse_capture(
                    task["kind"],
                    archived_body,
                    response.headers,
                    response.final_url,
                    request.get("source_id", "cn_ccgp"),
                )
            except (ValueError, TypeError, UnicodeError) as exc:
                parsed = {
                    "status": "parse_error",
                    "issues": [str(exc)],
                    "raw_sha256": sha256(response.body).hexdigest(),
                }
            following = _following(store, run_id, request, task, parsed, transport)
            error = parsed["status"] if parsed["status"] in ("parse_error", "blocked") else None
            store.record(
                run_id,
                token,
                task,
                response=response,
                parsed=parsed,
                parser_version=PARSER_VERSION,
                error=error,
                following=following,
            )
            if parsed["status"] == "blocked":
                store.finish(run_id, token, "blocked", "access_challenge")
                return store.report(run_id)
        report = store.report(run_id)
        captures = report["captures"]
        errors = [row for row in captures if row["error_code"]]
        gaps = any(
            row["parsed"]
            and (
                row["parsed"].get("status") == "partial"
                or row["parsed"].get("notices_outside_run_limit", 0)
                or any(
                    link["status"] in ("not_authorized", "run_limit", "not_requested")
                    for link in row["parsed"].get("attachments", ())
                )
            )
            for row in captures
        )
        status = "failed" if errors and len(errors) == len(captures) else ("partial" if errors or gaps else "succeeded")
        store.finish(run_id, token, status)
        return store.report(run_id)
    except KeyboardInterrupt:
        store.release(run_id, token)
        raise
    except StoreError:
        # 被取消或租约失效：保持原状。其他存储错误：释放租约，之后可以用同一个 run_id 恢复。
        store.release(run_id, token)
        if store.run(run_id)["status"] == "cancelled":
            return store.report(run_id)
        raise
    except BaseException:
        store.release(run_id, token)
        raise


def replay(store: Store, capture_id: str) -> dict:
    """用当前解析器重新解析一份已归档的原始文件。不联网，结果另存一条，不覆盖当初的结论。"""
    capture = store.capture(capture_id)
    if not capture["sha256"]:
        raise StoreError("capture_has_no_body")
    if capture["kind"] == "api_page":
        from services.ingestion import tianjin

        req = store.run(capture["run_id"])["request"]
        parsed = tianjin.parse_page(store.read_blob(capture["sha256"]), req["page_size"])
        return store.save_replay(capture_id, tianjin.PARSER_VERSION, parsed)
    source = store.run(capture["run_id"])["request"].get("source_id", "cn_ccgp")
    parsed = parse_capture(
        capture["kind"],
        store.read_blob(capture["sha256"]),
        capture["headers"],
        capture["final_url"] or capture["url"],
        source,
    )
    return store.save_replay(capture_id, PARSER_VERSION, parsed)

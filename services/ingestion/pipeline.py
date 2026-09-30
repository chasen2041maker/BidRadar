"""首源获取流程：持久登记→受控获取→原字节归档→解析→登记后继→本地结果。

模块只处理公开材料；不调用模型，也不把线索当资格结论。真实网络取决于当前
来源准入和robots，离线测试通过不能把来源准入自动置为已批准。
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from services.ingestion.archive import Store, StoreError
from services.ingestion.sources.ccgp import (
    MAX_HTML_BYTES, SEARCH_URL, _notice_url, build_search_params,
    parse_notice_page, parse_search_page,
)
from services.ingestion.transport import FetchError

PARSER_VERSION = "ccgp-acquisition-v2"
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
BLOCKING_ERRORS = frozenset({
    "blocked", "access_blocked", "policy_required", "policy_invalid", "policy_expired_or_not_yet_valid",
    "attachment_not_approved", "url_not_allowed", "dns_not_public", "robots_denied", "robots_unavailable",
    "robots_invalid", "redirect_not_allowed", "request_budget_exceeded", "byte_budget_exceeded",
    "time_budget_exceeded", "retry_after_out_of_bounds",
})


def _canonical_notice_url(url: str) -> str:
    """入口、列表后继和公告身份共用规则；不合并HTTP/HTTPS或不同查询参数。

    主机/协议大小写与片段不改变本次获取对象。列表原始链接仍留在解析结果/payload，
    只用规范链接排队及生成身份，避免同一公告提前耗尽条数上限。
    """
    accepted = _notice_url(url) if isinstance(url, str) else None
    if accepted is None:
        raise ValueError("公告须为CCGP已登记路径")
    parts = urlsplit(accepted)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def request_spec(*, keyword: str | None = None, notice_urls=(), pages: int = 1,
                 max_notices: int = 10, max_attachments: int = 5,
                 attachments: bool = False, start_date=None, end_date=None) -> dict:
    """固定v1请求语义：搜索与明确公告二选一，运行参数进入幂等比较。"""
    for name, value, limit in (("pages", pages, 5), ("max_notices", max_notices, 20),
                                ("max_attachments", max_attachments, 10)):
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
    return {"schema_version": 1, "source_id": "cn_ccgp", "keyword": keyword,
            "notice_urls": urls, "pages": pages, "max_notices": max_notices,
            "max_attachments": max_attachments, "attachments": attachments,
            "start_date": start_date, "end_date": end_date}


def decode_html(body: bytes, headers: dict[str, str]) -> tuple[str, str]:
    """严格按BOM/声明解码；乱码不是有效正文，不静默替换字节再作证据。"""
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    if content_type and content_type.split(";", 1)[0].strip().lower() not in ("text/html", "application/xhtml+xml"):
        raise ValueError("unexpected_html_content_type")
    if body.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        match = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
        if not match:
            match = re.search(r"<meta[^>]+charset\s*=\s*[\"']?([\w-]+)",
                              body[:4096].decode("ascii", errors="ignore"), re.I)
        encoding = match.group(1).lower() if match else "utf-8"
    if encoding not in ("utf-8", "utf8", "utf-8-sig", "gbk", "gb2312", "gb18030"):
        raise ValueError("unsupported_charset")
    try:
        return body.decode(encoding, errors="strict"), encoding
    except UnicodeError as exc:
        raise ValueError("invalid_html_encoding") from exc


def parse_capture(kind: str, body: bytes, headers: dict, url: str) -> dict:
    """原始字节哈希与解析器的UTF-8文本哈希各自命名，不能混成同一版本。"""
    if kind == "attachment":
        media = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        # HTML错误页常伪装成下载；不保存为“已取得PDF/Office文件”。
        if "html" in media.lower() or body.lstrip()[:32].lower().startswith((b"<!doctype html", b"<html")):
            return {"status": "parse_error", "issues": ["attachment_is_html"], "raw_sha256": sha256(body).hexdigest()}
        extension = urlsplit(url).path.lower().rsplit(".", 1)[-1]
        magic_ok = (extension == "pdf" and body.startswith(b"%PDF-")) or (
            extension in ("docx", "xlsx", "zip") and body.startswith(b"PK\x03\x04")) or (
            extension in ("doc", "xls") and body.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))) or (
            extension == "rar" and body.startswith(b"Rar!")) or (
            extension == "7z" and body.startswith(b"7z\xbc\xaf\x27\x1c"))
        return {"status": "ok" if magic_ok else "parse_error", "kind": "attachment",
                "issues": [] if magic_ok else ["attachment_signature_mismatch"],
                "raw_sha256": sha256(body).hexdigest(), "text_extracted": False}
    html, encoding = decode_html(body, headers)
    parsed = asdict(parse_search_page(html) if kind == "listing" else parse_notice_page(html, url))
    parsed["status"] = parsed["status"].value
    parsed["decoded_input_sha256"] = parsed.pop("input_sha256")
    parsed["raw_sha256"] = sha256(body).hexdigest()
    parsed["encoding"] = encoding
    if kind == "notice":
        parsed["notice_id"] = sha256(("cn_ccgp\n" + _canonical_notice_url(url)).encode()).hexdigest()
    return parsed


def _initial_tasks(request: dict) -> list[dict]:
    if request["keyword"] is None:
        return [{"kind": "notice", "url": url} for url in request["notice_urls"]]
    return [{"kind": "listing", "url": SEARCH_URL + "?" + urlencode(build_search_params(
        request["keyword"], page, start_date=request["start_date"], end_date=request["end_date"]))}
        for page in range(1, request["pages"] + 1)]


def _following(store: Store, run_id: str, request: dict, task: dict, parsed: dict, transport) -> list[dict]:
    tasks = []
    if task["kind"] == "listing":
        known = store.task_urls(run_id, "notice")
        skipped = 0
        for item in parsed.get("items", ()):
            url = _canonical_notice_url(item["url"])
            if url in known:
                continue
            if len(known) >= request["max_notices"]:
                skipped += 1
                continue
            known.add(url)
            tasks.append({"kind": "notice", "url": url, "parent_url": task["url"], "payload": item})
        parsed["notices_outside_run_limit"] = skipped
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
    """执行已有run；完成的请求不重复联网。外部访问受限是终态，不能自动绕过。"""
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
                # 回调在每个实际HTTP请求前重验持久取消/租约，覆盖内部robots、重试和跳转。
                # 不仅等整个fetch结束才检查；在途请求无法撤回，但之后不能再发下一次请求。
                response = transport.fetch(task["url"], max_bytes=(MAX_ATTACHMENT_BYTES if task["kind"] == "attachment"
                                                                  else MAX_HTML_BYTES), kind=task["kind"],
                                           checkpoint=lambda: store.heartbeat(run_id, token))
            except FetchError as exc:
                store.record(run_id, token, task, error=exc.code, http_status=exc.http_status, attempts=exc.attempts)
                # 一旦当前来源规则/访问受限，停止后续任务，不把剩余页面逐个再试一遍。
                if exc.code in BLOCKING_ERRORS:
                    store.finish(run_id, token, "blocked", exc.code)
                    return store.report(run_id)
                continue
            try:
                # 中文阅读点：原件先落盘再解析；返回的解析结果始终能回到同一份原字节。
                # 若此刻进程崩溃，恢复可能重复一次HTTP读取，但同字节不会覆盖/复制原件。
                store.heartbeat(run_id, token)
                archived_body = store.read_blob(store.put_blob(response.body))
                parsed = parse_capture(task["kind"], archived_body, response.headers, response.final_url)
            except (ValueError, TypeError, UnicodeError) as exc:
                parsed = {"status": "parse_error", "issues": [str(exc)], "raw_sha256": sha256(response.body).hexdigest()}
            following = _following(store, run_id, request, task, parsed, transport)
            error = parsed["status"] if parsed["status"] in ("parse_error", "blocked") else None
            store.record(run_id, token, task, response=response, parsed=parsed,
                         parser_version=PARSER_VERSION, error=error, following=following)
            if parsed["status"] == "blocked":
                store.finish(run_id, token, "blocked", "access_challenge")
                return store.report(run_id)
        report = store.report(run_id)
        captures = report["captures"]
        errors = [row for row in captures if row["error_code"]]
        gaps = any(row["parsed"] and (row["parsed"].get("status") == "partial"
                   or row["parsed"].get("notices_outside_run_limit", 0)
                   or any(link["status"] in ("not_authorized", "run_limit", "not_requested")
                          for link in row["parsed"].get("attachments", ()))) for row in captures)
        status = "failed" if errors and len(errors) == len(captures) else ("partial" if errors or gaps else "succeeded")
        store.finish(run_id, token, status)
        return store.report(run_id)
    except KeyboardInterrupt:
        store.release(run_id, token)
        raise
    except StoreError:
        # 取消/失效租约不可改回运行态；其他存储失败释放租约，供同run恢复。
        store.release(run_id, token)
        if store.run(run_id)["status"] == "cancelled":
            return store.report(run_id)
        raise
    except BaseException:
        store.release(run_id, token)
        raise


def replay(store: Store, capture_id: str) -> dict:
    """仅从校验过的已归档字节重解析；离线重放不请求网站、不覆盖旧结论。"""
    capture = store.capture(capture_id)
    if not capture["sha256"]:
        raise StoreError("capture_has_no_body")
    if capture["kind"] == "api_page":
        from services.ingestion import tianjin
        req = store.run(capture["run_id"])["request"]
        parsed = tianjin.parse_page(store.read_blob(capture["sha256"]), req["page_size"])
        return store.save_replay(capture_id, tianjin.PARSER_VERSION, parsed)
    parsed = parse_capture(capture["kind"], store.read_blob(capture["sha256"]), capture["headers"],
                           capture["final_url"] or capture["url"])
    return store.save_replay(capture_id, PARSER_VERSION, parsed)

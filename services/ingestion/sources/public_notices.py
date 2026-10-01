"""两个官方静态栏目的离线适配器；解析页面不授予联网或持续采集许可。

2026-09-30 核实 CCGP c_list_bid/Pager 与海南 newtable/newsCon 模板。
这里只生成固定栏目路径、提取原文与未获取的附件引用；DNS、robots、授权、
请求配额及原件保留由传输/存储层负责。不会执行页面 JavaScript 或访问链接。
"""
from __future__ import annotations

from hashlib import sha256
import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .ccgp import (
    MAX_HTML_BYTES, ListingResult, NoticeCandidate, NoticePage, ParseStatus,
    _Document, _HTMLResourceLimit, _Node, _notice_url, parse_notice_page,
)

# 2026-09-30 按官方中央/地方栏目 URL 核对；谈判栏目实际是 jzxtpgg，不能由简称猜路径。
# 此枚举核对不代表每个栏目都完成本机实时模板或持续可采集验证。
_CCGP_CATEGORIES = frozenset(
    f"{area}/{kind}" for area in ("zygg", "dfgg")
    for kind in ("gkzb", "jzxcs", "jzxtpgg", "gzgg", "zbgg", "cjgg")
)
_HAINAN_CATEGORIES = frozenset(("cggg", "zfcgqtgg", "cgzbgg"))
_HOSTS = {"cn_ccgp": ("www.ccgp.gov.cn", "ccgp.gov.cn"),
          "cn_hainan": ("ggzy.hainan.gov.cn",)}
_FILE_SUFFIX = re.compile(r"\.(?:pdf|docx?|xlsx?|zip|rar|7z)$", re.I)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?")
_CHALLENGE_WORDS = ("验证码", "访问频繁", "访问过于频繁", "访问受限")


def _parts(source: str, url: str, *, strict: bool = True):
    """仅认官方主机与标准端口；URL 语法约束不代替实际请求的 SSRF 防线。"""
    if (source not in _HOSTS or not isinstance(url, str) or not url
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or parts.hostname not in _HOSTS[source]
                or parts.username is not None or parts.password is not None
                or parts.port not in (None, 80 if parts.scheme == "http" else 443)):
            return None
    except ValueError:
        return None
    if "%" in parts.path or (strict and ("?" in url or "#" in url)):
        return None
    return parts


def build_listing_url(source: str, category: str, page: int = 1) -> str:
    """页码从 1 开始；仅生成已核实模板，不接受任意路径或查询参数。

    CCGP Pager 的 current 从 0 开始；海南 select/下一页链接使用自然页码。
    路径模式已核实不代表任意大页存在，实际请求仍受任务页数和请求预算限制。
    """
    if type(page) is not int or not 1 <= page <= 100_000:
        raise ValueError("page须为1–100000的整数")
    if source == "cn_ccgp" and category in _CCGP_CATEGORIES:
        suffix = "" if page == 1 else f"index_{page - 1}.htm"
        return f"https://www.ccgp.gov.cn/cggg/{category}/{suffix}"
    if source == "cn_hainan" and category in _HAINAN_CATEGORIES:
        suffix = "index.jhtml" if page == 1 else f"index_{page}.jhtml"
        return f"https://ggzy.hainan.gov.cn/ggzy/ggzy/{category}/{suffix}"
    raise ValueError("未支持的来源或栏目")


def listing_identity(source: str, url: str) -> tuple[str, int] | None:
    """供 parser 和 policy 共用的纯校验；拒绝 query/fragment/凭据/编码路径。"""
    parts = _parts(source, url)
    if parts is None:
        return None
    if source == "cn_ccgp":
        match = re.fullmatch(r"/cggg/(zygg|dfgg)/([a-z]+)/(?:(index)(?:_([1-9][0-9]{0,5}))?\.htm)?", parts.path)
        if match is None:
            return None
        category = f"{match[1]}/{match[2]}"
        page = int(match[4]) + 1 if match[4] else 1
        return (category, page) if category in _CCGP_CATEGORIES and page <= 100_000 else None
    match = re.fullmatch(r"/ggzy/ggzy/([a-z]+)/index(?:_([1-9][0-9]{0,5}))?\.jhtml", parts.path)
    if match is None:
        return None
    category, number = match.groups()
    page = int(number) if number else 1
    if number and page < 2:
        return None
    return (category, page) if category in _HAINAN_CATEGORIES and page <= 100_000 else None


def canonical_notice_url(source: str, url: str) -> str | None:
    """公告身份不跟随重定向；兼容 v1 CCGP query，去 fragment 后用于去重。

    CCGP 旧搜索已归档的路径/查询须保持身份稳定；静态栏目仍通过更窄的
    listing_identity 校验。海南仅认三个公告目录的数字 ID，不猜其它站点模板。
    """
    if source == "cn_ccgp":
        accepted = _notice_url(url) if isinstance(url, str) else None
        if accepted is None:
            return None
        parts = urlsplit(accepted)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    parts = _parts(source, url)
    if parts is None or source != "cn_hainan":
        return None
    match = re.fullmatch(r"/ggzy/ggzy/([a-z]+)/[1-9][0-9]*\.jhtml", parts.path)
    if match is None or match[1] not in _HAINAN_CATEGORIES:
        return None
    # 实际列表含 http:80；2026-09-30 已核实同路径官方 301 到 HTTPS:443。
    # 海南专属规范统一身份，避免目录线索与手工 HTTPS 捕获生成两份公告。
    return urlunsplit(("https", parts.hostname, parts.path, "", ""))


def _read(html: str) -> tuple[_Document | None, str, tuple[str, ...]]:
    if not isinstance(html, str):
        raise TypeError("html须为已解码文本")
    data = html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        raise ValueError("HTML超过大小上限")
    fingerprint = sha256(data).hexdigest()
    document = _Document()
    try:
        document.feed(html)
        document.close()
    except _HTMLResourceLimit as exc:
        return None, fingerprint, (str(exc),)
    except (ValueError, AssertionError):
        return None, fingerprint, ("malformed_html",)
    return document, fingerprint, ()


def _class(node: _Node, name: str) -> bool:
    return name in (node.attrs.get("class") or "").split()


def _hainan_footer(node: _Node) -> bool:
    cells = [n for n in node.children if isinstance(n, _Node) and n.tag == "td"]
    # 真实分页在表格最后的跨四列单元格中；只跳过这个已识别的控制行。
    return (len(cells) == 1 and cells[0].attrs.get("colspan") == "4"
            and re.match(r"共\s*[0-9]+\s*条记录", cells[0].text()) is not None)


def _outside_text(root: _Node, omit: list[_Node]) -> str:
    omitted = {id(node) for node in omit}
    pending: list[_Node | str] = [root]
    text = []
    while pending:
        node = pending.pop()
        if isinstance(node, str):
            text.append(node)
        elif id(node) not in omitted and node.tag not in ("head", "script", "style"):
            pending.extend(reversed(node.children))
    return " ".join(text)


def _next_page(document: _Document, source: str, url: str,
               identity: tuple[str, int]) -> tuple[str | None, list[str]]:
    category, page = identity
    found = []
    issues = []
    nodes = list(document.root.walk())
    # 只读取已核实 Pager 配置，绝不执行或泛化解释任意脚本。
    if source == "cn_ccgp":
        pattern = re.compile(r"\s*Pager\(\{size:([0-9]{1,6}),\s*current:([0-9]{1,6}),\s*prefix:'index',\s*suffix:'htm'\}\);?\s*")
        for node in nodes:
            if node.tag != "script":
                continue
            script = "".join(c for c in node.children if isinstance(c, str))
            match = pattern.fullmatch(script)
            if match:
                total, current = map(int, match.groups())
                if not 1 <= total <= 100_000 or current != page - 1 or current >= total:
                    issues.append("pagination_mismatch")
                elif page < total:
                    found.append(build_listing_url(source, category, page + 1))
    for node in nodes:
        if node.tag != "a" or "disabled" in node.attrs:
            continue
        if node.text().strip() != "下一页" and not _class(node, "next"):
            continue
        href = node.attrs.get("href") or ""
        if source == "cn_hainan" and href == "#":
            # 海南模板把相对路径放在 onclick；限定整句文法并再次校验目录与页码。
            match = re.fullmatch(r"\s*location\.href=encodeURI\('([^']+)'\);?\s*", node.attrs.get("onclick") or "")
            href = match[1] if match else ""
        try:
            candidate = urljoin(url, href) if href else ""
        except ValueError:
            candidate = ""
        if listing_identity(source, candidate) != (category, page + 1):
            issues.append("invalid_next_page")
        else:
            found.append(candidate)
    if issues:
        return None, list(dict.fromkeys(issues))
    if len(set(found)) > 1:
        return None, ["ambiguous_next_page"]
    return (found[0] if found else None), []


def parse_listing(html: str, url: str, source: str) -> ListingResult:
    """列表输出发现线索；显式零条才 EMPTY，模板变化、坏行与挑战分别表达。"""
    document, fingerprint, issues = _read(html)
    if document is None:
        return ListingResult(ParseStatus.PARSE_ERROR, (), issues, fingerprint)
    identity = listing_identity(source, url)
    if identity is None:
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("invalid_listing_url",), fingerprint)
    container_tag, container_class = ("ul", "c_list_bid") if source == "cn_ccgp" else ("table", "newtable")
    containers = [n for n in document.root.walk() if n.tag == container_tag and _class(n, container_class)]
    content_rows = [n for container in containers for n in container.walk()
                    if n.tag == ("li" if source == "cn_ccgp" else "tr")
                    and not _hainan_footer(n)]
    if any(word in _outside_text(document.root, content_rows) for word in _CHALLENGE_WORDS):
        return ListingResult(ParseStatus.BLOCKED, (), ("access_challenge",), fingerprint)
    if len(containers) != 1:
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("unexpected_listing_template",), fingerprint)
    container = containers[0]
    rows = ([n for n in container.children if isinstance(n, _Node) and n.tag == "li"]
            if source == "cn_ccgp" else [n for n in container.walk() if n.tag == "tr"
                                       and any(isinstance(c, _Node) and c.tag == "td" for c in n.children)
                                       and not _hainan_footer(n)])
    if not rows:
        zero = re.search(r"共\s*0\s*条", document.root.text()) is not None
        return ListingResult(ParseStatus.EMPTY if zero else ParseStatus.PARSE_ERROR, (),
                             () if zero else ("empty_without_confirmation",), fingerprint)
    items, errors, seen = [], [], set()
    for index, row in enumerate(rows, 1):
        if any(n is not row and n.tag == row.tag for n in row.walk()):
            errors.append(f"row_{index}:nested_listing_row")
            continue
        anchors = [n for n in row.walk() if n.tag == "a"]
        anchor = anchors[0] if len(anchors) == 1 else None
        title = ((anchor.attrs.get("title") or anchor.text()).strip()) if anchor else ""
        try:
            target = canonical_notice_url(source, urljoin(url, anchor.attrs.get("href") or "")) if anchor else None
        except ValueError:
            target = None
        # 即使域名正确，也不把相邻栏目/不相关路径混进本次栏目发现结果。
        prefix = f"/cggg/{identity[0]}/" if source == "cn_ccgp" else f"/ggzy/ggzy/{identity[0]}/"
        if not title or not target or not urlsplit(target).path.startswith(prefix):
            errors.append(f"row_{index}:invalid_title_or_url")
            continue
        purchaser = None
        if source == "cn_ccgp":
            ems = [n.text() for n in row.children if isinstance(n, _Node) and n.tag == "em"]
            raw = row.text(omit=anchor)
            published = ems[0] if ems and _DATE.fullmatch(ems[0]) else None
            # em 的位置须有实际标签配合；缺标签时保持未知，不能误将地域当采购人。
            match = re.search(r"采购人[：:]\s*(.+)$", raw)
            purchaser = match[1].strip() if match else None
            locator = f"ul.c_list_bid > li:nth-of-type({index})"
        else:
            cells = [n for n in row.children if isinstance(n, _Node) and n.tag == "td"]
            if len(cells) != 4:
                errors.append(f"row_{index}:invalid_table_columns")
                continue
            raw = " | ".join((cells[1].text(), cells[3].text()))
            published = cells[3].text() if _DATE.fullmatch(cells[3].text()) else None
            locator = f"table.newtable tr[data-row={index}]"
        if target in seen:
            continue
        seen.add(target)
        if published is None:
            errors.append(f"row_{index}:missing_published_date")
        items.append(NoticeCandidate(title, target, raw, published, purchaser, None, locator, source))
    next_url, next_errors = _next_page(document, source, url, identity)
    errors.extend(next_errors)
    status = ParseStatus.PARTIAL if items and errors else (ParseStatus.OK if items else ParseStatus.PARSE_ERROR)
    return ListingResult(status, tuple(items), tuple(errors), fingerprint, next_url)


def _segments(body: _Node, selector: str) -> tuple[dict[str, str], ...]:
    """海南正文以嵌套 div 排版；保留段落/表格行与内联文字，不重复父子正文。"""
    segments = []
    pending = [(body, selector)]
    while pending:
        node, locator = pending.pop()
        if isinstance(node, str):
            text = re.sub(r"\s+", " ", node).strip()
            if text:
                segments.append({"text": text, "locator": locator})
            continue
        if node.tag in ("script", "style", "input"):
            continue
        if node.tag in ("p", "li", "tr", "h1", "h2", "h3", "h4") and not any(
                child is not node and child.tag == "table" for child in node.walk()):
            cells = [n.text() for n in node.children if isinstance(n, _Node) and n.tag in ("td", "th")]
            text = " | ".join(cells) if node.tag == "tr" else node.text()
            if text:
                segments.append({"text": text, "locator": locator})
            continue
        children, inline, index = [], [], 0
        def flush():
            if inline:
                children.append(("".join(inline), f"{locator} text-run[{len(children) + 1}]"))
                inline.clear()
        for child in node.children:
            if isinstance(child, str):
                inline.append(child)
            else:
                index += 1
                if child.tag in ("script", "style", "input"):
                    continue
                if child.tag in ("span", "a", "b", "strong", "em", "i", "u", "font", "br"):
                    inline.append("\n" if child.tag == "br" else child.text())
                else:
                    flush()
                    children.append((child, f"{locator} > {child.tag}:nth-child({index})"))
        flush()
        pending.extend(reversed(children))
    return tuple(segments)


def _references(root: _Node, url: str, source: str, selector: str):
    """只发现引用；完整文件名不意味着有下载地址，非 ASCII 路径/query 编码后交传输层。"""
    attachments, links, issues, seen = [], [], [], set()
    for index, node in enumerate((n for n in root.walk() if n.tag == "a"), 1):
        href = node.attrs.get("href") or ""
        name = node.text()
        locator = f"{selector} a[{index}]"
        if not href:
            if _FILE_SUFFIX.search(name):
                issues.append("attachment_link_missing")
            continue
        try:
            target = urljoin(url, href)
            parts = urlsplit(target)
            valid = (parts.scheme in ("http", "https") and parts.hostname is not None
                     and parts.netloc.isascii()
                     and parts.username is None and parts.password is None
                     and parts.port in (None, 80 if parts.scheme == "http" else 443)
                     and not any(ord(c) <= 32 or ord(c) == 127 for c in target) and "\\" not in target)
        except ValueError:
            valid = False
        if not valid:
            issues.append("malformed_attachment_link")
            continue
        # 保留原协议和 query 分隔符/已有转义；避免中文 attname 导致 HTTP 客户端编码错误。
        target = urlunsplit((parts.scheme, parts.netloc, quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                             quote(parts.query, safe="%=&?/:@!$'()*+,;~-._"), ""))
        original = canonical_notice_url(source, target)
        if original and original not in {link["url"] for link in links}:
            links.append({"url": original, "name": name, "locator": locator})
        if _FILE_SUFFIX.search(parts.path) and target not in seen:
            seen.add(target)
            attachments.append({"url": target, "name": name, "locator": locator, "status": "not_fetched"})
    return tuple(attachments), tuple(links), list(dict.fromkeys(issues))


def parse_notice(html: str, url: str, source: str) -> NoticePage:
    """同一 NoticePage 契约保留正文段落与引用，不猜发布日期、截止或关联项目。"""
    document, fingerprint, issues = _read(html)
    if document is None:
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), issues, fingerprint)
    if canonical_notice_url(source, url) is None:
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("invalid_notice_url",), fingerprint)
    if source == "cn_ccgp":
        # v1 的正文、字段位置与挑战边界保持兼容；补摘要表内的附件以及安全引用编码。
        result = parse_notice_page(html, url)
        if result.status not in (ParseStatus.OK, ParseStatus.PARTIAL):
            return result
        wrappers = [n for n in document.root.walk() if _class(n, "vF_detail_main")]
        bodies = [n for n in document.root.walk() if _class(n, "vF_detail_content")]
        root = wrappers[0] if len(wrappers) == 1 else bodies[0]
        selector = "div.vF_detail_main" if len(wrappers) == 1 else "div.vF_detail_content"
        attachments, links, reference_issues = _references(root, url, source, selector)
        errors = tuple(dict.fromkeys((*result.issues, *reference_issues)))
        return NoticePage(ParseStatus.PARTIAL if errors else ParseStatus.OK, result.title, result.text,
                          attachments, errors, fingerprint, result.segments, result.metadata, links)
    nodes = list(document.root.walk())
    wrappers = [n for n in nodes if n.tag == "div" and _class(n, "newsTex")]
    bodies = [n for n in nodes if n.tag == "div" and _class(n, "newsCon")]
    titles = ([n for n in wrappers[0].children if isinstance(n, _Node) and n.tag == "h1"]
              if len(wrappers) == 1 else [])
    title_node = titles[0] if len(titles) == 1 else None
    title = title_node.text() if title_node else None
    title_challenge = bool(title and (title in ("验证码", "安全验证", "人机验证") or any(
        word in title for word in ("请输入验证码", "请填写验证码", "请完成验证", "请通过验证", "访问受限", "访问频繁", "访问过于频繁"))))
    # 标题/摘要含“验证码系统采购”是业务内容；页面额外的验证提示仍必须阻断。
    summaries = [n for n in wrappers[0].children if isinstance(n, _Node) and _class(n, "summary")] if len(wrappers) == 1 else []
    omit = bodies + ([title_node] if title_node and len(bodies) == 1 else [])
    omit += [n for n in summaries if title and n.text() == title]
    if title_challenge or any(word in _outside_text(document.root, omit) for word in _CHALLENGE_WORDS):
        return NoticePage(ParseStatus.BLOCKED, None, None, (), ("access_challenge",), fingerprint)
    if (len(wrappers) != 1 or len(bodies) != 1 or not bodies[0].text()
            or not any(n is bodies[0] for n in wrappers[0].walk())):
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("unexpected_notice_template",), fingerprint)
    attachments, links, errors = _references(bodies[0], url, source, "div.newsCon")
    if not title:
        errors.append("missing_notice_title")
    segments = _segments(bodies[0], "div.newsCon")
    # msgbar 没有发布时间；不拿正文中的截止/公告期限来补发布日期，交给列表证据。
    return NoticePage(ParseStatus.PARTIAL if errors else ParseStatus.OK, title, bodies[0].text(),
                      attachments, tuple(errors), fingerprint, segments, (), links)

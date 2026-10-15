"""两个官方网站的栏目页解析器：中国政府采购网（CCGP）的公告栏目，和海南省公共资源交易平台。

做三件事：生成栏目列表页的 URL、解析列表页、解析公告正文页。
页面模板在 2026-09-30 核对过（CCGP 的 c_list_bid/Pager，海南的 newtable/newsCon）。

这个模块不联网，也不执行页面里的 JavaScript。DNS、robots、准入、请求限额、原件保存由 transport 和 archive 负责。
能解析页面不代表获准持续采集。
"""

from __future__ import annotations

from hashlib import sha256
import re
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from .ccgp import (
    MAX_HTML_BYTES,
    ListingResult,
    NoticeCandidate,
    NoticePage,
    ParseStatus,
    _Document,
    _HTMLResourceLimit,
    _Node,
    _notice_url,
    parse_notice_page,
)

# 栏目代码 → 路径，2026-09-30 按官方网站核对（例如竞争性谈判栏目的路径是 jzxtpgg，不能按简称猜）。
# 列在这里只说明路径对，不代表每个栏目都实际抓取验证过。
_CCGP_CATEGORIES = frozenset(
    f"{area}/{kind}" for area in ("zygg", "dfgg") for kind in ("gkzb", "jzxcs", "jzxtpgg", "gzgg", "zbgg", "cjgg")
)
_HAINAN_CATEGORIES = frozenset(("cggg", "zfcgqtgg", "cgzbgg"))
_HOSTS = {"cn_ccgp": ("www.ccgp.gov.cn", "ccgp.gov.cn"), "cn_hainan": ("ggzy.hainan.gov.cn",)}
_FILE_SUFFIX = re.compile(r"\.(?:pdf|docx?|xlsx?|zip|rar|7z)$", re.I)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?: [0-9]{2}:[0-9]{2}(?::[0-9]{2})?)?")
_CHALLENGE_WORDS = ("验证码", "访问频繁", "访问过于频繁", "访问受限")


def _parts(source: str, url: str, *, strict: bool = True):
    """拆分并检查 URL：只接受官方主机和标准端口。真正联网时 transport 还会再做一遍安全检查。"""
    if (
        source not in _HOSTS
        or not isinstance(url, str)
        or not url
        or any(ord(c) <= 32 or ord(c) == 127 for c in url)
        or "\\" in url
    ):
        return None
    try:
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or parts.hostname not in _HOSTS[source]
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 80 if parts.scheme == "http" else 443)
        ):
            return None
    except ValueError:
        return None
    if "%" in parts.path or (strict and ("?" in url or "#" in url)):
        return None
    return parts


def build_listing_url(source: str, category: str, page: int = 1) -> str:
    """生成栏目列表页的 URL。page 从 1 开始。

    CCGP 的分页参数从 0 开始计数，海南从 1 开始，这里统一换算。
    只生成核对过的路径格式，不接受任意路径或参数。
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
    """判断一个 URL 是不是某个栏目的列表页，是的话返回 (栏目, 页码)，否则返回 None。"""
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
    """把公告 URL 规范化成"公告身份"，用来去重；不是这个来源的公告链接就返回 None。

    规范化不跟随重定向，只去掉 # 片段等。CCGP 旧版搜索采集时用过的 URL 格式仍然认，
    保证老数据的身份不变。海南只认三个公告目录下的数字 ID。
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
    # 海南列表里的链接是 http，官方会 301 跳到同路径的 https（2026-09-30 核实）。
    # 统一成 https，免得从列表发现的和手动给的链接被当成两条公告。
    return urlunsplit(("https", parts.hostname, parts.path, "", ""))


def _read(html: str) -> tuple[_Document | None, str, tuple[str, ...]]:
    """解析 HTML，返回 (节点树或 None, 状态, 问题列表)。"""
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
    """节点的 class 里是否包含 name。"""
    return name in (node.attrs.get("class") or "").split()


def _hainan_footer(node: _Node) -> bool:
    """是不是海南列表表格里最后那一行分页控件。"""
    cells = [n for n in node.children if isinstance(n, _Node) and n.tag == "td"]
    # 分页控件在表格最后一行、横跨四列的单元格里。只跳过这一行。
    return (
        len(cells) == 1
        and cells[0].attrs.get("colspan") == "4"
        and re.match(r"共\s*[0-9]+\s*条记录", cells[0].text()) is not None
    )


def _outside_text(root: _Node, omit: list[_Node]) -> str:
    """取页面上除了 omit 这些节点以外的文字（用来检查验证码提示）。"""
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


def _next_page(document: _Document, source: str, url: str, identity: tuple[str, int]) -> tuple[str | None, list[str]]:
    category, page = identity
    found = []
    issues = []
    nodes = list(document.root.walk())
    # 只按核对过的固定格式读取分页配置，不执行页面脚本。
    if source == "cn_ccgp":
        pattern = re.compile(
            r"\s*Pager\(\{size:([0-9]{1,6}),\s*current:([0-9]{1,6}),\s*prefix:'index',\s*suffix:'htm'\}\);?\s*"
        )
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
            # 海南的下一页链接写在 onclick 里。只接受固定格式，并再次检查栏目和页码。
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
    """解析栏目列表页，返回公告线索和下一页链接。

    页面明确是零条时才返回 EMPTY；模板变了、有坏行、遇到验证码，会分别用不同的状态表示。
    """
    document, fingerprint, issues = _read(html)
    if document is None:
        return ListingResult(ParseStatus.PARSE_ERROR, (), issues, fingerprint)
    identity = listing_identity(source, url)
    if identity is None:
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("invalid_listing_url",), fingerprint)
    container_tag, container_class = ("ul", "c_list_bid") if source == "cn_ccgp" else ("table", "newtable")
    containers = [n for n in document.root.walk() if n.tag == container_tag and _class(n, container_class)]
    content_rows = [
        n
        for container in containers
        for n in container.walk()
        if n.tag == ("li" if source == "cn_ccgp" else "tr") and not _hainan_footer(n)
    ]
    if any(word in _outside_text(document.root, content_rows) for word in _CHALLENGE_WORDS):
        return ListingResult(ParseStatus.BLOCKED, (), ("access_challenge",), fingerprint)
    if len(containers) != 1:
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("unexpected_listing_template",), fingerprint)
    container = containers[0]
    rows = (
        [n for n in container.children if isinstance(n, _Node) and n.tag == "li"]
        if source == "cn_ccgp"
        else [
            n
            for n in container.walk()
            if n.tag == "tr"
            and any(isinstance(c, _Node) and c.tag == "td" for c in n.children)
            and not _hainan_footer(n)
        ]
    )
    if not rows:
        zero = re.search(r"共\s*0\s*条", document.root.text()) is not None
        return ListingResult(
            ParseStatus.EMPTY if zero else ParseStatus.PARSE_ERROR,
            (),
            () if zero else ("empty_without_confirmation",),
            fingerprint,
        )
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
        # 只收本栏目下的公告链接，同域名下其他栏目的链接不要。
        prefix = f"/cggg/{identity[0]}/" if source == "cn_ccgp" else f"/ggzy/ggzy/{identity[0]}/"
        if not title or not target or not urlsplit(target).path.startswith(prefix):
            errors.append(f"row_{index}:invalid_title_or_url")
            continue
        purchaser = None
        if source == "cn_ccgp":
            ems = [n.text() for n in row.children if isinstance(n, _Node) and n.tag == "em"]
            raw = row.text(omit=anchor)
            published = ems[0] if ems and _DATE.fullmatch(ems[0]) else None
            # 只有带明确标签的字段才取值；没有标签时留空，免得把地区误当成采购人。
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
    """把海南公告正文拆成段落（海南用嵌套的 div 排版）。保留段落和表格行，父子节点的文字不重复计入。"""
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
            child is not node and child.tag == "table" for child in node.walk()
        ):
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
    """找出正文里的附件引用（链接和文件名）。

    有文件名不代表有下载地址。路径里的中文会先编码，再交给 transport。
    """
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
            valid = (
                parts.scheme in ("http", "https")
                and parts.hostname is not None
                and parts.netloc.isascii()
                and parts.username is None
                and parts.password is None
                and parts.port in (None, 80 if parts.scheme == "http" else 443)
                and not any(ord(c) <= 32 or ord(c) == 127 for c in target)
                and "\\" not in target
            )
        except ValueError:
            valid = False
        if not valid:
            issues.append("malformed_attachment_link")
            continue
        # 只编码中文等非 ASCII 字符，原有的协议、参数分隔符和已有的转义保持不变。
        target = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                quote(parts.path, safe="/%:@!$&'()*+,;=-._~"),
                quote(parts.query, safe="%=&?/:@!$'()*+,;~-._"),
                "",
            )
        )
        original = canonical_notice_url(source, target)
        if original and original not in {link["url"] for link in links}:
            links.append({"url": original, "name": name, "locator": locator})
        if _FILE_SUFFIX.search(parts.path) and target not in seen:
            seen.add(target)
            attachments.append({"url": target, "name": name, "locator": locator, "status": "not_fetched"})
    return tuple(attachments), tuple(links), list(dict.fromkeys(issues))


def parse_notice(html: str, url: str, source: str) -> NoticePage:
    """解析公告正文页，返回和 CCGP 相同结构的 NoticePage。不推测发布日期、截止时间或关联项目。"""
    document, fingerprint, issues = _read(html)
    if document is None:
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), issues, fingerprint)
    if canonical_notice_url(source, url) is None:
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("invalid_notice_url",), fingerprint)
    if source == "cn_ccgp":
        # CCGP 的正文解析沿用 v1 的逻辑，只是额外收集摘要表里的附件，并对链接做安全编码。
        result = parse_notice_page(html, url)
        if result.status not in (ParseStatus.OK, ParseStatus.PARTIAL):
            return result
        wrappers = [n for n in document.root.walk() if _class(n, "vF_detail_main")]
        bodies = [n for n in document.root.walk() if _class(n, "vF_detail_content")]
        root = wrappers[0] if len(wrappers) == 1 else bodies[0]
        selector = "div.vF_detail_main" if len(wrappers) == 1 else "div.vF_detail_content"
        attachments, links, reference_issues = _references(root, url, source, selector)
        errors = tuple(dict.fromkeys((*result.issues, *reference_issues)))
        return NoticePage(
            ParseStatus.PARTIAL if errors else ParseStatus.OK,
            result.title,
            result.text,
            attachments,
            errors,
            fingerprint,
            result.segments,
            result.metadata,
            links,
        )
    nodes = list(document.root.walk())
    wrappers = [n for n in nodes if n.tag == "div" and _class(n, "newsTex")]
    bodies = [n for n in nodes if n.tag == "div" and _class(n, "newsCon")]
    titles = [n for n in wrappers[0].children if isinstance(n, _Node) and n.tag == "h1"] if len(wrappers) == 1 else []
    title_node = titles[0] if len(titles) == 1 else None
    title = title_node.text() if title_node else None
    title_challenge = bool(
        title
        and (
            title in ("验证码", "安全验证", "人机验证")
            or any(
                word in title
                for word in (
                    "请输入验证码",
                    "请填写验证码",
                    "请完成验证",
                    "请通过验证",
                    "访问受限",
                    "访问频繁",
                    "访问过于频繁",
                )
            )
        )
    )
    # 标题里有"验证码"是正常的业务内容；但页面上另有验证提示时，仍按"被拦截"处理。
    summaries = (
        [n for n in wrappers[0].children if isinstance(n, _Node) and _class(n, "summary")] if len(wrappers) == 1 else []
    )
    omit = bodies + ([title_node] if title_node and len(bodies) == 1 else [])
    omit += [n for n in summaries if title and n.text() == title]
    if title_challenge or any(word in _outside_text(document.root, omit) for word in _CHALLENGE_WORDS):
        return NoticePage(ParseStatus.BLOCKED, None, None, (), ("access_challenge",), fingerprint)
    if (
        len(wrappers) != 1
        or len(bodies) != 1
        or not bodies[0].text()
        or not any(n is bodies[0] for n in wrappers[0].walk())
    ):
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("unexpected_notice_template",), fingerprint)
    attachments, links, errors = _references(bodies[0], url, source, "div.newsCon")
    if not title:
        errors.append("missing_notice_title")
    segments = _segments(bodies[0], "div.newsCon")
    # 海南详情页的信息栏里没有发布时间。不拿正文里的截止日期来充数，发布时间从列表页取。
    return NoticePage(
        ParseStatus.PARTIAL if errors else ParseStatus.OK,
        title,
        bodies[0].text(),
        attachments,
        tuple(errors),
        fingerprint,
        segments,
        (),
        links,
    )

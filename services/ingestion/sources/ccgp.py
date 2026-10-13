"""中国政府采购网（CCGP）的解析器：构造搜索参数、解析搜索列表页和公告正文页。

这个模块不联网。下载由 transport 负责，抓取顺序由 pipeline 安排。

搜索参数映射、列表选择器和元数据拆分的思路改编自 bidding-ai-analyzer（MIT 许可）：
Copyright (c) 2026 ichthyoplanktonzyh
上游提交、文件和完整许可见 third_party/README.md。

只认识约定好的页面模板，模板变了就明确报错。
附件只记下链接和名称，不解析文件内容，也不推断任何投标资格。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

SOURCE_ID = "cn_ccgp"
SEARCH_URL = "http://search.ccgp.gov.cn/bxsearch"  # 沿用上游的搜索入口（HTTP）。
MAX_HTML_BYTES = 2 * 1024 * 1024  # 单个页面最大 2 MB。
# 限制 HTML 的嵌套深度和节点数：防止一个很小但结构畸形的页面让解析器耗尽内存或变得极慢。
MAX_HTML_DEPTH = 128
MAX_HTML_NODES = 10_000  # 元素和文本片段的总数上限。
_VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class ParseStatus(str, Enum):
    """这一页的解析结果：成功、部分成功、空、被拦截（验证码/限流）、格式不认识等。只描述这一页。"""

    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class NoticeCandidate:
    """列表页上的一条公告线索：标题、链接、元数据原文。不推测预算、截止时间、地区或是否还在招标。"""

    title: str
    url: str
    metadata_text: str
    published_text: str | None
    purchaser: str | None
    agency: str | None
    locator: str
    source_id: str = SOURCE_ID


@dataclass(frozen=True)
class ListingResult:
    """一个列表页的解析结果：状态、公告线索、下一页链接、内容指纹。"""

    status: ParseStatus
    items: tuple[NoticeCandidate, ...]
    issues: tuple[str, ...]
    input_sha256: str
    # next_url：栏目列表页能解析出下一页时填上；旧的搜索列表不填。
    next_url: str | None = None


@dataclass(frozen=True)
class NoticePage:
    """一个公告页的解析结果：标题、正文段落、元数据、附件链接。看到附件链接不代表已经下载了附件。"""

    status: ParseStatus
    title: str | None
    text: str | None
    attachments: tuple[dict[str, str], ...]
    issues: tuple[str, ...]
    input_sha256: str
    segments: tuple[dict[str, str], ...] = ()
    metadata: tuple[dict[str, str], ...] = ()
    links: tuple[dict[str, str], ...] = ()


def build_search_params(
    keyword: str, page: int = 1, *, start_date: str | None = None, end_date: str | None = None
) -> dict[str, str]:
    """生成 CCGP 搜索的查询参数（只生成，不发请求）。

    start_date / end_date 是按发布日期筛选，不是投标截止日期；不传就不限，不会自动填今天。
    bidType=0 表示所有类型的公告都要，更正、意向、结果等类型由目录层再区分。
    """
    if not isinstance(keyword, str) or not keyword.strip() or len(keyword) > 200:
        raise ValueError("keyword须为1–200字符的非空文本")
    if type(page) is not int or page < 1:
        raise ValueError("page须为正整数，不能用布尔值冒充")
    dates: list[date | None] = []
    for value in (start_date, end_date):
        if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)):
            raise ValueError("日期须为YYYY-MM-DD或None")
        dates.append(date.fromisoformat(value) if value is not None else None)
    if dates[0] is not None and dates[1] is not None and dates[0] > dates[1]:
        raise ValueError("起始日期不能晚于结束日期")
    return {
        "searchtype": "1",
        "page_index": str(page),
        "bidSort": "0",
        "buyerName": "",
        "projectId": "",
        "pinMu": "0",
        "bidType": "0",
        "dbselect": "bidx",
        "kw": keyword.strip(),
        "start_time": start_date.replace("-", ":") if start_date else "",
        "end_time": end_date.replace("-", ":") if end_date else "",
        "timeType": "6",
        "displayZone": "",
        "zoneId": "",
        "pppStatus": "0",
        "agentName": "",
    }


@dataclass
class _Node:
    """简化的 HTML 节点：标签、属性、子节点、在原文中的位置。"""

    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    def walk(self):
        """遍历所有子孙节点。用循环而不是递归，嵌套再深也不会栈溢出。"""
        pending = [self]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(child for child in reversed(node.children) if isinstance(child, _Node))

    def text(
        self, *, skip_listing_rows: bool = False, skip_notice_body: bool = False, omit: _Node | None = None
    ) -> str:
        pending: list[_Node | str] = [self]
        parts: list[str] = []
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item is not omit and item.tag not in ("script", "style", "head"):
                if skip_notice_body and "vF_detail_content" in (item.attrs.get("class") or "").split():
                    continue
                # 页面级访问提示与招标标题分开；验证码系统采购不是验证码挑战页。
                if (
                    skip_listing_rows
                    and item.tag == "ul"
                    and "vT-srch-result-list-bid" in (item.attrs.get("class") or "").split()
                ):
                    pending.extend(
                        child for child in reversed(item.children) if not isinstance(child, _Node) or child.tag != "li"
                    )
                else:
                    pending.extend(reversed(item.children))
        return re.sub(r"\s+", " ", "".join(parts)).strip()


class _HTMLResourceLimit(ValueError):
    """页面超出大小、深度或节点数限制。整页解析失败，不返回已经解析出的一部分。"""


class _Document(HTMLParser):
    """用标准库 HTMLParser 构建节点树，有深度和节点数上限。不加载图片、脚本或外部链接。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]
        self.node_count = 0

    def _reserve_node(self):
        if self.node_count >= MAX_HTML_NODES:
            raise _HTMLResourceLimit("html_node_limit")
        self.node_count += 1

    def _append_element(self, tag, attrs, *, push):
        # 先检查深度和节点数，再创建节点。
        if len(self.stack) > MAX_HTML_DEPTH:
            raise _HTMLResourceLimit("html_depth_limit")
        self._reserve_node()
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if push:
            self.stack.append(node)

    def handle_starttag(self, tag, attrs):
        self._append_element(tag, attrs, push=tag not in _VOID_TAGS)

    def handle_startendtag(self, tag, attrs):
        self._append_element(tag, attrs, push=False)

    def handle_endtag(self, tag):
        # 找对应的开始标签。栈的深度有上限，所以这里的查找不会太慢。
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self._reserve_node()
        self.stack[-1].children.append(data)


def _notice_url(href: str | None) -> str | None:
    """检查并规范化 CCGP 公告链接，不是 CCGP 的链接返回 None。真正联网时 transport 还会再查一遍。"""
    if not href or any(ord(char) <= 32 for char in href) or "\\" in href:
        return None
    try:
        # 链接格式有问题就跳过这一条，不尝试修补，也不因为一个坏链接让整页解析失败。
        url = urljoin(SEARCH_URL, href)
        parts = urlsplit(url)
        if (
            parts.scheme not in ("http", "https")
            or parts.hostname not in ("www.ccgp.gov.cn", "ccgp.gov.cn")
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
        ):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"/cggg/(?:zygg|dfgg)/[A-Za-z0-9_/-]+\.html?", parts.path):
        return None
    return url


def parse_search_page(html: str) -> ListingResult:
    """解析 CCGP 搜索结果页（ul.vT-srch-result-list-bid），返回公告线索。

    "没有结果"只在列表容器存在、并且页面明确写着"共0条"时才成立；页面结构变了就报错，
    不会把"解析失败"当成"零条结果"。
    改编自上游思路，但去掉了上游"出错静默跳过""固定每页 20 条""最后一段一定是地区"这些假设。
    """
    if not isinstance(html, str):
        raise TypeError("html须为已解码文本")
    encoded = html.encode("utf-8")
    if len(encoded) > MAX_HTML_BYTES:
        raise ValueError("列表HTML超过本片大小上限")
    fingerprint = sha256(encoded).hexdigest()
    document = _Document()
    try:
        document.feed(html)
        document.close()
    except _HTMLResourceLimit as exc:
        return ListingResult(ParseStatus.PARSE_ERROR, (), (str(exc),), fingerprint)
    except (ValueError, AssertionError):
        # 页面格式不对就明确报解析失败，不能当成零条结果。
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("malformed_html",), fingerprint)
    containers = [
        node
        for node in document.root.walk()
        if node.tag == "ul" and "vT-srch-result-list-bid" in (node.attrs.get("class") or "").split()
    ]
    # 先检查是不是验证码或限流页（这种页面可能也带着空列表），再处理结果。
    # 检查时跳过列表条目本身：采购标题里出现"验证码"（比如"验证码系统采购"）是正常的。
    challenge_text = document.root.text(skip_listing_rows=True)
    if any(word in challenge_text for word in ("访问受限", "访问过于频繁", "验证码", "访问频繁")):
        return ListingResult(ParseStatus.BLOCKED, (), ("access_challenge",), fingerprint)
    if len(containers) != 1:
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("unexpected_listing_template",), fingerprint)
    rows = [node for node in containers[0].children if isinstance(node, _Node) and node.tag == "li"]
    if not rows:
        explicit_zero = re.search(r"共\s*0\s*条", document.root.text()) is not None
        return ListingResult(
            ParseStatus.EMPTY if explicit_zero else ParseStatus.PARSE_ERROR,
            (),
            () if explicit_zero else ("empty_without_confirmation",),
            fingerprint,
        )
    items: list[NoticeCandidate] = []
    issues: list[str] = []
    for index, row in enumerate(rows, 1):
        if any(node is not row and node.tag == "li" for node in row.walk()):
            issues.append(f"row_{index}:nested_or_unclosed_listing_item")
            continue
        anchor = next((node for node in row.walk() if node.tag == "a"), None)
        # 只取 li 直接下面的 span，标题里用来高亮关键词的 span 不算。
        metadata = next((node for node in row.children if isinstance(node, _Node) and node.tag == "span"), None)
        url = _notice_url(anchor.attrs.get("href")) if anchor else None
        if anchor is None or not anchor.text() or url is None or metadata is None or not metadata.text():
            issues.append(f"row_{index}:invalid_title_url_or_metadata")
            continue
        raw = metadata.text()
        parts = [part.strip() for part in raw.split("|")]
        fields: dict[str, str] = {}
        for part in parts:
            match = re.fullmatch(r"(采购人|代理机构)[：:]\s*(.+)", part)
            if match:
                fields[match[1]] = match[2].strip()
        # 只取第一段长得像日期的原文。不补时区，也不把它当成投标截止时间。
        published = parts[0] if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s|$)", parts[0]) else None
        items.append(
            NoticeCandidate(
                anchor.text(),
                url,
                raw,
                published,
                fields.get("采购人"),
                fields.get("代理机构"),
                f"ul.vT-srch-result-list-bid > li:nth-of-type({index})",
            )
        )
    status = ParseStatus.PARTIAL if items and issues else (ParseStatus.OK if items else ParseStatus.PARSE_ERROR)
    return ListingResult(status, tuple(items), tuple(issues), fingerprint)


def parse_notice_page(html: str, url: str) -> NoticePage:
    """解析 CCGP 公告正文页，返回标题、正文段落（带位置）、元数据和附件链接。

    只取约定的正文区域，不把整页的导航、页脚当成采购内容。
    附件只记下链接和显示名称，能不能下载由准入规则和 transport 决定。
    模板不认识就明确失败，不会拿 <title> 冒充正文。
    """
    if not isinstance(html, str):
        raise TypeError("html须为已解码文本")
    data = html.encode("utf-8")
    if len(data) > MAX_HTML_BYTES:
        raise ValueError("公告HTML超过本片大小上限")
    fingerprint = sha256(data).hexdigest()
    document = _Document()
    try:
        document.feed(html)
        document.close()
    except _HTMLResourceLimit as exc:
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), (str(exc),), fingerprint)
    except (ValueError, AssertionError):
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("malformed_html",), fingerprint)
    nodes = list(document.root.walk())
    body = [node for node in nodes if "vF_detail_content" in (node.attrs.get("class") or "").split()]
    title_node = next(
        (
            node
            for node in nodes
            if node.tag == "h2" and {"title", "tc"}.intersection((node.attrs.get("class") or "").split())
        ),
        None,
    )
    if title_node is None:
        title_node = next((node for node in nodes if node.tag == "h1"), None)
    # 先检查有没有验证码或访问限制提示。标题里出现"验证码"不算，但页面上有明确的访问提示时，
    # 即使页面上还残留着标题或旧正文，也要按"被拦截"处理。
    title = title_node.text() if title_node else None
    title_challenge = bool(
        title
        and (
            any(
                word in title
                for word in (
                    "请输入验证码",
                    "请填写验证码",
                    "请完成验证",
                    "请通过验证",
                    "访问频繁",
                    "访问过于频繁",
                    "访问受限",
                )
            )
            or title in ("验证码", "人机验证", "安全验证")
        )
    )
    if title_challenge or any(
        word in document.root.text(skip_notice_body=True, omit=title_node if len(body) == 1 else None)
        for word in ("验证码", "访问频繁", "访问过于频繁", "访问受限")
    ):
        return NoticePage(ParseStatus.BLOCKED, None, None, (), ("access_challenge",), fingerprint)
    if len(body) != 1 or not body[0].text():
        return NoticePage(ParseStatus.PARSE_ERROR, None, None, (), ("unexpected_notice_template",), fingerprint)
    attachments: list[dict[str, str]] = []
    links: list[dict[str, str]] = []
    issues = [] if title else ["missing_notice_title"]
    seen = set()
    for anchor in body[0].walk():
        if anchor.tag != "a" or not anchor.attrs.get("href"):
            continue
        href = anchor.attrs["href"]
        try:
            link = urljoin(url, href)
            parts = urlsplit(link)
        except ValueError:
            issues.append("malformed_attachment_link")
            continue
        original = _notice_url(link)
        if original is not None:
            links.append({"url": original, "name": anchor.text(), "locator": "div.vF_detail_content a"})
        if re.search(r"\.(?:pdf|docx?|xlsx?|zip|rar|7z)$", parts.path, re.I) and link not in seen:
            seen.add(link)
            attachments.append(
                {"url": link, "name": anchor.text(), "locator": "div.vF_detail_content a", "status": "not_fetched"}
            )
    # 按段落和表格行分段，每段记下位置，processing 才能准确找到"预算金额""截止时间"这些字段。
    segments = []
    pending = [(body[0], "div.vF_detail_content")]
    while pending:
        node, locator = pending.pop()
        if isinstance(node, str):
            if node.strip():
                segments.append({"text": node.strip(), "locator": locator})
            continue
        # 有些页面把表格嵌在段落里，要继续往下拆到表格行，否则表头单位和行里的数值就对不上了。
        if node.tag in ("p", "li", "tr", "h1", "h2", "h3", "h4") and not any(
            child is not node and child.tag == "table" for child in node.walk()
        ):
            cells = [child.text() for child in node.children if isinstance(child, _Node) and child.tag in ("td", "th")]
            text = " | ".join(cells) if node.tag == "tr" else node.text()
            if text:
                segments.append({"text": text, "locator": locator})
            continue
        children, inline, element_index = [], [], 0

        def flush_inline():
            if inline:
                children.append(("".join(inline), f"{locator} text-run[{len(children) + 1}]"))
                inline.clear()

        for child in node.children:
            if isinstance(child, str):
                inline.append(child)
                continue
            element_index += 1
            if child.tag in ("script", "style"):
                continue
            if child.tag in ("span", "b", "strong", "em", "a", "i", "br"):
                inline.append("\n" if child.tag == "br" else child.text())
                continue
            flush_inline()
            children.append((child, f"{locator} > {child.tag}:nth-child({element_index})"))
        flush_inline()
        pending.extend(reversed(children))
    if not segments:
        segments.append({"text": body[0].text(), "locator": "div.vF_detail_content"})
    # 页面顶部的摘要表只取白名单里的字段；电话、地址等联系信息不收录。
    labels = {
        "采购项目名称",
        "品目",
        "采购单位",
        "采购人",
        "行政区域",
        "公告时间",
        "公告发布时间",
        "预算金额",
        "公告类型",
    }
    metadata = []
    for index, node in enumerate((n for n in nodes if n.tag == "tr"), 1):
        cells = [child for child in node.children if isinstance(child, _Node) and child.tag in ("td", "th")]
        for label, value in zip(cells[::2], cells[1::2]):
            if label.text().rstrip("：:") in labels:
                metadata.append({"label": label.text().rstrip("：:"), "text": value.text(), "locator": f"tr[{index}]"})
    return NoticePage(
        ParseStatus.PARTIAL if issues else ParseStatus.OK,
        title,
        body[0].text(),
        tuple(attachments),
        tuple(issues),
        fingerprint,
        tuple(segments),
        tuple(metadata),
        tuple(links),
    )

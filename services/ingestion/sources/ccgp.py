"""中国政府采购网列表适配小片：只构造参数、解析传入HTML，不联网、不启动任务。

参数映射、列表选择器和元数据拆分改编自 bidding-ai-analyzer（MIT）：
Copyright (c) 2026 ichthyoplanktonzyh
上游提交/文件及完整许可见 third_party/README.md。
本片不证明网站允许自动采集，也不是公告正文、附件或投标资格分析器。
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
SEARCH_URL = "http://search.ccgp.gov.cn/bxsearch"  # 保留上游入口，不声称HTTPS已验证。
MAX_HTML_BYTES = 2 * 1024 * 1024  # 本片的防护上限，不是生产容量测量结果。
_VOID_TAGS = frozenset("area base br col embed hr img input link meta param source track wbr".split())


class ParseStatus(str, Enum):
    """仅描述这份列表HTML的解析结果，不代表商机状态或来源全量覆盖。"""

    OK = "ok"
    EMPTY = "empty"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    PARSE_ERROR = "parse_error"


@dataclass(frozen=True)
class NoticeCandidate:
    """目录线索；保留元数据原文，不猜预算、截止、地区、资格或在招状态。"""

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
    """内容指纹用于关联输入；真正归档、获取时间和版本持久化由后续采集链负责。"""

    status: ParseStatus
    items: tuple[NoticeCandidate, ...]
    issues: tuple[str, ...]
    input_sha256: str


def build_search_params(keyword: str, page: int = 1, *, start_date: str | None = None,
                        end_date: str | None = None) -> dict[str, str]:
    """复用上游查询映射；校验关键词、页码和自然日期，输出参数但不发送请求。

    起止是列表发布日期过滤，不是投标截止。未知日期不自动补当前日期。
    bidType=0保留各类公告，目录层须另行区分更正、意向、结果和潜在在招线索。
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
        "searchtype": "1", "page_index": str(page), "bidSort": "0",
        "buyerName": "", "projectId": "", "pinMu": "0", "bidType": "0",
        "dbselect": "bidx", "kw": keyword.strip(),
        "start_time": start_date.replace("-", ":") if start_date else "",
        "end_time": end_date.replace("-", ":") if end_date else "",
        "timeType": "6", "displayZone": "", "zoneId": "", "pppStatus": "0",
        "agentName": "",
    }


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str | None] = field(default_factory=dict)
    children: list[_Node | str] = field(default_factory=list)

    def walk(self):
        """迭代遍历避免恶意深层嵌套耗尽Python调用栈。"""
        pending = [self]
        while pending:
            node = pending.pop()
            yield node
            pending.extend(child for child in reversed(node.children) if isinstance(child, _Node))

    def text(self) -> str:
        pending: list[_Node | str] = [self]
        parts: list[str] = []
        while pending:
            item = pending.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in ("script", "style"):
                pending.extend(reversed(item.children))
        return re.sub(r"\s+", " ", "".join(parts)).strip()


class _Document(HTMLParser):
    """标准库解析器，仅支持本片约定的列表模板；不加载图片、脚本或外链。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.stack[-1].children.append(_Node(tag, dict(attrs)))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def _notice_url(href: str | None) -> str | None:
    """只接受CCGP公告链接；此校验不代替未来联网时的DNS/重定向/SSRF检查。"""
    if not href or any(ord(char) <= 32 for char in href) or "\\" in href:
        return None
    # 相对链接依据列表页面解析；不擅自改域名修补不明确的链接。
    url = urljoin(SEARCH_URL, href)
    try:
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or parts.hostname not in ("www.ccgp.gov.cn", "ccgp.gov.cn")
                or parts.username is not None or parts.password is not None or parts.port is not None):
            return None
    except ValueError:
        return None
    if not re.fullmatch(r"/cggg/(?:zygg|dfgg)/[A-Za-z0-9_/-]+\.html?", parts.path):
        return None
    return url


def parse_search_page(html: str) -> ListingResult:
    """从上游ul.vT-srch-result-list-bid提取线索；失败不能伪装成零条结果。

    列表和带标签的元数据拆分沿用上游思路；不沿用静默吞异常、固定20条分页、
    最后一段必是地区等假设。原HTML归档后才可把locator作为可追查的位置。
    空结果仅在约定列表容器存在且显式“共0条”时成立，DOM变化时保守报错。
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
    except (ValueError, AssertionError):
        # 畸形声明或不支持的HTML必须暴露为解析失败，不能当作零结果。
        return ListingResult(ParseStatus.PARSE_ERROR, (), ("malformed_html",), fingerprint)
    containers = [node for node in document.root.walk()
                  if node.tag == "ul" and "vT-srch-result-list-bid" in (node.attrs.get("class") or "").split()]
    if len(containers) != 1:
        blocked = any(word in document.root.text() for word in ("访问受限", "访问过于频繁", "验证码", "访问频繁"))
        return ListingResult(ParseStatus.BLOCKED if blocked else ParseStatus.PARSE_ERROR, (),
                             ("access_challenge" if blocked else "unexpected_listing_template",), fingerprint)
    rows = [node for node in containers[0].children if isinstance(node, _Node) and node.tag == "li"]
    if not rows:
        explicit_zero = re.search(r"共\s*0\s*条", document.root.text()) is not None
        return ListingResult(ParseStatus.EMPTY if explicit_zero else ParseStatus.PARSE_ERROR, (),
                             () if explicit_zero else ("empty_without_confirmation",), fingerprint)
    items: list[NoticeCandidate] = []
    issues: list[str] = []
    for index, row in enumerate(rows, 1):
        if any(node is not row and node.tag == "li" for node in row.walk()):
            issues.append(f"row_{index}:nested_or_unclosed_listing_item")
            continue
        anchor = next((node for node in row.walk() if node.tag == "a"), None)
        # 仅用li的直接span，避免把标题内高亮span误作发布时间。
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
        # 只保留具有日期外形的第一段原文，既不填时区，也不当作响应截止。
        published = parts[0] if re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}(?:\s|$)", parts[0]) else None
        items.append(NoticeCandidate(anchor.text(), url, raw, published,
                                     fields.get("采购人"), fields.get("代理机构"),
                                     f"ul.vT-srch-result-list-bid > li:nth-of-type({index})"))
    status = ParseStatus.PARTIAL if items and issues else (ParseStatus.OK if items else ParseStatus.PARSE_ERROR)
    return ListingResult(status, tuple(items), tuple(issues), fingerprint)

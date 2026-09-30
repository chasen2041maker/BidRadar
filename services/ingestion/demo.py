"""纯虚构、无网络的阶段演示；只证明本地链路，不替代真实来源验收。"""
from services.ingestion.archive import utc_now
from services.ingestion.pipeline import request_spec
from services.ingestion.transport import FetchError, FetchResult

NOTICE_URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_99990001.htm"
ATTACHMENT_URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/demo.pdf"


class DemoPolicy:
    attachments_allowed = True

    def authorize(self, url, kind):
        if kind != "attachment" or url != ATTACHMENT_URL:
            raise FetchError("demo_url_not_registered")


class DemoTransport:
    """与真实Transport共享返回契约；没有socket、HTTP或读取外部文件的路径。"""
    policy = DemoPolicy()

    def __init__(self):
        self.calls = []

    def fetch(self, url, *, max_bytes, kind):
        self.calls.append((url, kind))
        if kind == "listing":
            body = ('<ul class="vT-srch-result-list-bid"><li><a href="' + NOTICE_URL
                    + '">虚构软件采购演示</a><span>2026-09-30 | 采购人：虚构单位</span></li></ul>').encode()
            media = "text/html; charset=utf-8"
        elif kind == "notice" and url == NOTICE_URL:
            body = ('<h2 class="tc">虚构软件采购演示</h2><div class="vF_detail_content">'
                    '<p>全部文字仅用于演示，非真实采购机会。</p><a href="' + ATTACHMENT_URL
                    + '">虚构测试附件</a></div>').encode()
            media = "text/html; charset=utf-8"
        elif kind == "attachment" and url == ATTACHMENT_URL:
            body = b"%PDF-1.4\n% synthetic fixture; not a procurement document\n%%EOF"
            media = "application/pdf"
        else:
            raise FetchError("demo_url_not_registered")
        if len(body) > max_bytes:
            raise FetchError("response_too_large")
        return FetchResult(url, url, 200, {"content-type": media}, body, utc_now(), 1)


def demo_request():
    return request_spec(keyword="虚构软件", pages=2, max_notices=2, attachments=True)

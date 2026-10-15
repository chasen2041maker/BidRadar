"""离线演示用的假网络：返回写死的虚构列表页、公告页和附件。

用来在不联网的情况下跑通整条采集流程。演示成功只说明本地流程是通的，不代表真实来源能用。
"""

from bidradar.modules.ingestion.archive import utc_now
from bidradar.modules.ingestion.pipeline import request_spec
from bidradar.modules.ingestion.transport import FetchError, FetchResult

NOTICE_URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t20260930_99990001.htm"
ATTACHMENT_URL = "https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/demo.pdf"


class DemoPolicy:
    """演示用的准入规则：只允许那一个虚构附件。"""

    attachments_allowed = True

    def authorize(self, url, kind):
        if kind != "attachment" or url != ATTACHMENT_URL:
            raise FetchError("demo_url_not_registered")


class DemoTransport:
    """假的抓取器，返回格式和真实的 Transport 一样，但完全不碰网络。"""

    policy = DemoPolicy()

    def __init__(self):
        self.calls = []

    def fetch(self, url, *, max_bytes, kind, checkpoint=None):
        if checkpoint is not None:
            checkpoint()
        self.calls.append((url, kind))
        if kind == "listing":
            body = (
                '<ul class="vT-srch-result-list-bid"><li><a href="'
                + NOTICE_URL
                + '">虚构软件采购演示</a><span>2026-09-30 | 采购人：虚构单位</span></li></ul>'
            ).encode()
            media = "text/html; charset=utf-8"
        elif kind == "notice" and url == NOTICE_URL:
            body = (
                '<h2 class="tc">虚构软件采购演示招标公告</h2><div class="vF_detail_content">'
                "<p>全部文字仅用于演示，非真实采购机会。</p>"
                "<p>项目编号：DEMO-001</p><p>采购单位：虚构单位</p><p>预算金额：12.34万元</p>"
                '<p>投标截止时间：2026-10-10 09:30（北京时间）</p><a href="'
                + ATTACHMENT_URL
                + '">虚构测试附件</a></div>'
            ).encode()
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
    """演示用的采集请求：搜索“虚构软件”，最多 2 页、2 条公告，抓附件。"""
    return request_spec(keyword="虚构软件", pages=2, max_notices=2, attachments=True)

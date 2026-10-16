"""工作台页面和 API 同源提供，带上安全响应头。"""

from fastapi.testclient import TestClient


def test_workbench_files_are_served_with_csp(client: TestClient) -> None:
    for path, kind in [("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")]:
        response = client.get(path)
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind)
        assert "script-src 'self'" in response.headers["content-security-policy"]
    assert '<script src="/app.js"' in client.get("/").text


def test_page_has_no_inline_script_or_style(client: TestClient) -> None:
    """CSP 不允许内联脚本和样式：页面里写了也不会生效，所以干脆不写。"""
    html = client.get("/").text
    assert "<script>" not in html and "style=" not in html and "<style" not in html

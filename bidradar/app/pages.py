"""工作台页面：/、/app.js、/style.css 三个静态文件，和 API 同源。

同源有两个好处：Cookie 和 CSRF 校验不用跨域配置；CSP 可以写死"只允许本站的脚本和样式"。
页面本身不含任何数据，数据都由 app.js 登录后通过 /api/ 读取，所以这三个文件可以公开。
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

STATIC = Path(__file__).parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}

router = APIRouter(include_in_schema=False)


def _page(name: str, media_type: str):
    def serve() -> FileResponse:
        # no-cache：浏览器每次都来问一下有没有新版本（ETag 没变就是 304），改了前端不用强制刷新。
        return FileResponse(STATIC / name, media_type=media_type, headers={"Cache-Control": "no-cache"})

    return serve


for path, (name, media_type) in FILES.items():
    router.add_api_route(path, _page(name, media_type), methods=["GET"])

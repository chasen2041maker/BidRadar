"""API 进程入口：组装 FastAPI 应用。

启动：uv run uvicorn bidradar.app.main:app

create_app() 把配置、数据库、中间件和各模块的路由接到一起。测试用不同的配置调用它，
每次得到一个独立的应用，不会共用连接池，也不会互相影响。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from bidradar.app import health
from bidradar.app.settings import Settings
from bidradar.infra.db import make_engine, make_session_factory
from bidradar.infra.errors import AppError
from bidradar.modules.catalog import api as catalog_api
from bidradar.modules.identity import api as identity_api
from bidradar.modules.workspace import api as workspace_api

logger = logging.getLogger(__name__)

# 错误响应只有错误码和一句通用提示，由前端按错误码翻译成具体的中文说明。
# 服务端的异常细节（SQL、堆栈、别家公司的数据）不进响应，只进日志。
GENERIC_MESSAGE = "请求未完成，请核对权限、输入或服务状态。"

# 安全响应头。CSP 只允许加载本站的脚本和样式：就算公告原文里混进了 <script>，浏览器也不会执行。
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def error_body(code: str) -> dict:
    return {"error": {"code": code, "message": GENERIC_MESSAGE}}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = make_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        # 进程退出时关掉连接池，数据库那边不会留下一堆没人用的连接。
        engine.dispose()

    app = FastAPI(title="BidRadar", version="0.2.0", lifespan=lifespan)
    # 会话工厂挂在应用上，而不是放在全局变量里：这样同一个进程里可以有多个配置不同的应用，测试就是这么用的。
    app.state.session_factory = make_session_factory(engine)
    app.state.settings = settings

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # 接口返回的都是私有数据，浏览器和中间代理都不该缓存。
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError) -> JSONResponse:
        return JSONResponse(error_body(error.code), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # 统一成 400 invalid_request，前端只需要认一种"请求格式不对"。
        # 日志里只记出错的字段和原因，不记字段的值：登录请求的值里有密码。
        logger.info("请求格式不对：%s", [(e["loc"], e["type"]) for e in error.errors()])
        return JSONResponse(error_body("invalid_request"), status_code=400)

    app.include_router(health.router)
    app.include_router(identity_api.router)
    app.include_router(workspace_api.router)
    app.include_router(catalog_api.router)
    return app


app = create_app()

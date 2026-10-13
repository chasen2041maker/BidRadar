"""API 进程入口：组装 FastAPI 应用。

启动：uv run uvicorn bidradar.app.main:app

create_app() 把配置、数据库和路由接到一起。测试用不同的配置调用它，每次得到一个独立的应用，
不会共用连接池，也不会互相影响。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bidradar.app import health
from bidradar.app.settings import Settings
from bidradar.infra.db import make_engine, make_session_factory


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

    app.include_router(health.router)
    # 阶段 1 起，在这里挂上各模块 api.py 里的 router。
    return app


app = create_app()

"""健康检查：GET /health 报告 API 进程在运行，以及数据库能不能连上。

这也是阶段 0 的完整示例：一个请求从路由（这里）→ 依赖（deps.py 给会话）→ 基础设施（infra/db.py 执行 SQL）→ 数据库。

数据库连不上时返回 503，而不是 200 加一个错误字段。
部署平台和负载均衡只看状态码，503 会让它们把流量切到别的实例。
"""

from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from bidradar.app.deps import DbSession
from bidradar.infra.db import ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unreachable"]


@router.get("/health", responses={503: {"model": HealthResponse, "description": "数据库连不上"}})
def health(session: DbSession, response: Response) -> HealthResponse:
    if ping(session):
        return HealthResponse(status="ok", database="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="unavailable", database="unreachable")

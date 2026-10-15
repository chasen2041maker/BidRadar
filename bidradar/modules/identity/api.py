"""identity 的 HTTP 路由：登录、退出、读取当前会话。

这里不写业务规则，也不直接读写数据库。会话令牌和 CSRF 令牌只出现在 Set-Cookie 里，不出现在响应正文：
正文可能被前端日志、浏览器扩展看到，HttpOnly 的 Cookie 网页脚本读不到。
"""

from fastapi import APIRouter, Request, Response

from bidradar.app.deps import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AppSettings,
    CurrentUser,
    DbSession,
    check_origin,
    session_token,
)
from bidradar.modules.identity import service
from bidradar.modules.identity.models import SESSION_TTL
from bidradar.modules.identity.schemas import (
    EmptyRequest,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    SessionResponse,
    UserOut,
    WorkspaceOut,
)
from bidradar.modules.workspace import service as workspace

router = APIRouter(prefix="/api", tags=["identity"])


def _set_cookies(response: Response, session_value: str, csrf_value: str, max_age: int, secure: bool) -> None:
    # br_session：HttpOnly，网页脚本读不到，被注入了脚本也偷不走会话。
    # br_csrf：前端要读它放进请求头，所以不能 HttpOnly；别的网站读不到它，这就够了。
    # SameSite=Strict：从别的网站点链接过来的请求不带这两个 Cookie。
    response.set_cookie(
        SESSION_COOKIE, session_value, max_age=max_age, path="/", httponly=True, samesite="strict", secure=secure
    )
    response.set_cookie(CSRF_COOKIE, csrf_value, max_age=max_age, path="/", samesite="strict", secure=secure)


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response, session: DbSession, settings: AppSettings):
    """登录成功就换一个新会话；浏览器里如果还带着旧会话，顺手注销掉，防止会话固定攻击。"""
    check_origin(request)
    result = service.login(session, body.username, body.password)
    old = session_token(request)
    if old:
        service.logout(session, old)
    _set_cookies(
        response,
        result.session_token,
        result.csrf_token,
        int(SESSION_TTL.total_seconds()),
        settings.cookie_secure,
    )
    return LoginResponse(user=UserOut.of(result.user))


@router.post("/logout")
def logout(body: EmptyRequest, request: Request, response: Response, user: CurrentUser, session: DbSession):
    service.logout(session, session_token(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return LogoutResponse()


@router.get("/session")
def current_session(user: CurrentUser, session: DbSession) -> SessionResponse:
    """当前用户和他所在的公司。前端打开页面时先调它，401 就跳到登录页。"""
    return SessionResponse(
        user=UserOut.of(user),
        workspaces=[
            WorkspaceOut(workspace_id=w.workspace_id, name=w.name, role=w.role.value)
            for w in workspace.list_workspaces(session, user.id)
        ],
    )

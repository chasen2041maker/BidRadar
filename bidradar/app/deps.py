"""FastAPI 的依赖：路由需要的东西从这里取（数据库会话、配置、当前用户）。

路由只声明"我需要一个会话""我需要当前用户"，不关心它们从哪来。
测试时用 app.dependency_overrides 把会话换成跑在回滚事务里的会话，路由代码一行都不用改。

一个需要登录的请求，在进入路由之前要过这几关（get_current_user）：
    1. Cookie 里恰好有一个 br_session                → 否则 401
    2. 会话存在且没过期                                → 否则 401
    3. 修改类请求（POST 等）：Origin 是本站           → 否则 403
    4. 修改类请求：X-CSRF-Token 和会话匹配            → 否则 403
之后由各模块的 service 用 workspace.authorize() 检查"此人能否在这家公司做这件事"。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Path, Request
from sqlalchemy.orm import Session

from bidradar.app.settings import Settings
from bidradar.infra.errors import AppError
from bidradar.modules.identity import service as identity
from bidradar.modules.identity.service import User

SESSION_COOKIE = "br_session"
CSRF_COOKIE = "br_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_session(request: Request) -> Iterator[Session]:
    """每个请求一个会话，请求结束时关闭。

    这里不提交事务。一个业务动作什么时候算做完，只有 service 知道，所以由 service 提交。
    没提交的改动在会话关闭时自动回滚。
    """
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


# 路由参数写成 `session: DbSession`，FastAPI 就会调用 get_session 注入会话。
DbSession = Annotated[Session, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def session_token(request: Request) -> str | None:
    """从 Cookie 里取会话令牌。出现多个 br_session 就当作没有。

    为什么要专门挡重复：不同程序遇到重复的 Cookie，有的取第一个、有的取最后一个。
    攻击者如果能在子域名下种一个同名 Cookie，就可能让两边认成两个不同的会话。
    """
    raw = request.headers.get("cookie", "")
    if sum(part.strip().startswith(SESSION_COOKIE + "=") for part in raw.split(";")) != 1:
        return None
    return request.cookies.get(SESSION_COOKIE)


def check_origin(request: Request) -> None:
    """修改类请求必须来自本站页面。Origin 头由浏览器填写，网页脚本改不了。

    Host 头已经由 TrustedHostMiddleware 限制在白名单里（见 app/main.py），这里用它拼出本站的地址。
    """
    expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
    if request.headers.get("origin") != expected:
        raise AppError("invalid_origin", 403)


def get_current_user(request: Request, session: DbSession) -> User:
    token = session_token(request)
    user = identity.current_user(session, token)
    if request.method not in SAFE_METHODS:
        check_origin(request)
        identity.check_csrf(session, token, request.headers.get(CSRF_HEADER))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def workspace_id_param(workspace_id: Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]) -> str:
    """路径里的公司 ID。只检查格式；此人是不是这家公司的成员，由各模块 service 里的 authorize() 检查。

    TODO（练习 1，项目负责人主写）：行级安全（RLS）的第二道防线从这里接入。
    每个事务开始时执行 SET LOCAL app.workspace_id = '<这个 ID>'，再给私有表加 RLS 策略。
    要求和提示见 docs/exercises.md。
    """
    return workspace_id


WorkspaceId = Annotated[str, Depends(workspace_id_param)]

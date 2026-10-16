"""identity 的业务用例：一个函数对应一个业务动作，由它决定什么时候提交事务。

别的模块只能调用这个文件里的公开函数：
    current_user()   会话令牌 → 当前用户（每个需要登录的请求都会调）
    check_csrf()     修改类请求的 CSRF 检查
    get_users()      按 ID 批量查用户的公开信息（比如成员列表要显示名字）

阅读顺序：login() → current_user() → check_csrf() → logout()。
"""

from collections.abc import Iterable

from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.errors import AppError
from bidradar.infra.ids import new_id
from bidradar.modules.identity import models, repo
from bidradar.modules.identity.models import LoginResult, User
from bidradar.modules.identity.tables import SessionRow, UserRow


def _unauthenticated() -> AppError:
    return AppError("authentication_required", 401)


def create_user(session: Session, username: str, password: str, display_name: str) -> User:
    """新建用户。只给命令行初始化用（python -m bidradar seed-demo），没有对应的网页接口。

    用户名已存在就报错，绝不重置已有用户的密码。
    """
    username = models.normalize_username(username)
    password = models.check_new_password(password)
    if not isinstance(display_name, str) or not 0 < len(display_name.strip()) <= 100:
        raise AppError("invalid_display_name")
    if repo.get_user_by_username(session, username) is not None:
        raise AppError("user_exists", 409)
    row = UserRow(
        id=new_id(),
        username=username,
        display_name=display_name.strip(),
        password_hash=models.hash_password(password),
        created_at=clock.now(),
    )
    repo.add_user(session, row)
    session.commit()
    return repo.to_user(row)


def find_user(session: Session, username: str) -> User | None:
    """按用户名找用户，给命令行用。"""
    row = repo.get_user_by_username(session, models.normalize_username(username))
    return repo.to_user(row) if row else None


def login(session: Session, username: object, password: object) -> LoginResult:
    """登录。成功返回用户和两个新令牌；失败统一报 invalid_credentials，被锁报 login_limited。

    几条规则：
    - 用户名不存在、格式不对、密码错误，返回同一个错误、花同样的时间，别人猜不出哪个用户名存在。
    - 同一个用户名 15 分钟内失败 5 次就锁住，到窗口结束前连正确的密码也不放行。
    - 失败次数要提交了再报错：如果先报错，事务回滚，刚记下的失败就没了，限流形同虚设。
    """
    now = clock.now()
    valid_name = isinstance(username, str) and models.USERNAME_PATTERN.fullmatch(username) is not None
    name = username.casefold() if valid_name else "<invalid>"
    supplied = password if isinstance(password, str) and 0 < len(password) <= models.PASSWORD_MAX_LENGTH else ""

    key = models.token_hash(name)
    limit = repo.lock_login_limit(session, key, now)
    if models.login_locked(limit.window_started, limit.failures, now):
        session.commit()
        raise AppError("login_limited", 429)

    row = repo.get_user_by_username(session, name) if valid_name else None
    accepted = models.verify_password(supplied, row.password_hash if row else None) and bool(supplied)
    if row is None or not accepted:
        limit.window_started, limit.failures = models.after_failure(limit.window_started, limit.failures, now)
        session.commit()
        raise AppError("invalid_credentials", 401)

    repo.delete_login_limit(session, key)
    repo.delete_expired_sessions(session, now)
    session_token, csrf_token = models.new_token(), models.new_token()
    expires_at = now + models.SESSION_TTL
    repo.add_session(
        session,
        SessionRow(
            token_hash=models.token_hash(session_token),
            csrf_hash=models.token_hash(csrf_token),
            user_id=row.id,
            created_at=now,
            expires_at=expires_at,
        ),
    )
    session.commit()
    return LoginResult(
        user=repo.to_user(row), session_token=session_token, csrf_token=csrf_token, expires_at=expires_at
    )


def current_user(session: Session, session_token: object) -> User:
    """会话令牌对应的当前用户。令牌格式不对、不存在、已过期，都报 401。

    只读，不提交事务：调用它的请求接下来还要在同一个会话里做自己的事。
    """
    if not models.valid_token(session_token):
        raise _unauthenticated()
    row = repo.get_session_user(session, models.token_hash(session_token), clock.now())
    if row is None:
        raise _unauthenticated()
    return repo.to_user(row)


def check_csrf(session: Session, session_token: object, csrf_token: object) -> None:
    """修改类请求的第二道检查：请求头里的 CSRF 令牌必须和这个会话登录时发的那个一致。

    为什么 Cookie 之外还要它：别的网站可以诱导浏览器带着 Cookie 发请求，但读不到 br_csrf 这个 Cookie 的值，
    也就没法把它放进请求头。
    """
    if not models.valid_token(session_token):
        raise _unauthenticated()
    row = repo.get_session(session, models.token_hash(session_token))
    if row is None or row.expires_at <= clock.now():
        raise _unauthenticated()
    if not models.valid_token(csrf_token) or models.token_hash(csrf_token) != row.csrf_hash:
        raise AppError("csrf_failed", 403)


def logout(session: Session, session_token: object) -> None:
    """注销：删掉会话。令牌无效也算成功，注销本来就是想让这个令牌失效。"""
    if models.valid_token(session_token):
        repo.delete_session(session, models.token_hash(session_token))
        session.commit()


def get_users(session: Session, user_ids: Iterable[str]) -> dict[str, User]:
    """按 ID 批量查用户的公开信息。查不到的 ID 不出现在结果里。"""
    return {row.id: repo.to_user(row) for row in repo.get_users(session, user_ids)}

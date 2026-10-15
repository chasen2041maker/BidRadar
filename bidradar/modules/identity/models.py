"""identity 的领域对象和业务规则：用户名格式、密码哈希、会话有效期、登录限流。

纯 Python，不 import FastAPI 或 SQLAlchemy，不连数据库也能直接单测（tests_v2/identity/test_models.py）。
"""

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from bidradar.infra.errors import AppError

# 会话 8 小时过期。工作台是办公时间里用的工具，一个工作日登录一次。
SESSION_TTL = timedelta(hours=8)

# 防暴力破解：同一个用户名 15 分钟内失败 5 次，就锁到这 15 分钟过完。
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURE_LIMIT = 5

# PBKDF2-SHA256 的迭代次数，参考 OWASP 2023 年的建议值。迭代次数存在哈希里，以后调高也认得旧哈希。
# 用标准库而不是 argon2/bcrypt：少一个需要编译的依赖；这个强度对内部工具足够。
PASSWORD_ITERATIONS = 600_000
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 1024

USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.@-]{3,64}")
# secrets.token_urlsafe(32) 生成的令牌正好 43 个字符。格式不对的令牌直接当作"没登录"，不去查库。
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class User:
    """用户的公开信息。不含密码哈希，可以直接返回给前端。"""

    id: str
    username: str
    display_name: str


@dataclass(frozen=True)
class LoginResult:
    """登录成功的结果。两个令牌只放进 Cookie，不出现在响应正文里。"""

    user: User
    session_token: str
    csrf_token: str
    expires_at: datetime


def normalize_username(value: object) -> str:
    """检查用户名格式，统一转小写。Demo.Admin 和 demo.admin 是同一个人。"""
    if not isinstance(value, str) or not USERNAME_PATTERN.fullmatch(value):
        raise AppError("invalid_username")
    return value.casefold()


def check_new_password(value: object) -> str:
    """新建用户时的密码规则：12 到 1024 个字符，不含空字符。"""
    if not isinstance(value, str) or not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH or "\x00" in value:
        raise AppError("invalid_password")
    return value


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str, *, salt: bytes | None = None, iterations: int | None = None) -> str:
    """生成密码哈希，格式：pbkdf2_sha256$迭代次数$盐$哈希。每个用户一个随机盐，相同密码的哈希也不同。"""
    salt = salt if salt is not None else secrets.token_bytes(16)
    iterations = iterations or PASSWORD_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str | None) -> bool:
    """核对密码。encoded 为 None（用户不存在）时照样算一次哈希再返回 False。

    这样"用户不存在"和"密码错误"花的时间一样，别人没法靠响应快慢猜出某个用户名是否存在。
    """
    if encoded is None:
        hash_password(password, salt=b"\x00" * 16)
        return False
    try:
        algorithm, iterations, salt, expected = encoded.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hash_password(password, salt=base64.b64decode(salt), iterations=int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(computed.split("$")[3], expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """令牌只存 SHA-256。数据库泄露了，拿到的哈希也不能直接当 Cookie 用。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token(value: object) -> bool:
    return isinstance(value, str) and TOKEN_PATTERN.fullmatch(value) is not None


def login_locked(window_started: datetime | None, failures: int, now: datetime) -> bool:
    """这个用户名现在是否被锁：窗口还没过完，并且失败次数已经到上限。"""
    return window_started is not None and now - window_started < LOGIN_WINDOW and failures >= LOGIN_FAILURE_LIMIT


def after_failure(window_started: datetime | None, failures: int, now: datetime) -> tuple[datetime, int]:
    """记一次失败以后的窗口起点和失败次数。上一个窗口已经过完，就从这次失败重新开始计数。"""
    if window_started is None or now - window_started >= LOGIN_WINDOW:
        return now, 1
    return window_started, failures + 1

"""工作台服务（workspace）的数据库和业务规则：用户、登录、公司、成员、企业档案、商机选择。

这个文件现在一身兼了好几层（校验、权限、业务规则、SQL 都在 WorkspaceStore 里），
v2 计划把它拆开，见 docs/v2-blueprint.md 第 4 节。

主要概念：
    - 用户通过"成员关系"进入公司。角色有三种：管理员（admin）、成员（member）、只读（viewer）。
    - 企业档案采用"提议 → 确认"两步：成员和管理员都能提修改，只有管理员能确认成正式版本。
      每次确认生成一个新的版本号，旧版本永远保留。
    - 选择（selection）：从商机目录里挑出想研究的公告，确认后才能发起研究。

两种使用方式：
    - HTTP 请求：每个请求新建一个实例，并用 bind_session 绑定当前登录会话。
      之后所有操作都会核对"操作人就是会话里的这个人"。
    - 命令行初始化和测试：不绑定会话，可以直接创建用户和公司（bootstrap_*）。

阅读顺序：login / bind_session → _authorize → propose_profile / confirm_profile → create_selection。
现在用 SQLite 做本地开发，正式的身份系统、PostgreSQL 和行级安全（RLS）都还没做。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import pbkdf2_hmac, sha256
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import urlsplit


PASSWORD_ITERATIONS = 600_000  # PBKDF2 迭代次数
SESSION_SECONDS = 8 * 60 * 60  # 登录会话有效期：8 小时
LOGIN_WINDOW_SECONDS = 15 * 60  # 登录失败计数的时间窗口：15 分钟
LOGIN_FAILURE_LIMIT = 5  # 窗口内最多失败 5 次，超过就暂时锁定
LIST_LIMIT = 200  # 列表接口最多返回的条数
# 企业档案的九个字段及各自的长度上限。
PROFILE_LIMITS = {
    "company_name": 200,
    "city": 200,
    "project_types": 4000,
    "capabilities": 4000,
    "delivery_constraints": 4000,
    "cases": 4000,
    "qualifications": 4000,
    "staffing": 4000,
    "commercial_constraints": 4000,
}
ROLES = frozenset(("admin", "member", "viewer"))
WRITERS = frozenset(("admin", "member"))  # 能修改数据的角色（只读成员除外）


class WorkspaceError(Exception):
    """工作台的业务错误。只带错误码和 HTTP 状态码，绝不带 SQL、密码、令牌或私有数据。"""

    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    """字符串的 SHA-256。会话令牌和 CSRF 令牌只存哈希，不存原文。"""
    return sha256(value.encode("utf-8")).hexdigest()


def _id():
    """随机生成一个 32 位十六进制 ID。"""
    return secrets.token_hex(16)


def _now():
    """当前时间，整数秒。"""
    return int(time.time())


def _utc(epoch):
    """时间戳转成 UTC 的 ISO 8601 字符串。"""
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value, limit, *, empty=False):
    """检查是合法的字符串：不超长、不含空字符；empty=False 时不能全是空白。"""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()) or "\x00" in value:
        raise WorkspaceError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError("invalid_input") from None
    return value


def _integer(value, minimum=0):
    """检查是范围内的整数（bool 不算）。"""
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise WorkspaceError("invalid_version")
    return value


def _username(value):
    """检查用户名格式，返回统一转成小写后的用户名。"""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", value):
        raise WorkspaceError("invalid_username")
    return value.casefold()


def _profile_payload(value):
    """检查并整理企业档案：九个字段都要有，去掉首尾空白，空字符串转成 None。"""
    if not isinstance(value, dict) or set(value) != set(PROFILE_LIMITS):
        raise WorkspaceError("invalid_profile")
    result = {}
    for key, limit in PROFILE_LIMITS.items():
        item = value[key]
        if item is None and key != "company_name":
            result[key] = None
        else:
            _text(item, limit, empty=key != "company_name")
            # 空着表示"还没填"，不是"没有"。没填案例不代表没有案例，没填资质不代表没有资质。
            result[key] = item.strip() or None
    if len(_json(result).encode("utf-8")) > 64 * 1024:
        raise WorkspaceError("profile_too_large", 413)
    return result


def _items(value):
    """检查选择里的公告列表格式（1–20 条，每条含 ID、标题、来源链接）。

    这里只查格式。公告是否真实存在、是不是当前版本，由调用方先向目录服务核实。
    """
    keys = {"notice_id", "observation_id", "title", "source_url", "simulation"}
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise WorkspaceError("invalid_selection_items")
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != keys or type(item["simulation"]) is not bool:
            raise WorkspaceError("invalid_selection_items")
        for field in ("notice_id", "observation_id"):
            if not isinstance(item[field], str) or not re.fullmatch(r"[0-9a-f]{64}", item[field]):
                raise WorkspaceError("invalid_selection_items")
        if item["notice_id"] in seen:
            raise WorkspaceError("duplicate_selection_notice")
        seen.add(item["notice_id"])
        _text(item["title"], 2000, empty=True)
        url = _text(item["source_url"], 4096)
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.username
                or parts.password
                or any(ord(char) <= 32 for char in url)
                or "\\" in url
            ):
                raise ValueError()
        except ValueError:
            raise WorkspaceError("invalid_selection_url") from None
        result.append(dict(item))
    return result


def _item_references(value):
    """检查重放请求里的公告列表：只含公告 ID 和观察 ID。

    重放时只认 ID，标题和链接以第一次核实时保存的为准，不能被客户端改掉。
    """
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise WorkspaceError("invalid_selection_items")
    seen, result = set(), []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"notice_id", "observation_id"}:
            raise WorkspaceError("invalid_selection_items")
        if any(not isinstance(item[key], str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]) for key in item):
            raise WorkspaceError("invalid_selection_items")
        if item["notice_id"] in seen:
            raise WorkspaceError("duplicate_selection_notice")
        seen.add(item["notice_id"])
        result.append(dict(item))
    return result


def _checked_at(value, now):
    """目录核实时间：没传就用当前时间；传了必须是 UTC 时间。"""
    if value is None:
        return _utc(now)
    try:
        _text(value, 40)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        raise WorkspaceError("invalid_catalog_checked_at") from None


# 表结构。几张历史表（成员变更、档案提议和版本、选择历史、命令、事件）只能插入，
# 不能修改或删除，由构造函数里创建的触发器保证。
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users(
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
        password_salt BLOB NOT NULL, password_hash BLOB NOT NULL, password_iterations INTEGER NOT NULL,
        created_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS workspaces(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, current_profile_revision INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS memberships(
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), user_id TEXT NOT NULL REFERENCES users(id),
        role TEXT NOT NULL CHECK(role IN ('admin','member','viewer')),
        active INTEGER NOT NULL CHECK(active IN (0,1)), updated_at INTEGER NOT NULL,
        version INTEGER NOT NULL CHECK(version>=1),
        PRIMARY KEY(workspace_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS membership_events(
        workspace_id TEXT NOT NULL, user_id TEXT NOT NULL, version INTEGER NOT NULL,
        actor_id TEXT REFERENCES users(id), actor_type TEXT NOT NULL CHECK(actor_type IN ('bootstrap','user')),
        old_role TEXT, old_active INTEGER CHECK(old_active IN (0,1)),
        new_role TEXT NOT NULL CHECK(new_role IN ('admin','member','viewer')),
        new_active INTEGER NOT NULL CHECK(new_active IN (0,1)), recorded_at INTEGER NOT NULL,
        PRIMARY KEY(workspace_id,user_id,version),
        FOREIGN KEY(workspace_id,user_id) REFERENCES memberships(workspace_id,user_id))""",
    """CREATE TABLE IF NOT EXISTS sessions(
        token_hash TEXT PRIMARY KEY, csrf_hash TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS login_limits(
        username_key TEXT PRIMARY KEY, window_started INTEGER NOT NULL, failures INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS profile_proposals(
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id),
        base_revision INTEGER NOT NULL, payload TEXT NOT NULL, proposed_by TEXT NOT NULL REFERENCES users(id),
        created_at INTEGER NOT NULL, UNIQUE(workspace_id,id))""",
    """CREATE TABLE IF NOT EXISTS profile_revisions(
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), revision INTEGER NOT NULL,
        proposal_id TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
        confirmed_by TEXT NOT NULL REFERENCES users(id), confirmed_at INTEGER NOT NULL,
        PRIMARY KEY(workspace_id,revision),
        FOREIGN KEY(workspace_id,proposal_id) REFERENCES profile_proposals(workspace_id,id))""",
    """CREATE TABLE IF NOT EXISTS selections(
        id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), profile_revision INTEGER NOT NULL,
        items TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
        current_version INTEGER NOT NULL, UNIQUE(workspace_id,id),
        FOREIGN KEY(workspace_id,profile_revision) REFERENCES profile_revisions(workspace_id,revision))""",
    """CREATE TABLE IF NOT EXISTS selection_history(
        workspace_id TEXT NOT NULL, selection_id TEXT NOT NULL, version INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('awaiting_decision','selected','cancelled')),
        actor_id TEXT NOT NULL REFERENCES users(id), recorded_at INTEGER NOT NULL, catalog_checked_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id,selection_id,version),
        FOREIGN KEY(workspace_id,selection_id) REFERENCES selections(workspace_id,id))""",
    """CREATE TABLE IF NOT EXISTS commands(
        workspace_id TEXT NOT NULL REFERENCES workspaces(id), operation TEXT NOT NULL, command_key TEXT NOT NULL,
        request_hash TEXT NOT NULL, result TEXT NOT NULL, created_at INTEGER NOT NULL,
        PRIMARY KEY(workspace_id,operation,command_key))""",
    "CREATE INDEX IF NOT EXISTS member_user ON memberships(user_id,active)",
    "CREATE INDEX IF NOT EXISTS proposal_workspace ON profile_proposals(workspace_id,created_at)",
    "CREATE INDEX IF NOT EXISTS selection_workspace ON selections(workspace_id,created_at)",
    """CREATE TABLE IF NOT EXISTS owner_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL, workspace_id TEXT NOT NULL, actor_id TEXT,
        target_user_id TEXT, profile_revision INTEGER, membership_version INTEGER,
        occurred_at TEXT NOT NULL)""",
)


class WorkspaceStore:
    """工作台的数据存储和业务操作。root 是数据目录，里面放 workspace.sqlite3。"""

    def __init__(self, root):
        root = Path(root).absolute()
        self.db = None
        self._session_bound, self._session_hash = False, None
        try:
            if root.is_symlink():
                raise WorkspaceError("invalid_store_directory")
            root.mkdir(parents=True, exist_ok=True)
            marker = root / ".bidradar-workspace-v1"
            paths = (marker, root / "workspace.sqlite3", root / "workspace.sqlite3-wal", root / "workspace.sqlite3-shm")
            if any(path.is_symlink() for path in paths):
                raise WorkspaceError("invalid_store_directory")
            if not marker.exists() and any(root.iterdir()):
                raise WorkspaceError("store_directory_not_empty")
            marker.touch(exist_ok=True)
            self.db = sqlite3.connect(root / "workspace.sqlite3", isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self._transaction(write=True):
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2):
                    raise WorkspaceError("unsupported_store_version", 503)
                for statement in SCHEMA:
                    self.db.execute(statement)
                # 从第 1 版升级时，把已有的成员变更和档案确认补记成事件，供 tracking 服务消费。
                # 事件 ID 由业务内容决定，重复执行也不会插出重复的事件。
                if version < 2:
                    self.db.execute(
                        "INSERT OR IGNORE INTO owner_events(event_id,event_type,workspace_id,actor_id,target_user_id,"
                        "membership_version,occurred_at) SELECT 'access:'||workspace_id||':'||user_id||':'||version,"
                        "'AccessChanged',workspace_id,actor_id,user_id,version,"
                        "strftime('%Y-%m-%dT%H:%M:%SZ',recorded_at,'unixepoch') FROM membership_events ORDER BY recorded_at,workspace_id,user_id,version"
                    )
                    self.db.execute(
                        "INSERT OR IGNORE INTO owner_events(event_id,event_type,workspace_id,actor_id,profile_revision,"
                        "occurred_at) SELECT 'profile:'||workspace_id||':'||revision,'ProfileConfirmed',workspace_id,"
                        "confirmed_by,revision,strftime('%Y-%m-%dT%H:%M:%SZ',confirmed_at,'unixepoch') "
                        "FROM profile_revisions ORDER BY confirmed_at,workspace_id,revision"
                    )
                # 用触发器防止历史记录被程序误改。这只是防误操作，挡不住能直接改数据库文件的人。
                for table in (
                    "membership_events",
                    "profile_proposals",
                    "profile_revisions",
                    "selection_history",
                    "commands",
                    "owner_events",
                ):
                    for action in ("UPDATE", "DELETE"):
                        self.db.execute(
                            f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                            f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_record'); END"
                        )
                self.db.execute("PRAGMA user_version=2")
        except (OSError, sqlite3.Error):
            self.close()
            raise WorkspaceError("storage_unavailable", 503) from None
        except WorkspaceError:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def _transaction(self, *, write=False):
        """开启事务。写事务一开始就拿写锁，再检查会话、角色和版本。

        这样撤权、注销、两个管理员同时确认档案这类操作，都会按先后顺序执行，不会交错。
        """
        if self.db is None:
            raise WorkspaceError("store_closed", 503)
        try:
            self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield
            self.db.commit()
        except WorkspaceError:
            self.db.rollback()
            raise
        except (sqlite3.Error, json.JSONDecodeError):
            self.db.rollback()
            raise WorkspaceError("storage_unavailable", 503) from None
        except BaseException:
            self.db.rollback()
            raise

    @staticmethod
    def _user(row):
        """用户的公开信息：ID、用户名、显示名。"""
        return {key: row[key] for key in ("id", "username", "display_name")}

    @staticmethod
    def _token_hash(token):
        """检查会话令牌格式，返回它的哈希。格式不对按"未登录"处理。"""
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise WorkspaceError("authentication_required", 401)
        return _hash(token)

    def _session_user(self, token_hash):
        """按令牌哈希找到未过期的会话对应的用户。"""
        row = self.db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",
            (token_hash, _now()),
        ).fetchone()
        if row is None:
            raise WorkspaceError("authentication_required", 401)
        return self._user(row)

    def _check_user(self, user_id):
        """确认用户存在；如果绑定了会话，还要确认会话仍有效、并且操作人就是会话里的人。"""
        if self._session_bound:
            user = self._session_user(self._session_hash)
            if user["id"] != user_id:
                raise WorkspaceError("authentication_required", 401)
        if not isinstance(user_id, str) or not self.db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise WorkspaceError("authentication_required", 401)

    def _authorize(self, user_id, workspace_id, roles=ROLES):
        """确认此人是这家公司的有效成员，并且角色在 roles 里。返回角色。"""
        self._check_user(user_id)
        if not isinstance(workspace_id, str):
            raise WorkspaceError("not_found", 404)
        row = self.db.execute(
            "SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND active=1", (workspace_id, user_id)
        ).fetchone()
        # 公司不存在、不是本公司成员、已被移出，一律返回"不存在"，
        # 免得别人通过不同的错误信息猜出某家公司是否存在。
        if row is None:
            raise WorkspaceError("not_found", 404)
        if row["role"] not in roles:
            raise WorkspaceError("forbidden", 403)
        return row["role"]

    @staticmethod
    def _bounded(cursor):
        """读取查询结果，超过 LIST_LIMIT 条就报错，而不是悄悄截断。"""
        rows = cursor.fetchmany(LIST_LIMIT + 1)
        if len(rows) > LIST_LIMIT:
            raise WorkspaceError("list_limit_exceeded", 413)
        return rows

    def _bootstrap_only(self):
        """bootstrap_* 只能在命令行里用；通过网页请求（绑定了会话）调用会被拒绝。"""
        if self._session_bound:
            raise WorkspaceError("bootstrap_requires_cli", 403)

    def bootstrap_user(self, username, password, display_name):
        """创建用户（仅命令行）。用户名已存在就报错，绝不会重置已有用户的密码。"""
        self._bootstrap_only()
        username = _username(username)
        _text(password, 1024)
        if len(password) < 12:
            raise WorkspaceError("password_too_short")
        _text(display_name, 100)
        salt = secrets.token_bytes(16)
        digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
        with self._transaction(write=True):
            if self.db.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                raise WorkspaceError("user_exists", 409)
            user_id = _id()
            self.db.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                (user_id, username, display_name, salt, digest, PASSWORD_ITERATIONS, _now()),
            )
            return user_id

    def bootstrap_workspace(self, name, admin_user_id):
        """创建公司，并把指定用户设为第一个管理员（仅命令行）。"""
        self._bootstrap_only()
        _text(name, 200)
        with self._transaction(write=True):
            self._check_user(admin_user_id)
            workspace_id, now = _id(), _now()
            self.db.execute("INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)", (workspace_id, name, now))
            self.db.execute(
                "INSERT INTO memberships VALUES(?,?,?,?,?,?)", (workspace_id, admin_user_id, "admin", 1, now, 1)
            )
            self._membership_event(workspace_id, admin_user_id, 1, None, None, None, "admin", True, now)
            return workspace_id

    def bootstrap_member(self, workspace_id, user_id, role):
        """把用户加入公司（仅命令行）。"""
        self._bootstrap_only()
        if not isinstance(role, str) or role not in ROLES:
            raise WorkspaceError("invalid_role")
        with self._transaction(write=True):
            self._check_user(user_id)
            if not self.db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise WorkspaceError("not_found", 404)
            if self.db.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)
            ).fetchone():
                raise WorkspaceError("member_exists", 409)
            now = _now()
            self.db.execute("INSERT INTO memberships VALUES(?,?,?,?,?,?)", (workspace_id, user_id, role, 1, now, 1))
            self._membership_event(workspace_id, user_id, 1, None, None, None, role, True, now)

    def login(self, username, password):
        """登录，成功返回会话令牌、CSRF 令牌和用户信息。

        防暴力破解：同一用户名 15 分钟内失败 5 次就暂时锁定。
        防探测用户名：用户名不存在、格式不对、密码错误，返回的错误和耗时都一样。
        """
        valid_name = isinstance(username, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username)
        name = username.casefold() if valid_name else "<invalid>"
        valid_password = isinstance(password, str) and 0 < len(password) <= 1024
        if valid_password:
            try:
                password.encode("utf-8")
            except UnicodeError:
                valid_password = False
        supplied = password if valid_password else "<invalid>"
        result, failure = None, None
        with self._transaction(write=True):
            now, key = _now(), _hash(name)
            limit = self.db.execute("SELECT * FROM login_limits WHERE username_key=?", (key,)).fetchone()
            if (
                limit
                and now - limit["window_started"] < LOGIN_WINDOW_SECONDS
                and limit["failures"] >= LOGIN_FAILURE_LIMIT
            ):
                failure = WorkspaceError("login_limited", 429)
            else:
                row = (
                    self.db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone() if valid_name else None
                )
                # 用户不存在也照样算一次密码哈希，让响应时间和"密码错误"一样。
                salt = bytes(row["password_salt"]) if row else b"\x00" * 16
                iterations = row["password_iterations"] if row else PASSWORD_ITERATIONS
                computed = pbkdf2_hmac("sha256", supplied.encode("utf-8"), salt, iterations)
                expected = bytes(row["password_hash"]) if row else b"\x00" * len(computed)
                accepted = hmac.compare_digest(computed, expected) and row is not None and valid_password
                if not accepted:
                    started = (
                        limit["window_started"]
                        if limit and now - limit["window_started"] < LOGIN_WINDOW_SECONDS
                        else now
                    )
                    failures = limit["failures"] + 1 if limit and started == limit["window_started"] else 1
                    self.db.execute(
                        "INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(username_key) DO UPDATE "
                        "SET window_started=excluded.window_started,failures=excluded.failures",
                        (key, started, failures),
                    )
                    failure = WorkspaceError("invalid_credentials", 401)
                else:
                    self.db.execute("DELETE FROM login_limits WHERE username_key=?", (key,))
                    self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    self.db.execute(
                        "INSERT INTO sessions VALUES(?,?,?,?,?)",
                        (_hash(token), _hash(csrf), row["id"], now, now + SESSION_SECONDS),
                    )
                    result = {"token": token, "csrf_token": csrf, "user": self._user(row)}
        # 在事务外抛出失败：如果在事务里抛，刚记下的失败次数会被回滚。
        if failure is not None:
            raise failure
        return result

    def authenticate(self, token):
        """用会话令牌查当前用户。"""
        token_hash = self._token_hash(token)
        with self._transaction():
            return self._session_user(token_hash)

    def bind_session(self, token):
        """把这个实例绑定到一个登录会话。之后每个操作都会核对会话仍有效、操作人是会话本人。"""
        self._session_bound, self._session_hash = True, None
        self._session_hash = self._token_hash(token)
        with self._transaction():
            return self._session_user(self._session_hash)

    def check_csrf(self, token, csrf):
        """检查 CSRF 令牌和会话匹配。所有修改类请求都要过这一关。"""
        token_hash = self._token_hash(token)
        with self._transaction():
            self._session_user(token_hash)
            if self._session_bound and token_hash != self._session_hash:
                raise WorkspaceError("authentication_required", 401)
            row = self.db.execute("SELECT csrf_hash FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if (
                not isinstance(csrf, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf)
                or not hmac.compare_digest(row["csrf_hash"], _hash(csrf))
            ):
                raise WorkspaceError("csrf_failed", 403)

    def logout(self, token):
        """注销：删除会话。"""
        token_hash = self._token_hash(token)
        with self._transaction(write=True):
            if self._session_bound and token_hash != self._session_hash:
                raise WorkspaceError("authentication_required", 401)
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def list_workspaces(self, user_id):
        """此人有效加入的所有公司，以及在每家公司的角色。"""
        with self._transaction():
            self._check_user(user_id)
            rows = self._bounded(
                self.db.execute(
                    "SELECT w.id AS workspace_id,w.name,m.role FROM workspaces w "
                    "JOIN memberships m ON m.workspace_id=w.id WHERE m.user_id=? AND m.active=1 ORDER BY w.created_at,w.id",
                    (user_id,),
                )
            )
            return [dict(row) for row in rows]

    def members(self, user_id, workspace_id):
        """本公司的全部成员（含已移出的）。"""
        with self._transaction():
            self._authorize(user_id, workspace_id)
            rows = self._bounded(
                self.db.execute(
                    "SELECT m.user_id,u.username,u.display_name,m.role,m.active,m.version FROM memberships m "
                    "JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.username",
                    (workspace_id,),
                )
            )
            return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def _membership_event(
        self, workspace_id, target_user_id, version, actor_id, old_role, old_active, role, active, now
    ):
        """记录一次成员变更：写审计记录，并发一个 AccessChanged 事件给 tracking。

        和变更本身在同一个事务里提交。命令行初始化记为 bootstrap，不冒充成管理员操作。
        """
        self.db.execute(
            "INSERT INTO membership_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                workspace_id,
                target_user_id,
                version,
                actor_id,
                "bootstrap" if actor_id is None else "user",
                old_role,
                old_active,
                role,
                int(active),
                now,
            ),
        )
        self.db.execute(
            "INSERT INTO owner_events(event_id,event_type,workspace_id,actor_id,target_user_id,membership_version,occurred_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                f"access:{workspace_id}:{target_user_id}:{version}",
                "AccessChanged",
                workspace_id,
                actor_id,
                target_user_id,
                version,
                _utc(now),
            ),
        )

    def member_history(self, user_id, workspace_id, target_user_id):
        """管理员查看某个成员的变更历史。已被移出的成员，历史仍然保留在公司里。"""
        with self._transaction():
            self._authorize(user_id, workspace_id, {"admin"})
            if not self.db.execute(
                "SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, target_user_id)
            ).fetchone():
                raise WorkspaceError("not_found", 404)
            rows = self._bounded(
                self.db.execute(
                    "SELECT * FROM membership_events WHERE workspace_id=? AND user_id=? ORDER BY version",
                    (workspace_id, target_user_id),
                )
            )
            return [
                {
                    **dict(row),
                    "old_active": None if row["old_active"] is None else bool(row["old_active"]),
                    "new_active": bool(row["new_active"]),
                    "recorded_at": _utc(row["recorded_at"]),
                }
                for row in rows
            ]

    def change_member(self, user_id, workspace_id, target_user_id, role, active, expected_version):
        """管理员修改成员的角色或启用状态。公司至少要保留一个有效管理员。"""
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, {"admin"})
            if not isinstance(role, str) or role not in ROLES or type(active) is not bool:
                raise WorkspaceError("invalid_membership")
            expected_version = _integer(expected_version, 1)
            target = self.db.execute(
                "SELECT m.*,u.username,u.display_name FROM memberships m JOIN users u ON u.id=m.user_id "
                "WHERE m.workspace_id=? AND m.user_id=?",
                (workspace_id, target_user_id),
            ).fetchone()
            if target is None:
                raise WorkspaceError("not_found", 404)
            # 先核对版本号。管理员 A 刚把某人移出，管理员 B 用没刷新的旧页面改角色，
            # 如果不核对版本，就会把已移出的人"复活"。
            if target["version"] != expected_version:
                raise WorkspaceError("membership_version_conflict", 409)
            if target["active"] and target["role"] == "admin" and (not active or role != "admin"):
                count = self.db.execute(
                    "SELECT count(*) FROM memberships WHERE workspace_id=? AND active=1 AND role='admin'",
                    (workspace_id,),
                ).fetchone()[0]
                if count <= 1:
                    raise WorkspaceError("last_admin", 409)
            now, version = _now(), expected_version + 1
            self.db.execute(
                "UPDATE memberships SET role=?,active=?,updated_at=?,version=? WHERE workspace_id=? AND user_id=?",
                (role, int(active), now, version, workspace_id, target_user_id),
            )
            self._membership_event(
                workspace_id, target_user_id, version, user_id, target["role"], target["active"], role, active, now
            )
            return {
                "user_id": target_user_id,
                "username": target["username"],
                "display_name": target["display_name"],
                "role": role,
                "active": active,
                "version": version,
            }

    def _current_revision(self, workspace_id):
        """公司当前正式档案的版本号；0 表示还没有正式档案。"""
        return self.db.execute(
            "SELECT current_profile_revision FROM workspaces WHERE id=?", (workspace_id,)
        ).fetchone()[0]

    def authorize(self, user_id, workspace_id, action):
        """供其他服务调用：此人能否在这家公司执行 action（read / analyze / track / admin）。

        返回角色、成员版本号和当前档案版本。成员版本号每次变更都加一，
        其他服务据此判断"权限有没有变过"：被移出后再加回来，版本号也不同了，旧任务不会因此恢复。
        """
        if action not in ("read", "analyze", "track", "admin"):
            raise WorkspaceError("invalid_action")
        roles = ROLES if action == "read" else ({"admin"} if action == "admin" else WRITERS)
        with self._transaction():
            role = self._authorize(user_id, workspace_id, roles)
            version = self.db.execute(
                "SELECT version FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)
            ).fetchone()[0]
            return {
                "role": role,
                "membership_version": version,
                "current_profile_revision": self._current_revision(workspace_id),
            }

    def context(self, user_id, workspace_id, profile_revision, selection_id=None):
        """供研究服务调用：返回指定版本的档案（以及选择），同时告诉调用方当前最新的版本号。"""
        with self._transaction():
            self._authorize(user_id, workspace_id)
            _integer(profile_revision, 1)
            row = self.db.execute(
                "SELECT * FROM profile_revisions WHERE workspace_id=? AND revision=?", (workspace_id, profile_revision)
            ).fetchone()
            if row is None:
                raise WorkspaceError("not_found", 404)
            selection = self._selection(workspace_id, _text(selection_id, 128)) if selection_id is not None else None
            return {
                "profile": self._revision(row),
                "current_profile_revision": self._current_revision(workspace_id),
                "selection": selection,
            }

    def events(self, after=0, limit=100):
        """供 tracking 服务拉取事件（档案确认、成员变更），按序号分页。事件里不含档案内容。"""
        _integer(after)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise WorkspaceError("invalid_limit")
        with self._transaction():
            high = self.db.execute("SELECT coalesce(max(seq),0) FROM owner_events").fetchone()[0]
            if after > high:
                raise WorkspaceError("cursor_ahead")
            rows = self.db.execute(
                "SELECT * FROM owner_events WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?", (after, high, limit)
            ).fetchall()
            return {
                "schema_version": 1,
                "items": [dict(r) for r in rows],
                "next_after": rows[-1]["seq"] if rows else after,
                "high_watermark": high,
            }

    def _command(self, workspace_id, operation, key, payload):
        """查幂等键：返回 (请求指纹, 当时的结果或 None)。同一个键但内容不同，报冲突。

        返回的是这个命令当时的结果，不一定是对象的最新状态。调用前要先检查权限。
        """
        _text(key, 128)
        request_hash = _hash(_json(payload))
        row = self.db.execute(
            "SELECT * FROM commands WHERE workspace_id=? AND operation=? AND command_key=?",
            (workspace_id, operation, key),
        ).fetchone()
        if row and row["request_hash"] != request_hash:
            raise WorkspaceError("idempotency_conflict", 409)
        return request_hash, json.loads(row["result"]) if row else None

    def _remember(self, workspace_id, operation, key, request_hash, result):
        """记下幂等键对应的结果。"""
        self.db.execute(
            "INSERT INTO commands VALUES(?,?,?,?,?,?)",
            (workspace_id, operation, key, request_hash, _json(result), _now()),
        )
        return result

    @staticmethod
    def _revision(row):
        """一个正式档案版本。附带的几个状态说明：内容由企业确认，但证明材料没有提供，也没有核验过。"""
        return {
            "workspace_id": row["workspace_id"],
            "revision": row["revision"],
            "proposal_id": row["proposal_id"],
            "payload": json.loads(row["payload"]),
            "confirmed_by": row["confirmed_by"],
            "confirmed_at": _utc(row["confirmed_at"]),
            "declaration_status": "user_confirmed",
            "proof_status": "not_provided",
            "read_status": "not_attempted",
            "validity_status": "not_checked",
        }

    def _proposal(self, row):
        """一个档案修改提议，以及它是否已被确认成正式版本。"""
        confirmed = self.db.execute(
            "SELECT revision FROM profile_revisions WHERE workspace_id=? AND proposal_id=?",
            (row["workspace_id"], row["id"]),
        ).fetchone()
        return {
            "id": row["id"],
            "workspace_id": row["workspace_id"],
            "base_revision": row["base_revision"],
            "payload": json.loads(row["payload"]),
            "proposed_by": row["proposed_by"],
            "created_at": _utc(row["created_at"]),
            "status": "confirmed" if confirmed else "proposed",
            "confirmed_revision": confirmed[0] if confirmed else None,
        }

    def profile(self, user_id, workspace_id):
        """当前正式档案、全部历史版本和全部提议。"""
        with self._transaction():
            self._authorize(user_id, workspace_id)
            revisions = self._bounded(
                self.db.execute(
                    "SELECT * FROM profile_revisions WHERE workspace_id=? ORDER BY revision DESC", (workspace_id,)
                )
            )
            proposals = self._bounded(
                self.db.execute(
                    "SELECT * FROM profile_proposals WHERE workspace_id=? ORDER BY created_at DESC,id", (workspace_id,)
                )
            )
            history = [self._revision(row) for row in revisions]
            return {
                "current": history[0] if history else None,
                "history": history,
                "proposals": [self._proposal(row) for row in proposals],
            }

    def propose_profile(self, user_id, workspace_id, payload, base_revision, key):
        """提交一份档案修改提议。base_revision 必须是当前正式版本，否则说明别人刚改过，需要刷新。"""
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            payload, base_revision = _profile_payload(payload), _integer(base_revision)
            request_hash, cached = self._command(
                workspace_id, "propose_profile", key, [user_id, payload, base_revision]
            )
            if cached is not None:
                return cached
            if self._current_revision(workspace_id) != base_revision:
                raise WorkspaceError("profile_revision_conflict", 409)
            proposal_id = _id()
            self.db.execute(
                "INSERT INTO profile_proposals VALUES(?,?,?,?,?,?)",
                (proposal_id, workspace_id, base_revision, _json(payload), user_id, _now()),
            )
            result = self._proposal(
                self.db.execute("SELECT * FROM profile_proposals WHERE id=?", (proposal_id,)).fetchone()
            )
            return self._remember(workspace_id, "propose_profile", key, request_hash, result)

    def confirm_profile(self, user_id, workspace_id, proposal_id, expected_revision, key):
        """管理员把一份提议确认为新的正式版本，并发出 ProfileConfirmed 事件。"""
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, {"admin"})
            _text(proposal_id, 128)
            expected_revision = _integer(expected_revision)
            request_hash, cached = self._command(
                workspace_id, "confirm_profile", key, [user_id, proposal_id, expected_revision]
            )
            if cached is not None:
                return cached
            proposal = self.db.execute(
                "SELECT * FROM profile_proposals WHERE workspace_id=? AND id=?", (workspace_id, proposal_id)
            ).fetchone()
            if proposal is None:
                raise WorkspaceError("not_found", 404)
            if self.db.execute("SELECT 1 FROM profile_revisions WHERE proposal_id=?", (proposal_id,)).fetchone():
                raise WorkspaceError("proposal_already_confirmed", 409)
            if (
                self._current_revision(workspace_id) != expected_revision
                or proposal["base_revision"] != expected_revision
            ):
                raise WorkspaceError("profile_revision_conflict", 409)
            revision = expected_revision + 1
            self.db.execute(
                "INSERT INTO profile_revisions VALUES(?,?,?,?,?,?)",
                (workspace_id, revision, proposal_id, proposal["payload"], user_id, _now()),
            )
            self.db.execute("UPDATE workspaces SET current_profile_revision=? WHERE id=?", (revision, workspace_id))
            self.db.execute(
                "INSERT INTO owner_events(event_id,event_type,workspace_id,actor_id,profile_revision,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"profile:{workspace_id}:{revision}",
                    "ProfileConfirmed",
                    workspace_id,
                    user_id,
                    revision,
                    _utc(_now()),
                ),
            )
            result = self._revision(
                self.db.execute(
                    "SELECT * FROM profile_revisions WHERE workspace_id=? AND revision=?", (workspace_id, revision)
                ).fetchone()
            )
            return self._remember(workspace_id, "confirm_profile", key, request_hash, result)

    def _selection(self, workspace_id, selection_id):
        """一个选择的完整信息：公告列表、当前状态和状态历史。"""
        row = self.db.execute(
            "SELECT * FROM selections WHERE workspace_id=? AND id=?", (workspace_id, selection_id)
        ).fetchone()
        if row is None:
            raise WorkspaceError("not_found", 404)
        events = self._bounded(
            self.db.execute(
                "SELECT * FROM selection_history WHERE workspace_id=? AND selection_id=? ORDER BY version",
                (workspace_id, selection_id),
            )
        )
        history = [
            {
                "version": event["version"],
                "state": event["state"],
                "actor_id": event["actor_id"],
                "recorded_at": _utc(event["recorded_at"]),
                "catalog_checked_at": event["catalog_checked_at"],
            }
            for event in events
        ]
        current = history[-1]
        return {
            "id": row["id"],
            "workspace_id": workspace_id,
            "profile_revision": row["profile_revision"],
            "items": json.loads(row["items"]),
            "created_by": row["created_by"],
            "created_at": _utc(row["created_at"]),
            "state": current["state"],
            "version": current["version"],
            "catalog_checked_at": current["catalog_checked_at"],
            "updated_at": current["recorded_at"],
            "history": history,
        }

    def selection(self, user_id, workspace_id, selection_id):
        """查看一个选择。"""
        with self._transaction():
            self._authorize(user_id, workspace_id)
            _text(selection_id, 128)
            return self._selection(workspace_id, selection_id)

    def selections(self, user_id, workspace_id):
        """本公司的全部选择，新的在前。"""
        with self._transaction():
            self._authorize(user_id, workspace_id)
            rows = self._bounded(
                self.db.execute(
                    "SELECT id FROM selections WHERE workspace_id=? ORDER BY created_at DESC,id", (workspace_id,)
                )
            )
            return [self._selection(workspace_id, row["id"]) for row in rows]

    def create_selection(self, user_id, workspace_id, profile_revision, items, key, *, checked_at=None):
        """新建一个选择，状态为 awaiting_decision（等待确认）。

        公告列表应当已经由调用方向目录服务核实过。这里把它原样保存下来，
        并核对会话、角色和档案版本；目录在别的服务里，这个事务锁不住它。
        """
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            profile_revision, items = _integer(profile_revision, 1), _items(items)
            request_hash, cached = self._command(
                workspace_id, "create_selection", key, [user_id, profile_revision, items]
            )
            if cached is not None:
                return cached
            if self._current_revision(workspace_id) != profile_revision:
                raise WorkspaceError("profile_revision_conflict", 409)
            now, selection_id = _now(), _id()
            checked = _checked_at(checked_at, now)
            self.db.execute(
                "INSERT INTO selections VALUES(?,?,?,?,?,?,?)",
                (selection_id, workspace_id, profile_revision, _json(items), user_id, now, 1),
            )
            self.db.execute(
                "INSERT INTO selection_history VALUES(?,?,?,?,?,?,?)",
                (workspace_id, selection_id, 1, "awaiting_decision", user_id, now, checked),
            )
            return self._remember(
                workspace_id, "create_selection", key, request_hash, self._selection(workspace_id, selection_id)
            )

    def replay_selection(self, user_id, workspace_id, profile_revision, items, key):
        """重复提交同一个创建请求时，直接返回第一次的结果；没有记录就返回 None。

        这一步不需要目录服务在线，但照样检查当前会话和成员权限。
        """
        with self._transaction():
            self._authorize(user_id, workspace_id, WRITERS)
            profile_revision, references = _integer(profile_revision, 1), _item_references(items)
            _text(key, 128)
            row = self.db.execute(
                "SELECT result FROM commands WHERE workspace_id=? AND operation='create_selection' AND command_key=?",
                (workspace_id, key),
            ).fetchone()
            if row is None:
                return None
            cached = json.loads(row["result"])
            recorded = [{name: item[name] for name in ("notice_id", "observation_id")} for item in cached["items"]]
            if cached["profile_revision"] != profile_revision or recorded != references:
                raise WorkspaceError("idempotency_conflict", 409)
            # 用当时保存的完整内容和当前操作人重新算一次指纹：别的成员拿同一个键来，指纹对不上。
            _, result = self._command(
                workspace_id, "create_selection", key, [user_id, profile_revision, cached["items"]]
            )
            return result

    def _transition_selection(self, user_id, workspace_id, selection_id, expected_version, key, state, checked_at=None):
        """把选择从 awaiting_decision 改成 selected（确认）或 cancelled（取消）。"""
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            _text(selection_id, 128)
            expected_version = _integer(expected_version, 1)
            operation = "confirm_selection" if state == "selected" else "cancel_selection"
            request_hash, cached = self._command(
                workspace_id, operation, key, [user_id, selection_id, expected_version]
            )
            if cached is not None:
                return cached
            current = self._selection(workspace_id, selection_id)
            if current["version"] != expected_version or current["state"] != "awaiting_decision":
                raise WorkspaceError("selection_version_conflict", 409)
            if state == "selected" and self._current_revision(workspace_id) != current["profile_revision"]:
                raise WorkspaceError("profile_revision_conflict", 409)
            now = _now()
            checked = _checked_at(checked_at, now) if state == "selected" else current["catalog_checked_at"]
            self.db.execute(
                "INSERT INTO selection_history VALUES(?,?,?,?,?,?,?)",
                (workspace_id, selection_id, expected_version + 1, state, user_id, now, checked),
            )
            self.db.execute(
                "UPDATE selections SET current_version=? WHERE workspace_id=? AND id=?",
                (expected_version + 1, workspace_id, selection_id),
            )
            return self._remember(
                workspace_id, operation, key, request_hash, self._selection(workspace_id, selection_id)
            )

    def confirm_selection(self, user_id, workspace_id, selection_id, expected_version, key, *, checked_at=None):
        """确认选择。确认后才能对其中的公告发起研究。"""
        return self._transition_selection(
            user_id, workspace_id, selection_id, expected_version, key, "selected", checked_at
        )

    def cancel_selection(self, user_id, workspace_id, selection_id, expected_version, key):
        """取消选择。"""
        return self._transition_selection(user_id, workspace_id, selection_id, expected_version, key, "cancelled")

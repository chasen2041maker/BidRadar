"""R1-A 的本地虚构企业工作台存储；不代替 PostgreSQL、RLS 或生产身份服务。

阅读顺序：bind_session/_authorize → propose_profile/confirm_profile → 选择会话转换。
HTTP 每请求独立实例并绑定会话；CLI 预置和测试可以使用未绑定的可信实例。
私有数据仅在本库内事务写入；目录快照由调用方通过服务契约核实，不跨读目录库。
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


PASSWORD_ITERATIONS = 600_000
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURE_LIMIT = 5
LIST_LIMIT = 200
PROFILE_LIMITS = {"company_name": 200, "city": 200, "project_types": 4000, "capabilities": 4000,
                  "delivery_constraints": 4000, "cases": 4000, "qualifications": 4000,
                  "staffing": 4000, "commercial_constraints": 4000}
ROLES = frozenset(("admin", "member", "viewer"))
WRITERS = frozenset(("admin", "member"))


class WorkspaceError(Exception):
    """边界异常只给稳定代码与HTTP状态，不回传SQL、密码、令牌或私有对象正文。"""
    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return sha256(value.encode("utf-8")).hexdigest()


def _id():
    return secrets.token_hex(16)


def _now():
    return int(time.time())


def _utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value, limit, *, empty=False):
    if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
            or "\x00" in value):
        raise WorkspaceError("invalid_input")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise WorkspaceError("invalid_input") from None
    return value


def _integer(value, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**31 - 1:
        raise WorkspaceError("invalid_version")
    return value


def _username(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", value):
        raise WorkspaceError("invalid_username")
    return value.casefold()


def _profile_payload(value):
    if not isinstance(value, dict) or set(value) != set(PROFILE_LIMITS):
        raise WorkspaceError("invalid_profile")
    result = {}
    for key, limit in PROFILE_LIMITS.items():
        item = value[key]
        if item is None and key != "company_name":
            result[key] = None
        else:
            _text(item, limit, empty=key != "company_name")
            # 空白代表尚未提供，不能自动解释为“没有案例/没有资质”。
            result[key] = item.strip() or None
    if len(_json(result).encode("utf-8")) > 64 * 1024:
        raise WorkspaceError("profile_too_large", 413)
    return result


def _items(value):
    """此处校验快照契约；ID存在性/当前观察须由可信目录客户端先核实。"""
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
            if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password
                    or any(ord(char) <= 32 for char in url) or "\\" in url):
                raise ValueError()
        except ValueError:
            raise WorkspaceError("invalid_selection_url") from None
        result.append(dict(item))
    return result


def _item_references(value):
    """HTTP重放请求仅含稳定ID，不能用客户端标题/URL取代原先已核实的快照。"""
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
)


class WorkspaceStore:
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
                if version not in (0, 1):
                    raise WorkspaceError("unsupported_store_version", 503)
                for statement in SCHEMA:
                    self.db.execute(statement)
                # 只保护本库的版本记录不被误更新；不是对有磁盘权限者的安全隔离。
                for table in ("membership_events", "profile_proposals", "profile_revisions", "selection_history", "commands"):
                    for action in ("UPDATE", "DELETE"):
                        self.db.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action.lower()} "
                                        f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'immutable_record'); END")
                self.db.execute("PRAGMA user_version=1")
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
        """写事务先占写锁再查会话/角色/版本，撤权、注销与双确认按同一数据库排序。"""
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
        return {key: row[key] for key in ("id", "username", "display_name")}

    @staticmethod
    def _token_hash(token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise WorkspaceError("authentication_required", 401)
        return _hash(token)

    def _session_user(self, token_hash):
        row = self.db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                              "WHERE s.token_hash=? AND s.expires_at>?", (token_hash, _now())).fetchone()
        if row is None:
            raise WorkspaceError("authentication_required", 401)
        return self._user(row)

    def _check_user(self, user_id):
        if self._session_bound:
            user = self._session_user(self._session_hash)
            if user["id"] != user_id:
                raise WorkspaceError("authentication_required", 401)
        if not isinstance(user_id, str) or not self.db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise WorkspaceError("authentication_required", 401)

    def _authorize(self, user_id, workspace_id, roles=ROLES):
        self._check_user(user_id)
        if not isinstance(workspace_id, str):
            raise WorkspaceError("not_found", 404)
        row = self.db.execute("SELECT role FROM memberships WHERE workspace_id=? AND user_id=? AND active=1",
                              (workspace_id, user_id)).fetchone()
        # 未知公司、跨公司和已撤权统一返回不存在，避免用错误差异探测私有对象。
        if row is None:
            raise WorkspaceError("not_found", 404)
        if row["role"] not in roles:
            raise WorkspaceError("forbidden", 403)
        return row["role"]

    @staticmethod
    def _bounded(cursor):
        rows = cursor.fetchmany(LIST_LIMIT + 1)
        if len(rows) > LIST_LIMIT:
            raise WorkspaceError("list_limit_exceeded", 413)
        return rows

    def _bootstrap_only(self):
        if self._session_bound:
            raise WorkspaceError("bootstrap_requires_cli", 403)

    def bootstrap_user(self, username, password, display_name):
        """仅可信CLI预置；重复用户名冲突，绝不重置既有密码或展示旧凭据。"""
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
            self.db.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?)",
                            (user_id, username, display_name, salt, digest, PASSWORD_ITERATIONS, _now()))
            return user_id

    def bootstrap_workspace(self, name, admin_user_id):
        self._bootstrap_only()
        _text(name, 200)
        with self._transaction(write=True):
            self._check_user(admin_user_id)
            workspace_id, now = _id(), _now()
            self.db.execute("INSERT INTO workspaces(id,name,created_at) VALUES(?,?,?)", (workspace_id, name, now))
            self.db.execute("INSERT INTO memberships VALUES(?,?,?,?,?,?)", (workspace_id, admin_user_id, "admin", 1, now, 1))
            self._membership_event(workspace_id, admin_user_id, 1, None, None, None, "admin", True, now)
            return workspace_id

    def bootstrap_member(self, workspace_id, user_id, role):
        self._bootstrap_only()
        if not isinstance(role, str) or role not in ROLES:
            raise WorkspaceError("invalid_role")
        with self._transaction(write=True):
            self._check_user(user_id)
            if not self.db.execute("SELECT 1 FROM workspaces WHERE id=?", (workspace_id,)).fetchone():
                raise WorkspaceError("not_found", 404)
            if self.db.execute("SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, user_id)).fetchone():
                raise WorkspaceError("member_exists", 409)
            now = _now()
            self.db.execute("INSERT INTO memberships VALUES(?,?,?,?,?,?)", (workspace_id, user_id, role, 1, now, 1))
            self._membership_event(workspace_id, user_id, 1, None, None, None, role, True, now)

    def login(self, username, password):
        # 非法格式也走同一失败结果；不泄漏用户名是否存在。HTTP还需限制请求体/来源。
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
            if limit and now - limit["window_started"] < LOGIN_WINDOW_SECONDS and limit["failures"] >= LOGIN_FAILURE_LIMIT:
                failure = WorkspaceError("login_limited", 429)
            else:
                row = self.db.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone() if valid_name else None
                # 未知用户也执行同成本KDF，不能靠跳过哈希明显区分存在性。
                salt = bytes(row["password_salt"]) if row else b"\x00" * 16
                iterations = row["password_iterations"] if row else PASSWORD_ITERATIONS
                computed = pbkdf2_hmac("sha256", supplied.encode("utf-8"), salt, iterations)
                expected = bytes(row["password_hash"]) if row else b"\x00" * len(computed)
                accepted = hmac.compare_digest(computed, expected) and row is not None and valid_password
                if not accepted:
                    started = limit["window_started"] if limit and now - limit["window_started"] < LOGIN_WINDOW_SECONDS else now
                    failures = limit["failures"] + 1 if limit and started == limit["window_started"] else 1
                    self.db.execute("INSERT INTO login_limits VALUES(?,?,?) ON CONFLICT(username_key) DO UPDATE "
                                    "SET window_started=excluded.window_started,failures=excluded.failures", (key, started, failures))
                    failure = WorkspaceError("invalid_credentials", 401)
                else:
                    self.db.execute("DELETE FROM login_limits WHERE username_key=?", (key,))
                    self.db.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
                    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                    self.db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (_hash(token), _hash(csrf), row["id"], now, now + SESSION_SECONDS))
                    result = {"token": token, "csrf_token": csrf, "user": self._user(row)}
        # 失败计数先提交再抛业务异常；在事务内抛出会把限流记录回滚掉。
        if failure is not None:
            raise failure
        return result

    def authenticate(self, token):
        token_hash = self._token_hash(token)
        with self._transaction():
            return self._session_user(token_hash)

    def bind_session(self, token):
        self._session_bound, self._session_hash = True, None
        self._session_hash = self._token_hash(token)
        with self._transaction():
            return self._session_user(self._session_hash)

    def check_csrf(self, token, csrf):
        token_hash = self._token_hash(token)
        with self._transaction():
            self._session_user(token_hash)
            if self._session_bound and token_hash != self._session_hash:
                raise WorkspaceError("authentication_required", 401)
            row = self.db.execute("SELECT csrf_hash FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not isinstance(csrf, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", csrf) or not hmac.compare_digest(row["csrf_hash"], _hash(csrf)):
                raise WorkspaceError("csrf_failed", 403)

    def logout(self, token):
        token_hash = self._token_hash(token)
        with self._transaction(write=True):
            if self._session_bound and token_hash != self._session_hash:
                raise WorkspaceError("authentication_required", 401)
            self.db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))

    def list_workspaces(self, user_id):
        with self._transaction():
            self._check_user(user_id)
            rows = self._bounded(self.db.execute("SELECT w.id AS workspace_id,w.name,m.role FROM workspaces w "
                "JOIN memberships m ON m.workspace_id=w.id WHERE m.user_id=? AND m.active=1 ORDER BY w.created_at,w.id", (user_id,)))
            return [dict(row) for row in rows]

    def members(self, user_id, workspace_id):
        with self._transaction():
            self._authorize(user_id, workspace_id)
            rows = self._bounded(self.db.execute("SELECT m.user_id,u.username,u.display_name,m.role,m.active,m.version FROM memberships m "
                "JOIN users u ON u.id=m.user_id WHERE m.workspace_id=? ORDER BY u.username", (workspace_id,)))
            return [{**dict(row), "active": bool(row["active"])} for row in rows]

    def _membership_event(self, workspace_id, target_user_id, version, actor_id, old_role, old_active, role, active, now):
        # CLI初始化明确记bootstrap，不伪造管理员操作；变更和审计必须同事务提交。
        self.db.execute("INSERT INTO membership_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (workspace_id, target_user_id, version, actor_id, "bootstrap" if actor_id is None else "user",
                         old_role, old_active, role, int(active), now))

    def member_history(self, user_id, workspace_id, target_user_id):
        """当前管理员读取本公司成员审计，已撤权成员的既有记录仍属于公司。"""
        with self._transaction():
            self._authorize(user_id, workspace_id, {"admin"})
            if not self.db.execute("SELECT 1 FROM memberships WHERE workspace_id=? AND user_id=?", (workspace_id, target_user_id)).fetchone():
                raise WorkspaceError("not_found", 404)
            rows = self._bounded(self.db.execute("SELECT * FROM membership_events WHERE workspace_id=? AND user_id=? ORDER BY version",
                                                (workspace_id, target_user_id)))
            return [{**dict(row), "old_active": None if row["old_active"] is None else bool(row["old_active"]),
                     "new_active": bool(row["new_active"]), "recorded_at": _utc(row["recorded_at"])} for row in rows]

    def change_member(self, user_id, workspace_id, target_user_id, role, active, expected_version):
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, {"admin"})
            if not isinstance(role, str) or role not in ROLES or type(active) is not bool:
                raise WorkspaceError("invalid_membership")
            expected_version = _integer(expected_version, 1)
            target = self.db.execute("SELECT m.*,u.username,u.display_name FROM memberships m JOIN users u ON u.id=m.user_id "
                                    "WHERE m.workspace_id=? AND m.user_id=?", (workspace_id, target_user_id)).fetchone()
            if target is None:
                raise WorkspaceError("not_found", 404)
            # 保存角色同样带旧active值；先比较整体版本，避免旧页面意外复活被撤权成员。
            if target["version"] != expected_version:
                raise WorkspaceError("membership_version_conflict", 409)
            if target["active"] and target["role"] == "admin" and (not active or role != "admin"):
                count = self.db.execute("SELECT count(*) FROM memberships WHERE workspace_id=? AND active=1 AND role='admin'", (workspace_id,)).fetchone()[0]
                if count <= 1:
                    raise WorkspaceError("last_admin", 409)
            now, version = _now(), expected_version + 1
            self.db.execute("UPDATE memberships SET role=?,active=?,updated_at=?,version=? WHERE workspace_id=? AND user_id=?",
                            (role, int(active), now, version, workspace_id, target_user_id))
            self._membership_event(workspace_id, target_user_id, version, user_id, target["role"], target["active"], role, active, now)
            return {"user_id": target_user_id, "username": target["username"], "display_name": target["display_name"],
                    "role": role, "active": active, "version": version}

    def _current_revision(self, workspace_id):
        return self.db.execute("SELECT current_profile_revision FROM workspaces WHERE id=?", (workspace_id,)).fetchone()[0]

    def _command(self, workspace_id, operation, key, payload):
        # 缓存是该命令当时的结果，不冒充对象当前状态；授权必须由调用方先重验。
        _text(key, 128)
        request_hash = _hash(_json(payload))
        row = self.db.execute("SELECT * FROM commands WHERE workspace_id=? AND operation=? AND command_key=?", (workspace_id, operation, key)).fetchone()
        if row and row["request_hash"] != request_hash:
            raise WorkspaceError("idempotency_conflict", 409)
        return request_hash, json.loads(row["result"]) if row else None

    def _remember(self, workspace_id, operation, key, request_hash, result):
        self.db.execute("INSERT INTO commands VALUES(?,?,?,?,?,?)", (workspace_id, operation, key, request_hash, _json(result), _now()))
        return result

    @staticmethod
    def _revision(row):
        return {"workspace_id": row["workspace_id"], "revision": row["revision"], "proposal_id": row["proposal_id"],
                "payload": json.loads(row["payload"]), "confirmed_by": row["confirmed_by"], "confirmed_at": _utc(row["confirmed_at"]),
                "declaration_status": "user_confirmed", "proof_status": "not_provided", "read_status": "not_attempted", "validity_status": "not_checked"}

    def _proposal(self, row):
        confirmed = self.db.execute("SELECT revision FROM profile_revisions WHERE workspace_id=? AND proposal_id=?", (row["workspace_id"], row["id"])).fetchone()
        return {"id": row["id"], "workspace_id": row["workspace_id"], "base_revision": row["base_revision"],
                "payload": json.loads(row["payload"]), "proposed_by": row["proposed_by"], "created_at": _utc(row["created_at"]),
                "status": "confirmed" if confirmed else "proposed", "confirmed_revision": confirmed[0] if confirmed else None}

    def profile(self, user_id, workspace_id):
        with self._transaction():
            self._authorize(user_id, workspace_id)
            revisions = self._bounded(self.db.execute("SELECT * FROM profile_revisions WHERE workspace_id=? ORDER BY revision DESC", (workspace_id,)))
            proposals = self._bounded(self.db.execute("SELECT * FROM profile_proposals WHERE workspace_id=? ORDER BY created_at DESC,id", (workspace_id,)))
            history = [self._revision(row) for row in revisions]
            return {"current": history[0] if history else None, "history": history, "proposals": [self._proposal(row) for row in proposals]}

    def propose_profile(self, user_id, workspace_id, payload, base_revision, key):
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            payload, base_revision = _profile_payload(payload), _integer(base_revision)
            request_hash, cached = self._command(workspace_id, "propose_profile", key, [user_id, payload, base_revision])
            if cached is not None:
                return cached
            if self._current_revision(workspace_id) != base_revision:
                raise WorkspaceError("profile_revision_conflict", 409)
            proposal_id = _id()
            self.db.execute("INSERT INTO profile_proposals VALUES(?,?,?,?,?,?)", (proposal_id, workspace_id, base_revision, _json(payload), user_id, _now()))
            result = self._proposal(self.db.execute("SELECT * FROM profile_proposals WHERE id=?", (proposal_id,)).fetchone())
            return self._remember(workspace_id, "propose_profile", key, request_hash, result)

    def confirm_profile(self, user_id, workspace_id, proposal_id, expected_revision, key):
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, {"admin"})
            _text(proposal_id, 128)
            expected_revision = _integer(expected_revision)
            request_hash, cached = self._command(workspace_id, "confirm_profile", key, [user_id, proposal_id, expected_revision])
            if cached is not None:
                return cached
            proposal = self.db.execute("SELECT * FROM profile_proposals WHERE workspace_id=? AND id=?", (workspace_id, proposal_id)).fetchone()
            if proposal is None:
                raise WorkspaceError("not_found", 404)
            if self.db.execute("SELECT 1 FROM profile_revisions WHERE proposal_id=?", (proposal_id,)).fetchone():
                raise WorkspaceError("proposal_already_confirmed", 409)
            if self._current_revision(workspace_id) != expected_revision or proposal["base_revision"] != expected_revision:
                raise WorkspaceError("profile_revision_conflict", 409)
            revision = expected_revision + 1
            self.db.execute("INSERT INTO profile_revisions VALUES(?,?,?,?,?,?)", (workspace_id, revision, proposal_id, proposal["payload"], user_id, _now()))
            self.db.execute("UPDATE workspaces SET current_profile_revision=? WHERE id=?", (revision, workspace_id))
            result = self._revision(self.db.execute("SELECT * FROM profile_revisions WHERE workspace_id=? AND revision=?", (workspace_id, revision)).fetchone())
            return self._remember(workspace_id, "confirm_profile", key, request_hash, result)

    def _selection(self, workspace_id, selection_id):
        row = self.db.execute("SELECT * FROM selections WHERE workspace_id=? AND id=?", (workspace_id, selection_id)).fetchone()
        if row is None:
            raise WorkspaceError("not_found", 404)
        events = self._bounded(self.db.execute("SELECT * FROM selection_history WHERE workspace_id=? AND selection_id=? ORDER BY version", (workspace_id, selection_id)))
        history = [{"version": event["version"], "state": event["state"], "actor_id": event["actor_id"], "recorded_at": _utc(event["recorded_at"]),
                    "catalog_checked_at": event["catalog_checked_at"]} for event in events]
        current = history[-1]
        return {"id": row["id"], "workspace_id": workspace_id, "profile_revision": row["profile_revision"],
                "items": json.loads(row["items"]), "created_by": row["created_by"], "created_at": _utc(row["created_at"]),
                "state": current["state"], "version": current["version"], "catalog_checked_at": current["catalog_checked_at"],
                "updated_at": current["recorded_at"], "history": history}

    def selection(self, user_id, workspace_id, selection_id):
        with self._transaction():
            self._authorize(user_id, workspace_id)
            _text(selection_id, 128)
            return self._selection(workspace_id, selection_id)

    def selections(self, user_id, workspace_id):
        with self._transaction():
            self._authorize(user_id, workspace_id)
            rows = self._bounded(self.db.execute("SELECT id FROM selections WHERE workspace_id=? ORDER BY created_at DESC,id", (workspace_id,)))
            return [self._selection(workspace_id, row["id"]) for row in rows]

    def create_selection(self, user_id, workspace_id, profile_revision, items, key, *, checked_at=None):
        """冻结已核实的目录输入；本事务不能锁住远端目录，只重验本库会话/角色/档案。"""
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            profile_revision, items = _integer(profile_revision, 1), _items(items)
            request_hash, cached = self._command(workspace_id, "create_selection", key, [user_id, profile_revision, items])
            if cached is not None:
                return cached
            if self._current_revision(workspace_id) != profile_revision:
                raise WorkspaceError("profile_revision_conflict", 409)
            now, selection_id = _now(), _id()
            checked = _checked_at(checked_at, now)
            self.db.execute("INSERT INTO selections VALUES(?,?,?,?,?,?,?)", (selection_id, workspace_id, profile_revision, _json(items), user_id, now, 1))
            self.db.execute("INSERT INTO selection_history VALUES(?,?,?,?,?,?,?)", (workspace_id, selection_id, 1, "awaiting_decision", user_id, now, checked))
            return self._remember(workspace_id, "create_selection", key, request_hash, self._selection(workspace_id, selection_id))

    def replay_selection(self, user_id, workspace_id, profile_revision, items, key):
        """创建成功后的重放不再依赖目录在线或最新版本；也不绕过当前会话/成员权限。"""
        with self._transaction():
            self._authorize(user_id, workspace_id, WRITERS)
            profile_revision, references = _integer(profile_revision, 1), _item_references(items)
            _text(key, 128)
            row = self.db.execute("SELECT result FROM commands WHERE workspace_id=? AND operation='create_selection' AND command_key=?", (workspace_id, key)).fetchone()
            if row is None:
                return None
            cached = json.loads(row["result"])
            recorded = [{name: item[name] for name in ("notice_id", "observation_id")} for item in cached["items"]]
            if cached["profile_revision"] != profile_revision or recorded != references:
                raise WorkspaceError("idempotency_conflict", 409)
            # 再用原始完整快照重算命令hash，保留actor限制；别的成员不能借同key冒领结果。
            _, result = self._command(workspace_id, "create_selection", key, [user_id, profile_revision, cached["items"]])
            return result

    def _transition_selection(self, user_id, workspace_id, selection_id, expected_version, key, state, checked_at=None):
        with self._transaction(write=True):
            self._authorize(user_id, workspace_id, WRITERS)
            _text(selection_id, 128)
            expected_version = _integer(expected_version, 1)
            operation = "confirm_selection" if state == "selected" else "cancel_selection"
            request_hash, cached = self._command(workspace_id, operation, key, [user_id, selection_id, expected_version])
            if cached is not None:
                return cached
            current = self._selection(workspace_id, selection_id)
            if current["version"] != expected_version or current["state"] != "awaiting_decision":
                raise WorkspaceError("selection_version_conflict", 409)
            if state == "selected" and self._current_revision(workspace_id) != current["profile_revision"]:
                raise WorkspaceError("profile_revision_conflict", 409)
            now = _now()
            checked = _checked_at(checked_at, now) if state == "selected" else current["catalog_checked_at"]
            self.db.execute("INSERT INTO selection_history VALUES(?,?,?,?,?,?,?)", (workspace_id, selection_id, expected_version + 1, state, user_id, now, checked))
            self.db.execute("UPDATE selections SET current_version=? WHERE workspace_id=? AND id=?", (expected_version + 1, workspace_id, selection_id))
            return self._remember(workspace_id, operation, key, request_hash, self._selection(workspace_id, selection_id))

    def confirm_selection(self, user_id, workspace_id, selection_id, expected_version, key, *, checked_at=None):
        return self._transition_selection(user_id, workspace_id, selection_id, expected_version, key, "selected", checked_at)

    def cancel_selection(self, user_id, workspace_id, selection_id, expected_version, key):
        return self._transition_selection(user_id, workspace_id, selection_id, expected_version, key, "cancelled")

"""本地核查工作台的 HTTP 边界：身份→当前权限→目录证据→私有版本。

仅绑定回环地址、只服务虚构企业开发环境。每请求独立连接；不会读取 catalog 库，
更不会把 selected 当作 research 任务。先读 Handler._dispatch，再读选择确认分支。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
from urllib.parse import parse_qs, urlsplit

from services.workspace.catalog_client import CatalogClient, CatalogError
from services.workspace.store import WorkspaceStore, WorkspaceError

ASSETS = Path(__file__).with_name("web")
MAX_BODY = 128 * 1024


def _json(raw):
    """重复键或 NaN 不是兼容 JSON；拒绝歧义，避免两层解释不同请求。"""
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_number")))


def _fields(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise WorkspaceError("invalid_request", 400)
    return value


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    """Cookie 只证明会话；角色与对象归属永远重新从 workspace 存储查询。"""
    server_version = "BidRadarLocal/1"

    def log_message(self, *_):
        pass  # 登录请求、私有标识和外部公告文本均不写 HTTP 控制台日志。

    def _send(self, status, value, *, mime="application/json; charset=utf-8", cookies=()):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code, status=400):
        # API 的稳定 code 可被中文界面解释，异常正文不从服务端透出。
        self._send(status, {"error": {"code": code, "message": "请求未完成，请核对权限、输入或服务状态。"}})

    def _guard(self):
        if len(self.path) > 8192 or self.headers.get_all("Host") != [self.server.authority]:
            raise WorkspaceError("invalid_host", 400)
        if any(self.headers.get(name) for name in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto")):
            raise WorkspaceError("forwarded_request_rejected", 400)
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
            raise WorkspaceError("invalid_path", 400)
        if self.command == "POST":
            if self.headers.get_all("Origin") != [self.server.origin]:
                raise WorkspaceError("invalid_origin", 403)
            if self.headers.get_all("Content-Type") != ["application/json"]:
                raise WorkspaceError("json_required", 415)
            if self.headers.get("Transfer-Encoding") is not None:
                raise WorkspaceError("invalid_framing", 400)
        return parts

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"\d{1,7}", lengths[0]):
            raise WorkspaceError("invalid_content_length", 400)
        count = int(lengths[0])
        if not 1 <= count <= MAX_BODY:
            raise WorkspaceError("body_too_large", 413)
        raw = self.rfile.read(count)
        if len(raw) != count:
            raise WorkspaceError("incomplete_body", 400)
        try:
            result = _json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise WorkspaceError("invalid_json", 400) from None
        if not isinstance(result, dict):
            raise WorkspaceError("invalid_request", 400)
        return result

    def _token(self):
        values = self.headers.get_all("Cookie", [])
        if len(values) != 1 or len(values[0]) > 4096:
            raise WorkspaceError("unauthenticated", 401)
        # SimpleCookie 覆盖重复键；先拒绝重复会话，避免代理/框架选取不同值。
        if sum(part.strip().startswith("br_session=") for part in values[0].split(";")) != 1:
            raise WorkspaceError("unauthenticated", 401)
        cookie = SimpleCookie()
        try:
            cookie.load(values[0])
            return cookie["br_session"].value
        except (CookieError, KeyError):
            raise WorkspaceError("unauthenticated", 401) from None

    def _dispatch(self):
        parts = self._guard()
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if self.command == "GET" and parts.path in static and not parts.query:
            filename, mime = static[parts.path]
            self._send(200, (ASSETS / filename).read_bytes(), mime=mime)
            return
        if self.command == "GET" and parts.path == "/health" and not parts.query:
            self._send(200, {"status": "ok", "mode": "local_development", "model_calls": 0})
            return
        body = self._body() if self.command == "POST" else None
        store = WorkspaceStore(self.server.store_path)
        try:
            if self.command == "POST" and parts.path == "/api/login" and not parts.query:
                _fields(body, "username password")
                result = store.login(body["username"], body["password"])
                # 登录成功轮换浏览器会话；旧会话如存在则持久注销，不输出令牌到JSON。
                try:
                    old_token = self._token()
                except WorkspaceError:
                    old_token = None
                if old_token:
                    try:
                        store.logout(old_token)
                    except WorkspaceError:
                        pass
                self._send(200, {"user": result["user"]}, cookies=(
                    f"br_session={result['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800",
                    f"br_csrf={result['csrf_token']}; Path=/; SameSite=Strict; Max-Age=28800"))
                return
            token = self._token()
            user = store.bind_session(token)
            uid = user["id"]
            if self.command == "POST":
                csrf_values = self.headers.get_all("X-CSRF-Token", [])
                if len(csrf_values) != 1:
                    raise WorkspaceError("invalid_csrf", 403)
                store.check_csrf(token, csrf_values[0])
            if parts.path == "/api/session" and self.command == "GET" and not parts.query:
                self._send(200, {"user": user, "workspaces": store.list_workspaces(uid),
                                 "mode": "local_development", "analysis_enabled": False})
                return
            if parts.path == "/api/logout" and self.command == "POST" and not parts.query:
                _fields(body, "")
                store.logout(token)
                self._send(200, {"logged_out": True}, cookies=(
                    "br_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                    "br_csrf=; Path=/; SameSite=Strict; Max-Age=0"))
                return
            match = re.fullmatch(r"/api/workspaces/([A-Za-z0-9_-]{1,128})/(.+)", parts.path)
            if not match:
                raise WorkspaceError("not_found", 404)
            wid, action = match.groups()

            def current_member(write=False):
                # 远程 catalog 返回之后也重查，旧Cookie或已移除成员不能读到迟到响应。
                store.bind_session(token)
                member = next((w for w in store.list_workspaces(uid) if w["workspace_id"] == wid), None)
                if member is None:
                    raise WorkspaceError("forbidden", 403)
                if write and member["role"] not in ("admin", "member"):
                    raise WorkspaceError("forbidden", 403)
                return member

            current_member()
            client = self.server.catalog_client
            if self.command == "GET":
                if action == "notices":
                    values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=6)
                    if any(len(v) != 1 for v in values.values()) or set(values) - {"keyword", "kind", "page_size", "simulation", "cursor"}:
                        raise WorkspaceError("invalid_query", 400)
                    values = {k: v[0] for k, v in values.items()}
                    if values.get("simulation", "false") not in ("true", "false"):
                        raise WorkspaceError("invalid_query", 400)
                    result = client.query(keyword=values.get("keyword", ""), kind=values.get("kind") or None,
                                          page_size=int(values.get("page_size", "20")),
                                          simulation=values.get("simulation", "false") == "true",
                                          cursor=values.get("cursor") or None)
                    current_member()
                elif parts.query:
                    raise WorkspaceError("invalid_query", 400)
                elif re.fullmatch(r"notices/[0-9a-f]{64}", action):
                    result = client.detail(action.split("/")[1])
                    current_member()
                elif action == "profile":
                    result = store.profile(uid, wid)
                elif action == "members":
                    result = {"items": store.members(uid, wid)}
                elif action == "selections":
                    result = {"items": store.selections(uid, wid)}
                elif re.fullmatch(r"selections/[A-Za-z0-9_-]{1,128}", action):
                    result = store.selection(uid, wid, action.split("/")[1])
                else:
                    raise WorkspaceError("not_found", 404)
            elif parts.query:
                raise WorkspaceError("invalid_query", 400)
            elif action == "profile/proposals":
                _fields(body, "payload base_revision key")
                result = store.propose_profile(uid, wid, body["payload"], body["base_revision"], body["key"])
            elif action == "profile/confirm":
                _fields(body, "proposal_id expected_revision key")
                result = store.confirm_profile(uid, wid, body["proposal_id"], body["expected_revision"], body["key"])
            elif action == "members/change":
                _fields(body, "target_user_id role active expected_version")
                result = store.change_member(uid, wid, body["target_user_id"], body["role"], body["active"], body["expected_version"])
            elif action == "selections":
                _fields(body, "profile_revision items key")
                current_member(write=True)
                # 成功响应丢失后的重放不能依赖目录仍在线/未变化。先按当前授权核对
                # 已持久命令与原输入；只有真正新命令才做外部核验。
                result = store.replay_selection(uid, wid, body["profile_revision"], body["items"], body["key"])
                if result is None:
                    items = client.validate_selection(body["items"])
                    checked_at = datetime.now(timezone.utc).isoformat()
                    # 写事务仍由store重验绑定会话、成员和档案，不把远程调用包在数据库锁里。
                    result = store.create_selection(uid, wid, body["profile_revision"], items, body["key"], checked_at=checked_at)
            elif re.fullmatch(r"selections/[A-Za-z0-9_-]{1,128}/(confirm|cancel)", action):
                _fields(body, "expected_version key")
                _, sid, command = action.split("/")
                current_member(write=True)
                previous = store.selection(uid, wid, sid)
                if command == "confirm":
                    checked_at = None
                    if previous["state"] == "awaiting_decision":
                        client.validate_selection([{k: item[k] for k in ("notice_id", "observation_id")} for item in previous["items"]])
                        checked_at = datetime.now(timezone.utc).isoformat()
                    result = store.confirm_selection(uid, wid, sid, body["expected_version"], body["key"], checked_at=checked_at)
                else:
                    result = store.cancel_selection(uid, wid, sid, body["expected_version"], body["key"])
            else:
                raise WorkspaceError("not_found", 404)
            self._send(200, result)
        finally:
            store.close()

    def _handle(self):
        try:
            self._dispatch()
        except (WorkspaceError, CatalogError) as error:
            self._error(error.code, error.status)
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            self._error("invalid_request", 400)
        except (socket.timeout, TimeoutError):
            self._error("request_timeout", 408)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self._error("internal_error", 500)

    do_GET = _handle
    do_POST = _handle

    def _unsupported(self):
        try:
            self._guard()
            self._error("method_not_allowed", 405)
        except WorkspaceError as error:
            self._error(error.code, error.status)

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _unsupported

    def __getattr__(self, name):
        if name.startswith("do_"):
            return self._unsupported
        raise AttributeError(name)


def create_server(store, catalog_url, catalog_token, *, host="127.0.0.1", port=0):
    """端口0供集成测试；只允许回环，不能把开发身份系统暴露到网卡。"""
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("loopback_required")
    client = CatalogClient(catalog_url, catalog_token)
    server = WorkbenchServer((host, port), Handler)
    server.store_path = Path(store)
    server.catalog_client = client
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="R1-A 本地工作台；仅虚构企业开发数据")
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--catalog-url", required=True)
    parser.add_argument("--catalog-token-file", required=True, type=Path)
    args = parser.parse_args(argv)
    with args.catalog_token_file.open("r", encoding="utf-8") as stream:
        token = stream.read(257).strip()
    server = create_server(args.store, args.catalog_url, token, port=args.port)
    print(json.dumps({"url": server.origin, "mode": "local_development"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

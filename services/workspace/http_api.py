"""工作台的 HTTP 服务：浏览器的唯一入口，同时给 research 和 tracking 提供内部接口。

一个浏览器请求的处理顺序：
    检查 Host / Origin / 请求格式 → 从 Cookie 找到会话 → 修改类请求检查 CSRF
    → 检查此人是不是这家公司的成员 → 执行操作（读写本地数据，或转发给其他服务）
    → 其他服务返回后，再检查一次成员身份（防止等待期间被移出）

浏览器永远不能自己声明"我是谁、我在哪家公司"：身份只从会话里取。
服务只监听本机回环地址，只用于虚构企业的本地开发。

阅读顺序：Handler._dispatch → 选择（selections）的创建和确认分支 → research_routes.dispatch。
"""

from __future__ import annotations

import argparse
import hmac
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
from services.common.local_http import token_valid
from services.common.local_http import LocalHTTPError
from services.workspace.research_routes import dispatch as dispatch_research

ASSETS = Path(__file__).with_name("web")  # 前端静态文件目录
MAX_BODY = 128 * 1024  # 请求体最大 128 KB


def _json(raw):
    """严格解析请求体 JSON：有重复键或 NaN 就拒绝。

    不同程序对重复键的处理不一样（取第一个还是最后一个），拒绝掉可以避免同一个请求被两边理解成两件事。
    """

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result

    return json.loads(
        raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("invalid_number"))
    )


def _fields(value, keys):
    """检查请求体正好包含 keys 里列出的字段（空格分隔），不多不少。"""
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise WorkspaceError("invalid_request", 400)
    return value


class WorkbenchServer(ThreadingHTTPServer):
    """多线程 HTTP 服务器。每个连接 10 秒超时，防止慢连接一直占着线程。"""

    daemon_threads = True
    allow_reuse_address = False

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    """处理单个 HTTP 请求。

    Cookie 只用来找到会话；角色和数据归属每次都重新从数据库查，不信任浏览器传来的任何身份信息。
    """

    server_version = "BidRadarLocal/1"

    def log_message(self, *_):
        pass  # 不打印访问日志：日志里可能出现登录信息、私有 ID 或公告原文。

    def _send(self, status, value, *, mime="application/json; charset=utf-8", cookies=()):
        """发送响应，附带一组安全响应头（禁止缓存、禁止嗅探类型、严格的内容安全策略）。"""
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        )
        for cookie in cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(raw)

    def _error(self, code, status=400):
        # 只返回错误码，由前端翻译成中文提示；服务端的异常细节不外露。
        self._send(status, {"error": {"code": code, "message": "请求未完成，请核对权限、输入或服务状态。"}})

    def _guard(self):
        """请求的基础检查：Host 必须是本服务、不接受代理转发头、路径格式正确；
        POST 请求还要检查来源（Origin）、Content-Type 和请求体的分帧方式。"""
        if len(self.path) > 8192 or self.headers.get_all("Host") != [self.server.authority]:
            raise WorkspaceError("invalid_host", 400)
        if any(self.headers.get(name) for name in ("Forwarded", "X-Forwarded-Host", "X-Forwarded-Proto")):
            raise WorkspaceError("forwarded_request_rejected", 400)
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
            raise WorkspaceError("invalid_path", 400)
        if self.command == "POST":
            if parts.path.startswith("/internal/v1/"):
                self._internal_identity()
            elif self.headers.get_all("Origin") != [self.server.origin]:
                raise WorkspaceError("invalid_origin", 403)
            if self.headers.get_all("Content-Type") != ["application/json"]:
                raise WorkspaceError("json_required", 415)
            if self.headers.get("Transfer-Encoding") is not None:
                raise WorkspaceError("invalid_framing", 400)
        return parts

    def _internal_identity(self):
        """识别内部调用方：比对 Bearer 令牌，返回服务名（research 或 tracking）。

        内部令牌只用于 /internal/v1/ 接口，拿着它也访问不了任何浏览器接口。
        """
        values = self.headers.get_all("Authorization", [])
        if len(values) == 1:
            for name, secret in self.server.internal_tokens.items():
                if hmac.compare_digest(values[0].encode(), ("Bearer " + secret).encode()):
                    return name
        raise WorkspaceError("unauthorized_service", 401)

    def _internal(self, store, path, body):
        """处理内部接口：authorize（查权限）、context（取档案和选择）、events（拉事件，仅 tracking）。"""
        identity = self._internal_identity()
        if self.command != "POST" or "?" in self.path:
            raise WorkspaceError("not_found", 404)
        if identity not in ("research", "tracking"):
            raise WorkspaceError("forbidden", 403)
        if path == "/internal/v1/authorize":
            _fields(body, "schema_version actor_id workspace_id action")
            result = lambda: store.authorize(body["actor_id"], body["workspace_id"], body["action"])
        elif path == "/internal/v1/context":
            _fields(body, "schema_version actor_id workspace_id profile_revision selection_id")
            result = lambda: store.context(
                body["actor_id"], body["workspace_id"], body["profile_revision"], body["selection_id"]
            )
        elif path == "/internal/v1/events" and identity == "tracking":
            _fields(body, "schema_version after limit")
            result = lambda: store.events(body["after"], body["limit"])
        else:
            raise WorkspaceError("not_found", 404)
        if type(body["schema_version"]) is not int or body["schema_version"] != 1:
            raise WorkspaceError("unsupported_contract")
        return result()

    def _body(self):
        """读取并解析 JSON 请求体。必须有且只有一个 Content-Length，大小不超过 MAX_BODY。"""
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
        """从 Cookie 里取出会话令牌 br_session。"""
        values = self.headers.get_all("Cookie", [])
        if len(values) != 1 or len(values[0]) > 4096:
            raise WorkspaceError("unauthenticated", 401)
        # 出现多个 br_session 就拒绝。SimpleCookie 遇到重复只保留最后一个，别的程序可能取第一个，会产生歧义。
        if sum(part.strip().startswith("br_session=") for part in values[0].split(";")) != 1:
            raise WorkspaceError("unauthenticated", 401)
        cookie = SimpleCookie()
        try:
            cookie.load(values[0])
            return cookie["br_session"].value
        except (CookieError, KeyError):
            raise WorkspaceError("unauthenticated", 401) from None

    def _dispatch(self):
        """按路径分发请求。顺序：静态文件 → 健康检查 → 内部接口 → 登录 → 需要登录的接口。"""
        parts = self._guard()
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
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
            if parts.path.startswith("/internal/v1/"):
                self._send(200, self._internal(store, parts.path, body))
                return
            if self.command == "POST" and parts.path == "/api/login" and not parts.query:
                _fields(body, "username password")
                result = store.login(body["username"], body["password"])
                # 登录成功就换一个新会话，旧会话（如果有）直接注销。令牌只放在 Cookie 里，不出现在响应正文。
                try:
                    old_token = self._token()
                except WorkspaceError:
                    old_token = None
                if old_token:
                    try:
                        store.logout(old_token)
                    except WorkspaceError:
                        pass
                self._send(
                    200,
                    {"user": result["user"]},
                    cookies=(
                        f"br_session={result['token']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=28800",
                        f"br_csrf={result['csrf_token']}; Path=/; SameSite=Strict; Max-Age=28800",
                    ),
                )
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
                self._send(
                    200,
                    {
                        "user": user,
                        "workspaces": store.list_workspaces(uid),
                        "mode": "local_development",
                        "analysis_enabled": self.server.research_client is not None,
                    },
                )
                return
            if parts.path == "/api/logout" and self.command == "POST" and not parts.query:
                _fields(body, "")
                store.logout(token)
                self._send(
                    200,
                    {"logged_out": True},
                    cookies=(
                        "br_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                        "br_csrf=; Path=/; SameSite=Strict; Max-Age=0",
                    ),
                )
                return
            match = re.fullmatch(r"/api/workspaces/([A-Za-z0-9_-]{1,128})/(.+)", parts.path)
            if not match:
                raise WorkspaceError("not_found", 404)
            wid, action = match.groups()

            def current_member(write=False):
                """确认会话仍有效、此人仍是这家公司的成员；write=True 时还要求不是只读成员。

                调用其他服务前后都要查一次：等待期间如果被移出公司，迟到的响应也不能交给他。
                """
                store.bind_session(token)
                member = next((w for w in store.list_workspaces(uid) if w["workspace_id"] == wid), None)
                if member is None:
                    raise WorkspaceError("forbidden", 403)
                if write and member["role"] not in ("admin", "member"):
                    raise WorkspaceError("forbidden", 403)
                return member

            current_member()
            handled, result = dispatch_research(
                self.command, action, parts.query, body, uid, wid, self.server, current_member
            )
            if handled:
                self._send(200, result)
                return
            client = self.server.catalog_client
            if self.command == "GET":
                if action == "notices":
                    values = parse_qs(parts.query, keep_blank_values=True, max_num_fields=6)
                    if any(len(v) != 1 for v in values.values()) or set(values) - {
                        "keyword",
                        "kind",
                        "page_size",
                        "simulation",
                        "cursor",
                    }:
                        raise WorkspaceError("invalid_query", 400)
                    values = {k: v[0] for k, v in values.items()}
                    if values.get("simulation", "false") not in ("true", "false"):
                        raise WorkspaceError("invalid_query", 400)
                    result = client.query(
                        keyword=values.get("keyword", ""),
                        kind=values.get("kind") or None,
                        page_size=int(values.get("page_size", "20")),
                        simulation=values.get("simulation", "false") == "true",
                        cursor=values.get("cursor") or None,
                    )
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
                result = store.change_member(
                    uid, wid, body["target_user_id"], body["role"], body["active"], body["expected_version"]
                )
            elif action == "selections":
                _fields(body, "profile_revision items key")
                current_member(write=True)
                # 先看这是不是一次重复提交（上次成功了但响应丢了）。
                # 是的话直接返回上次的结果，不需要目录在线；只有新请求才去目录核实公告。
                result = store.replay_selection(uid, wid, body["profile_revision"], body["items"], body["key"])
                if result is None:
                    items = client.validate_selection(body["items"])
                    checked_at = datetime.now(timezone.utc).isoformat()
                    # 远程核实在事务外完成；写入时 store 会再检查一遍会话、成员和档案版本。
                    result = store.create_selection(
                        uid, wid, body["profile_revision"], items, body["key"], checked_at=checked_at
                    )
            elif re.fullmatch(r"selections/[A-Za-z0-9_-]{1,128}/(confirm|cancel)", action):
                _fields(body, "expected_version key")
                _, sid, command = action.split("/")
                current_member(write=True)
                previous = store.selection(uid, wid, sid)
                if command == "confirm":
                    checked_at = None
                    if previous["state"] == "awaiting_decision":
                        client.validate_selection(
                            [{k: item[k] for k in ("notice_id", "observation_id")} for item in previous["items"]]
                        )
                        checked_at = datetime.now(timezone.utc).isoformat()
                    result = store.confirm_selection(
                        uid, wid, sid, body["expected_version"], body["key"], checked_at=checked_at
                    )
                else:
                    result = store.cancel_selection(uid, wid, sid, body["expected_version"], body["key"])
            else:
                raise WorkspaceError("not_found", 404)
            self._send(200, result)
        finally:
            store.close()

    def _handle(self):
        """处理请求并把各种异常转成统一的错误响应。"""
        try:
            self._dispatch()
        except (CatalogError, LocalHTTPError) as error:
            # 其他服务返回 401，说明服务之间的令牌配置有问题，不是用户没登录。
            # 转成 503，免得前端以为会话失效、把用户踢回登录页，陷入重新登录也解决不了的循环。
            self._error(
                "dependency_authentication_failed" if error.status == 401 else error.code,
                503 if error.status == 401 else error.status,
            )
        except WorkspaceError as error:
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
        """不支持的 HTTP 方法：照样做基础检查，然后返回 405。"""
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


def create_server(
    store,
    catalog_url,
    catalog_token,
    *,
    host="127.0.0.1",
    port=0,
    internal_tokens=None,
    research_client=None,
    tracking_client=None,
):
    """创建工作台 HTTP 服务器（未启动）。

    只允许监听 127.0.0.1：这套开发用的身份系统不能暴露到网络上。port=0 表示随机端口，测试用。
    """
    if host != "127.0.0.1" or type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("loopback_required")
    client = CatalogClient(catalog_url, catalog_token)
    internal_tokens = dict(internal_tokens or {})
    if (
        set(internal_tokens) - {"research", "tracking"}
        or not all(token_valid(v) for v in internal_tokens.values())
        or len(set(internal_tokens.values())) != len(internal_tokens)
    ):
        raise ValueError("invalid_service_tokens")
    server = WorkbenchServer((host, port), Handler)
    server.store_path = Path(store)
    server.catalog_client = client
    server.internal_tokens = internal_tokens
    server.research_client, server.tracking_client = research_client, tracking_client
    server.authority = f"127.0.0.1:{server.server_port}"
    server.origin = "http://" + server.authority
    return server


def main(argv=None):
    """命令行入口：只启动工作台（旧的双服务模式），不含研究和跟踪。"""
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

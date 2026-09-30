"""tracking内部HTTP；服务身份白名单与当前公司权限分别检查。"""
from services.common.local_http import LocalHTTPError, create_server as local_server

from .service import TrackingService
from .store import TrackingError, TrackingStore


def create_server(root, access, catalog, research, *, tokens, port=0, host="127.0.0.1"):
    if not isinstance(tokens, dict) or set(tokens) != {"workspace", "research"}:
        raise ValueError("tracking_service_identities_required")

    def dispatch(method, path, body, identity):
        if method != "POST" or not isinstance(body, dict) or body.get("schema_version") != 1:
            raise LocalHTTPError("invalid_request", 400)
        base = {"schema_version", "actor_id", "workspace_id"}
        routes = {
            "/internal/v1/decisions/get": ("decision", {"notice_id"}),
            "/internal/v1/decisions/set": ("set_decision", {"notice_id", "state", "reason", "expected_version", "key"}),
            "/internal/v1/watches/get": ("watch", {"notice_id"}),
            "/internal/v1/watches/set": ("set_watch", {"notice_id", "active", "auto_reassess", "expected_version", "key", "expires_at", "scope"}),
            "/internal/v1/changes": ("changes", {"after", "limit"}),
            "/internal/v1/notifications": ("notifications", {"after", "limit"}),
            "/internal/v1/notifications/read": ("mark_read", {"notification_id"}),
            "/internal/v1/reassessments/list": ("reassessments", set()),
            "/internal/v1/reassessments/cancel": ("cancel_reassessment", {"reassessment_id", "expected_version", "key"}),
            "/internal/v1/reassessments/retry": ("retry_reassessment", {"reassessment_id", "expected_version", "key"}),
            "/internal/v1/delegations/check": ("check_delegation", {"delegation"}),
        }
        route = routes.get(path)
        if route is None:
            raise LocalHTTPError("not_found", 404)
        allowed = "research" if path.endswith("/delegations/check") else "workspace"
        if identity != allowed:
            raise LocalHTTPError("service_forbidden", 403)
        operation, fields = route
        if set(body) != base | fields or type(body["schema_version"]) is not int:
            raise LocalHTTPError("invalid_request", 400)
        store = None
        try:
            # 每请求连接独立，避免线程复用SQLite或把上一次授权带到下一个主体。
            store = TrackingStore(root)
            service = TrackingService(store, access, catalog, research)
            arguments = {key: value for key, value in body.items() if key != "schema_version"}
            return getattr(service, operation)(**arguments)
        except TrackingError as exc:
            raise LocalHTTPError(exc.code, exc.status) from None
        finally:
            if store is not None:
                store.close()

    return local_server(dispatch, tokens, port=port, host=host)

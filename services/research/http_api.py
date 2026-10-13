"""研究服务的内部 HTTP 接口。

只有两个调用方：workspace（替浏览器转发）和 tracking（自动复核）。
接口只负责受理和查询任务，从不在请求里直接调模型；真正的研究由 worker 在后台完成。
两个调用方能做的事不一样，例如只有 workspace 能查预算，tracking 只能操作自己发起的复核任务。
"""

from services.common.local_http import LocalHTTPError, create_server as local_server
from .budget import BudgetError, BudgetLedger
from .service import ResearchService
from .store import ResearchStore, ResearchError


def create_server(root, access, catalog, tracking, *, tokens, budget_path, port=0, config=None):
    """创建研究服务的 HTTP 服务器（未启动）。tokens 是两个调用方各自的访问令牌。"""
    if not isinstance(tokens, dict) or set(tokens) != {"workspace", "tracking"}:
        raise ValueError("research_service_identities_required")

    def dispatch(method, path, body, identity):
        """处理一个已通过身份校验的请求。identity 是调用方的名字："workspace" 或 "tracking"。"""
        if (
            method != "POST"
            or not isinstance(body, dict)
            or type(body.get("schema_version")) is not int
            or body["schema_version"] != 1
        ):
            raise LocalHTTPError("invalid_request", 400)
        store = ResearchStore(root)
        try:
            service = ResearchService(store, access, catalog, tracking, identity=identity, config=config)
            if path == "/internal/v1/analyses":
                return 202, service.create_analysis(body)
            routes = {
                "/internal/v1/runs/get": ("run", {"run_id"}),
                "/internal/v1/commands/get": ("command", {"key"}),
                "/internal/v1/runs/cancel": ("cancel", {"run_id", "key"}),
                "/internal/v1/runs/list": ("list_runs", {"before", "limit"}),
                "/internal/v1/budget": ("budget", set()),
            }
            if path not in routes:
                raise ResearchError("not_found", 404)
            operation, fields = routes[path]
            if set(body) != {"schema_version", "actor_id", "workspace_id"} | fields:
                raise ResearchError("invalid_request", 400)
            if operation == "budget":
                if identity != "workspace":
                    raise ResearchError("service_forbidden", 403)
                service._authorize(body["actor_id"], body["workspace_id"])
                ledger = BudgetLedger(budget_path)
                try:
                    return ledger.summary(body["workspace_id"])
                finally:
                    ledger.close()
            return getattr(service, operation)(**{k: v for k, v in body.items() if k != "schema_version"})
        except (ResearchError, BudgetError) as exc:
            raise LocalHTTPError(exc.code, exc.status) from None
        finally:
            store.close()

    return local_server(dispatch, tokens, port=port)

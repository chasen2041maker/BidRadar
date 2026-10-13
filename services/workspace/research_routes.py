"""把浏览器的研究、跟踪请求转发给 research 和 tracking 服务。

浏览器路径 /api/workspaces/{公司}/research/... 和 /tracking/... 在这里映射成内部接口。
发起人和公司 ID 由工作台从会话里取出后加上去，浏览器的请求体里不能指定这两样。
"""
import re

from services.common.query import parse_query_pairs

from .store import WorkspaceError


def fields(value, expected):
    """检查请求体正好包含 expected 里列出的字段（空格分隔）。"""
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise WorkspaceError("invalid_request")


def page(query, *, research=False):
    """解析分页参数。研究列表用 before（上一页最后一个任务 ID），变化和提醒用 after（序号）。"""
    try:
        pairs = parse_query_pairs(query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
        values = dict(pairs)
        allowed = {"before", "limit"} if research else {"after", "limit"}
        if len(values) != len(pairs) or set(values) - allowed:
            raise ValueError()
        limit = values.get("limit", "30" if research else "20")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", limit) or int(limit) > 100:
            raise ValueError()
        if research:
            before = values.get("before") or None
            if before is not None and not re.fullmatch(r"[a-f0-9]{32}", before):
                raise ValueError()
            return {"before": before, "limit": int(limit)}
        after = values.get("after", "0")
        if not re.fullmatch(r"0|[1-9][0-9]{0,15}", after):
            raise ValueError()
        return {"after": int(after), "limit": int(limit)}
    except ValueError:
        raise WorkspaceError("invalid_query") from None


def dispatch(method, action, query, body, actor, wid, server, current_member):
    """处理研究或跟踪类请求，返回 (是否由这里处理, 结果)。

    不是 research/ 或 tracking/ 开头的路径返回 (False, None)，交回给工作台自己处理。
    转发前后都会调用 current_member 检查成员身份。
    """
    if not action.startswith(("research/", "tracking/")):
        return False, None
    base = {"schema_version": 1, "actor_id": actor, "workspace_id": wid}
    client = server.research_client if action.startswith("research/") else server.tracking_client
    if client is None:
        raise WorkspaceError("service_not_configured", 503)
    is_read = method == "GET" or re.fullmatch(r"tracking/notifications/[A-Za-z0-9_-]+/read", action)
    current_member(write=not is_read)
    if method == "POST" and query:
        raise WorkspaceError("invalid_query")
    path, arguments = None, {}
    if method == "GET":
        if action == "research/runs":
            path, arguments = "/internal/v1/runs/list", page(query, research=True)
        elif action in ("tracking/changes", "tracking/notifications"):
            path, arguments = "/internal/v1/" + action.split("/")[1], page(query)
        elif query:
            raise WorkspaceError("invalid_query")
        elif re.fullmatch(r"research/runs/[a-f0-9]{32}", action):
            path, arguments = "/internal/v1/runs/get", {"run_id": action.split("/")[2]}
        elif action == "research/budget":
            path = "/internal/v1/budget"
        elif re.fullmatch(r"tracking/(decisions|watches)/[a-f0-9]{64}", action):
            _, entity, nid = action.split("/")
            path, arguments = f"/internal/v1/{entity}/get", {"notice_id": nid}
        elif action == "tracking/reassessments":
            path = "/internal/v1/reassessments/list"
    elif action == "research/analyses":
        fields(body, "selection_id notice_id key mode")
        path, arguments = "/internal/v1/analyses", {**body, "kind": "analysis"}
    elif action == "research/questions":
        fields(body, "parent_run_id question key")
        parent = client.run(body["parent_run_id"], wid, actor)
        current_member(write=True)
        path, arguments = "/internal/v1/analyses", {**body, "kind": "question", "mode": "agent", "notice_id": parent["notice_id"]}
    elif re.fullmatch(r"research/runs/[a-f0-9]{32}/cancel", action):
        fields(body, "key")
        path, arguments = "/internal/v1/runs/cancel", {**body, "run_id": action.split("/")[2]}
    elif re.fullmatch(r"tracking/(decisions|watches)/[a-f0-9]{64}", action):
        _, entity, nid = action.split("/")
        fields(body, "state reason expected_version key" if entity == "decisions" else "active auto_reassess expected_version key expires_at scope")
        path, arguments = f"/internal/v1/{entity}/set", {**body, "notice_id": nid}
    elif re.fullmatch(r"tracking/notifications/[A-Za-z0-9_-]+/read", action):
        fields(body, "")
        path, arguments = "/internal/v1/notifications/read", {"notification_id": action.split("/")[2]}
    elif re.fullmatch(r"tracking/reassessments/[A-Za-z0-9_-]+/(cancel|retry)", action):
        fields(body, "expected_version key")
        _, _, rid, operation = action.split("/")
        path, arguments = f"/internal/v1/reassessments/{operation}", {**body, "reassessment_id": rid}
    if path is None:
        raise WorkspaceError("not_found", 404)
    result = client.request("POST", path, {**base, **arguments})
    current_member(write=not is_read)
    return True, result

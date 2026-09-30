"""浏览器到所属服务的窄网关：身份从会话派生，正文不能指定公司或actor。"""
import re
from urllib.parse import parse_qsl

from .store import WorkspaceError


def fields(value, expected):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise WorkspaceError("invalid_request")


def page(query, *, research=False):
    try:
        pairs = parse_qsl(query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
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
    """返回(是否本扩展路由,结果)；跨服务响应后重验浏览器会话和当前成员。"""
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

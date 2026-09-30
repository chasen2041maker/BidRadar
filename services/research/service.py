"""研究业务边界：当前权限→冻结输入→持久受理；不在HTTP请求中调用模型。"""
from copy import deepcopy
from hashlib import sha256
import re
from services.common.local_http import LocalHTTPError

from .budget import canonical
from .store import ResearchError


def text(value, limit=128):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\x00" in value:
        raise ResearchError("invalid_input", 400)
    return value


class ResearchService:
    def __init__(self, store, access, catalog, tracking, *, identity="workspace", config=None):
        self.store, self.access, self.catalog, self.tracking = store, access, catalog, tracking
        self.identity = identity
        from .agent import VERSION as agent_version, PROMPT_VERSION
        from .evidence import VERSION as evidence_version
        from .provider import EGRESS_VERSION, REQUESTED_MODEL, OFFICIAL_RESOLUTION, PRICE_VERSION
        # 冻结的是实际运行代码/提示词/出口版本；可信部署配置也不得伪报另一个实现版本。
        actual = {"version": "research-v2", "agent_version": agent_version, "prompt_version": PROMPT_VERSION,
                  "evidence_version": evidence_version, "egress_version": EGRESS_VERSION,
                  "requested_model": REQUESTED_MODEL, "official_resolution": OFFICIAL_RESOLUTION,
                  "price_version": PRICE_VERSION, "thinking": "disabled", "max_output_tokens": 4096,
                  "max_model_calls": 8, "max_tools": 16}
        if config is not None and (not isinstance(config, dict) or any(key in actual and actual[key] != value for key, value in config.items())):
            raise ResearchError("configuration_changed")
        self.config = {**deepcopy(config or {}), **actual}

    def _authorize(self, actor, wid, action="read"):
        text(actor)
        text(wid)
        grant = self.access.authorize(actor, wid, action)
        if (not isinstance(grant, dict) or type(grant.get("membership_version")) is not int
                or grant["membership_version"] < 1 or type(grant.get("current_profile_revision")) is not int):
            raise ResearchError("invalid_access_response", 503)
        return grant

    def _tracking_only(self, run, actor):
        if self.identity == "tracking" and (run["kind"] != "reassessment" or run["actor_id"] != actor):
            raise ResearchError("service_forbidden", 403)

    def create_analysis(self, request):
        """幂等重放先于远程目录获取，已受理任务不会因为目录离线再创建一次。"""
        request = deepcopy(request)
        if not isinstance(request, dict) or request.get("schema_version") != 1 or type(request["schema_version"]) is not int:
            raise ResearchError("invalid_request", 400)
        required = {"schema_version", "actor_id", "workspace_id", "notice_id", "key", "mode", "kind"}
        kind = request.get("kind")
        additional = {"analysis": {"selection_id"}, "question": {"parent_run_id", "question"},
                      "reassessment": {"profile_revision", "catalog_snapshot", "delegation"}}.get(kind)
        if additional is None or set(request) != required | additional or request["mode"] not in ("agent", "baseline"):
            raise ResearchError("invalid_request", 400)
        if self.identity == "tracking":
            if kind != "reassessment" or request["mode"] != "agent":
                raise ResearchError("service_forbidden", 403)
        elif self.identity != "workspace" or kind == "reassessment":
            raise ResearchError("service_forbidden", 403)
        actor, wid, key = request["actor_id"], request["workspace_id"], text(request["key"])
        if not isinstance(request["notice_id"], str) or not re.fullmatch(r"[0-9a-f]{64}", request["notice_id"]):
            raise ResearchError("invalid_notice_id", 400)
        grant = self._authorize(actor, wid, "analyze")
        request_hash = sha256(canonical(request).encode()).hexdigest()
        previous = self.store.command(wid, actor, key, request_hash)
        if previous is not None:
            self._tracking_only(previous, actor)
            return previous
        if kind == "question":
            text(request["question"], 2000)
            parent = self.store.load(wid, text(request["parent_run_id"]))
            public = self.store.public(wid, parent["id"])
            if public["report"] is None or parent["request"]["notice_id"] != request["notice_id"]:
                raise ResearchError("parent_report_unavailable")
            manifest = deepcopy(parent["manifest"])
            manifest.update({"actor_id": actor, "kind": kind, "question": request["question"],
                             "previous_report": public["report"], "config": deepcopy(self.config)})
        else:
            revision = grant["current_profile_revision"] if kind == "analysis" else request["profile_revision"]
            if revision < 1:
                raise ResearchError("profile_required")
            delegation = None
            if kind == "reassessment":
                delegation = self.tracking.check_delegation(actor, wid, request["delegation"])
                if (delegation.get("valid") is not True or delegation.get("notice_id") != request["notice_id"]
                        or delegation.get("profile_revision") != revision or delegation.get("catalog_snapshot") != request["catalog_snapshot"]
                        or request["key"] != request["delegation"].get("command_key")):
                    raise ResearchError("delegation_input_mismatch")
            context = self.access.context(actor, wid, revision, request.get("selection_id"))
            bundle = self.catalog.bundle(request["notice_id"], snapshot=request.get("catalog_snapshot"))
            if kind == "analysis":
                selection = context.get("selection")
                if (not isinstance(selection, dict) or selection.get("state") != "selected"
                        or selection.get("profile_revision") != revision):
                    raise ResearchError("selected_input_required")
                selected = next((i for i in selection["items"] if i["notice_id"] == request["notice_id"]), None)
                if selected is None:
                    raise ResearchError("notice_outside_selection")
                if selected["observation_id"] != bundle["current"]["observation_id"]:
                    raise ResearchError("selection_input_stale")
            if delegation and delegation.get("observation_ids") != [o["observation_id"] for o in bundle["observations"]]:
                raise ResearchError("delegation_input_mismatch")
            manifest = {"schema_version": 1, "workspace_id": wid, "actor_id": actor, "scope_notice_id": request["notice_id"],
                        "profile": context["profile"], "observations": bundle["observations"], "catalog_snapshot": bundle["snapshot"],
                        "kind": kind, "question": None, "previous_report": None, "config": deepcopy(self.config)}
        if manifest["profile"]["revision"] != grant["current_profile_revision"]:
            raise ResearchError("profile_revision_changed")
        from .evidence import EvidenceIndex
        EvidenceIndex(manifest)  # 所属服务响应也要验证，不能把结构错误当正常冻结输入。
        latest = self._authorize(actor, wid, "analyze")
        if latest != grant:
            raise ResearchError("authorization_changed")
        if kind == "reassessment":
            self.tracking.check_delegation(actor, wid, request["delegation"])
        return self.store.create(request, request_hash, manifest, grant["membership_version"])

    def run(self, run_id, workspace_id, actor_id):
        self._authorize(actor_id, workspace_id)
        result = self.store.public(workspace_id, text(run_id))
        self._tracking_only(result, actor_id)
        catalog_state = "unavailable"
        try:
            current = self.catalog.bundle(result["notice_id"])
            ids = [entry["observation_id"] for entry in current["observations"]]
            if ids and all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
                catalog_state = "current" if ids == result["scope"]["observation_ids"] else "changed"
        except (LocalHTTPError, KeyError, TypeError, ValueError):
            # 目录离线时仍可查看已获准的历史报告，但不能把“无法检查”冒充最新。
            pass
        grant = self._authorize(actor_id, workspace_id)
        result["freshness"] = {"profile": "current" if grant["current_profile_revision"] == result["profile_revision"] else "changed",
                               "catalog": catalog_state}
        return result

    def command(self, workspace_id, actor_id, key):
        self._authorize(actor_id, workspace_id)
        result = self.store.command(workspace_id, actor_id, text(key))
        if result is None:
            raise ResearchError("not_found", 404)
        self._tracking_only(result, actor_id)
        return result

    def cancel(self, run_id, workspace_id, actor_id, key):
        text(key)
        self._authorize(actor_id, workspace_id, "analyze")
        result = self.store.public(workspace_id, text(run_id))
        self._tracking_only(result, actor_id)
        return self.store.cancel(workspace_id, run_id, actor_id=actor_id, key=key)

    def list_runs(self, actor_id, workspace_id, before=None, limit=30):
        if self.identity != "workspace":
            raise ResearchError("service_forbidden", 403)
        self._authorize(actor_id, workspace_id)
        return self.store.list_runs(workspace_id, before=before, limit=limit)

    def guard(self, run, action):
        """动作前检查任务取消、原授权代数、正式档案及有限跟踪委托；失败关闭动作。"""
        self.store.check_lease(run)
        if run["manifest"]["config"] != self.config:
            raise ResearchError("configuration_changed")
        grant = self._authorize(run["actor_id"], run["workspace_id"], "analyze")
        if grant["membership_version"] != run["membership_version"]:
            raise ResearchError("authorization_changed")
        if grant["current_profile_revision"] != run["manifest"]["profile"]["revision"]:
            raise ResearchError("profile_revision_changed")
        if run["request"]["kind"] == "reassessment":
            self.tracking.check_delegation(run["actor_id"], run["workspace_id"], run["request"]["delegation"])
        # 当前权限远程检查期间也可能发生取消；再查本任务，缩小迟到响应窗口。
        self.store.check_lease(run)

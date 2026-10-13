"""研究服务的业务规则。

受理一次研究的顺序：检查当前权限 → 取固定版本的企业档案和公告 → 冻结成 manifest → 存为排队任务。
HTTP 请求里只做到"存为排队任务"，不调模型；模型调用全部在 worker 里进行。

guard() 是 worker 每个动作前都要调用的检查：任务没被取消、租约还在、
成员权限和档案版本都没变、配置没被换掉。任何一项不满足就停止。
"""
from copy import deepcopy
from hashlib import sha256
import re
from services.common.local_http import LocalHTTPError

from .budget import canonical
from .store import ResearchError


def text(value, limit=128):
    """检查是非空字符串、不超过 limit 个字符、不含空字符，否则报 invalid_input。"""
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or "\x00" in value:
        raise ResearchError("invalid_input", 400)
    return value


class ResearchService:
    """研究服务的业务入口。

    依赖都从外面传入：
        store     ResearchStore，本服务自己的数据库
        access    workspace 服务的客户端：检查权限、读取档案和选择
        catalog   catalog 服务的客户端：读取公告及其版本
        tracking  tracking 服务的客户端：核对自动复核的授权（委托）
        identity  当前调用方是谁，决定能用哪些操作
    """

    def __init__(self, store, access, catalog, tracking, *, identity="workspace", config=None):
        self.store, self.access, self.catalog, self.tracking = store, access, catalog, tracking
        self.identity = identity
        from .agent import VERSION as agent_version, PROMPT_VERSION
        from .evidence import VERSION as evidence_version
        from .provider import EGRESS_VERSION, REQUESTED_MODEL, OFFICIAL_RESOLUTION, PRICE_VERSION, VERSION as provider_version, TEMPERATURE
        # 记录当前实际运行的代码版本、提示词版本和模型参数，冻结进每个任务。
        # 外部传入的配置如果和实际版本对不上，直接拒绝，不能谎报自己是另一个版本。
        actual = {"version": "research-v3", "agent_version": agent_version, "prompt_version": PROMPT_VERSION,
                  "evidence_version": evidence_version, "egress_version": EGRESS_VERSION,
                  "requested_model": REQUESTED_MODEL, "official_resolution": OFFICIAL_RESOLUTION,
                  "price_version": PRICE_VERSION, "thinking": "disabled", "max_output_tokens": 4096,
                  "provider_version": provider_version, "temperature": TEMPERATURE,
                  "max_model_calls": 8, "max_tools": 16}
        if config is not None and (not isinstance(config, dict) or any(key in actual and actual[key] != value for key, value in config.items())):
            raise ResearchError("configuration_changed")
        self.config = {**deepcopy(config or {}), **actual}

    def _authorize(self, actor, wid, action="read"):
        """向 workspace 查询此人当前在这家公司能否执行 action，返回成员版本和当前档案版本。"""
        text(actor)
        text(wid)
        grant = self.access.authorize(actor, wid, action)
        if (not isinstance(grant, dict) or type(grant.get("membership_version")) is not int
                or grant["membership_version"] < 1 or type(grant.get("current_profile_revision")) is not int):
            raise ResearchError("invalid_access_response", 503)
        return grant

    def _tracking_only(self, run, actor):
        """tracking 服务只能看到和操作它自己发起的复核任务。"""
        if self.identity == "tracking" and (run["kind"] != "reassessment" or run["actor_id"] != actor):
            raise ResearchError("service_forbidden", 403)

    def create_analysis(self, request):
        """受理一次研究请求，返回任务信息（状态为 queued）。

        kind 有三种：
            analysis      用户对选定的公告发起研究
            question      在已有报告上追问，沿用父报告的冻结输入
            reassessment  tracking 发现变化后自动发起的复核，必须带有效的委托
        同一个幂等键（key）重复提交时直接返回原任务。这一步放在读取目录之前，
        所以即使目录服务暂时不可用，重复提交也不会多建一个任务。
        """
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
        EvidenceIndex(manifest)  # 其他服务返回的数据也要检查一遍，结构不对就不受理。
        latest = self._authorize(actor, wid, "analyze")
        if latest != grant:
            raise ResearchError("authorization_changed")
        if kind == "reassessment":
            self.tracking.check_delegation(actor, wid, request["delegation"])
        return self.store.create(request, request_hash, manifest, grant["membership_version"])

    def run(self, run_id, workspace_id, actor_id):
        """查看一个任务及其报告，并标明报告是否还是最新：档案或公告变了就是 changed。"""
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
            # 目录服务暂时不可用时，报告照样能看，只是新鲜度显示为 unavailable，而不是假装"最新"。
            pass
        grant = self._authorize(actor_id, workspace_id)
        result["freshness"] = {"profile": "current" if grant["current_profile_revision"] == result["profile_revision"] else "changed",
                               "catalog": catalog_state}
        return result

    def command(self, workspace_id, actor_id, key):
        """按幂等键查找之前提交的任务。用于"提交后没收到响应"时找回结果。"""
        self._authorize(actor_id, workspace_id)
        result = self.store.command(workspace_id, actor_id, text(key))
        if result is None:
            raise ResearchError("not_found", 404)
        self._tracking_only(result, actor_id)
        return result

    def cancel(self, run_id, workspace_id, actor_id, key):
        """取消任务。取消会持久保存，worker 在下一个动作前就会停下。"""
        text(key)
        self._authorize(actor_id, workspace_id, "analyze")
        result = self.store.public(workspace_id, text(run_id))
        self._tracking_only(result, actor_id)
        return self.store.cancel(workspace_id, run_id, actor_id=actor_id, key=key)

    def list_runs(self, actor_id, workspace_id, before=None, limit=30):
        """按创建时间倒序列出本公司的研究任务，before 是上一页最后一个任务的 ID。"""
        if self.identity != "workspace":
            raise ResearchError("service_forbidden", 403)
        self._authorize(actor_id, workspace_id)
        return self.store.list_runs(workspace_id, before=before, limit=limit)

    def guard(self, run, action):
        """worker 在每个动作前调用：任何一项检查不通过就抛异常，任务停下。

        检查项：
            - 租约还在、任务没被取消（查数据库，不看内存）
            - 运行配置和任务冻结时一致
            - 发起人仍有分析权限，且成员版本没变（被移除再加回来也算变了）
            - 公司的正式档案版本没变
            - 如果是自动复核，委托仍然有效
        """
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
        # 远程检查权限需要时间，期间任务可能被取消，所以最后再查一次。
        self.store.check_lease(run)

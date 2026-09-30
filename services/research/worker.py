"""受控Worker：任务领取、逐动作授权、统一预算调用、断点恢复与报告发布。

关键顺序是 guard→账本预留→外发→响应入账→Agent checkpoint→guard→发布。
服务故障可有限等待；外部计费不明不能自动重试。模型无法更改此状态机。
"""
from contextlib import closing
import sqlite3

from services.common.local_http import LocalHTTPError
from .budget import BudgetLedger, BudgetError
from .service import ResearchService
from .store import ResearchStore, ResearchError


def tick(root, access, catalog, tracking, provider, budget_path, *, worker_id="research-worker", config=None):
    # 延迟导入便于CLI启动先校验配置，缺模块时不能声称真实模型已可用。
    from .agent import run_agent
    from .evidence import EvidenceError
    from .evaluation import run_baseline
    from .provider import ProviderError, prepare_egress, estimate_reservation, usage_cost

    with closing(ResearchStore(root)) as store:
        run = store.claim(worker_id)
        if run is None:
            return None
        ledger = None
        try:
            service = ResearchService(store, access, catalog, tracking, config=config)
            guard = lambda action: service.guard(run, action)
            guard("start")
            if run["request"]["mode"] == "baseline":
                result = run_baseline(run["manifest"])
            else:
                if provider is None:
                    raise ResearchError("model_not_configured")
                if provider.metadata.get("provider") == "deepseek" and any(
                        provider.metadata.get(key) != run["manifest"]["config"][key]
                        for key in ("requested_model", "official_resolution", "price_version", "thinking", "max_output_tokens")):
                    raise ResearchError("configuration_changed")
                ledger = BudgetLedger(budget_path)
                def complete(messages, tools):
                    # 全量消息统一最小化后才估算/生成计费身份；provider内再兜底脱敏。
                    clean_messages, clean_tools = prepare_egress(messages, tools)
                    return ledger.complete(provider, run["workspace_id"], run["id"], clean_messages, clean_tools,
                                           guard=guard, estimate=estimate_reservation, cost=usage_cost)
                def replay(messages, tools):
                    guard("before_model_replay")
                    clean_messages, clean_tools = prepare_egress(messages, tools)
                    return ledger.replay(provider, run["workspace_id"], run["id"], clean_messages, clean_tools)
                complete.replay = replay  # 可信恢复能力，不来自模型/JSON；没有新增外发或预留路径。
                def checkpoint(value):
                    guard("checkpoint")
                    store.checkpoint(run, value)
                result = run_agent(run["manifest"], complete, guard=guard, checkpoint=checkpoint,
                                   resume=run["checkpoint"], max_steps=8)
                result["quality"]["usage"] = ledger.summary(run["workspace_id"], run["id"])
                result["quality"]["provider"] = provider.metadata
            guard("publish")
            store.finish(run, result)
        except (ResearchError, LocalHTTPError, BudgetError, ProviderError, EvidenceError) as exc:
            code = getattr(exc, "code", "research_failed")
            if code == "model_outcome_unknown":
                # 模型响应可能已在账本settled，只是Agent尚未应用checkpoint；这不等于
                # 账单未知。当前保守等待人工新命令，不盲重发，也不虚构一次退款/失败响应。
                code = "checkpoint_incomplete_no_replay"
            # 用户动作可解决的终态明确标原因；等待不会靠重新加入公司自行恢复。
            wait = {"authorization_changed", "profile_revision_changed", "delegation_invalid", "not_found", "forbidden",
                    "budget_exhausted", "budget_suspended", "billing_unknown", "model_not_configured",
                    "checkpoint_incomplete_no_replay", "checkpoint_request_mismatch", "checkpoint_request_not_dispatched",
                    "configuration_changed"}
            if isinstance(exc, LocalHTTPError) and exc.status >= 500:
                state = "retry_wait"
            else:
                state = "waiting_input" if code in wait or isinstance(exc, ProviderError) and exc.usage_unknown else "failed"
            try:
                store.stop(run, state, code)
            except ResearchError as stale:
                if stale.code != "lease_lost":
                    raise
        except (ValueError, TypeError, KeyError, sqlite3.Error):
            try:
                store.stop(run, "failed", "invalid_research_result")
            except ResearchError as stale:
                if stale.code != "lease_lost":
                    raise
        finally:
            if ledger is not None:
                ledger.close()
        return store.public(run["workspace_id"], run["id"])


def run_worker(root, access, catalog, tracking, provider, budget_path, *, stop_event, worker_id="research-worker", config=None):
    while not stop_event.is_set():
        try:
            result = tick(root, access, catalog, tracking, provider, budget_path, worker_id=worker_id, config=config)
        except (OSError, sqlite3.Error):
            result = None
        if result is None:
            stop_event.wait(1)

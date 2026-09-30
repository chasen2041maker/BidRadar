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
        service = ResearchService(store, access, catalog, tracking, config=config)
        guard = lambda action: service.guard(run, action)
        ledger = None
        try:
            guard("start")
            if run["request"]["mode"] == "baseline":
                result = run_baseline(run["manifest"])
            else:
                if provider is None:
                    raise ResearchError("model_not_configured")
                ledger = BudgetLedger(budget_path)
                def complete(messages, tools):
                    # 全量消息统一最小化后才估算/生成计费身份；provider内再兜底脱敏。
                    clean_messages, clean_tools = prepare_egress(messages, tools)
                    return ledger.complete(provider, run["workspace_id"], run["id"], clean_messages, clean_tools,
                                           guard=guard, estimate=estimate_reservation, cost=usage_cost)
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
            # 用户动作可解决的终态明确标原因；等待不会靠重新加入公司自行恢复。
            wait = {"authorization_changed", "profile_revision_changed", "delegation_invalid", "not_found", "forbidden",
                    "budget_exhausted", "budget_suspended", "billing_unknown", "model_not_configured"}
            if isinstance(exc, LocalHTTPError) and exc.status >= 500:
                state = "retry_wait"
            else:
                state = "waiting_input" if code in wait else "failed"
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

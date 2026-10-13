"""研究 worker：在后台领取排队的任务，跑完研究，把报告存起来。

一次模型调用的顺序是固定的：
    检查权限（guard）→ 在账本里预留费用 → 发请求 → 响应和用量入账 → 保存 Agent 进度 → 检查权限 → 发布
先入账再保存进度，所以进程在任何一步崩溃，恢复时都能从账本取回已经付过钱的响应，不会重复发送。

出错时怎么办：
    - 其他服务暂时不可用（5xx）：任务进入 retry_wait，稍后自动重试，最多 3 次。
    - 费用结果不确定、权限变了、配置变了等：任务进入 waiting_input，等人处理，不自动重试。
    - 其他错误：任务记为 failed。
"""
from contextlib import closing
import sqlite3

from services.common.local_http import LocalHTTPError
from .budget import BudgetLedger, BudgetError
from .service import ResearchService
from .store import ResearchStore, ResearchError


def tick(root, access, catalog, tracking, provider, budget_path, *, worker_id="research-worker", config=None):
    """领取并执行一个任务，返回执行后的任务信息；没有可领取的任务时返回 None。

    provider 为 None 表示没有配置模型：agent 模式的任务会停在 waiting_input，baseline 模式照常运行。
    """
    # 在函数内导入，让命令行工具可以先检查配置再加载这些模块。
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
                def check_provider_config():
                    # 模型参数（温度、模型名、价格版本等）必须和任务冻结时一致。
                    # 否则恢复时会算出不同的请求指纹，等于把一个已扣费的请求换个身份重发。
                    if provider.metadata.get("provider") == "deepseek" and any(
                            provider.metadata.get(key) != run["manifest"]["config"][key]
                            for key in ("requested_model", "official_resolution", "price_version", "thinking",
                                        "max_output_tokens", "provider_version", "temperature")):
                        raise ResearchError("configuration_changed")
                check_provider_config()
                ledger = BudgetLedger(budget_path)
                def complete(messages, tools):
                    check_provider_config()
                    # 先统一去掉联系方式，再估算费用、计算请求指纹；provider 里还会再去一遍，双保险。
                    clean_messages, clean_tools = prepare_egress(messages, tools)
                    return ledger.complete(provider, run["workspace_id"], run["id"], clean_messages, clean_tools,
                                           guard=guard, estimate=estimate_reservation, cost=usage_cost)
                def replay(messages, tools):
                    guard("before_model_replay")
                    check_provider_config()
                    clean_messages, clean_tools = prepare_egress(messages, tools)
                    return ledger.replay(provider, run["workspace_id"], run["id"], clean_messages, clean_tools)
                complete.replay = replay  # 恢复专用：只从账本取回已有响应，不会发请求，也不会预留费用。
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
                # 响应可能已经入账，只是 Agent 还没来得及保存进度。账单是清楚的，
                # 但为了稳妥，仍然停下等人处理，不重发，也不假装这次调用失败了。
                code = "checkpoint_incomplete_no_replay"
            # 这些原因需要人来处理（重新授权、充值、确认档案等），任务停在 waiting_input。
            # 注意：被移出公司后再加回来，也不会让旧任务自动继续。
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
    """循环执行 tick，直到 stop_event 被设置。没有任务时每秒检查一次。"""
    while not stop_event.is_set():
        try:
            result = tick(root, access, catalog, tracking, provider, budget_path, worker_id=worker_id, config=config)
        except (OSError, sqlite3.Error):
            result = None
        if result is None:
            stop_event.wait(1)

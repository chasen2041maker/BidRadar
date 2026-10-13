"""模型费用账本：每次调用模型前先在这里预留费用，调用后按实际用量结算。

为什么单独一个数据库：业务数据库在开发中经常被删掉重建，但花掉的钱是真实的。
账本放在仓库外的固定位置，删了业务库也不会"重置"额度。

规则：
    - 金额单位是 micro-CNY（百万分之一元），全部用整数，避免小数误差。
    - 先预留再外发。结果不确定（超时、断网）时，预留额继续算作已花，不会因为重启就当成免费。
    - 记录的是按峰时价格算的保守估计，不是供应商的实际账单。
    - 三层上限：本轮总额、每家公司、每个任务。
    - 所有真实运行（试跑、评测、worker）必须用同一个账本。
"""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time


class BudgetError(Exception):
    """账本相关的错误，例如额度用完（budget_exhausted）、费用结果不确定（billing_unknown）。"""

    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def canonical(value):
    """固定格式的 JSON 序列化，用来计算请求指纹。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class BudgetLedger:
    """费用账本。每个使用方各开一个连接；多个 worker 同时预留时，靠 BEGIN IMMEDIATE 保证不会超额。

    默认上限：本轮共 20 元，每家公司 10 元，每个任务 1 元（单位换算成 micro-CNY）。
    """

    def __init__(
        self, path, *, authorization="r12-20261001", cap=20_000_000, workspace_cap=10_000_000, run_cap=1_000_000
    ):
        if (
            any(type(v) is not int or v <= 0 for v in (cap, workspace_cap, run_cap))
            or not run_cap <= workspace_cap <= cap
        ):
            raise BudgetError("invalid_budget_config")
        self.path = Path(path).absolute()
        marker = self.path.with_suffix(".authorized")
        self.db = None
        if self.path.is_symlink() or marker.is_symlink() or self.path.parent.is_symlink():
            raise BudgetError("invalid_budget_path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if marker.exists() and not self.path.exists():
            raise BudgetError("budget_ledger_missing", 503)
        self.db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS authorization(id TEXT PRIMARY KEY,cap INTEGER NOT NULL,"
                    "workspace_cap INTEGER NOT NULL,run_cap INTEGER NOT NULL,blocked INTEGER NOT NULL DEFAULT 0)"
                )
                self.db.execute(
                    "CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,run_id TEXT NOT NULL,"
                    "request_hash TEXT NOT NULL,reserved INTEGER NOT NULL,charged INTEGER NOT NULL,state TEXT NOT NULL,"
                    "response TEXT,usage TEXT,model TEXT,provider_request_id TEXT,created_at REAL NOT NULL,settled_at REAL)"
                )
                rows = self.db.execute("SELECT * FROM authorization").fetchall()
                config = (authorization, cap, workspace_cap, run_cap)
                if not rows:
                    self.db.execute("INSERT INTO authorization(id,cap,workspace_cap,run_cap) VALUES(?,?,?,?)", config)
                elif len(rows) != 1 or tuple(rows[0])[:4] != config:
                    raise BudgetError("budget_authorization_mismatch")
            # 账本旁边放一个标记文件。如果标记在而账本没了，说明账本丢了，拒绝继续，必须人工恢复。
            marker.touch(exist_ok=True)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    @contextmanager
    def transaction(self):
        """开启写事务，一开始就拿写锁。"""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def reserve(self, workspace_id, run_id, attempt_id, request_hash, amount):
        """为一次调用预留费用。成功返回 None，调用方可以外发。

        如果同一个调用（attempt_id）之前已经成功结算，直接返回当时的响应，不再外发。
        如果之前那次还在进行中或结果不确定，报 billing_unknown，不允许再发一次。
        """
        if type(amount) is not int or amount <= 0:
            raise BudgetError("invalid_reservation")
        if any(
            not isinstance(v, str) or not 1 <= len(v) <= 128 for v in (workspace_id, run_id, attempt_id, request_hash)
        ):
            raise BudgetError("invalid_attempt")
        with self.transaction():
            existing = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if existing:
                if (existing["workspace_id"], existing["run_id"], existing["request_hash"]) != (
                    workspace_id,
                    run_id,
                    request_hash,
                ):
                    raise BudgetError("budget_attempt_conflict")
                if existing["state"] == "settled":
                    return json.loads(existing["response"])
                raise BudgetError("billing_unknown")
            config = self.db.execute("SELECT * FROM authorization").fetchone()
            if config["blocked"]:
                raise BudgetError("budget_suspended")
            if self.db.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND state IN ('pending','unknown')", (run_id,)
            ).fetchone():
                raise BudgetError("billing_unknown")
            for clause, args, limit in (
                ("", (), config["cap"]),
                (" WHERE workspace_id=?", (workspace_id,), config["workspace_cap"]),
                (" WHERE run_id=?", (run_id,), config["run_cap"]),
            ):
                charged = self.db.execute("SELECT coalesce(sum(charged),0) FROM attempts" + clause, args).fetchone()[0]
                if charged + amount > limit:
                    raise BudgetError("budget_exhausted")
            self.db.execute(
                "INSERT INTO attempts(id,workspace_id,run_id,request_hash,reserved,charged,state,created_at) "
                "VALUES(?,?,?,?,?,?,'pending',?)",
                (attempt_id, workspace_id, run_id, request_hash, amount, amount, time.time()),
            )
        return None

    def settle(self, attempt_id, response, cost):
        """收到响应后按实际用量结算。

        先入账、再让 Agent 保存进度：这样即使 Agent 还没保存就崩溃了，恢复时也能从账本取回这次的响应。
        用量缺失时按预留额记账，并报 billing_unknown。
        """
        if cost is not None and (type(cost) is not int or cost < 0):
            raise BudgetError("invalid_usage_cost")
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise BudgetError("attempt_not_pending")
            state = "settled" if cost is not None else "unknown"
            charged = cost if cost is not None else row["reserved"]
            self.db.execute(
                "UPDATE attempts SET charged=?,state=?,response=?,usage=?,model=?,provider_request_id=?,settled_at=? WHERE id=?",
                (
                    charged,
                    state,
                    canonical(response),
                    canonical(response.get("usage")),
                    response.get("model"),
                    response.get("provider_request_id"),
                    time.time(),
                    attempt_id,
                ),
            )
            if charged > row["reserved"]:
                # 实际花费超过了预留额：照实记账，并暂停整个账本，等人确认。
                self.db.execute("UPDATE authorization SET blocked=1")
        if cost is None:
            raise BudgetError("billing_unknown")

    def abort(self, attempt_id, *, dispatched):
        """调用失败时收尾。已经发出去的标为 unknown（费用不确定）；确实没发出去的才退还预留。"""
        with self.transaction():
            if dispatched:
                self.db.execute(
                    "UPDATE attempts SET state='unknown',settled_at=? WHERE id=? AND state='pending'",
                    (time.time(), attempt_id),
                )
            else:
                self.db.execute(
                    "UPDATE attempts SET state='not_dispatched',charged=0,settled_at=? WHERE id=? AND state='pending'",
                    (time.time(), attempt_id),
                )

    def summary(self, workspace_id=None, run_id=None):
        """汇总额度和用量。只返回数字，不返回任何模型请求或回复的内容。"""
        clause, args = "", []
        if workspace_id is not None:
            clause, args = " WHERE workspace_id=?", [workspace_id]
            if run_id is not None:
                clause += " AND run_id=?"
                args.append(run_id)
        rows = self.db.execute("SELECT charged,state,usage FROM attempts" + clause, args).fetchall()
        config = self.db.execute("SELECT * FROM authorization").fetchone()
        usage = [json.loads(r["usage"]) for r in rows if r["usage"]]
        return {
            "unit": "micro_cny",
            "cap": config["cap"],
            "workspace_cap": config["workspace_cap"],
            "run_cap": config["run_cap"],
            "charged_or_reserved": sum(r["charged"] for r in rows),
            "attempts": len(rows),
            "unknown_attempts": sum(r["state"] in ("pending", "unknown") for r in rows),
            "input_tokens": sum(u.get("input_tokens", 0) for u in usage if u),
            "output_tokens": sum(u.get("output_tokens", 0) for u in usage if u),
            "blocked": bool(config["blocked"]),
            "cost_basis": "conservative_peak_estimate",
        }

    def complete(self, provider, workspace_id, run_id, messages, tools, *, guard, estimate, cost):
        """唯一的付费调用入口：预留 → 外发 → 结算。传进来的内容必须已经去掉联系方式。

        请求指纹由"消息 + 工具 + 模型参数"算出，同一个任务里同样的请求只会付一次钱。
        """
        request_hash = sha256(canonical([messages, tools, provider.metadata]).encode()).hexdigest()
        attempt_id = sha256((run_id + ":" + request_hash).encode()).hexdigest()
        guard("before_reservation")
        previous = self.reserve(
            workspace_id, run_id, attempt_id, request_hash, estimate(messages, tools, provider.max_output_tokens)
        )
        if previous is not None:
            return previous
        dispatched = False
        try:
            guard("before_provider")
            dispatched = True
            response = provider.complete(messages, tools)
            self.settle(attempt_id, response, cost(response.get("usage")))
            return response
        except BaseException:
            self.abort(attempt_id, dispatched=dispatched)
            raise

    def replay(self, provider, workspace_id, run_id, messages, tools):
        """恢复专用：取回一次已经结算过的调用的响应。不预留，也不联网。

        用在"响应已入账，但 Agent 崩溃前没来得及保存进度"的情况。
        找不到这次调用、或者它的结果还不确定，都会报错，让任务停下等人处理。
        权限和取消的检查由调用方负责。
        """
        request_hash = sha256(canonical([messages, tools, provider.metadata]).encode()).hexdigest()
        attempt_id = sha256((run_id + ":" + request_hash).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or (row["workspace_id"], row["run_id"], row["request_hash"]) != (
            workspace_id,
            run_id,
            request_hash,
        ):
            raise BudgetError("checkpoint_request_mismatch")
        if row["state"] in ("pending", "unknown"):
            raise BudgetError("billing_unknown")
        if row["state"] != "settled":
            raise BudgetError("checkpoint_request_not_dispatched")
        return json.loads(row["response"])

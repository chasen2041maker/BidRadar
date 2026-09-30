"""单轮授权预算账本，独立于可重建的开发业务库。

金额单位 micro-CNY（百万分之一元），全部整数。先持久预留再外发；未知结果
继续占额，绝不靠超时/重启推断免费。这里记录保守峰价估算，不冒充供应商账单。
所有真实试跑、评测、HTTP Worker 必须使用同一账本和固定授权编号。
"""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time


class BudgetError(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class BudgetLedger:
    """每调用方独立连接；BEGIN IMMEDIATE 使多个 Worker 的额度判断和预留原子化。"""
    def __init__(self, path, *, authorization="r12-20261001", cap=20_000_000,
                 workspace_cap=10_000_000, run_cap=1_000_000):
        if any(type(v) is not int or v <= 0 for v in (cap, workspace_cap, run_cap)) or not run_cap <= workspace_cap <= cap:
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
                self.db.execute("CREATE TABLE IF NOT EXISTS authorization(id TEXT PRIMARY KEY,cap INTEGER NOT NULL,"
                                "workspace_cap INTEGER NOT NULL,run_cap INTEGER NOT NULL,blocked INTEGER NOT NULL DEFAULT 0)")
                self.db.execute("CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,workspace_id TEXT NOT NULL,run_id TEXT NOT NULL,"
                                "request_hash TEXT NOT NULL,reserved INTEGER NOT NULL,charged INTEGER NOT NULL,state TEXT NOT NULL,"
                                "response TEXT,usage TEXT,model TEXT,provider_request_id TEXT,created_at REAL NOT NULL,settled_at REAL)")
                rows = self.db.execute("SELECT * FROM authorization").fetchall()
                config = (authorization, cap, workspace_cap, run_cap)
                if not rows:
                    self.db.execute("INSERT INTO authorization(id,cap,workspace_cap,run_cap) VALUES(?,?,?,?)", config)
                elif len(rows) != 1 or tuple(rows[0])[:4] != config:
                    raise BudgetError("budget_authorization_mismatch")
            # 标记与账本都在仓库外；普通重启不能生成新额度。丢库必须人工恢复旧账。
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
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def reserve(self, workspace_id, run_id, attempt_id, request_hash, amount):
        """重放仅返回已知响应。pending/unknown 不能自动再次外发，避免重复计费。"""
        if type(amount) is not int or amount <= 0:
            raise BudgetError("invalid_reservation")
        if any(not isinstance(v, str) or not 1 <= len(v) <= 128 for v in (workspace_id, run_id, attempt_id, request_hash)):
            raise BudgetError("invalid_attempt")
        with self.transaction():
            existing = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if existing:
                if (existing["workspace_id"], existing["run_id"], existing["request_hash"]) != (workspace_id, run_id, request_hash):
                    raise BudgetError("budget_attempt_conflict")
                if existing["state"] == "settled":
                    return json.loads(existing["response"])
                raise BudgetError("billing_unknown")
            config = self.db.execute("SELECT * FROM authorization").fetchone()
            if config["blocked"]:
                raise BudgetError("budget_suspended")
            if self.db.execute("SELECT 1 FROM attempts WHERE run_id=? AND state IN ('pending','unknown')", (run_id,)).fetchone():
                raise BudgetError("billing_unknown")
            for clause, args, limit in (("", (), config["cap"]),
                                       (" WHERE workspace_id=?", (workspace_id,), config["workspace_cap"]),
                                       (" WHERE run_id=?", (run_id,), config["run_cap"])):
                charged = self.db.execute("SELECT coalesce(sum(charged),0) FROM attempts" + clause, args).fetchone()[0]
                if charged + amount > limit:
                    raise BudgetError("budget_exhausted")
            self.db.execute("INSERT INTO attempts(id,workspace_id,run_id,request_hash,reserved,charged,state,created_at) "
                            "VALUES(?,?,?,?,?,?,'pending',?)", (attempt_id, workspace_id, run_id, request_hash, amount, amount, time.time()))
        return None

    def settle(self, attempt_id, response, cost):
        """响应和用量先入账，随后业务 checkpoint；这个顺序允许断点复用已收费响应。"""
        if cost is not None and (type(cost) is not int or cost < 0):
            raise BudgetError("invalid_usage_cost")
        with self.transaction():
            row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row["state"] != "pending":
                raise BudgetError("attempt_not_pending")
            state = "settled" if cost is not None else "unknown"
            charged = cost if cost is not None else row["reserved"]
            self.db.execute("UPDATE attempts SET charged=?,state=?,response=?,usage=?,model=?,provider_request_id=?,settled_at=? WHERE id=?",
                            (charged, state, canonical(response), canonical(response.get("usage")), response.get("model"),
                             response.get("provider_request_id"), time.time(), attempt_id))
            if charged > row["reserved"]:
                # 供应商实报超预估时记真实估算并暂停，而不是截断数字制造预算达标。
                self.db.execute("UPDATE authorization SET blocked=1")
        if cost is None:
            raise BudgetError("billing_unknown")

    def abort(self, attempt_id, *, dispatched):
        with self.transaction():
            if dispatched:
                self.db.execute("UPDATE attempts SET state='unknown',settled_at=? WHERE id=? AND state='pending'", (time.time(), attempt_id))
            else:
                # 已证实未外发才退还预留；不复用此尝试身份，任务需要显式新命令。
                self.db.execute("UPDATE attempts SET state='not_dispatched',charged=0,settled_at=? WHERE id=? AND state='pending'",
                                (time.time(), attempt_id))

    def summary(self, workspace_id=None, run_id=None):
        """页面只见额度/用量/模型身份，不能借预算接口读其他公司的模型正文。"""
        clause, args = "", []
        if workspace_id is not None:
            clause, args = " WHERE workspace_id=?", [workspace_id]
            if run_id is not None:
                clause += " AND run_id=?"
                args.append(run_id)
        rows = self.db.execute("SELECT charged,state,usage FROM attempts" + clause, args).fetchall()
        config = self.db.execute("SELECT * FROM authorization").fetchone()
        usage = [json.loads(r["usage"]) for r in rows if r["usage"]]
        return {"unit": "micro_cny", "cap": config["cap"], "workspace_cap": config["workspace_cap"],
                "run_cap": config["run_cap"], "charged_or_reserved": sum(r["charged"] for r in rows),
                "attempts": len(rows), "unknown_attempts": sum(r["state"] in ("pending", "unknown") for r in rows),
                "input_tokens": sum(u.get("input_tokens", 0) for u in usage if u),
                "output_tokens": sum(u.get("output_tokens", 0) for u in usage if u),
                "blocked": bool(config["blocked"]), "cost_basis": "conservative_peak_estimate"}

    def complete(self, provider, workspace_id, run_id, messages, tools, *, guard, estimate, cost):
        """唯一付费出口。传入内容须先统一脱敏；账本不读取或存储供应商密钥。"""
        request_hash = sha256(canonical([messages, tools, provider.metadata]).encode()).hexdigest()
        attempt_id = sha256((run_id + ":" + request_hash).encode()).hexdigest()
        guard("before_reservation")
        previous = self.reserve(workspace_id, run_id, attempt_id, request_hash,
                                estimate(messages, tools, provider.max_output_tokens))
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
        """仅复用精确请求与供应商配置的已入账响应；此恢复入口没有预留或网络路径。

        已知响应先settle而Agent checkpoint未保存时，恢复可继续原步骤。若配置变化、
        原attempt不存在、pending或unknown，必须等待处理，不能生成新hash再次计费。
        调用者负责每次恢复前重验当前权限/取消/租约，本方法只管理账本身份。
        """
        request_hash = sha256(canonical([messages, tools, provider.metadata]).encode()).hexdigest()
        attempt_id = sha256((run_id + ":" + request_hash).encode()).hexdigest()
        row = self.db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None or (row["workspace_id"], row["run_id"], row["request_hash"]) != (workspace_id, run_id, request_hash):
            raise BudgetError("checkpoint_request_mismatch")
        if row["state"] in ("pending", "unknown"):
            raise BudgetError("billing_unknown")
        if row["state"] != "settled":
            raise BudgetError("checkpoint_request_not_dispatched")
        return json.loads(row["response"])

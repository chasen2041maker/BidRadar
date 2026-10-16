"""流水线需要的四样东西，在这里用数据库实现：步骤存储、费用账本、执行守卫、抽取缓存。

agent/ 只认接口（StepStore、Ledger、Guard、ExtractionCache），不碰数据库；这里是它们的 PostgreSQL 版本。
四样东西都在 Worker 的会话里工作，并且各自提交事务：每一步做完就落库，崩了重启从断点接着跑。

几条顺序上的保证：
    - 预留费用先提交，再发请求。进程在发请求时崩了，账本里也已经有这笔预留，重启后不会当作免费重发。
    - 每存一步之前确认任务租约还是自己的（jobs.hold）。租约被别的 Worker 接手后，旧 Worker 的写入会被拒绝。
"""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.agent.llm import BudgetExceeded, CallOutcomeUnknown, LLMRequest, LLMResponse, Pricing, Usage
from bidradar.agent.pipeline import PipelineStopped
from bidradar.agent.schemas import Chunk
from bidradar.infra import clock, jobs
from bidradar.infra.errors import AppError
from bidradar.infra.ids import fingerprint
from bidradar.modules.research.models import Limits, RunStatus
from bidradar.modules.research.tables import LlmCallRow, RequirementSetRow, RunRow, RunStepRow
from bidradar.modules.workspace import service as workspace

RUN_LEASE = timedelta(minutes=5)
OPEN_STATES = ("pending", "unknown")


class DbStepStore:
    def __init__(self, session: Session, run_id: str, job: jobs.Job | None) -> None:
        self.session, self.run_id, self.job = session, run_id, job

    def get(self, name: str, input_hash: str) -> dict | None:
        return self.session.scalar(
            select(RunStepRow.output).where(
                RunStepRow.run_id == self.run_id, RunStepRow.name == name, RunStepRow.input_hash == input_hash
            )
        )

    def save(self, name: str, input_hash: str, value: dict) -> None:
        if self.job is not None:
            jobs.hold(self.session, self.job)
        self.session.execute(
            insert(RunStepRow)
            .values(run_id=self.run_id, name=name, input_hash=input_hash, output=value, created_at=clock.now())
            .on_conflict_do_nothing(index_elements=["run_id", "name", "input_hash"])
        )
        self.session.commit()


class DbLedger:
    """模型调用账本。三层额度：全部公司合计、每家公司、每个任务。

    已花 = 已结算的实际费用 + 还在进行中或结果不确定的预留额。结果不确定的预留额照算已花，
    重启也不会当作免费：宁可多算，不能少算。
    """

    def __init__(self, session: Session, run: RunRow, limits: Limits, pricing: Pricing) -> None:
        self.session, self.run, self.limits, self.pricing = session, run, limits, pricing

    def _row(self, call_key: str) -> LlmCallRow | None:
        return self.session.get(LlmCallRow, call_key)

    def lookup(self, call_key: str) -> LLMResponse | None:
        row = self._row(call_key)
        if row is None or row.state == "released":
            return None
        if row.state in OPEN_STATES:
            # 之前那次还在进行中，或者结果不确定：不能再发一次，可能会被计费两次。
            raise CallOutcomeUnknown()
        usage = row.response.get("usage")
        return LLMResponse(
            text=row.response["text"],
            usage=Usage(**usage) if usage else None,
            model=row.model or "",
            request_id=row.request_id,
        )

    def _spent(self, *conditions) -> int:
        charged = self.session.scalar(
            select(func.coalesce(func.sum(LlmCallRow.charged), 0)).where(LlmCallRow.state == "settled", *conditions)
        )
        reserved = self.session.scalar(
            select(func.coalesce(func.sum(LlmCallRow.reserved), 0)).where(
                LlmCallRow.state.in_(OPEN_STATES), *conditions
            )
        )
        return int(charged) + int(reserved)

    def reserve(self, request: LLMRequest, amount: int) -> None:
        # 全局串行：两个 Worker 同时预留时，不会都看到"还剩 1 元"、各自花掉 1 元。锁在提交时释放。
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtext("research.budget"))))
        if (
            self._spent() + amount > self.limits.total_cap
            or self._spent(LlmCallRow.workspace_id == self.run.workspace_id) + amount > self.limits.workspace_cap
            or self._spent(LlmCallRow.run_id == self.run.id) + amount > self.limits.run_cap
        ):
            self.session.rollback()
            raise BudgetExceeded()
        now = clock.now()
        values = {
            "run_id": self.run.id,
            "workspace_id": self.run.workspace_id,
            "purpose": request.purpose,
            "request_hash": fingerprint([request.system, request.user, request.max_output_tokens]),
            "reserved": amount,
            "charged": 0,
            "state": "pending",
            "price_version": self.pricing.version,
            "created_at": now,
        }
        self.session.execute(
            insert(LlmCallRow)
            .values(call_key=request.call_key, **values)
            .on_conflict_do_update(index_elements=["call_key"], set_=values, where=LlmCallRow.state == "released")
        )
        self.session.commit()

    def settle(self, request: LLMRequest, response: LLMResponse, cost: int) -> None:
        row = self._row(request.call_key)
        row.state, row.charged, row.settled_at = "settled", cost, clock.now()
        row.response = {"text": response.text, "usage": response.usage.model_dump() if response.usage else None}
        row.model, row.request_id = response.model, response.request_id
        self.session.commit()

    def mark_unknown(self, request: LLMRequest) -> None:
        self.session.rollback()
        row = self._row(request.call_key)
        if row is not None:
            row.state = "unknown"
            self.session.commit()

    def release(self, request: LLMRequest) -> None:
        self.session.rollback()
        row = self._row(request.call_key)
        if row is not None:
            row.state, row.reserved = "released", 0
            self.session.commit()


class RunGuard:
    """每一步之前检查能不能继续：任务没被取消；发起人仍有分析权限、成员版本没变；档案没出新版本；租约还在。"""

    def __init__(self, session: Session, run_id: str, job: jobs.Job | None) -> None:
        self.session, self.run_id, self.job = session, run_id, job

    def check(self, step: str) -> None:
        self.session.expire_all()
        run = self.session.get(RunRow, self.run_id)
        if run is None or run.status == RunStatus.CANCELLED:
            raise PipelineStopped("cancelled")
        try:
            access = workspace.authorize(self.session, run.actor_id, run.workspace_id, "analyze")
        except AppError:
            raise PipelineStopped("access_revoked") from None
        if access.membership_version != run.membership_version:
            raise PipelineStopped("access_changed")
        if access.current_profile_version != run.profile_version and run.kind != "question":
            # 档案出了新版本：这个任务的输入已经过期，别再为它花钱。报告要按新档案重新发起。
            raise PipelineStopped("profile_version_changed")
        if self.job is not None:
            try:
                jobs.extend(self.session, self.job, RUN_LEASE)
            except jobs.LeaseLost:
                raise PipelineStopped("lease_lost") from None


class DbExtractionCache:
    def __init__(self, session: Session, workspace_id: str | None, reader_workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.reader = reader_workspace_id

    def get(self, key: str) -> dict | None:
        row = self.session.get(RequirementSetRow, key)
        if row is None or (row.workspace_id is not None and row.workspace_id != self.reader):
            return None
        return row.payload

    def put(self, key: str, value: dict) -> None:
        self.session.execute(
            insert(RequirementSetRow)
            .values(key=key, workspace_id=self.workspace_id, payload=value, created_at=clock.now())
            .on_conflict_do_nothing(index_elements=["key"])
        )
        self.session.commit()


class ChunkToolBox:
    """追问用的检索：只在这次研究固定下来的片段里找。"""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks

    def search_chunks(self, query: str, limit: int) -> list[Chunk]:
        terms = [t for t in query.split() if t][:8]
        if not terms:
            return []
        scored = [(sum(t in c.text for t in terms), c) for c in self.chunks]
        return [c for score, c in sorted(scored, key=lambda x: (-x[0], x[1].document_id, x[1].seq)) if score][:limit]

"""research 的表定义，全部放在 PostgreSQL 的 research schema 里。

    runs              研究任务：发起人、公司、固定下来的输入（档案版本、公告版本、文件）、状态
    run_steps         流水线每一步的结果。Worker 崩了重启，已完成的步骤直接复用
    reports           任务完成后的报告（研究报告或追问回答），只插入不修改
    llm_calls         模型调用账本：每次调用先预留、后结算。续跑时靠 call_key 找回已付费的响应
    requirement_sets  抽取结果缓存：同一批文件、同一版抽取规则只抽一次

改了这里的表定义，要用 alembic revision --autogenerate 生成对应的迁移。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bidradar.infra.db import module_metadata


class Base(DeclarativeBase):
    metadata = module_metadata("research")


class RunRow(Base):
    """一个研究任务。创建时就把输入固定下来：之后档案改了版本、公告来了新版本，这个任务和它的报告都不变。

    status：queued → running → succeeded / partial / failed / cancelled / waiting_input
    succeeded 指"研究流程完成"，不指"企业满足条件"。
    waiting_input：模型调用结果不确定（可能已计费）或额度用完，停下来等人处理，不自动重试。
    """

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_workspace_created", "workspace_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(32))
    # 发起时的成员版本。执行中每一步都核对：被移出公司（哪怕又加回来）的人发起的任务会停下。
    membership_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # analysis / question / reassessment
    mode: Mapped[str] = mapped_column(String(16))  # agent / baseline
    notice_id: Mapped[str] = mapped_column(String(64))
    parent_run_id: Mapped[str | None] = mapped_column(String(32))
    question: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(64))
    profile_version: Mapped[int] = mapped_column(Integer)
    catalog_snapshot: Mapped[int] = mapped_column(BigInteger)
    observation_ids: Mapped[list[str]] = mapped_column(JSONB)
    document_ids: Mapped[list[str]] = mapped_column(JSONB)
    config_version: Mapped[str] = mapped_column(String(32))
    versions: Mapped[dict[str, str]] = mapped_column(JSONB)
    estimate_micro: Mapped[int] = mapped_column(BigInteger)
    # 自动复核时，是哪个关注、哪次变化触发的。
    delegation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # 每次状态变化加一。前端按它判断哪个状态更新：晚到的旧响应不会把"已完成"改回"排队中"。
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunStepRow(Base):
    __tablename__ = "run_steps"

    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), primary_key=True)
    input_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    output: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReportRow(Base):
    __tablename__ = "reports"

    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # report / answer
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LlmCallRow(Base):
    """一次模型调用的账本记录。金额单位 micro-CNY（百万分之一元）。

    state：pending（已预留、已外发或正要外发）→ settled（已结算）/ unknown（结果不确定，预留额照算已花）
           / released（确定没发出去，预留额释放）
    """

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_workspace", "workspace_id", "state"),)

    call_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(32), ForeignKey("runs.id"))
    workspace_id: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(String(64))
    reserved: Mapped[int] = mapped_column(BigInteger)
    charged: Mapped[int] = mapped_column(BigInteger, default=0)
    state: Mapped[str] = mapped_column(String(16))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    model: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(128))
    price_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RequirementSetRow(Base):
    """抽取结果缓存。workspace_id 为空表示只用了公开材料，各公司共用；不为空表示含这家公司的私有文件。"""

    __tablename__ = "requirement_sets"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

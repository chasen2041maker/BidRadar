"""Worker 进程入口：从任务表领取后台任务，交给对应模块的 service 执行。

启动：uv run python -m bidradar worker

一次循环（tick）：
    1. claim()：用 SKIP LOCKED 领一个任务，拿到租约。多个 Worker 同时跑互不阻塞，也不会领到同一个任务
    2. 按任务类型找到处理函数，执行
    3. 成功：处理函数自己在写结果的事务里调 jobs.complete()
       业务错误（AppError）：不重试，标为失败，等人处理
       租约丢了（LeaseLost）：说明别人接手了，什么都不做
       其他异常：按重试规则过一会儿再排
    4. 没有任务就睡一会儿

app 是唯一知道"一共有哪些模块、哪些任务类型"的地方，所以任务类型和处理函数的对应关系写在这里。
"""

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.orm import Session, sessionmaker

from bidradar.app.settings import Settings
from bidradar.infra import jobs, outbox
from bidradar.infra.blobs import BlobStore
from bidradar.infra.db import make_engine, make_session_factory
from bidradar.infra.errors import AppError
from bidradar.modules.documents import service as documents
from bidradar.modules.tracking import service as tracking

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Handler:
    run: Callable[[Session, jobs.Job], None]
    # 租约长度按任务类型定：解析几百页的 PDF 要几十秒，租约要比它长，不然没崩也会被别人接手。
    lease: timedelta


def build_handlers(settings: Settings) -> dict[str, Handler]:
    blobs = BlobStore(settings.data_dir / "blobs")
    return {
        documents.JOB_PARSE: Handler(
            run=lambda session, job: documents.handle_parse(session, blobs, job),
            lease=timedelta(minutes=5),
        ),
    }


def worker_id() -> str:
    """日志和租约里记下是哪台机器的哪个进程、哪个线程领的任务，排查问题时用得上。"""
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"[:64]


def tick(session_factory: sessionmaker[Session], handlers: dict[str, Handler], owner: str) -> bool:
    """领取并执行一个任务。领到了返回 True，没有任务返回 False。"""
    with session_factory() as session:
        # 不同类型的租约长度不同：逐个类型领，先到先得。
        job, handler = None, None
        for kind, candidate in handlers.items():
            job = jobs.claim(session, [kind], owner, candidate.lease)
            if job is not None:
                handler = candidate
                break
        if job is None:
            return False
        try:
            handler.run(session, job)
        except jobs.LeaseLost:
            session.rollback()
            logger.warning("任务 %s 的租约已被别人接手，本次结果丢弃", job.id)
        except AppError as error:
            logger.warning("任务 %s 业务错误 %s，不再重试", job.id, error.code)
            jobs.fail(session, job, error.code, retry=False)
        except Exception as error:  # 意料之外的错误：记下类型（不记内容，可能含私有数据），过一会儿重试
            logger.exception("任务 %s 执行出错", job.id)
            jobs.fail(session, job, type(error).__name__)
        return True


def deliver_events(consumer: outbox.OutboxConsumer | None) -> tuple[outbox.OutboxConsumer | None, int]:
    """投递一批 outbox 事件。消费者还没实现（练习 3）时提示一次，之后不再尝试。"""
    if consumer is None:
        return None, 0
    try:
        return consumer, consumer.run_once()
    except NotImplementedError:
        logger.warning("outbox 消费者还没实现（练习 3）：变化检测、站内提醒和自动复核不会运行。见 docs/exercises.md")
        return None, 0


def run(settings: Settings, *, idle_seconds: float = 1.0, stop: threading.Event | None = None) -> None:
    """一直跑，直到 stop 被设置（测试用）或进程被 Ctrl+C 停掉。每一轮先投递事件，再执行一个任务。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    handlers = build_handlers(settings)
    consumer: outbox.OutboxConsumer | None = outbox.OutboxConsumer(session_factory, tracking.EVENT_HANDLERS)
    owner = worker_id()
    logger.info("Worker 启动：%s，任务类型 %s", owner, ", ".join(handlers))
    try:
        while stop is None or not stop.is_set():
            consumer, delivered = deliver_events(consumer)
            if not tick(session_factory, handlers, owner) and not delivered:
                time.sleep(idle_seconds)
    except KeyboardInterrupt:
        logger.info("Worker 停止")
    finally:
        engine.dispose()

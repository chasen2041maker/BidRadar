"""练习 3（项目负责人主写）：outbox 消费者的验收测试。

现在整个文件是跳过的。实现 bidradar/infra/outbox.py 的 OutboxConsumer.run_once() 以后，
删掉下面的 pytestmark，这些测试应该全部通过。要求和提示见 docs/exercises.md 的"练习 3"。

测试里的处理函数故意不做幂等：每调用一次就往数据库写一行"副作用"（借用 infra.idempotency_keys 表，
scope 固定为 test-side-effect，key 每次随机）。所以"副作用只出现一次"只能靠消费者本身保证，
而不是靠处理函数自己去重。
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.infra import clock, outbox
from bidradar.infra.tables import IdempotencyKey, OutboxEvent

pytestmark = pytest.mark.skip(reason="练习 3：outbox 消费者由项目负责人主写，见 docs/exercises.md")


class SessionFactory:
    """把测试会话包装成会话工厂：consumer 每次 with factory() 拿到的都是同一个回滚事务里的会话。"""

    def __init__(self, session: Session) -> None:
        self.session = session

    def __call__(self):
        return self

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *args) -> bool:
        return False


def write_side_effect(session: Session, payload: dict[str, Any]) -> None:
    """每次调用都写一行新的（key 随机），处理两次就会有两行。"""
    session.add(
        IdempotencyKey(
            scope="test-side-effect",
            operation="ping",
            key=uuid.uuid4().hex,
            request_hash="-",
            response=payload,
            created_at=clock.now(),
        )
    )
    session.flush()


def side_effects(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(IdempotencyKey).where(IdempotencyKey.scope == "test-side-effect")
    )


def counting_handler(calls: list[dict[str, Any]]):
    def handler(session: Session, payload: dict[str, Any]) -> None:
        calls.append(payload)
        write_side_effect(session, payload)

    return handler


def publish(session: Session, *numbers: int, topic: str = "test.ping") -> None:
    for n in numbers:
        outbox.publish(session, topic, {"n": n}, event_key=f"ping:{n}")
    session.commit()


def test_events_are_delivered_in_order_and_marked_published(db_session: Session) -> None:
    calls: list[dict] = []
    publish(db_session, 1, 2, 3)
    consumer = outbox.OutboxConsumer(SessionFactory(db_session), {"test.ping": counting_handler(calls)})
    assert consumer.run_once() == 3
    assert [c["n"] for c in calls] == [1, 2, 3]
    pending = db_session.scalar(
        select(func.count())
        .select_from(OutboxEvent)
        .where(OutboxEvent.topic == "test.ping", OutboxEvent.published_at.is_(None))
    )
    assert pending == 0


def test_running_again_does_not_process_twice(db_session: Session) -> None:
    calls: list[dict] = []
    publish(db_session, 1)
    consumer = outbox.OutboxConsumer(SessionFactory(db_session), {"test.ping": counting_handler(calls)})
    consumer.run_once()
    assert consumer.run_once() == 0
    assert side_effects(db_session) == 1


def test_failed_handler_rolls_back_its_writes_and_retries(db_session: Session) -> None:
    """处理函数写了一半就出错：那一半要回滚，事件留着下次重试；重试成功后副作用只有一份。

    同一次 run_once 里，每个事件最多处理一次：失败的事件等下一轮再试，不在这一轮里马上重试。
    """
    attempts = {"n": 0}

    def flaky(session: Session, payload: dict[str, Any]) -> None:
        attempts["n"] += 1
        write_side_effect(session, payload)
        if attempts["n"] == 1:
            raise RuntimeError("第一次失败")

    publish(db_session, 1)
    consumer = outbox.OutboxConsumer(SessionFactory(db_session), {"test.ping": flaky})
    consumer.run_once()
    assert side_effects(db_session) == 0
    event = db_session.scalar(select(OutboxEvent).where(OutboxEvent.topic == "test.ping"))
    assert event.published_at is None and event.attempts == 1 and event.last_error
    consumer.run_once()
    assert side_effects(db_session) == 1


def test_one_bad_event_does_not_block_the_others(db_session: Session) -> None:
    """有毒的事件（每次都失败）重试几次后放弃，后面的事件照常投递。"""
    calls: list[dict] = []

    def handler(session: Session, payload: dict[str, Any]) -> None:
        if payload["n"] == 1:
            raise RuntimeError("永远失败")
        calls.append(payload)

    publish(db_session, 1, 2)
    consumer = outbox.OutboxConsumer(SessionFactory(db_session), {"test.ping": handler}, max_attempts=3)
    for _ in range(5):
        consumer.run_once()
    assert [c["n"] for c in calls] == [2]
    poisoned = db_session.scalar(select(OutboxEvent).where(OutboxEvent.event_key == "ping:1"))
    assert poisoned.attempts == 3


def test_events_without_a_handler_are_skipped_not_stuck(db_session: Session) -> None:
    calls: list[dict] = []
    publish(db_session, 1, topic="nobody.listens")
    publish(db_session, 2)
    consumer = outbox.OutboxConsumer(SessionFactory(db_session), {"test.ping": counting_handler(calls)})
    consumer.run_once()
    assert [c["n"] for c in calls] == [2]
    assert db_session.scalar(select(OutboxEvent.published_at).where(OutboxEvent.event_key == "ping:1")) is not None

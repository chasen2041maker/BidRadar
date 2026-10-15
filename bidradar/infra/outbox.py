"""outbox：业务数据和事件在同一个事务里写入，再由 Worker 投递。

为什么不直接调用订阅方：如果先提交业务数据再通知，进程恰好在中间崩了，通知就丢了；
如果先通知再提交，提交失败时订阅方已经收到一个"不存在"的事件。
写进同一个事务，两件事要么都成功、要么都没发生。投递交给 Worker 慢慢做。

以后拆服务时，只要把投递那一步换成发到 Redis Streams 或 RabbitMQ，发布方的代码不用改。

本文件现在只有发布（publish）。消费者（OutboxConsumer）是阶段 5 留给项目负责人主写的部分，
要求和提示见 docs/exercises.md。
"""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.tables import OutboxEvent


def publish(session: Session, topic: str, payload: dict[str, Any], *, event_key: str) -> None:
    """在当前事务里写入一个事件。不提交事务，由调用方的 service 一起提交。

    event_key 由业务内容决定，同一件事重复发布只保留第一条（ON CONFLICT DO NOTHING）。
    payload 里只放 ID 和版本号这类引用，不放档案正文、公告原文这类大块或私有内容：
    消费者需要内容时，拿着 ID 回去调对方的 service 查，那时还会再检查一次权限。
    """
    session.execute(
        insert(OutboxEvent)
        .values(event_key=event_key, topic=topic, payload=payload, created_at=clock.now(), attempts=0)
        .on_conflict_do_nothing(index_elements=["event_key"])
    )

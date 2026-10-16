"""幂等键：同一个修改请求提交多次，只执行一次。

为什么需要：用户点了"确认档案"，响应在路上丢了，前端用同一个键重试。
没有幂等键，档案会被确认两次，版本号跳两格；有了它，第二次直接返回第一次的结果。

用法（在 service 的同一个事务里）：

    cached = idempotency.replay(session, scope=wid, operation="confirm_profile", key=key, request=request)
    if cached is not None:
        return cached
    ... 真正执行业务动作 ...
    return idempotency.remember(session, scope=wid, operation="confirm_profile", key=key,
                                request=request, response=result)

request 里要包含操作人：别的成员拿同一个键来，指纹对不上，会报冲突，而不是拿到别人的结果。
"""

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.errors import AppError, conflict
from bidradar.infra.ids import fingerprint
from bidradar.infra.tables import IdempotencyKey

KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


def replay(session: Session, *, scope: str, operation: str, key: str, request: Any) -> Any | None:
    """返回这个键上次的结果；没用过返回 None。同一个键但请求内容不同，报 idempotency_conflict。

    先拿一把以键为名的事务级锁：两个相同的请求同时到达时，第二个会等第一个提交，
    然后直接读到第一个的结果，而不是两个都执行。锁在事务结束时自动释放。
    """
    if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key):
        raise AppError("invalid_idempotency_key")
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"{scope}|{operation}|{key}", 0))))
    row = session.get(IdempotencyKey, (scope, operation, key))
    if row is None:
        return None
    if row.request_hash != fingerprint(request):
        raise conflict("idempotency_conflict")
    return row.response


def remember(session: Session, *, scope: str, operation: str, key: str, request: Any, response: Any) -> Any:
    """记下结果，和业务数据在同一个事务里提交。返回 response，方便直接 return。"""
    session.add(
        IdempotencyKey(
            scope=scope,
            operation=operation,
            key=key,
            request_hash=fingerprint(request),
            response=response,
            created_at=clock.now(),
        )
    )
    return response

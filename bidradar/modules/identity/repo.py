"""identity 的数据库读写，只给本模块的 service 用。

函数接收会话参数，只读写，不提交事务：一个业务动作里可能要写好几张表，什么时候提交由 service 决定。
"""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from bidradar.modules.identity.models import User
from bidradar.modules.identity.tables import LoginLimitRow, SessionRow, UserRow


def to_user(row: UserRow) -> User:
    return User(id=row.id, username=row.username, display_name=row.display_name)


def get_user_by_username(session: Session, username: str) -> UserRow | None:
    return session.scalar(select(UserRow).where(UserRow.username == username))


def get_users(session: Session, user_ids: Iterable[str]) -> list[UserRow]:
    ids = list(set(user_ids))
    if not ids:
        return []
    return list(session.scalars(select(UserRow).where(UserRow.id.in_(ids))))


def add_user(session: Session, row: UserRow) -> None:
    session.add(row)
    session.flush()


def get_session_user(session: Session, token_hash: str, now: datetime) -> UserRow | None:
    """令牌对应的、还没过期的会话的用户。"""
    return session.scalar(
        select(UserRow)
        .join(SessionRow, SessionRow.user_id == UserRow.id)
        .where(SessionRow.token_hash == token_hash, SessionRow.expires_at > now)
    )


def get_session(session: Session, token_hash: str) -> SessionRow | None:
    return session.get(SessionRow, token_hash)


def add_session(session: Session, row: SessionRow) -> None:
    session.add(row)


def delete_session(session: Session, token_hash: str) -> None:
    session.execute(delete(SessionRow).where(SessionRow.token_hash == token_hash))


def delete_expired_sessions(session: Session, now: datetime) -> None:
    session.execute(delete(SessionRow).where(SessionRow.expires_at <= now))


def lock_login_limit(session: Session, username_key: str, now: datetime) -> LoginLimitRow:
    """取出这个用户名的失败计数，并锁住这一行，直到事务结束。

    先插一行"零次失败"再加锁：两个人同时输错同一个用户名时，不会都读到"没有记录"，各自从 1 开始计数。
    """
    session.execute(
        insert(LoginLimitRow)
        .values(username_key=username_key, window_started=now, failures=0)
        .on_conflict_do_nothing(index_elements=["username_key"])
    )
    return session.scalars(
        select(LoginLimitRow).where(LoginLimitRow.username_key == username_key).with_for_update()
    ).one()


def delete_login_limit(session: Session, username_key: str) -> None:
    session.execute(delete(LoginLimitRow).where(LoginLimitRow.username_key == username_key))

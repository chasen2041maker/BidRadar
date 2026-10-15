"""identity 的业务用例，连真实 PostgreSQL，每个测试结束后回滚。"""

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from bidradar.infra import clock
from bidradar.infra.errors import AppError
from bidradar.modules.identity import models, service
from bidradar.modules.identity.tables import LoginLimitRow
from tests_v2.factories import PASSWORD, make_user


def test_create_user_and_login(db_session: Session) -> None:
    user = make_user(db_session, "Alice.Dev")
    assert user.username == "alice.dev"
    result = service.login(db_session, "ALICE.dev", PASSWORD)
    assert result.user == user
    assert service.current_user(db_session, result.session_token) == user


def test_duplicate_username_rejected(db_session: Session) -> None:
    make_user(db_session, "alice")
    with pytest.raises(AppError, match="user_exists"):
        make_user(db_session, "ALICE")


def test_wrong_password_and_unknown_user_give_the_same_error(db_session: Session) -> None:
    make_user(db_session, "alice")
    with pytest.raises(AppError) as wrong:
        service.login(db_session, "alice", "wrong-password-123")
    with pytest.raises(AppError) as unknown:
        service.login(db_session, "nobody", PASSWORD)
    assert (
        (wrong.value.code, wrong.value.status)
        == (unknown.value.code, unknown.value.status)
        == (
            "invalid_credentials",
            401,
        )
    )


def test_failures_are_recorded_even_though_login_raises(db_session: Session) -> None:
    """失败次数要在报错之前提交，否则回滚以后限流就失效了。"""
    make_user(db_session, "alice")
    with pytest.raises(AppError):
        service.login(db_session, "alice", "wrong-password-123")
    db_session.rollback()  # 模拟请求结束时的回滚：已提交的失败次数应该还在
    row = db_session.scalar(select(LoginLimitRow).where(LoginLimitRow.username_key == models.token_hash("alice")))
    assert row is not None and row.failures == 1


def test_lockout_blocks_even_the_right_password(db_session: Session) -> None:
    make_user(db_session, "alice")
    for _ in range(models.LOGIN_FAILURE_LIMIT):
        with pytest.raises(AppError, match="invalid_credentials"):
            service.login(db_session, "alice", "wrong-password-123")
    with pytest.raises(AppError, match="login_limited") as error:
        service.login(db_session, "alice", PASSWORD)
    assert error.value.status == 429


def test_lockout_ends_after_window(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    make_user(db_session, "alice")
    for _ in range(models.LOGIN_FAILURE_LIMIT):
        with pytest.raises(AppError):
            service.login(db_session, "alice", "wrong-password-123")
    later = clock.now() + models.LOGIN_WINDOW + timedelta(seconds=1)
    monkeypatch.setattr(clock, "now", lambda: later)
    assert service.login(db_session, "alice", PASSWORD).user.username == "alice"


def test_successful_login_resets_failure_count(db_session: Session) -> None:
    make_user(db_session, "alice")
    for _ in range(models.LOGIN_FAILURE_LIMIT - 1):
        with pytest.raises(AppError):
            service.login(db_session, "alice", "wrong-password-123")
    service.login(db_session, "alice", PASSWORD)
    for _ in range(models.LOGIN_FAILURE_LIMIT - 1):
        with pytest.raises(AppError, match="invalid_credentials"):
            service.login(db_session, "alice", "wrong-password-123")


def test_session_expires_after_eight_hours(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    make_user(db_session, "alice")
    token = service.login(db_session, "alice", PASSWORD).session_token
    later = clock.now() + models.SESSION_TTL + timedelta(seconds=1)
    monkeypatch.setattr(clock, "now", lambda: later)
    with pytest.raises(AppError, match="authentication_required"):
        service.current_user(db_session, token)


def test_logout_invalidates_session(db_session: Session) -> None:
    make_user(db_session, "alice")
    token = service.login(db_session, "alice", PASSWORD).session_token
    service.logout(db_session, token)
    with pytest.raises(AppError, match="authentication_required"):
        service.current_user(db_session, token)


@pytest.mark.parametrize("token", [None, "", "x" * 43, "not a token"])
def test_bad_tokens_are_unauthenticated(db_session: Session, token: object) -> None:
    with pytest.raises(AppError, match="authentication_required"):
        service.current_user(db_session, token)


def test_csrf_must_match_the_session(db_session: Session) -> None:
    make_user(db_session, "alice")
    make_user(db_session, "bob")
    alice = service.login(db_session, "alice", PASSWORD)
    bob = service.login(db_session, "bob", PASSWORD)
    service.check_csrf(db_session, alice.session_token, alice.csrf_token)
    with pytest.raises(AppError, match="csrf_failed"):
        service.check_csrf(db_session, alice.session_token, bob.csrf_token)
    with pytest.raises(AppError, match="csrf_failed"):
        service.check_csrf(db_session, alice.session_token, None)


def test_get_users_returns_public_fields_only(db_session: Session) -> None:
    alice = make_user(db_session, "alice")
    users = service.get_users(db_session, [alice.id, "f" * 32])
    assert users == {alice.id: alice}
    assert not hasattr(users[alice.id], "password_hash")

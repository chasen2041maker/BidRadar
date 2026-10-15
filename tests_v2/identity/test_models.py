"""identity 的纯逻辑：密码哈希、用户名、登录限流的时间窗口。不连数据库。"""

from datetime import UTC, datetime, timedelta

import pytest

from bidradar.infra.errors import AppError
from bidradar.modules.identity import models

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def test_password_hash_round_trip() -> None:
    encoded = models.hash_password("correct-horse-battery")
    assert encoded.startswith("pbkdf2_sha256$")
    assert models.verify_password("correct-horse-battery", encoded)
    assert not models.verify_password("wrong-password-123", encoded)


def test_same_password_gets_different_hashes() -> None:
    """每个用户一个随机盐：两个人用了同一个密码，库里也看不出来。"""
    assert models.hash_password("same-password-xx") != models.hash_password("same-password-xx")


def test_verify_against_missing_user_is_false() -> None:
    assert models.verify_password("anything-at-all", None) is False


def test_verify_rejects_garbage_hash() -> None:
    assert models.verify_password("anything-at-all", "not-a-hash") is False
    assert models.verify_password("anything-at-all", "md5$1$abc$def") is False


def test_old_hashes_still_verify_after_iterations_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """迭代次数存在哈希里：以后调高默认值，老用户照样能登录。"""
    old = models.hash_password("correct-horse-battery", iterations=500)
    monkeypatch.setattr(models, "PASSWORD_ITERATIONS", 2000)
    assert models.verify_password("correct-horse-battery", old)


def test_username_is_case_insensitive() -> None:
    assert models.normalize_username("Demo.Admin") == "demo.admin"


@pytest.mark.parametrize("bad", ["ab", "has space", "中文名", "x" * 65, None, 123])
def test_bad_usernames_rejected(bad: object) -> None:
    with pytest.raises(AppError, match="invalid_username"):
        models.normalize_username(bad)


@pytest.mark.parametrize("bad", ["short", "x" * 1025, "has\x00nul-character", None])
def test_bad_new_passwords_rejected(bad: object) -> None:
    with pytest.raises(AppError, match="invalid_password"):
        models.check_new_password(bad)


def test_token_format() -> None:
    token = models.new_token()
    assert models.valid_token(token)
    assert not models.valid_token(token + "x")
    assert not models.valid_token(None)


def test_login_lock_after_five_failures_in_window() -> None:
    started, failures = None, 0
    for i in range(models.LOGIN_FAILURE_LIMIT):
        assert not models.login_locked(started, failures, T0 + timedelta(minutes=i))
        started, failures = models.after_failure(started, failures, T0 + timedelta(minutes=i))
    assert failures == 5
    assert models.login_locked(started, failures, T0 + timedelta(minutes=10))


def test_login_lock_expires_with_the_window() -> None:
    assert not models.login_locked(T0, 5, T0 + models.LOGIN_WINDOW)


def test_failure_after_window_starts_a_new_count() -> None:
    assert models.after_failure(T0, 4, T0 + models.LOGIN_WINDOW) == (T0 + models.LOGIN_WINDOW, 1)

"""练习 1（项目负责人主写）：行级安全（RLS），公司隔离的第二道防线。

现在整个文件是跳过的。做完练习后删掉下面的 pytestmark，这些测试应该全部通过。
要求、提示和常见的坑见 docs/exercises.md 的"练习 1"。

为什么要第二道防线：应用层的隔离靠"每个 repo 函数都带 workspace_id 条件"。
哪天有人写了一句漏掉条件的查询，第一道防线就破了。RLS 让数据库自己保证：
就算 SQL 里没写 WHERE workspace_id = ...，也只能看到当前公司的行。

下面的测试故意绕开 repo，直接写原始 SQL，模拟"有人漏写了条件"。
"""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests_v2.factories import confirm_profile, make_company

pytestmark = pytest.mark.skip(reason="练习 1：RLS 由项目负责人主写，见 docs/exercises.md")

# 练习里要建的数据库角色：应用平时用它连库。它不是表的所有者，所以受 RLS 约束。
APP_ROLE = "bidradar_app"


def scoped(session: Session, workspace_id: str) -> None:
    """模拟一个请求开始时做的事：切到应用角色，告诉数据库"这个事务只代表这家公司"。"""
    session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    session.execute(text("SELECT set_config('app.workspace_id', :wid, true)"), {"wid": workspace_id})


def test_raw_sql_only_sees_own_company(db_session: Session) -> None:
    a, users_a = make_company(db_session, "a")
    b, users_b = make_company(db_session, "b")
    confirm_profile(db_session, a, users_a["admin"])
    confirm_profile(db_session, b, users_b["admin"])

    scoped(db_session, a)
    rows = db_session.execute(text("SELECT workspace_id FROM workspace.profile_versions")).scalars().all()
    assert set(rows) == {a}


def test_cannot_insert_into_another_company(db_session: Session) -> None:
    a, _ = make_company(db_session, "a")
    b, users_b = make_company(db_session, "b")

    scoped(db_session, a)
    with pytest.raises(Exception):
        db_session.execute(
            text(
                "INSERT INTO workspace.profile_proposals(id, workspace_id, base_version, payload, proposed_by, created_at)"
                " VALUES (:id, :wid, 0, '{}'::jsonb, :uid, now())"
            ),
            {"id": "f" * 32, "wid": b, "uid": users_b["admin"].id},
        )


def test_no_scope_means_no_rows(db_session: Session) -> None:
    """忘了设置 app.workspace_id 时，应该什么都看不到，而不是什么都看得到。"""
    a, users_a = make_company(db_session, "a")
    confirm_profile(db_session, a, users_a["admin"])

    db_session.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
    assert db_session.execute(text("SELECT count(*) FROM workspace.profile_versions")).scalar() == 0

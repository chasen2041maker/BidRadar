"""R1-A 的虚构企业持久层验收：当前会话/成员、公司隔离、版本、幂等及重启。

不访问真实企业或外部服务；SQLite并发实验不等于PostgreSQL/RLS验收。
"""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from unittest.mock import patch

from services.workspace.store import WorkspaceStore, WorkspaceError, PROFILE_LIMITS, PASSWORD_ITERATIONS, SESSION_SECONDS, LOGIN_WINDOW_SECONDS


PASSWORD = "fictional-password-2026!"


def payload(company="虚构甲公司", **changes):
    return {**{key: None for key in PROFILE_LIMITS}, "company_name": company, **changes}


def items(number="1"):
    return [{"notice_id": sha256(("notice" + number).encode()).hexdigest(),
             "observation_id": sha256(("observation" + number).encode()).hexdigest(),
             "title": "虚构软件采购公告", "source_url": "https://example.test/notice/" + number, "simulation": True}]


class WorkspaceStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed_temp = tempfile.TemporaryDirectory()
        cls.seed_root = Path(cls.seed_temp.name) / "seed"
        store = WorkspaceStore(cls.seed_root)
        cls.admin = store.bootstrap_user("admin-a", PASSWORD, "虚构管理员甲")
        cls.other = store.bootstrap_user("admin-b", PASSWORD, "虚构管理员乙")
        cls.member = store.bootstrap_user("member-a", PASSWORD, "虚构成员")
        cls.viewer = store.bootstrap_user("viewer-a", PASSWORD, "虚构只读成员")
        cls.wid = store.bootstrap_workspace("虚构甲公司", cls.admin)
        cls.other_wid = store.bootstrap_workspace("虚构乙公司", cls.other)
        store.bootstrap_member(cls.wid, cls.member, "member")
        store.bootstrap_member(cls.wid, cls.viewer, "viewer")
        store.close()

    @classmethod
    def tearDownClass(cls):
        cls.seed_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspace"
        # 种子连接已关闭；每个实验得到独立数据库、凭据和文件作用域。
        shutil.copytree(self.seed_root, self.root)
        self.store = WorkspaceStore(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def error(self, code, callback, status=None):
        with self.assertRaises(WorkspaceError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def reopen(self):
        self.store.close()
        self.store = WorkspaceStore(self.root)

    def confirmed(self, *, revision=0, name="虚构甲公司"):
        proposal = self.store.propose_profile(self.member, self.wid, payload(name), revision, "propose-" + str(revision))
        return self.store.confirm_profile(self.admin, self.wid, proposal["id"], revision, "confirm-" + str(revision))

    def selected(self, key="choose", *, profile_revision=1):
        return self.store.create_selection(self.member, self.wid, profile_revision, items(), key)

    def race(self, callbacks):
        barrier = Barrier(len(callbacks))
        def run(callback):
            store = WorkspaceStore(self.root)
            try:
                barrier.wait(timeout=10)
                try:
                    return ("ok", callback(store))
                except WorkspaceError as exc:
                    return ("error", exc.code)
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:
            return list(pool.map(run, callbacks))

    def test_passwords_use_salted_kdf_and_sessions_store_hashes_only(self):
        rows = self.store.db.execute("SELECT password_salt,password_hash,password_iterations FROM users").fetchall()
        self.assertEqual(len({bytes(row[0]) for row in rows}), 4)
        self.assertEqual(len({bytes(row[1]) for row in rows}), 4)
        self.assertTrue(all(len(row[0]) == 16 and row[2] == PASSWORD_ITERATIONS for row in rows))
        login = self.store.login("ADMIN-A", PASSWORD)
        self.assertEqual(set(login["user"]), {"id", "username", "display_name"})
        self.assertEqual(len(login["token"]), 43)
        row = self.store.db.execute("SELECT token_hash,csrf_hash,expires_at-created_at FROM sessions").fetchone()
        self.assertEqual(row[0], sha256(login["token"].encode()).hexdigest())
        self.assertEqual(row[1], sha256(login["csrf_token"].encode()).hexdigest())
        self.assertNotIn(login["token"], " ".join(str(value) for value in row))
        self.assertEqual(row[2], SESSION_SECONDS)

    def test_bootstrap_never_replaces_password_or_existing_membership(self):
        self.error("user_exists", lambda: self.store.bootstrap_user("ADMIN-A", "different-password", "替代名称"), 409)
        self.assertEqual(self.store.login("admin-a", PASSWORD)["user"]["display_name"], "虚构管理员甲")
        self.store.change_member(self.admin, self.wid, self.member, "viewer", False, 1)
        self.error("member_exists", lambda: self.store.bootstrap_member(self.wid, self.member, "admin"), 409)
        row = next(row for row in self.store.members(self.admin, self.wid) if row["user_id"] == self.member)
        self.assertEqual((row["role"], row["active"]), ("viewer", False))

    def test_login_failures_are_uniform_and_rate_limit_persists_after_restart(self):
        with patch("services.workspace.store._now", return_value=1_000):
            for name in ("admin-a", "missing-user"):
                for _ in range(5):
                    self.error("invalid_credentials", lambda name=name: self.store.login(name, "wrong-password"), 401)
            self.reopen()
            for name in ("admin-a", "missing-user"):
                self.error("login_limited", lambda name=name: self.store.login(name, PASSWORD), 429)
        with patch("services.workspace.store._now", return_value=1_000 + LOGIN_WINDOW_SECONDS):
            self.assertEqual(self.store.login("admin-a", PASSWORD)["user"]["id"], self.admin)
            self.error("invalid_credentials", lambda: self.store.login("missing-user", PASSWORD), 401)

    def test_csrf_is_session_bound_and_expired_or_logged_out_tokens_fail(self):
        with patch("services.workspace.store._now", return_value=1_000):
            first = self.store.login("admin-a", PASSWORD)
            second = self.store.login("admin-a", PASSWORD)
            self.store.check_csrf(first["token"], first["csrf_token"])
            for wrong in (second["csrf_token"], "", "\ud800"):
                self.error("csrf_failed", lambda wrong=wrong: self.store.check_csrf(first["token"], wrong), 403)
            self.store.logout(first["token"])
            self.store.logout(first["token"])
            self.error("authentication_required", lambda: self.store.authenticate(first["token"]), 401)
        with patch("services.workspace.store._now", return_value=1_000 + SESSION_SECONDS):
            self.error("authentication_required", lambda: self.store.authenticate(second["token"]), 401)

    def test_bound_session_rechecks_logout_before_writes_and_cached_replies(self):
        login = self.store.login("member-a", PASSWORD)
        bound = WorkspaceStore(self.root)
        try:
            self.assertEqual(bound.bind_session(login["token"])["id"], self.member)
            bound.propose_profile(self.member, self.wid, payload(), 0, "same")
            self.store.logout(login["token"])
            # 模拟HTTP完成目录读取后、进入写事务前被另一请求注销。
            self.error("authentication_required", lambda: bound.propose_profile(self.member, self.wid, payload(), 0, "same"), 401)
            self.error("authentication_required", lambda: bound.profile(self.member, self.wid), 401)
            self.error("authentication_required", lambda: bound.list_workspaces(self.member), 401)
        finally:
            bound.close()

    def test_binding_never_allows_different_user_or_failed_bind_to_fall_back_to_cli(self):
        login = self.store.login("member-a", PASSWORD)
        self.store.bind_session(login["token"])
        self.error("authentication_required", lambda: self.store.profile(self.admin, self.wid), 401)
        self.error("authentication_required", lambda: self.store.bind_session("bad"), 401)
        self.error("authentication_required", lambda: self.store.profile(self.member, self.wid), 401)

    def test_bound_http_instance_cannot_bootstrap_or_log_out_another_session(self):
        first = self.store.login("admin-a", PASSWORD)
        other = self.store.login("admin-b", PASSWORD)
        self.store.bind_session(first["token"])
        self.error("authentication_required", lambda: self.store.logout(other["token"]), 401)
        self.assertEqual(self.store.authenticate(other["token"])["id"], self.other)
        self.error("bootstrap_requires_cli", lambda: self.store.bootstrap_user("new-admin", PASSWORD, "虚构新增管理员"), 403)
        self.error("bootstrap_requires_cli", lambda: self.store.bootstrap_workspace("虚构新公司", self.admin), 403)
        self.error("bootstrap_requires_cli", lambda: self.store.bootstrap_member(self.wid, self.other, "admin"), 403)

    def test_bound_session_expiry_is_checked_again_after_initial_authentication(self):
        with patch("services.workspace.store._now", return_value=1000):
            login = self.store.login("member-a", PASSWORD)
            self.store.bind_session(login["token"])
        with patch("services.workspace.store._now", return_value=1000 + SESSION_SECONDS):
            self.error("authentication_required", lambda: self.store.propose_profile(self.member, self.wid, payload(), 0, "after-expiry"), 401)

    def test_companies_are_isolated_and_missing_objects_have_same_error(self):
        self.confirmed()
        selection = self.selected()
        for forbidden in (self.wid, "not-a-workspace"):
            self.error("not_found", lambda forbidden=forbidden: self.store.profile(self.other, forbidden), 404)
            self.error("not_found", lambda forbidden=forbidden: self.store.members(self.other, forbidden), 404)
            self.error("not_found", lambda forbidden=forbidden: self.store.selections(self.other, forbidden), 404)
        for selection_id in (selection["id"], "unknown-selection"):
            self.error("not_found", lambda selection_id=selection_id: self.store.selection(self.other, self.other_wid, selection_id), 404)
        self.assertEqual(self.store.list_workspaces(self.other), [{"workspace_id": self.other_wid, "name": "虚构乙公司", "role": "admin"}])

    def test_old_session_reflects_demotion_and_removal_without_deleting_history(self):
        login = self.store.login("member-a", PASSWORD)
        self.confirmed()
        selected = self.selected()
        self.store.change_member(self.admin, self.wid, self.member, "viewer", True, 1)
        bound = WorkspaceStore(self.root)
        try:
            bound.bind_session(login["token"])
            self.assertEqual(bound.selection(self.member, self.wid, selected["id"])["id"], selected["id"])
            self.error("forbidden", lambda: bound.create_selection(self.member, self.wid, 1, items(), "choose"), 403)
            self.store.change_member(self.admin, self.wid, self.member, "viewer", False, 2)
            self.error("not_found", lambda: bound.profile(self.member, self.wid), 404)
            self.assertEqual(bound.list_workspaces(self.member), [])
            self.assertEqual(self.store.profile(self.admin, self.wid)["current"]["revision"], 1)
        finally:
            bound.close()

    def test_last_admin_protection_and_concurrent_self_demotion(self):
        for role, active in (("member", True), ("admin", False)):
            self.error("last_admin", lambda role=role, active=active: self.store.change_member(self.admin, self.wid, self.admin, role, active, 1), 409)
        self.store.change_member(self.admin, self.wid, self.member, "admin", True, 1)
        results = self.race([lambda store: store.change_member(self.admin, self.wid, self.admin, "member", True, 1),
                             lambda store: store.change_member(self.member, self.wid, self.member, "member", True, 2)])
        self.assertEqual(sorted(result[0] for result in results), ["error", "ok"])
        self.assertIn(("error", "last_admin"), results)
        self.assertEqual(sum(row["active"] and row["role"] == "admin" for row in self.store.members(self.admin, self.wid)), 1)

    def test_stale_admin_role_edit_cannot_restore_revoked_member(self):
        self.store.change_member(self.admin, self.wid, self.viewer, "admin", True, 1)
        stale = next(row for row in self.store.members(self.viewer, self.wid) if row["user_id"] == self.member)
        revoked = self.store.change_member(self.admin, self.wid, self.member, "member", False, stale["version"])
        # 第二位管理员仍持有active=True的旧列表，保存角色不能撤销刚发生的撤权。
        self.error("membership_version_conflict", lambda: self.store.change_member(
            self.viewer, self.wid, self.member, "viewer", stale["active"], stale["version"]), 409)
        self.assertEqual(next(row for row in self.store.members(self.viewer, self.wid) if row["user_id"] == self.member), revoked)
        self.assertEqual((revoked["active"], revoked["role"], revoked["version"]), (False, "member", 2))

    def test_stale_admin_removal_cannot_overwrite_new_role(self):
        self.store.change_member(self.admin, self.wid, self.viewer, "admin", True, 1)
        stale = next(row for row in self.store.members(self.viewer, self.wid) if row["user_id"] == self.member)
        changed = self.store.change_member(self.admin, self.wid, self.member, "viewer", True, stale["version"])
        self.error("membership_version_conflict", lambda: self.store.change_member(
            self.viewer, self.wid, self.member, stale["role"], False, stale["version"]), 409)
        self.assertEqual(next(row for row in self.store.members(self.viewer, self.wid) if row["user_id"] == self.member), changed)
        for invalid in (False, 0, "2"):
            self.error("invalid_version", lambda invalid=invalid: self.store.change_member(
                self.admin, self.wid, self.member, "viewer", False, invalid))

    def test_member_audit_is_immutable_scoped_and_restored_after_restart(self):
        self.store.change_member(self.admin, self.wid, self.member, "viewer", False, 1)
        history = self.store.member_history(self.admin, self.wid, self.member)
        self.assertEqual([row["version"] for row in history], [1, 2])
        first, changed = history
        self.assertEqual((first["actor_id"], first["actor_type"], first["old_role"], first["old_active"]), (None, "bootstrap", None, None))
        self.assertEqual((first["new_role"], first["new_active"]), ("member", True))
        self.assertEqual((changed["actor_id"], changed["actor_type"], changed["old_role"], changed["old_active"], changed["new_role"], changed["new_active"]),
                         (self.admin, "user", "member", True, "viewer", False))
        self.assertTrue(all(row["recorded_at"].endswith("Z") for row in history))
        self.error("forbidden", lambda: self.store.member_history(self.viewer, self.wid, self.member), 403)
        self.error("not_found", lambda: self.store.member_history(self.other, self.other_wid, self.member), 404)
        self.error("not_found", lambda: self.store.member_history(self.other, self.wid, self.member), 404)
        for sql in ("UPDATE membership_events SET new_active=1", "DELETE FROM membership_events"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.store.db.execute(sql)
        self.reopen()
        self.assertEqual(self.store.member_history(self.admin, self.wid, self.member), history)

    def test_membership_and_audit_commit_atomically(self):
        before = self.store.members(self.admin, self.wid)
        with patch.object(self.store, "_membership_event", side_effect=WorkspaceError("fixture_failure", 503)):
            self.error("fixture_failure", lambda: self.store.change_member(self.admin, self.wid, self.member, "viewer", False, 1), 503)
        self.assertEqual(self.store.members(self.admin, self.wid), before)
        self.assertEqual(len(self.store.member_history(self.admin, self.wid, self.member)), 1)

    def test_member_proposes_admin_confirms_and_proof_states_are_not_inferred(self):
        values = payload(qualifications="用户声称证书齐全有效，系统不得据此认证")
        proposal = self.store.propose_profile(self.member, self.wid, values, 0, "propose")
        self.error("forbidden", lambda: self.store.propose_profile(self.viewer, self.wid, values, 0, "viewer"), 403)
        self.error("forbidden", lambda: self.store.confirm_profile(self.member, self.wid, proposal["id"], 0, "confirm"), 403)
        revision = self.store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm")
        self.assertEqual(revision["payload"], values)
        self.assertEqual((revision["proof_status"], revision["read_status"], revision["validity_status"]),
                         ("not_provided", "not_attempted", "not_checked"))
        read = self.store.profile(self.viewer, self.wid)
        self.assertEqual(read["current"], revision)
        self.assertEqual(read["proposals"][0]["status"], "confirmed")

    def test_profile_contract_rejects_bad_fields_types_lengths_and_versions(self):
        invalid = [payload(company_name=""), payload(company_name=None), payload(capabilities=5),
                   payload(city="x" * 201), payload(qualifications="x" * 4001), payload(city="\ud800"),
                   {**payload(), "proof_status": "verified"}]
        missing = payload(); del missing["cases"]; invalid.append(missing)
        for index, value in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(WorkspaceError):
                self.store.propose_profile(self.member, self.wid, value, 0, "bad-" + str(index))
        for version in (False, -1, "0"):
            self.error("invalid_version", lambda version=version: self.store.propose_profile(self.member, self.wid, payload(), version, "bad-version"))
        self.assertEqual(self.store.profile(self.admin, self.wid)["proposals"], [])

    def test_stale_proposal_cannot_be_confirmed_by_changing_expected_version(self):
        first = self.store.propose_profile(self.member, self.wid, payload("版本一"), 0, "first")
        second = self.store.propose_profile(self.member, self.wid, payload("版本二"), 0, "second")
        self.store.confirm_profile(self.admin, self.wid, first["id"], 0, "confirm-first")
        for expected in (0, 1):
            self.error("profile_revision_conflict", lambda expected=expected: self.store.confirm_profile(self.admin, self.wid, second["id"], expected, "confirm-second"), 409)
        self.error("profile_revision_conflict", lambda: self.store.propose_profile(self.member, self.wid, payload(), 0, "stale"), 409)
        third = self.store.propose_profile(self.member, self.wid, payload("正式版本二"), 1, "third")
        self.store.confirm_profile(self.admin, self.wid, third["id"], 1, "confirm-third")
        history = self.store.profile(self.viewer, self.wid)["history"]
        self.assertEqual([(row["revision"], row["payload"]["company_name"]) for row in history], [(2, "正式版本二"), (1, "版本一")])

    def test_two_simultaneous_confirmations_create_only_one_revision(self):
        proposal = self.store.propose_profile(self.member, self.wid, payload(), 0, "propose")
        results = self.race([lambda store: store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm-a"),
                             lambda store: store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm-b")])
        self.assertEqual(sorted(result[0] for result in results), ["error", "ok"])
        self.assertIn(("error", "proposal_already_confirmed"), results)
        self.assertEqual(len(self.store.profile(self.admin, self.wid)["history"]), 1)

    def test_idempotency_preserves_original_result_and_rejects_different_actor_or_payload(self):
        original = self.store.propose_profile(self.member, self.wid, payload(), 0, "same")
        self.assertEqual(self.store.propose_profile(self.member, self.wid, payload(), 0, "same"), original)
        self.error("idempotency_conflict", lambda: self.store.propose_profile(self.member, self.wid, payload("变化"), 0, "same"), 409)
        self.error("idempotency_conflict", lambda: self.store.propose_profile(self.admin, self.wid, payload(), 0, "same"), 409)
        revision = self.store.confirm_profile(self.admin, self.wid, original["id"], 0, "confirm")
        self.assertEqual(self.store.confirm_profile(self.admin, self.wid, original["id"], 0, "confirm"), revision)
        self.assertEqual(self.store.propose_profile(self.member, self.wid, payload(), 0, "same"), original)
        self.error("proposal_already_confirmed", lambda: self.store.confirm_profile(self.admin, self.wid, original["id"], 1, "different"), 409)
        self.store.change_member(self.admin, self.wid, self.member, "member", False, 1)
        self.error("not_found", lambda: self.store.propose_profile(self.member, self.wid, payload(), 0, "same"), 404)

    def test_proposal_id_is_scoped_to_company_before_any_confirmation(self):
        proposal = self.store.propose_profile(self.member, self.wid, payload(), 0, "same")
        for proposal_id in (proposal["id"], "missing"):
            self.error("not_found", lambda proposal_id=proposal_id: self.store.confirm_profile(self.other, self.other_wid, proposal_id, 0, "same"), 404)
        other = self.store.propose_profile(self.other, self.other_wid, payload("虚构乙公司"), 0, "same")
        self.assertNotEqual(proposal["id"], other["id"])

    def test_selection_requires_formal_current_profile_and_nonempty_fixed_items(self):
        self.error("profile_revision_conflict", lambda: self.selected(), 409)
        self.confirmed()
        cases = [[], items() * 2, items() * 21, [{**items()[0], "simulation": "true"}],
                 [{**items()[0], "notice_id": "not-a-hash"}], [{**items()[0], "source_url": "file:///private"}],
                 [{**items()[0], "extra": True}]]
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(WorkspaceError):
                self.store.create_selection(self.member, self.wid, 1, case, "bad-" + str(index))
        self.assertEqual(self.store.selections(self.member, self.wid), [])
        self.error("invalid_version", lambda: self.store.create_selection(self.member, self.wid, False, items(), "bad-version"))

    def test_shared_selection_lifecycle_preserves_history_without_analysis(self):
        self.confirmed()
        chosen = self.selected()
        self.assertEqual((chosen["state"], chosen["version"]), ("awaiting_decision", 1))
        self.assertEqual(self.store.selection(self.viewer, self.wid, chosen["id"]), chosen)
        self.error("forbidden", lambda: self.store.confirm_selection(self.viewer, self.wid, chosen["id"], 1, "viewer"), 403)
        confirmed = self.store.confirm_selection(self.admin, self.wid, chosen["id"], 1, "confirm")
        self.assertEqual((confirmed["state"], confirmed["version"]), ("selected", 2))
        self.assertEqual([event["state"] for event in confirmed["history"]], ["awaiting_decision", "selected"])
        self.assertEqual(confirmed["items"], chosen["items"])
        self.assertEqual(self.store.confirm_selection(self.admin, self.wid, chosen["id"], 1, "confirm"), confirmed)
        self.error("selection_version_conflict", lambda: self.store.cancel_selection(self.admin, self.wid, chosen["id"], 2, "cancel"), 409)
        self.assertNotIn("analysis_task_id", confirmed)

    def test_concurrent_confirm_cancel_has_one_terminal_outcome(self):
        self.confirmed()
        chosen = self.selected()
        results = self.race([lambda store: store.confirm_selection(self.admin, self.wid, chosen["id"], 1, "confirm"),
                             lambda store: store.cancel_selection(self.member, self.wid, chosen["id"], 1, "cancel")])
        self.assertEqual(sorted(result[0] for result in results), ["error", "ok"])
        self.assertIn(("error", "selection_version_conflict"), results)
        self.assertEqual(len(self.store.selection(self.member, self.wid, chosen["id"])["history"]), 2)

    def test_profile_change_blocks_confirmation_but_allows_explicit_cancel(self):
        self.confirmed()
        chosen = self.selected()
        self.confirmed(revision=1, name="虚构甲公司新声明")
        self.error("profile_revision_conflict", lambda: self.store.confirm_selection(self.member, self.wid, chosen["id"], 1, "confirm"), 409)
        self.error("profile_revision_conflict", lambda: self.selected("new-stale"), 409)
        cancelled = self.store.cancel_selection(self.admin, self.wid, chosen["id"], 1, "cancel")
        self.assertEqual((cancelled["state"], cancelled["profile_revision"]), ("cancelled", 1))
        self.assertEqual(cancelled["items"], chosen["items"])
        self.error("selection_version_conflict", lambda: self.store.confirm_selection(self.member, self.wid, chosen["id"], 2, "after-cancel"), 409)

    def test_catalog_check_time_and_items_are_frozen_and_not_idempotency_noise(self):
        self.confirmed()
        source_items = items()
        chosen = self.store.create_selection(self.member, self.wid, 1, source_items, "choose", checked_at="2026-09-30T12:00:00Z")
        source_items[0]["title"] = "调用者后续修改"
        self.assertNotEqual(self.store.selection(self.member, self.wid, chosen["id"])["items"], source_items)
        retry = self.store.create_selection(self.member, self.wid, 1, items(), "choose", checked_at="2026-09-30T13:00:00Z")
        self.assertEqual(retry, chosen)
        confirmed = self.store.confirm_selection(self.member, self.wid, chosen["id"], 1, "confirm", checked_at="2026-09-30T14:00:00Z")
        self.assertEqual([h["catalog_checked_at"] for h in confirmed["history"]], ["2026-09-30T12:00:00Z", "2026-09-30T14:00:00Z"])
        self.error("invalid_catalog_checked_at", lambda: self.store.create_selection(self.member, self.wid, 1, items("2"), "bad-time", checked_at="2026-09-30T14:00:00"))

    def test_selection_replay_survives_restart_profile_update_and_no_network(self):
        self.confirmed()
        chosen = self.selected()
        self.confirmed(revision=1, name="后续正式档案")
        self.reopen()
        references = [{key: value for key, value in item.items() if key in ("notice_id", "observation_id")} for item in items()]
        # 已完成命令读取自己的冻结结果；目录不可达/已更新都不要求再次联网。
        with patch("socket.socket", side_effect=AssertionError("replay_must_not_use_catalog_network")):
            self.assertEqual(self.store.replay_selection(self.member, self.wid, 1, references, "choose"), chosen)
            self.assertIsNone(self.store.replay_selection(self.member, self.wid, 1, references, "missing-key"))
        self.assertEqual(len(self.store.selections(self.member, self.wid)), 1)
        self.assertEqual(self.store.profile(self.member, self.wid)["current"]["revision"], 2)

    def test_selection_replay_checks_actor_request_and_current_permission(self):
        self.confirmed()
        self.selected()
        refs = [{name: item[name] for name in ("notice_id", "observation_id")} for item in items()]
        self.error("idempotency_conflict", lambda: self.store.replay_selection(self.admin, self.wid, 1, refs, "choose"), 409)
        self.error("idempotency_conflict", lambda: self.store.replay_selection(self.member, self.wid, 2, refs, "choose"), 409)
        changed = [{**refs[0], "observation_id": "a" * 64}]
        self.error("idempotency_conflict", lambda: self.store.replay_selection(self.member, self.wid, 1, changed, "choose"), 409)
        for invalid in ([], refs * 2, refs * 21, [{**refs[0], "title": "伪造标题"}], [{"notice_id": "bad", "observation_id": "a" * 64}]):
            with self.assertRaises(WorkspaceError):
                self.store.replay_selection(self.member, self.wid, 1, invalid, "choose")
        self.store.change_member(self.admin, self.wid, self.member, "viewer", True, 1)
        self.error("forbidden", lambda: self.store.replay_selection(self.member, self.wid, 1, refs, "choose"), 403)
        self.store.change_member(self.admin, self.wid, self.member, "viewer", False, 2)
        self.error("not_found", lambda: self.store.replay_selection(self.member, self.wid, 1, refs, "choose"), 404)

    def test_failed_command_rolls_back_revision_and_current_pointer_together(self):
        proposal = self.store.propose_profile(self.member, self.wid, payload(), 0, "propose")
        with patch.object(self.store, "_remember", side_effect=WorkspaceError("fixture_failure", 503)):
            self.error("fixture_failure", lambda: self.store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm"), 503)
        self.assertIsNone(self.store.profile(self.admin, self.wid)["current"])
        self.assertEqual(self.store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm")["revision"], 1)

    def test_restart_restores_sessions_profile_selection_and_immutable_history(self):
        login = self.store.login("member-a", PASSWORD)
        self.confirmed()
        chosen = self.selected()
        final = self.store.confirm_selection(self.member, self.wid, chosen["id"], 1, "confirm")
        profile = self.store.profile(self.member, self.wid)
        self.reopen()
        self.assertEqual(self.store.bind_session(login["token"])["id"], self.member)
        self.assertEqual(self.store.profile(self.member, self.wid), profile)
        self.assertEqual(self.store.selection(self.member, self.wid, chosen["id"]), final)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_cached_confirmations_recheck_current_role(self):
        proposal = self.store.propose_profile(self.member, self.wid, payload(), 0, "propose")
        self.store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm-profile")
        chosen = self.selected()
        self.store.confirm_selection(self.member, self.wid, chosen["id"], 1, "confirm-selection")
        self.store.change_member(self.admin, self.wid, self.viewer, "admin", True, 1)
        self.store.change_member(self.viewer, self.wid, self.admin, "member", True, 1)
        self.store.change_member(self.viewer, self.wid, self.member, "viewer", True, 1)
        self.error("forbidden", lambda: self.store.confirm_profile(self.admin, self.wid, proposal["id"], 0, "confirm-profile"), 403)
        self.error("forbidden", lambda: self.store.confirm_selection(self.member, self.wid, chosen["id"], 1, "confirm-selection"), 403)

    def test_process_crash_before_commit_leaves_no_partial_proposal_or_cached_result(self):
        self.store.close()
        child = """import os, sys
from services.workspace.store import WorkspaceStore, PROFILE_LIMITS
store = WorkspaceStore(sys.argv[1])
store._remember = lambda *args: os._exit(73)
value = {key: None for key in PROFILE_LIMITS}
value['company_name'] = 'Fictional crash fixture'
store.propose_profile(sys.argv[2], sys.argv[3], value, 0, 'crash-key')
"""
        # 子进程在插入建议后、幂等结果/事务提交前硬退出，验证真实进程中断恢复。
        result = subprocess.run([sys.executable, "-c", child, str(self.root), self.member, self.wid],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73)
        self.store = WorkspaceStore(self.root)
        self.assertEqual(self.store.profile(self.admin, self.wid), {"current": None, "history": [], "proposals": []})
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.store.propose_profile(self.member, self.wid, payload(), 0, "crash-key")["status"], "proposed")

    def test_lists_are_bounded_and_fail_explicitly_instead_of_hiding_records(self):
        self.store.propose_profile(self.member, self.wid, payload(), 0, "first")
        self.store.propose_profile(self.member, self.wid, payload(), 0, "second")
        with patch("services.workspace.store.LIST_LIMIT", 1):
            self.error("list_limit_exceeded", lambda: self.store.profile(self.member, self.wid), 413)
        self.assertEqual(len(self.store.profile(self.member, self.wid)["proposals"]), 2)

    def test_invalid_store_root_does_not_adopt_existing_unrelated_files(self):
        root = Path(self.temp.name) / "unrelated"
        root.mkdir()
        (root / "keep.txt").write_text("fixture", encoding="utf-8")
        self.error("store_directory_not_empty", lambda: WorkspaceStore(root))
        self.assertEqual((root / "keep.txt").read_text(encoding="utf-8"), "fixture")


if __name__ == "__main__":
    unittest.main()

"""虚构输入验证R2事务、授权、固定版本和恢复；不调用供应商或读取生产数据。"""
from copy import deepcopy
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from datetime import datetime
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from services.common.local_http import LocalClient, LocalHTTPError
from services.common.owner_clients import WorkspaceAccessClient
from services.research.store import ResearchStore
from services.tracking.diff import compare_bundles, fingerprint, input_fingerprint
from services.tracking.http_api import create_server
from services.tracking.service import TrackingService
from services.tracking.store import TrackingStore, TrackingError
from services.tracking.worker import tick
from services.workspace.http_api import create_server as create_workspace_server
from services.workspace.store import PROFILE_LIMITS, WorkspaceStore

NOTICE = sha256(b"fictional-notice").hexdigest()
OTHER = sha256(b"other-notice").hexdigest()
ACTOR, VIEWER, SECOND, WID, OTHER_WID = "member-a", "viewer-a", "member-b", "company-a", "company-b"


class Access:
    def __init__(self):
        self.members = {(ACTOR, WID): ["member", 1], (VIEWER, WID): ["viewer", 1], (SECOND, OTHER_WID): ["admin", 1]}
        self.profiles = {WID: 1, OTHER_WID: 1}
        self.feed = []

    def authorize(self, actor, wid, action):
        membership = self.members.get((actor, wid))
        if membership is None:
            raise TrackingError("not_found", 404)
        if action in ("analyze", "track") and membership[0] == "viewer":
            raise TrackingError("forbidden", 403)
        return {"role": membership[0], "membership_version": membership[1], "current_profile_revision": self.profiles[wid]}

    def events(self, after, limit):
        values = self.feed[after:after + limit]
        return {"schema_version": 1, "items": values, "next_after": values[-1]["seq"] if values else after, "high_watermark": len(self.feed)}

    def emit(self, kind, *, profile=None, membership=None, actor=ACTOR):
        seq = len(self.feed) + 1
        event = {"seq": seq, "event_id": "workspace-" + str(seq), "event_type": kind, "workspace_id": WID,
                 "actor_id": ACTOR, "target_user_id": actor if membership else None, "profile_revision": profile,
                 "membership_version": membership, "occurred_at": "2026-10-01T00:00:00Z"}
        self.feed.append(event)
        return event


class Catalog:
    def __init__(self):
        self.data, self.feed = [], []
        self.add()

    def add(self, *, title="虚构采购", raw=None, facts=None, extra=None, notice=NOTICE, kind="procurement", relations=None):
        seq = len(self.data) + 1
        observation = {"notice_id": notice, "observation_id": sha256((str(seq) + title).encode()).hexdigest(),
                       "raw_sha256": raw or sha256(title.encode()).hexdigest(), "title": title, "notice_type": kind,
                       "identity_kind": "source_url", "source_url": "https://example.test/fictional", "facts": facts or {},
                       "evidence_fields": extra or {}, "references": [], "material_status": "not_obtained"}
        self.data.append((observation, relations or []))
        self.feed.append({"seq": seq, "event_id": "catalog-" + str(seq), "notice_id": notice,
                          "observation_id": observation["observation_id"], "observed_at": "2026-10-01T00:00:00Z"})
        return deepcopy(observation)

    def bundle(self, notice, snapshot=None):
        snapshot = len(self.data) if snapshot is None else snapshot
        current = {item["notice_id"]: (item, relations) for item, relations in self.data[:snapshot]}
        if notice not in current:
            raise TrackingError("not_found", 404)
        item = current[notice][0]
        related = [(other, relation) for other, relations in current.values() for relation in relations if relation.get("target_notice_id") == notice]
        incoming = [other for other, relation in related if relation["status"] == "evidenced"]
        return deepcopy({"notice_id": notice, "snapshot": snapshot, "current": item, "observations": [item] + incoming,
                         "relationships": [relation for _, relation in related]})

    def changes(self, after, limit):
        values = self.feed[after:after + limit]
        return {"schema_version": 1, "items": deepcopy(values), "next_after": values[-1]["seq"] if values else after,
                "high_watermark": len(self.feed)}


class Research:
    def __init__(self):
        self.runs, self.creates, self.cancels = {}, 0, 0
        self.lose_response = False

    def command(self, wid, actor, key):
        if (wid, actor, key) not in self.runs:
            raise TrackingError("not_found", 404)
        return deepcopy(self.runs[(wid, actor, key)])

    def create_analysis(self, request):
        key = (request["workspace_id"], request["actor_id"], request["key"])
        if key not in self.runs:
            self.creates += 1
            self.runs[key] = {"id": "run-" + str(self.creates), "state": "queued", "workspace_id": request["workspace_id"]}
        if self.lose_response:
            self.lose_response = False
            raise TrackingError("dependency_unavailable", 503)
        return deepcopy(self.runs[key])

    def cancel(self, run_id, wid, actor, key):
        self.cancels += 1
        for (run_wid, run_actor, _), run in self.runs.items():
            if run["id"] == run_id and run_wid == wid and run_actor == actor:
                run["state"] = "cancelled"
                return deepcopy(run)
        raise TrackingError("not_found", 404)


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "tracking"
        self.access, self.catalog, self.research = Access(), Catalog(), Research()
        self.store = TrackingStore(self.root)
        self.service = TrackingService(self.store, self.access, self.catalog, self.research)

    def tearDown(self):
        self.store.close()
        if hasattr(self, "research_owner"):
            self.research_owner.close()
        self.temp.cleanup()

    def watch(self, auto=False):
        return self.service.set_watch(ACTOR, WID, NOTICE, True, 0, "watch", auto_reassess=auto)

    def changed(self, auto=True):
        watch = self.watch(auto)
        self.catalog.add(title="更改后的虚构需求")
        self.service.poll("catalog")
        return watch

    def command(self):
        return self.service.reassessments(ACTOR, WID)["items"][0]

    def error(self, code, function):
        with self.assertRaises(TrackingError) as raised:
            function()
        self.assertEqual(raised.exception.code, code)

    def ready(self):
        self.store.db.execute("UPDATE reassessments SET next_attempt=0")

    def reopen(self):
        self.store.close()
        self.store = TrackingStore(self.root)
        self.service = TrackingService(self.store, self.access, self.catalog, self.research)

    def test_decision_and_watch_are_independent_versioned_and_idempotent(self):
        decision = self.service.set_decision(ACTOR, WID, NOTICE, "dismissed", "人工决定不等于资质判断", 0, "decision")
        watch = self.watch()
        self.assertFalse(watch["auto_reassess"])
        self.assertIsNone(watch["expires_at"])
        self.service.set_watch(ACTOR, WID, NOTICE, False, 1, "stop")
        self.assertEqual(self.service.decision(ACTOR, WID, NOTICE)["current"], decision)
        self.assertEqual(self.service.set_decision(ACTOR, WID, NOTICE, "dismissed", "人工决定不等于资质判断", 0, "decision"), decision)
        self.error("idempotency_conflict", lambda: self.service.set_decision(ACTOR, WID, NOTICE, "follow_up", "", 0, "decision"))
        self.error("decision_version_conflict", lambda: self.service.set_decision(ACTOR, WID, NOTICE, "follow_up", "", 0, "new"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.db.execute("DELETE FROM decision_history")

    def test_two_companies_viewer_and_revocation_do_not_bypass_cached_commands(self):
        self.watch()
        self.error("not_found", lambda: self.service.watch(SECOND, WID, NOTICE))
        self.error("forbidden", lambda: self.service.set_watch(VIEWER, WID, NOTICE, False, 1, "viewer"))
        self.assertIsNone(self.service.watch(SECOND, OTHER_WID, NOTICE)["watch"])
        del self.access.members[(ACTOR, WID)]
        self.error("not_found", lambda: self.watch())

    def test_watch_expiry_is_finite_and_invalid_scope_rejected(self):
        watch = self.watch(True)
        self.assertIsNotNone(watch["expires_at"])
        self.error("invalid_delegation_expiry", lambda: self.service.set_watch(ACTOR, WID, NOTICE, True, 1, "forever", auto_reassess=True, expires_at="2999-01-01T00:00:00Z"))
        self.error("invalid_watch_scope", lambda: self.service.set_watch(ACTOR, WID, NOTICE, True, 1, "scope", scope=["arbitrary_tool"]))
        self.error("watch_version_conflict", lambda: self.service.set_watch(ACTOR, WID, NOTICE, False, 0, "old"))

    def test_expired_or_tampered_delegation_does_not_dispatch_research(self):
        watch = self.changed()
        claim = self.service.claim("worker")
        self.error("invalid_delegation", lambda: self.service.check_delegation(ACTOR, WID, {**claim["request"]["delegation"], "extra": True}))
        self.error("delegation_invalid", lambda: self.service.check_delegation(ACTOR, WID, {**claim["request"]["delegation"], "input_fingerprint": "a" * 64}))
        expiry = datetime.fromisoformat(watch["expires_at"].replace("Z", "+00:00")).timestamp()
        with patch("services.tracking.store.time.time", return_value=expiry + 1):
            self.error("delegation_invalid", lambda: self.service.check_delegation(ACTOR, WID, claim["request"]["delegation"]))
        self.assertEqual(self.research.creates, 0)

    def test_notice_identity_warning_does_not_prohibit_tracking(self):
        self.catalog.data[0][0]["identity_kind"] = "content_snapshot"
        self.assertIn("unstable_content_snapshot_identity", self.watch()["warnings"])

    def test_no_delegation_records_change_and_unique_notification_without_model(self):
        self.changed(False)
        self.assertEqual(len(self.service.changes(ACTOR, WID)["items"]), 1)
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)
        self.assertEqual(self.service.reassessments(ACTOR, WID)["items"], [])
        self.service.consume_event("catalog", self.catalog.feed[-1])
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)
        self.assertEqual(self.research.creates, 0)

    def test_notification_read_is_per_user_and_scoped(self):
        self.changed(False)
        notification = self.service.notifications(ACTOR, WID)["items"][0]
        first = self.service.mark_read(ACTOR, WID, notification["id"])
        self.assertEqual(self.service.mark_read(ACTOR, WID, notification["id"]), first)
        self.assertIsNotNone(self.service.notifications(ACTOR, WID)["items"][0]["read_at"])
        self.assertIsNone(self.service.notifications(VIEWER, WID)["items"][0]["read_at"])
        self.error("not_found", lambda: self.service.mark_read(SECOND, OTHER_WID, notification["id"]))

    def test_global_cursor_does_not_skip_new_watch_baseline_race(self):
        baseline = self.catalog.bundle(NOTICE)
        original = self.catalog.bundle
        def racing(notice, snapshot=None):
            self.catalog.add(title="baseline读取后到达")
            self.service.poll("catalog")
            return baseline
        with patch.object(self.catalog, "bundle", side_effect=racing):
            self.watch()
        self.assertEqual(self.store.cursor("catalog"), 2)
        self.service.poll("catalog")
        changes = self.service.changes(ACTOR, WID)["items"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["event_seq"], 2)
        self.assertEqual(self.service.watch(ACTOR, WID, NOTICE)["watch"]["catalog_seq"], 2)

    def test_each_intermediate_version_is_processed_not_latest_only(self):
        self.watch()
        self.catalog.add(title="B")
        self.catalog.add(title="虚构采购")
        self.service.poll("catalog")
        values = self.service.changes(ACTOR, WID)["items"]
        self.assertEqual([v["catalog_snapshot"] for v in values], [2, 3])
        self.assertNotEqual(values[0]["fingerprint"], values[1]["fingerprint"])

    def test_recapture_without_business_change_and_locator_noise_is_not_alert(self):
        self.watch()
        self.catalog.add()
        self.service.poll("catalog")
        self.assertEqual(self.service.notifications(ACTOR, WID)["items"], [])
        self.assertEqual(self.service.watch(ACTOR, WID, NOTICE)["watch"]["catalog_seq"], 2)

    def test_raw_only_change_is_unclassified_not_proven_formatting(self):
        self.watch()
        self.catalog.add(raw="a" * 64)
        self.service.poll("catalog")
        self.assertEqual(self.service.changes(ACTOR, WID)["items"][0]["diff"]["kind"], "unclassified_content_change")

    def test_incoming_evidenced_and_candidate_relationships_preserve_scope(self):
        self.watch()
        self.catalog.add(notice=OTHER, title="虚构更正", kind="correction", relations=[{"status": "candidate", "target_notice_id": NOTICE, "source_notice_id": OTHER}])
        self.service.poll("catalog")
        first = self.service.changes(ACTOR, WID)["items"][0]
        self.assertEqual(len(first["diff"]["after_observation_ids"]), 1)
        self.catalog.add(notice=OTHER, title="虚构更正", kind="correction", relations=[{"status": "evidenced", "target_notice_id": NOTICE, "source_notice_id": OTHER}])
        self.service.poll("catalog")
        self.assertEqual(len(self.service.changes(ACTOR, WID)["items"][-1]["diff"]["after_observation_ids"]), 2)

    def test_event_gap_conflicting_replay_and_unknown_schema_fail_closed(self):
        self.watch()
        self.catalog.add(title="B")
        self.error("event_gap", lambda: self.service.consume_event("catalog", self.catalog.feed[1]))
        self.service.poll("catalog")
        bad = {**self.catalog.feed[0], "event_id": "different"}
        self.error("event_conflict", lambda: self.service.consume_event("catalog", bad))
        with patch.object(self.catalog, "changes", return_value={"schema_version": 2, "items": [], "next_after": 2, "high_watermark": 2}):
            self.error("invalid_event_page", lambda: self.service.poll("catalog"))

    def test_rebuild_updates_projection_without_notifications_or_commands(self):
        self.watch(True)
        self.catalog.add(title="B")
        self.service.poll("catalog", replay=True)
        self.assertEqual(len(self.service.changes(ACTOR, WID)["items"]), 1)
        self.assertEqual(self.service.notifications(ACTOR, WID)["items"], [])
        self.assertEqual(self.service.reassessments(ACTOR, WID)["items"], [])
        self.service.consume_event("catalog", self.catalog.feed[-1])
        self.assertEqual(self.service.notifications(ACTOR, WID)["items"], [])

    def test_profile_change_freezes_new_revision_and_marks_stale(self):
        self.watch(True)
        self.access.profiles[WID] = 2
        self.access.emit("ProfileConfirmed", profile=2)
        self.service.poll("workspace")
        change = self.service.changes(ACTOR, WID)["items"][0]
        self.assertEqual((change["profile_revision"], change["diff"]["kind"], change["stale"]), (2, "profile_change", True))
        request = self.service.claim("worker")["request"]
        self.assertEqual(request["profile_revision"], 2)
        self.assertEqual(self.service.check_delegation(ACTOR, WID, request["delegation"])["profile_revision"], 2)

    def test_auto_request_fixes_inputs_and_terminal_reconciliation_deduplicates(self):
        self.changed()
        self.assertEqual(self.service.dispatch_one()["state"], "accepted")
        self.assertEqual(self.research.creates, 1)
        for run in self.research.runs.values():
            run["state"] = "succeeded"
        self.ready()
        self.assertEqual(self.service.dispatch_one()["state"], "succeeded")
        self.assertIsNone(self.service.dispatch_one())
        self.assertEqual([x["kind"] for x in self.service.notifications(ACTOR, WID)["items"]], ["change_detected", "reassessment_completed"])
        request = next(iter(self.store.db.execute("SELECT request FROM reassessments")))[0]
        import json
        request = json.loads(request)
        bundle = self.catalog.bundle(NOTICE, snapshot=2)
        self.assertEqual(request["delegation"]["input_fingerprint"], input_fingerprint(NOTICE, 1, 2, [x["observation_id"] for x in bundle["observations"]]))

    def test_lost_create_response_reuses_command_after_restart(self):
        self.changed()
        self.research.lose_response = True
        self.assertEqual(self.service.dispatch_one()["state"], "pending")
        self.reopen(); self.ready()
        self.assertEqual(self.service.dispatch_one()["state"], "accepted")
        self.assertEqual(self.research.creates, 1)

    def test_real_research_waiting_input_is_terminal_and_keeps_billing_reason(self):
        self.changed()
        owner = ResearchStore(Path(self.temp.name) / "research")
        self.research_owner = owner

        def create(request):
            bundle = self.catalog.bundle(NOTICE, snapshot=request["catalog_snapshot"])
            return owner.create(request, fingerprint(request), {"profile": {"revision": 1},
                                "observations": bundle["observations"], "catalog_snapshot": bundle["snapshot"]}, 1)

        def lookup(wid, actor, key):
            result = owner.command(wid, actor, key)
            if result is None:
                raise TrackingError("not_found", 404)
            return result

        # 使用真实research持久层的stop/public响应，避免测试夹具再造错终态契约。
        with patch.object(self.research, "create_analysis", side_effect=create) as created, patch.object(self.research, "command", side_effect=lookup) as queried:
            self.assertEqual(self.service.dispatch_one()["state"], "accepted")
            owner.stop(owner.claim("research-worker"), "waiting_input", "billing_unknown")
            self.ready()
            stopped = self.service.dispatch_one()
            self.assertEqual((stopped["state"], stopped["reason"]), ("waiting_input", "billing_unknown"))
            self.assertIsNone(owner.claim("research-worker"))
            query_count = queried.call_count
            self.reopen(); self.ready()
            self.service.consume_event("catalog", self.catalog.feed[-1])
            self.assertIsNone(self.service.dispatch_one())
            self.error("reassessment_version_conflict", lambda: self.service.retry_reassessment(ACTOR, WID, stopped["id"], stopped["version"], "no-new-charge"))
            self.error("reassessment_version_conflict", lambda: self.service.cancel_reassessment(ACTOR, WID, stopped["id"], stopped["version"], "already-stopped"))
            # 后续失权/关闭watch不能抹去计费未知的历史终态，也不会把它变成可派送命令。
            self.access.members[(ACTOR, WID)] = ["member", 3]
            self.access.emit("AccessChanged", membership=3)
            self.service.poll("workspace")
            self.service.set_watch(ACTOR, WID, NOTICE, False, 1, "stop")
            self.assertEqual(self.command(), stopped)
            self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 0)
            self.assertEqual(queried.call_count, query_count)
            self.assertEqual(created.call_count, 1)
            self.assertEqual(owner.db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            raw = self.store.db.execute("SELECT result FROM reassessments WHERE id=?", (stopped["id"],)).fetchone()[0]
            history = self.store.db.execute("SELECT payload FROM reassessment_history WHERE reassessment_id=? ORDER BY seq DESC LIMIT 1", (stopped["id"],)).fetchone()[0]
            self.assertEqual(json.loads(raw)["reason"], "billing_unknown")
            self.assertEqual(json.loads(history)["reason"], "billing_unknown")
            self.assertEqual([x["kind"] for x in self.service.notifications(ACTOR, WID)["items"]], ["change_detected"])

    def test_cancelled_reconciliation_does_not_cancel_remote_waiting_input(self):
        self.changed(); self.service.dispatch_one()
        for run in self.research.runs.values():
            run.update(state="waiting_input", reason="billing_unknown")
        self.service.set_watch(ACTOR, WID, NOTICE, False, 1, "stop")
        self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 1)
        self.assertEqual(self.research.cancels, 0)
        self.assertEqual(self.command()["state"], "cancelled")

    def test_demotion_and_restore_does_not_reactivate_old_delegation(self):
        self.changed()
        self.access.members[(ACTOR, WID)] = ["member", 3]
        self.access.emit("AccessChanged", membership=3)
        self.service.poll("workspace")
        self.assertEqual(self.command()["state"], "cancelled")
        self.assertIsNone(self.service.dispatch_one())
        self.assertEqual(self.research.creates, 0)

    def test_authorization_unavailable_does_not_advance_event_or_call_model(self):
        self.watch(True)
        self.catalog.add(title="B")
        self.service.consume_event("catalog", self.catalog.feed[0])
        with patch.object(self.access, "authorize", side_effect=TrackingError("dependency_unavailable", 503)):
            self.error("dependency_unavailable", lambda: self.service.poll("catalog"))
        self.assertEqual(self.store.cursor("catalog"), 1)
        self.assertEqual(self.research.creates, 0)

    def test_stop_watch_cancels_pending_and_late_success_only_history(self):
        self.changed()
        self.service.dispatch_one()
        for run in self.research.runs.values():
            run["state"] = "succeeded"
        self.service.set_watch(ACTOR, WID, NOTICE, False, 1, "stop")
        self.assertEqual(self.command()["state"], "cancelled")
        self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 1)
        self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 0)
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)
        self.assertFalse(self.service.watch(ACTOR, WID, NOTICE)["watch"]["active"])

    def test_explicit_cancel_stops_remote_without_changing_watch(self):
        self.changed()
        current = self.service.dispatch_one()
        value = self.service.cancel_reassessment(ACTOR, WID, current["id"], current["version"], "cancel")
        self.assertEqual(self.service.cancel_reassessment(ACTOR, WID, current["id"], current["version"], "cancel"), value)
        self.service.reconcile_cancelled()
        self.assertEqual(self.research.cancels, 1)
        self.assertTrue(self.service.watch(ACTOR, WID, NOTICE)["watch"]["active"])

    def test_old_worker_lease_cannot_publish_after_takeover(self):
        self.changed()
        first = self.service.claim("first")
        self.store.db.execute("UPDATE reassessments SET lease_until=0")
        second = self.service.claim("second")
        self.assertGreater(second["lease_epoch"], first["lease_epoch"])
        self.error("lease_lost", lambda: self.service.finish(first, {"id": "run", "state": "succeeded"}))
        self.service.finish(second, {"id": "run", "state": "succeeded"})
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 2)

    def test_new_change_cancels_previous_automatic_input_without_erasing_history(self):
        self.changed()
        old = self.service.claim("old")
        self.catalog.add(title="第三个输入版本")
        self.service.poll("catalog")
        values = self.service.reassessments(ACTOR, WID)["items"]
        self.assertEqual([x["state"] for x in values], ["pending", "cancelled"])
        self.error("delegation_invalid", lambda: self.service.finish(old, {"id": "old-run", "state": "succeeded"}))
        self.assertEqual(len(self.service.changes(ACTOR, WID)["items"]), 2)
        self.assertEqual([n["kind"] for n in self.service.notifications(ACTOR, WID)["items"]], ["change_detected", "change_detected"])

    def test_cancelled_request_not_found_can_be_reconciled_if_create_arrives_late(self):
        self.changed()
        claim = self.service.claim("old")
        current = self.command()
        self.service.cancel_reassessment(ACTOR, WID, current["id"], current["version"], "cancel")
        self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 0)
        self.research.create_analysis(claim["request"])
        self.ready()
        self.assertEqual(self.service.reconcile_cancelled()["reconciled"], 1)
        self.assertEqual(self.research.cancels, 1)
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)

    def test_cancel_during_remote_dependency_rejects_stale_worker(self):
        self.changed()
        claim = self.service.claim("worker")
        original = self.catalog.bundle
        def cancelled(*args, **kwargs):
            self.service.set_watch(ACTOR, WID, NOTICE, False, 1, "stop")
            return original(*args, **kwargs)
        with patch.object(self.catalog, "bundle", side_effect=cancelled):
            self.error("delegation_invalid", lambda: self.service.finish(claim, {"id": "run", "state": "succeeded"}))
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)

    def test_three_dependency_failures_defer_and_explicit_retry_preserves_key(self):
        self.changed()
        with patch.object(self.research, "command", side_effect=TrackingError("dependency_unavailable", 503)):
            for _ in range(3):
                self.ready(); self.service.dispatch_one()
        before = self.command()
        self.assertEqual(before["state"], "deferred")
        retried = self.service.retry_reassessment(ACTOR, WID, before["id"], before["version"], "retry")
        self.assertEqual(retried["command_key"], before["command_key"])
        self.assertEqual(self.service.dispatch_one()["state"], "accepted")

    def test_event_failure_rolls_back_change_notification_job_and_cursor_together(self):
        self.watch(True)
        self.catalog.add(title="B")
        self.service.consume_event("catalog", self.catalog.feed[0])
        with patch.object(self.store, "notify", side_effect=TrackingError("fixture_failure", 503)):
            self.error("fixture_failure", lambda: self.service.poll("catalog"))
        self.assertEqual(self.store.cursor("catalog"), 1)
        self.assertEqual(self.service.changes(ACTOR, WID)["items"], [])
        self.assertEqual(self.service.reassessments(ACTOR, WID)["items"], [])
        self.service.poll("catalog")
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)

    def test_restart_preserves_history_cursors_notifications_and_sqlite_integrity(self):
        self.changed(False)
        before = self.service.changes(ACTOR, WID)
        self.reopen()
        self.assertEqual(self.service.changes(ACTOR, WID), before)
        self.service.poll("catalog")
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)
        self.assertEqual(self.store.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_process_crash_rolls_back_inflight_event_and_resumes_once(self):
        self.watch(True)
        self.catalog.add(title="更改后的虚构需求")
        self.store.close()
        script = """import os, sys
sys.path.insert(0, str(__import__('pathlib').Path.cwd() / 'tests'))
from test_tracking import Access, Catalog, Research
from services.tracking.store import TrackingStore
from services.tracking.service import TrackingService
store = TrackingStore(sys.argv[1])
catalog = Catalog(); catalog.add(title='更改后的虚构需求')
service = TrackingService(store, Access(), catalog, Research())
store.notify = lambda *args, **kwargs: os._exit(73)
service.poll('catalog')
"""
        # 在变化行已写入、提醒/游标尚未提交时硬退出；不用正常close冒充进程中断。
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 73)
        self.store = TrackingStore(self.root)
        self.service = TrackingService(self.store, self.access, self.catalog, self.research)
        self.assertEqual(self.store.cursor("catalog"), 1)
        self.assertEqual(self.service.changes(ACTOR, WID)["items"], [])
        self.service.poll("catalog")
        self.assertEqual(len(self.service.notifications(ACTOR, WID)["items"]), 1)
        self.assertEqual(len(self.service.reassessments(ACTOR, WID)["items"]), 1)

    def test_diff_preserves_money_role_package_timezone_precision_and_unknown(self):
        before = self.catalog.bundle(NOTICE)
        self.catalog.add(facts={"response_deadline": {"status": "unknown", "value": None}},
                         extra={"money": [{"role": "unit_price", "amount": "5", "package": "C", "unit_basis": "人次"}]})
        difference = compare_bundles(before, self.catalog.bundle(NOTICE))
        self.assertIn("money", difference["scopes"])
        self.assertIn("deadline", difference["scopes"])
        self.assertEqual(difference["kind"], "material_change")

    def test_http_enforces_caller_identity_strict_body_and_current_company(self):
        server = create_server(self.root, self.access, self.catalog, self.research,
                               tokens={"workspace": "w" * 32, "research": "r" * 32})
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            url = "http://127.0.0.1:" + str(server.server_port)
            work, research = LocalClient(url, "w" * 32), LocalClient(url, "r" * 32)
            body = {"schema_version": 1, "actor_id": ACTOR, "workspace_id": WID, "notice_id": NOTICE}
            self.assertIsNone(work.request("POST", "/internal/v1/watches/get", body)["watch"])
            for client, value, status in ((research, body, 403), (work, {**body, "extra": 1}, 400), (work, {**body, "workspace_id": OTHER_WID}, 404)):
                with self.assertRaises(LocalHTTPError) as raised:
                    client.request("POST", "/internal/v1/watches/get", value)
                self.assertEqual(raised.exception.status, status)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)


class WorkspaceTrackingContractTests(unittest.TestCase):
    """真实owner建档/迁移经回环HTTP消费；两个服务各写自己的虚构开发库。"""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp.name) / "workspace"
        self.tracking_root = Path(self.temp.name) / "tracking"
        self.owner = WorkspaceStore(self.workspace_root)
        self.actor = self.owner.bootstrap_user("fixture-admin", "fictional-password", "虚构管理员")
        self.wid = self.owner.bootstrap_workspace("虚构契约测试公司", self.actor)
        self.payload = {field: None for field in PROFILE_LIMITS}
        self.payload["company_name"] = "虚构契约测试公司"
        self.confirm(0)

    def tearDown(self):
        self.owner.close()
        self.temp.cleanup()

    def confirm(self, base):
        proposal = self.owner.propose_profile(self.actor, self.wid, self.payload, base, "propose-" + str(base))
        return self.owner.confirm_profile(self.actor, self.wid, proposal["id"], base, "confirm-" + str(base))

    def exercise_owner_feed(self):
        server = create_workspace_server(self.workspace_root, "http://127.0.0.1:1", "c" * 32,
                                         internal_tokens={"tracking": "t" * 32})
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        store = TrackingStore(self.tracking_root)
        try:
            client = WorkspaceAccessClient("http://127.0.0.1:" + str(server.server_port), "t" * 32)
            catalog, research = Catalog(), Research()
            service = TrackingService(store, client, catalog, research)
            initial = client.events()
            self.assertEqual([x["event_type"] for x in initial["items"]], ["AccessChanged", "ProfileConfirmed"])
            profile = initial["items"][1]
            self.assertEqual(set(profile), {"seq", "event_id", "event_type", "workspace_id", "actor_id", "target_user_id", "profile_revision", "membership_version", "occurred_at"})
            self.assertEqual((profile["profile_revision"], profile["target_user_id"], profile["membership_version"]), (1, None, None))
            self.assertTrue(profile["occurred_at"].endswith("Z"))
            service.set_watch(self.actor, self.wid, NOTICE, True, 0, "watch")
            self.confirm(1)
            catalog.add(title="虚构公告新版本")
            # 实际Worker先消费workspace再catalog；建档事件错误时此处会阻断目录游标。
            result = tick(self.tracking_root, client, catalog, research)
            self.assertEqual(result["workspace"]["next_after"], 3)
            self.assertEqual(result["catalog"]["next_after"], 2)
            changes = service.changes(self.actor, self.wid)["items"]
            self.assertEqual([x["diff"]["kind"] for x in changes], ["profile_change", "material_change"])
            self.assertEqual(changes[0]["profile_revision"], 2)
            tick(self.tracking_root, client, catalog, research)
            self.assertEqual(len(service.notifications(self.actor, self.wid)["items"]), 2)
            self.assertEqual(research.creates, 0)
        finally:
            store.close(); server.shutdown(); server.server_close(); thread.join(2)

    def test_new_owner_profile_event_flows_through_http_and_worker(self):
        self.exercise_owner_feed()

    def test_v1_owner_backfill_uses_same_contract_without_restart_duplicates(self):
        self.owner.close()
        # v1已有成员和正式档案，尚无owner_events；在独立临时库复现这个升级起点。
        with closing(sqlite3.connect(self.workspace_root / "workspace.sqlite3")) as old:
            old.execute("DROP TABLE owner_events")
            old.execute("PRAGMA user_version=1")
        self.owner = WorkspaceStore(self.workspace_root)
        first = self.owner.events()
        self.assertEqual(self.owner.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual([x["event_id"] for x in first["items"]], [f"access:{self.wid}:{self.actor}:1", f"profile:{self.wid}:1"])
        self.owner.close(); self.owner = WorkspaceStore(self.workspace_root)
        self.assertEqual(self.owner.events(), first)
        self.exercise_owner_feed()


if __name__ == "__main__":
    unittest.main()

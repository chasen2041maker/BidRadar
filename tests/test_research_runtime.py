"""四个真实本机HTTP服务与持久Worker集成；只有模型用脚本替身，不证明真实AI质量。

目录样本经 ingestion→processing 导入；服务各自持有独立目录/SQLite，运行时
权限、目录、委托与研究均走HTTP。直接数据库写只用于模拟租约过期/进程断点。
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import json
from pathlib import Path
import secrets
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services.catalog.http_api import create_server as catalog_server
from services.catalog.store import Catalog
from services.common.local_http import LocalClient, LocalHTTPError
from services.common.owner_clients import WorkspaceAccessClient, CatalogEvidenceClient, ResearchClient, TrackingClient
from services.ingestion.archive import Store
from services.ingestion.demo import DemoTransport, NOTICE_URL, demo_request
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute
from services.processing.normalize import normalize_bundle
from services.research.budget import BudgetLedger
from services.research.http_api import create_server as research_server
from services.research.provider import ProviderError
from services.research.store import ResearchStore, ResearchError
from services.research.worker import tick, run_worker
from services.tracking.http_api import create_server as tracking_server
from services.tracking.service import TrackingService
from services.tracking.store import TrackingStore
from services.workspace.http_api import create_server as workspace_server
from services.workspace.store import WorkspaceStore
from test_workbench_http import PASSWORD, profile


class FixtureTransport(DemoTransport):
    def __init__(self, number, version):
        super().__init__()
        self.url = NOTICE_URL.replace("99990001", f"{number:08d}")
        self.version = version

    def fetch(self, url, **kwargs):
        result = super().fetch(NOTICE_URL if kwargs["kind"] == "notice" else url, **kwargs)
        body = result.body.replace(NOTICE_URL.encode(), self.url.encode())
        if kwargs["kind"] == "notice":
            body = body.replace(b"12.34", ("12.34" if self.version == 1 else "56.78").encode())
            body = body.replace(b"</div>", "<p>采购需求：软件开发及接口集成服务。</p><p>资格要求：须核查相关证明。</p></div>".encode())
        return replace(result, url=url, final_url=url, body=body, fetched_at=f"2026-10-01T00:{self.version:02d}:00Z")


class FakeProvider:
    """按工具协议返回脚本响应；故意没有真实Provider的密钥/网络属性。"""
    max_output_tokens = 512
    metadata = {"provider": "scripted_test_only", "requested_model": "scripted_test_only"}

    def __init__(self, *, hook=None, failure=None):
        self.calls, self.hook, self.failure = [], hook, failure

    def complete(self, messages, tools):
        self.calls.append(deepcopy(messages))
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        if self.failure:
            raise self.failure
        if not tools:
            report = json.loads(messages[1]["content"])["report"]
            message = {"role": "assistant", "content": json.dumps({"checks": [
                {"finding_index": i, "verdict": "supported", "reason": "脚本仅核验协议，不是人工语义真值。"}
                for i in range(len(report["findings"]))], "summary_supported": True, "answer_supported": True,
                "questions_supported": True, "report_issues": []})}
            finish = "stop"
        else:
            evidence = []
            for item in messages:
                if item["role"] == "tool":
                    evidence.extend(json.loads(item["content"]).get("evidence", []))
            if not evidence:
                name, arguments = "search_evidence", {"query": "软件开发", "category": None, "limit": 6}
            else:
                entry = evidence[0]
                context = json.loads(messages[1]["content"])
                report = {"summary": "软件项目相关性待核查，现有资料不足以确定参与条件。",
                          "findings": [{"category": entry["category"], "requirement": entry["text"],
                           "status": "unknown", "reason": "仅有公告片段，仍需核验企业声明和完整材料。",
                           "evidence_ids": [entry["evidence_id"]], "profile_fields": [], "unknown_reason": "证明尚未核验。"}],
                          "questions": ["需要取得完整采购材料。"],
                          "answer": "同项目证据不足以判定资格满足。" if context["kind"] == "question" else None}
                name, arguments = "finish_report", {"report": report}
            message = {"role": "assistant", "content": None, "tool_calls": [{"type": "function",
                       "id": "call-" + str(len(self.calls)), "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]}
            finish = "tool_calls"
        return {"message": message, "usage": {"input_tokens": 100, "output_tokens": 20, "cached_tokens": 0},
                "model": "scripted_test_only", "provider_request_id": "test-" + str(len(self.calls)), "finish_reason": finish}


class ResearchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.kdf = patch("services.workspace.store.PASSWORD_ITERATIONS", 1000)
        self.kdf.start()
        self.addCleanup(self.kdf.stop)
        self.workspace, self.research, self.tracking = (self.root / name for name in ("workspace", "research", "tracking"))
        self.budget = self.root / "authorized-budget" / "ledger.sqlite3"
        self.tokens = {name: secrets.token_urlsafe(32) for name in ("catalog", "wr", "wt", "rw", "rt", "tw", "tr")}
        with closing(WorkspaceStore(self.workspace)) as store:
            self.admin = store.bootstrap_user("admin", PASSWORD, "管理员（虚构）")
            self.member = store.bootstrap_user("member", PASSWORD, "成员（虚构）")
            self.viewer = store.bootstrap_user("viewer", PASSWORD, "只读（虚构）")
            self.other = store.bootstrap_user("other", PASSWORD, "乙公司（虚构）")
            self.a = store.bootstrap_workspace("甲公司（虚构）", self.admin)
            self.b = store.bootstrap_workspace("乙公司（虚构）", self.other)
            store.bootstrap_member(self.a, self.member, "member")
            store.bootstrap_member(self.a, self.viewer, "viewer")
            for actor, wid in ((self.admin, self.a), (self.other, self.b)):
                p = store.propose_profile(actor, wid, profile(), 0, "profile")
                store.confirm_profile(actor, wid, p["id"], 0, "confirm")
        self.fixture_counter = 0
        self.item = self.add_notice(1)
        self.second = self.add_notice(2)
        self.servers, self.threads = [], []
        self.addCleanup(self.shutdown)
        self.catalog_http = self.start(catalog_server(self.root / "catalog", token=self.tokens["catalog"]))
        self.web = self.start(workspace_server(self.workspace, self.address(self.catalog_http), self.tokens["catalog"],
                              internal_tokens={"research": self.tokens["wr"], "tracking": self.tokens["wt"]}))
        self.access = WorkspaceAccessClient(self.address(self.web), self.tokens["wr"])
        self.tracking_access = WorkspaceAccessClient(self.address(self.web), self.tokens["wt"])
        self.catalog = CatalogEvidenceClient(self.address(self.catalog_http), self.tokens["catalog"])
        self.tracking_http = self.start(tracking_server(self.tracking, self.tracking_access, self.catalog, None,
                                         tokens={"workspace": self.tokens["tw"], "research": self.tokens["tr"]}))
        self.tracking_client = TrackingClient(self.address(self.tracking_http), self.tokens["tr"])
        self.research_http = self.start(research_server(self.research, self.access, self.catalog, self.tracking_client,
            tokens={"workspace": self.tokens["rw"], "tracking": self.tokens["rt"]}, budget_path=self.budget))
        self.client = ResearchClient(self.address(self.research_http), self.tokens["rw"])
        self.delegated_client = ResearchClient(self.address(self.research_http), self.tokens["rt"])
        self.web.research_client = self.client
        self.web.tracking_client = LocalClient(self.address(self.tracking_http), self.tokens["tw"])
        self.login_data = self.login("member")
        status, selected = self.browser("selections", {"profile_revision": 1,
            "items": [{k: self.item[k] for k in ("notice_id", "observation_id")}], "key": "select"})
        self.assertEqual(status, 200, selected)
        status, selected = self.browser("selections/" + selected["id"] + "/confirm", {"expected_version": 1, "key": "confirm"})
        self.assertEqual(status, 200, selected)
        self.selection = selected

    @staticmethod
    def address(server):
        return f"http://127.0.0.1:{server.server_port}"

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        return server

    def shutdown(self):
        for server in reversed(self.servers):
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(2)

    def add_notice(self, number, version=1):
        self.fixture_counter += 1
        with closing(Store(self.root / "ingestion")) as ingestion:
            run, _ = ingestion.create_run({**demo_request(), "pages": 1, "attachments": False, "simulation": True},
                                           "fixture-" + str(self.fixture_counter))
            execute(ingestion, run, FixtureTransport(number, version))
            bundle = normalize_bundle(export_bundle(ingestion, run))
        with closing(Catalog(self.root / "catalog")) as catalog:
            catalog.import_bundle(bundle)
            return next(x for x in catalog.query(simulation=True)["items"] if f"{number:08d}" in x["source_url"])

    def raw(self, route, data=None, login=None):
        headers = {}
        if login:
            headers.update(Cookie=login["cookie"], **{"X-CSRF-Token": login["csrf"]})
        body = json.dumps(data).encode() if data is not None else None
        if body is not None:
            headers.update(Origin=self.address(self.web), **{"Content-Type": "application/json"})
        with closing(HTTPConnection("127.0.0.1", self.web.server_port, timeout=10)) as connection:
            connection.request("POST" if data is not None else "GET", route, body, headers)
            response = connection.getresponse()
            return response.status, response.getheaders(), json.loads(response.read())

    def login(self, username):
        status, headers, result = self.raw("/api/login", {"username": username, "password": PASSWORD})
        self.assertEqual(status, 200, result)
        jar = SimpleCookie()
        for name, value in headers:
            if name.lower() == "set-cookie":
                jar.load(value)
        return {"cookie": "; ".join(f"{k}={v.value}" for k, v in jar.items()), "csrf": jar["br_csrf"].value}

    def browser(self, action, data=None, *, wid=None, login=None):
        status, _, result = self.raw(f"/api/workspaces/{wid or self.a}/" + action, data, login or self.login_data)
        return status, result

    def request(self, **changes):
        return {"schema_version": 1, "actor_id": self.member, "workspace_id": self.a, "kind": "analysis", "mode": "agent",
                "selection_id": self.selection["id"], "notice_id": self.item["notice_id"], "key": "analysis", **changes}

    def tick(self, provider=None, *, root=None):
        return tick(root or self.research, self.access, self.catalog, self.tracking_client, provider, self.budget)

    def error(self, status, code, function):
        with self.assertRaises(LocalHTTPError) as caught:
            function()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_queued_then_real_worker_thread_scripted_agent_and_browser_safe_report(self):
        status, run = self.browser("research/analyses", {k: self.request()[k] for k in ("selection_id", "notice_id", "key", "mode")})
        self.assertEqual(status, 200, run)  # 工作台窄网关当前返回200；研究所属服务内部受理为202。
        self.assertEqual(run["state"], "queued")
        provider = FakeProvider()
        stop = threading.Event()
        worker = threading.Thread(target=run_worker, args=(self.research, self.access, self.catalog, self.tracking_client, provider, self.budget), kwargs={"stop_event": stop}, daemon=True)
        worker.start()
        try:
            import time
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                final = self.client.run(run["id"], self.a, self.member)
                if final["state"] in ("succeeded", "failed", "partial", "waiting_input"):
                    break
                stop.wait(.02)
            self.assertEqual(final["state"], "succeeded", final)
        finally:
            stop.set()
            worker.join(3)
        self.assertEqual(len(provider.calls), 3)
        status, shown = self.browser("research/runs/" + run["id"])
        self.assertEqual(status, 200)
        self.assertFalse(shown["report"]["coverage"]["full_tender_read"])
        self.assertEqual(shown["quality"]["provider"]["provider"], "scripted_test_only")
        self.assertEqual(shown["freshness"], {"profile": "current", "catalog": "current"})
        self.assertTrue(shown["created_at"].endswith("Z"))
        datetime.fromisoformat(shown["created_at"].replace("Z", "+00:00"))
        self.assertNotIn("checkpoint", shown)
        for token in self.tokens.values():
            self.assertNotIn(token, json.dumps(shown))
        self.assertEqual(self.tick(provider), None)
        self.assertEqual(len(provider.calls), 3)

    def test_report_freshness_changes_without_rewriting_frozen_report(self):
        run = self.client.create_analysis(self.request())
        final = self.tick(FakeProvider())
        frozen = deepcopy(final["report"])
        self.add_notice(1, 2)
        with closing(WorkspaceStore(self.workspace)) as store:
            p = store.propose_profile(self.admin, self.a, profile("新虚构档案"), 1, "new-profile")
            store.confirm_profile(self.admin, self.a, p["id"], 1, "new-confirm")
        detail = self.client.run(run["id"], self.a, self.member)
        self.assertEqual(detail["freshness"], {"profile": "changed", "catalog": "changed"})
        self.assertEqual(detail["report"], frozen)
        status, listed = self.browser("research/runs")
        self.assertEqual(status, 200)
        self.assertEqual(listed["items"][0]["freshness"], {"profile": "not_checked", "catalog": "not_checked"})

    def test_current_scope_selection_cross_company_viewer_and_command_conflict(self):
        self.error(409, "notice_outside_selection", lambda: self.client.create_analysis(self.request(notice_id=self.second["notice_id"])))
        self.error(403, "forbidden", lambda: self.client.create_analysis(self.request(actor_id=self.viewer)))
        self.error(404, "not_found", lambda: self.client.create_analysis(self.request(workspace_id=self.b)))
        run = self.client.create_analysis(self.request())
        self.error(409, "idempotency_conflict", lambda: self.client.create_analysis(self.request(mode="baseline")))
        self.error(404, "not_found", lambda: self.client.run(run["id"], self.b, self.other))
        self.assertEqual(self.client.run(run["id"], self.a, self.viewer)["id"], run["id"])
        self.error(403, "forbidden", lambda: self.client.cancel(run["id"], self.a, self.viewer, "cancel"))
        self.add_notice(1, 2)
        self.error(409, "selection_input_stale", lambda: self.client.create_analysis(self.request(key="fresh")))

    def test_created_time_pagination_is_stable_and_rejects_foreign_or_unknown_cursor(self):
        # 故意令UUID次序与创建先后相反，防止随机ID排序把新任务藏到后页。
        created = []
        for i, fixed_id in enumerate(("f" * 32, "0" * 32, "7" * 32)):
            with patch("services.research.store.uuid4", return_value=SimpleNamespace(hex=fixed_id)):
                created.append(self.client.create_analysis(self.request(key="ordered-" + str(i))))
        status, first = self.browser("research/runs?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(first["order"], "created_at_desc_id_desc")
        self.assertEqual([item["id"] for item in first["items"]], [created[2]["id"], created[1]["id"]])
        status, second = self.browser("research/runs?limit=2&before=" + first["next_before"])
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in second["items"]], [created[0]["id"]])
        self.assertIsNone(second["next_before"])
        for wid, actor, cursor in ((self.b, self.other, created[0]["id"]), (self.a, self.member, "1" * 32)):
            self.error(400, "invalid_cursor", lambda: self.client.request("POST", "/internal/v1/runs/list", {
                "schema_version": 1, "workspace_id": wid, "actor_id": actor, "before": cursor, "limit": 2}))

    def test_accepted_command_replays_with_catalog_offline_and_no_second_task(self):
        run = self.client.create_analysis(self.request())
        self.catalog_http.shutdown()
        self.catalog_http.server_close()
        self.assertEqual(self.client.create_analysis(self.request())["id"], run["id"])
        self.assertEqual(self.client.command(self.a, self.member, "analysis")["id"], run["id"])
        provider = FakeProvider()
        self.assertEqual(self.tick(provider)["state"], "succeeded")  # 冻结清单足够，不补查latest。
        self.assertEqual(len(provider.calls), 3)
        read = self.client.run(run["id"], self.a, self.member)
        self.assertEqual(read["freshness"], {"profile": "current", "catalog": "unavailable"})

    def test_persistent_cancel_before_claim_and_during_model_stops_next_action(self):
        run = self.client.create_analysis(self.request())
        self.assertEqual(self.client.cancel(run["id"], self.a, self.member, "cancel")["state"], "cancelled")
        provider = FakeProvider()
        self.assertIsNone(self.tick(provider))
        self.assertEqual(provider.calls, [])
        second = self.client.create_analysis(self.request(key="second"))
        self.error(409, "idempotency_conflict", lambda: self.client.cancel(second["id"], self.a, self.member, "cancel"))
        self.assertEqual(self.client.run(second["id"], self.a, self.member)["state"], "queued")
        provider.hook = lambda: self.client.cancel(second["id"], self.a, self.member, "cancel-second")
        result = self.tick(provider)
        self.assertEqual(result["state"], "cancelled")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(result["report"])
        with closing(BudgetLedger(self.budget)) as ledger:
            self.assertEqual(ledger.summary(self.a, second["id"])["attempts"], 1)

    def test_revocation_rejoin_generation_and_profile_changes_do_not_revive_old_work(self):
        run = self.client.create_analysis(self.request())
        with closing(WorkspaceStore(self.workspace)) as store:
            store.change_member(self.admin, self.a, self.member, "member", False, 1)
            store.change_member(self.admin, self.a, self.member, "member", True, 2)
        provider = FakeProvider()
        self.assertEqual(self.tick(provider)["reason"], "authorization_changed")
        self.assertEqual(provider.calls, [])
        self.assertIsNone(self.tick(provider))
        newer = self.client.create_analysis(self.request(key="after-rejoin"))
        def change_profile():
            with closing(WorkspaceStore(self.workspace)) as store:
                p = store.propose_profile(self.admin, self.a, profile("虚构更新公司"), 1, "revision2")
                store.confirm_profile(self.admin, self.a, p["id"], 1, "confirm2")
        provider.hook = change_profile
        final = self.tick(provider)
        self.assertEqual((final["state"], final["reason"]), ("waiting_input", "profile_revision_changed"))
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(final["report"])
        self.assertEqual(final["profile_revision"], 1)

    def test_expired_lease_fences_old_checkpoint_publish_and_stop(self):
        created = self.client.create_analysis(self.request())
        with closing(ResearchStore(self.research)) as store:
            old = store.claim("worker-old")
            store.db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (created["id"],))
            new = store.claim("worker-new")
            self.assertGreater(new["lease_epoch"], old["lease_epoch"])
            for operation in (lambda: store.checkpoint(old, {}), lambda: store.stop(old, "failed", "old"),
                              lambda: store.finish(old, {"state": "partial", "report": {}, "trace": [], "quality": {}})):
                with self.assertRaisesRegex(ResearchError, "lease_lost"):
                    operation()
            store.stop(new, "waiting_input", "test_end")
        self.assertEqual(self.client.run(created["id"], self.a, self.member)["reason"], "test_end")

    def test_frozen_implementation_config_does_not_silently_upgrade_queued_task(self):
        from services.research.agent import VERSION, PROMPT_VERSION
        from services.research.evidence import VERSION as evidence_version
        from services.research.provider import EGRESS_VERSION
        run = self.client.create_analysis(self.request())
        with closing(ResearchStore(self.research)) as store:
            frozen = store.load(self.a, run["id"])["manifest"]
            self.assertEqual(frozen["config"]["agent_version"], VERSION)
            self.assertEqual(frozen["config"]["prompt_version"], PROMPT_VERSION)
            self.assertEqual(frozen["config"]["evidence_version"], evidence_version)
            self.assertEqual(frozen["config"]["egress_version"], EGRESS_VERSION)
            frozen["config"]["agent_version"] = "historical-agent-version"
            store.db.execute("UPDATE runs SET manifest=? WHERE id=?", (json.dumps(frozen), run["id"]))
        provider = FakeProvider()
        result = self.tick(provider)
        self.assertEqual((result["state"], result["reason"]), ("waiting_input", "configuration_changed"))
        self.assertEqual(provider.calls, [])

    def test_provider_unknown_keeps_budget_after_restart_and_new_business_store(self):
        run = self.client.create_analysis(self.request())
        provider = FakeProvider(failure=ProviderError("provider_network_error", True))
        final = self.tick(provider)
        self.assertEqual(final["state"], "waiting_input")
        self.assertIsNone(self.tick(provider))
        with closing(BudgetLedger(self.budget)) as ledger:
            original = ledger.summary()
            self.assertEqual(original["unknown_attempts"], 1)
            self.assertGreater(original["charged_or_reserved"], 0)
        with closing(ResearchStore(self.research)) as old, closing(ResearchStore(self.root / "fresh-research")) as fresh:
            frozen = old.load(self.a, run["id"])
            request = deepcopy(frozen["request"])
            request["key"] = "new-business-database"
            fresh.create(request, "synthetic-new-hash", frozen["manifest"], frozen["membership_version"])
        provider.failure = None
        self.assertEqual(self.tick(provider, root=self.root / "fresh-research")["state"], "succeeded")
        with closing(BudgetLedger(self.budget)) as restarted:
            later = restarted.summary()
        self.assertEqual(later["unknown_attempts"], 1)
        self.assertEqual(later["attempts"], 4)
        self.assertGreater(later["charged_or_reserved"], original["charged_or_reserved"])

    def test_settled_response_before_checkpoint_crash_reuses_exact_model_response(self):
        run = self.client.create_analysis(self.request())
        class Crash(BaseException):
            pass
        original = ResearchStore.checkpoint
        def crash_on_result(store, claimed, state):
            if state["phase"] == "model_result":
                raise Crash()
            return original(store, claimed, state)
        provider = FakeProvider()
        with patch.object(ResearchStore, "checkpoint", crash_on_result), self.assertRaises(Crash):
            self.tick(provider)
        with closing(ResearchStore(self.research)) as store:
            self.assertEqual(store.load(self.a, run["id"])["checkpoint"]["phase"], "model_inflight")
            store.db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (run["id"],))
        final = self.tick(provider)
        self.assertEqual(final["state"], "succeeded", final)
        self.assertEqual(len(provider.calls), 3)  # 原首轮只发一次，其余是finish与语义核验。
        self.assertEqual(sum(not any(m["role"] == "tool" for m in messages) and "工具" in messages[0]["content"]
                             and "核验器" not in messages[0]["content"] for messages in provider.calls), 1)
        with closing(BudgetLedger(self.budget)) as ledger:
            self.assertEqual(ledger.summary()["unknown_attempts"], 0)
            self.assertEqual(ledger.summary()["attempts"], 3)

    def test_semantic_checkpoint_recovery_reuses_its_response_and_has_no_new_provider_call(self):
        run = self.client.create_analysis(self.request())
        class Crash(BaseException):
            pass
        original = ResearchStore.checkpoint
        def crash_review_result(store, claimed, state):
            if state["phase"] == "review_result":
                raise Crash()
            return original(store, claimed, state)
        provider = FakeProvider()
        with patch.object(ResearchStore, "checkpoint", crash_review_result), self.assertRaises(Crash):
            self.tick(provider)
        self.assertEqual(len(provider.calls), 3)
        with closing(ResearchStore(self.research)) as store:
            checkpoint = store.load(self.a, run["id"])["checkpoint"]
            self.assertEqual(checkpoint["pending_request"]["purpose"], "review")
            self.assertEqual(checkpoint["pending_request"]["tools"], [])
            store.db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (run["id"],))
        final = self.tick(provider)
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["quality"]["model_calls"], 3)
        self.assertEqual(len(provider.calls), 3)

    def test_crashed_unknown_attempt_or_changed_provider_cannot_dispatch_on_resume(self):
        class Crash(BaseException):
            pass
        for changed in (False, True):
            request = self.request(key="crash-" + str(changed))
            run = self.client.create_analysis(request)
            provider = FakeProvider(failure=None if changed else Crash())
            original = ResearchStore.checkpoint
            def crash_on_result(store, claimed, state):
                if state["phase"] == "model_result":
                    raise Crash()
                return original(store, claimed, state)
            with patch.object(ResearchStore, "checkpoint", crash_on_result), self.assertRaises(Crash):
                self.tick(provider)
            with closing(ResearchStore(self.research)) as store:
                store.db.execute("UPDATE runs SET lease_until=0 WHERE id=?", (run["id"],))
            provider.failure = None
            if changed:
                provider.metadata = {**provider.metadata, "version": "changed-config"}
            final = self.tick(provider)
            self.assertEqual((final["state"], final["reason"]), ("waiting_input", "checkpoint_request_mismatch" if changed else "billing_unknown"))
            self.assertEqual(len(provider.calls), 1)

    def test_question_freezes_same_parent_scope_and_tracking_identity_has_no_manual_route(self):
        created = self.client.create_analysis(self.request())
        self.tick(FakeProvider())
        question = {"schema_version": 1, "actor_id": self.member, "workspace_id": self.a, "notice_id": self.item["notice_id"],
                    "key": "question", "kind": "question", "mode": "agent", "parent_run_id": created["id"], "question": "资格是否满足？"}
        self.error(409, "parent_report_unavailable", lambda: self.client.create_analysis({**question, "notice_id": self.second["notice_id"]}))
        accepted = self.client.create_analysis(question)
        final = self.tick(FakeProvider())
        self.assertEqual(final["id"], accepted["id"])
        self.assertEqual(final["state"], "succeeded")
        self.assertEqual(final["scope"], self.client.run(created["id"], self.a, self.member)["scope"])
        self.assertTrue(final["report"]["answer"])
        self.error(403, "service_forbidden", lambda: self.delegated_client.create_analysis(self.request(key="forged")))
        self.error(403, "service_forbidden", lambda: self.delegated_client.run(created["id"], self.a, self.member))
        self.error(403, "service_forbidden", lambda: self.delegated_client.cancel(created["id"], self.a, self.member, "forged"))

    def test_real_tracking_delegation_accepts_only_matching_snapshot_then_stop_watch_blocks_worker(self):
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        status, watch = self.browser("tracking/watches/" + self.item["notice_id"], {"active": True, "auto_reassess": True,
             "expected_version": 0, "key": "watch", "expires_at": expires, "scope": None})
        self.assertEqual(status, 200, watch)
        self.add_notice(1, 2)
        with closing(TrackingStore(self.tracking)) as store:
            service = TrackingService(store, self.tracking_access, self.catalog, self.delegated_client)
            service.poll("catalog")
            request = json.loads(store.db.execute("SELECT request FROM reassessments LIMIT 1").fetchone()[0])
            self.error(409, "delegation_input_mismatch", lambda: self.delegated_client.create_analysis({**request, "profile_revision": 2}))
            delegated = service.dispatch_one()
            self.assertEqual(delegated["state"], "accepted", delegated)
        remote = self.delegated_client.run(delegated["run_id"], self.a, self.member)
        self.assertEqual(remote["kind"], "reassessment")
        status, stopped = self.browser("tracking/watches/" + self.item["notice_id"], {"active": False, "auto_reassess": False,
            "expected_version": watch["version"], "key": "stop", "expires_at": None, "scope": None})
        self.assertEqual(status, 200, stopped)
        provider = FakeProvider()
        final = self.tick(provider)
        self.assertEqual(final["state"], "waiting_input")
        self.assertEqual(final["reason"], "delegation_invalid")
        self.assertEqual(provider.calls, [])


if __name__ == "__main__":
    unittest.main()

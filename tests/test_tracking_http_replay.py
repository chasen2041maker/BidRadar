"""R2四服务HTTP回放验收：只有本机临时库/随机端口，采购与模型全部模拟。

复用已有四服务fixture而不继承其测试，避免重复运行整套边界用例。采购原件由
DemoTransport生成并经真实ingestion→processing→catalog入库；服务间的目录、
权限、委托及研究调用全部走HTTP。此测试不证明真实模型效果或生产可靠性。
"""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import time
import unittest
from unittest.mock import patch

from services.catalog.store import Catalog
from services.ingestion.archive import Store
from services.ingestion.demo import demo_request
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute
from services.processing.normalize import normalize_bundle
from services.tracking.service import TrackingService
from services.tracking.store import TrackingStore
from services.tracking.worker import tick as tracking_tick
import test_research_runtime as runtime_fixture


class BudgetReplayTransport(runtime_fixture.FixtureTransport):
    """固定虚构公告只改预算；A→B→A保留三个独立观察，不访问公告URL。"""
    def __init__(self, version, amount):
        super().__init__(1, version)
        self.amount = amount

    def fetch(self, url, **kwargs):
        result = super().fetch(url, **kwargs)
        if kwargs["kind"] == "notice":
            result = replace(result, body=result.body.replace(b"12.34", self.amount.encode())
                             .replace(b"56.78", self.amount.encode()))
        return result


class TrackingHTTPReplayTests(unittest.TestCase):
    def setUp(self):
        # 使用组合而非继承TestCase，discover只增加本文件这一条完整业务验收。
        self.runtime = runtime_fixture.ResearchRuntimeTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.provider = runtime_fixture.FakeProvider()

    def browser(self, action, payload=None, **kwargs):
        status, result = self.runtime.browser(action, payload, **kwargs)
        self.assertEqual(status, 200, result)
        return result

    def add_budget(self, version, amount):
        r = self.runtime
        with closing(Store(r.root / "ingestion")) as owner:
            run, _ = owner.create_run({**demo_request(), "pages": 1, "attachments": False, "simulation": True},
                                      "tracking-budget-" + str(version))
            execute(owner, run, BudgetReplayTransport(version, amount))
            bundle = normalize_bundle(export_bundle(owner, run))
        with closing(Catalog(r.root / "catalog")) as owner:
            owner.import_bundle(bundle)
        current = r.catalog.bundle(r.item["notice_id"])["current"]
        self.assertIs(current["simulation"], True)
        return current

    def tick(self):
        r = self.runtime
        return tracking_tick(r.tracking, r.tracking_access, r.catalog, r.delegated_client)

    def replay_catalog_events(self):
        r = self.runtime
        events = r.catalog.changes(0, 100)["items"]
        # 只打开tracking自己的库，重放通过catalog HTTP收到的同一批事件。
        with closing(TrackingStore(r.tracking)) as owner:
            service = TrackingService(owner, r.tracking_access, r.catalog, r.delegated_client)
            for event in events:
                service.consume_event("catalog", event)

    def test_simulated_budget_replay_preserves_reports_and_unique_user_notifications(self):
        r = self.runtime
        nid = r.item["notice_id"]
        self.assertIs(r.item["simulation"], True)
        initial = self.browser("research/analyses", {"selection_id": r.selection["id"], "notice_id": nid,
                                                     "key": "frozen-original", "mode": "baseline"})
        old = r.tick()
        self.assertEqual((old["id"], old["state"]), (initial["id"], "partial"))
        frozen_report, frozen_scope = deepcopy(old["report"]), deepcopy(old["scope"])

        # 人工决定不隐式启用watch；全局事件游标可前进，但公司不会收到提醒或复核。
        self.browser("tracking/decisions/" + nid, {"state": "follow_up", "reason": "虚构回放验收",
                                                  "expected_version": 0, "key": "decision"})
        self.add_budget(2, "15.00")
        self.tick()
        self.assertEqual(self.browser("tracking/notifications")["items"], [])
        self.assertEqual(self.browser("tracking/reassessments")["items"], [])
        self.assertIsNone(self.browser("tracking/watches/" + nid)["watch"])
        baseline = self.add_budget(3, "12.34")
        self.tick()
        expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        watch = self.browser("tracking/watches/" + nid, {"active": True, "auto_reassess": True,
                             "expected_version": 0, "key": "explicit-watch", "expires_at": expires, "scope": ["money"]})

        # 在一次轮询之前提交A→B→A，必须记录两次差异；不能只比较latest而丢掉B。
        changed = self.add_budget(4, "15.00")
        returned = self.add_budget(5, "12.34")
        self.assertEqual(baseline["raw_sha256"], returned["raw_sha256"])
        self.assertEqual(len({x["observation_id"] for x in (baseline, changed, returned)}), 3)
        dispatched = self.tick()["reassessment"]
        self.assertEqual(dispatched["state"], "accepted", dispatched)
        changes = self.browser("tracking/changes")["items"]
        self.assertEqual(len(changes), 2)
        amounts = []
        for change in changes:
            entry = next(x for x in change["diff"]["entries"] if x["scope"] == "money")
            amounts.append(tuple(Decimal(entry[side]["budget"]["value"]["amount"]) for side in ("before", "after")))
            self.assertTrue(change["stale"])
        self.assertEqual(amounts, [(Decimal("123400"), Decimal("150000")), (Decimal("150000"), Decimal("123400"))])
        jobs = self.browser("tracking/reassessments")["items"]
        self.assertEqual(sorted(x["state"] for x in jobs), ["accepted", "cancelled"])
        self.assertEqual(next(x for x in jobs if x["state"] == "cancelled")["reason"], "input_superseded")

        notifications = self.browser("tracking/notifications")["items"]
        self.assertEqual([x["kind"] for x in notifications], ["change_detected", "change_detected"])
        self.assertEqual({x["change_id"] for x in notifications}, {x["id"] for x in changes})
        self.replay_catalog_events(); self.tick()
        self.assertEqual(self.browser("tracking/notifications")["items"], notifications)
        # 慢机器可能已到正常对账时间，version允许前进；命令身份/状态不得重复。
        identity = lambda items: [(x["id"], x["state"], x["command_key"], x["run_id"]) for x in items]
        self.assertEqual(identity(self.browser("tracking/reassessments")["items"]), identity(jobs))

        # 同公司报告/提醒共享，但已读状态属于当前用户；浏览器cookie派生身份。
        notification_id = notifications[0]["id"]
        read = self.browser("tracking/notifications/" + notification_id + "/read", {})
        self.assertIsNotNone(read["read_at"])
        self.assertIsNotNone(self.browser("tracking/notifications")["items"][0]["read_at"])
        viewer = r.login("viewer")
        self.assertIsNone(self.browser("tracking/notifications", login=viewer)["items"][0]["read_at"])

        # 唯一最新输入的复核使用脚本provider执行真实Worker/工具/账本，不是付费模型。
        reviewed = r.tick(self.provider)
        self.assertEqual((reviewed["id"], reviewed["state"]), (dispatched["run_id"], "succeeded"), reviewed)
        self.assertEqual(reviewed["quality"]["provider"]["provider"], "scripted_test_only")
        self.assertIs(reviewed["simulation"], True)
        self.assertEqual(reviewed["scope"]["observation_ids"], [returned["observation_id"]])
        self.assertEqual(len(self.provider.calls), 3)
        # 只推进tracking调度时间越过5秒对账间隔；不sleep或直接改业务库状态。
        with patch("services.tracking.service.now", return_value=time.time() + 6):
            self.assertEqual(self.tick()["reassessment"]["state"], "succeeded")
        self.replay_catalog_events(); self.tick()
        complete = self.browser("tracking/notifications")["items"]
        self.assertEqual([x["kind"] for x in complete], ["change_detected", "change_detected", "reassessment_completed"])
        self.assertEqual(len({x["id"] for x in complete}), 3)
        preserved = self.browser("research/runs/" + initial["id"])
        self.assertEqual((preserved["report"], preserved["scope"]), (frozen_report, frozen_scope))
        self.assertEqual(preserved["freshness"]["catalog"], "changed")

        # 关闭watch不改人工决定，也不因后来再变成B自动产生提醒或新模型任务。
        self.browser("tracking/watches/" + nid, {"active": False, "auto_reassess": False,
                     "expected_version": watch["version"], "key": "explicit-stop", "expires_at": None, "scope": ["money"]})
        before_jobs = self.browser("tracking/reassessments")["items"]
        self.add_budget(6, "15.00")
        self.tick(); self.replay_catalog_events()
        self.assertEqual(self.browser("tracking/notifications")["items"], complete)
        self.assertEqual(self.browser("tracking/reassessments")["items"], before_jobs)
        self.assertIsNone(r.tick(self.provider))
        self.assertEqual(len(self.provider.calls), 3)
        self.assertEqual(self.browser("tracking/decisions/" + nid)["current"]["state"], "follow_up")
        self.assertEqual(len(self.browser("research/runs")["items"]), 2)
        self.assertEqual(self.browser("research/runs/" + initial["id"])["report"], frozen_report)


if __name__ == "__main__":
    unittest.main()

"""离线验收完整证据链：原件→规范观察→目录/历史。所有采购内容均为虚构。

这些测试验证本地行为；没有账号/真实接口响应，不能据此标成真实接入通过。
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.catalog.store import Catalog, currentness
from services.ingestion.archive import Store, StoreError
from services.ingestion.demo import DemoTransport, demo_request, NOTICE_URL
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import execute, parse_capture
from services.processing.normalize import normalize_bundle, fingerprint


def raw_bundle(*, title="虚构软件采购招标公告", key="https://www.ccgp.gov.cn/cggg/zygg/gkzb/202609/t1.htm",
               capture="capture1", observed="2026-09-30T10:00:00Z", entries=None, links=None):
    if entries is None:
        entries = [("项目编号", "DEMO-001"), ("采购单位", "虚构采购方"), ("预算（万元）", "12.34"),
                   ("公告发布时间", "2026-09-29"), ("投标截止时间", "2026-10-10 09:30（北京时间）")]
    return {"schema_version": 1, "kind": "raw_evidence_bundle", "source_id": "cn_ccgp", "simulation": True,
            "run_id": "demo-run", "run_status": "succeeded", "coverage": "bounded_sample", "failures": [],
            "documents": [{"capture_id": capture, "source_url": key, "final_url": key, "source_record_key": key,
                "identity_kind": "source_url", "raw_sha256": sha256(title.encode()).hexdigest(),
                "observed_at": observed, "parser_version": "fixture-v1", "content": {"status": "ok", "title": title,
                    "metadata": [{"label": k, "text": v, "locator": f"fixture[{i}]"} for i, (k, v) in enumerate(entries)],
                    "segments": [], "links": links or []}}]}


class NormalizeTests(unittest.TestCase):
    def fact(self, name, entries):
        return normalize_bundle(raw_bundle(entries=entries))["observations"][0]["facts"][name]

    def test_minimum_fields_keep_provenance_and_decimal_units(self):
        item = normalize_bundle(raw_bundle())["observations"][0]
        self.assertEqual(item["facts"]["budget"]["value"]["amount"], "123400.00")
        self.assertEqual(item["facts"]["budget"]["value"]["scope"], "unspecified")
        self.assertEqual(item["facts"]["budget"]["evidence"][0]["text"], "12.34")
        self.assertEqual(item["facts"]["published_at"]["value"]["local"], "2026-09-29")
        self.assertEqual(item["facts"]["response_deadline"]["value"]["timezone"], "+08:00")
        self.assertEqual(item["notice_type"], "procurement")

    def test_missing_not_zero_and_highest_price_not_budget(self):
        item = self.fact("budget", [("最高限价", "500万元")])
        self.assertEqual(item, {"status": "missing", "value": None, "evidence": []})
        zero = self.fact("budget", [("预算金额", "0元")])
        self.assertEqual(zero["status"], "known")
        self.assertEqual(zero["value"]["amount"], "0")

    def test_conflicts_and_unparsed_values_are_not_dropped(self):
        item = self.fact("budget", [("预算金额", "10万元"), ("预算金额", "12万元")])
        self.assertEqual(item["status"], "conflicting")
        self.assertEqual(len(item["evidence"]), 2)
        item = self.fact("budget", [("预算金额", "10万元"), ("预算金额", "另见附件")])
        self.assertEqual(item["status"], "unparsed")
        for value in ("约10万元", "10-12万元", "人民币12", "12美元", "最高10万元", "1234,567元"):
            self.assertEqual(self.fact("budget", [("预算金额", value)])["status"], "unparsed")

    def test_date_role_timezone_precision_and_invalid_calendar(self):
        fact = self.fact("response_deadline", [("投标截止时间", "2026年10月10日 09时30分")])
        self.assertEqual(fact["value"]["timezone"], None)
        self.assertEqual(fact["value"]["precision"], "minute")
        self.assertEqual(self.fact("published_at", [("发布日期", "2026-02-30")])["status"], "unparsed")
        self.assertEqual(self.fact("response_deadline", [("开标时间", "2026-10-10 09:30")])["status"], "missing")

    def test_correction_does_not_claim_before_or_after_as_current_amount(self):
        item = normalize_bundle(raw_bundle(title="虚构软件采购更正公告"))["observations"][0]
        self.assertEqual(item["notice_type"], "correction")
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
        self.assertEqual(item["facts"]["project_number"]["status"], "known")

    def test_inline_negation_and_table_headers_survive_ingestion(self):
        html = ('<h2 class="tc">虚构采购公告</h2><div class="vF_detail_content">不接受<strong>联合体</strong>。'
                '<p>项目编号：DEMO-001</p>以下金额是更正前记录'
                '<table><tr><th>包号</th><th>预算</th></tr><tr><td>1</td><td>10万元</td></tr></table></div>')
        content = parse_capture("notice", html.encode(), {"content-type": "text/html"}, NOTICE_URL)
        texts = [segment["text"] for segment in content["segments"]]
        self.assertIn("不接受联合体。", texts)
        self.assertIn("以下金额是更正前记录", texts)
        self.assertIn("包号 | 预算", texts)
        raw = raw_bundle()
        raw["documents"][0]["content"] = content
        item = normalize_bundle(raw)["observations"][0]
        self.assertEqual(item["facts"]["project_number"]["value"], "DEMO-001")
        # 没有已确认多包结构映射时宁可留空，不能把第1包当项目总预算。
        self.assertEqual(item["facts"]["budget"]["status"], "missing")

    def test_capture_and_parser_changes_keep_notice_identity_but_new_observation(self):
        first = normalize_bundle(raw_bundle())["observations"][0]
        second = normalize_bundle(raw_bundle(capture="capture2"))["observations"][0]
        changed = raw_bundle()
        changed["documents"][0]["parser_version"] = "fixture-v2"
        third = normalize_bundle(changed)["observations"][0]
        self.assertEqual(first["notice_id"], second["notice_id"])
        self.assertEqual(first["notice_id"], third["notice_id"])
        self.assertEqual(len({x["observation_id"] for x in (first, second, third)}), 3)

    def test_simulated_identity_never_collides_with_real(self):
        raw = raw_bundle()
        fake = normalize_bundle(raw)["observations"][0]
        raw["simulation"] = False
        real = normalize_bundle(raw)["observations"][0]
        self.assertNotEqual(fake["notice_id"], real["notice_id"])

    def test_contract_rejects_bad_header_time_hash_and_versions(self):
        for field, value in (("schema_version", True), ("schema_version", 2), ("source_id", "unknown"),
                             ("simulation", "false"), ("run_status", "running"), ("documents", {})):
            raw = raw_bundle()
            raw[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize_bundle(raw)
        for field, value in (("observed_at", "2026-09-30T10:00:00"), ("raw_sha256", "../bad"),
                             ("identity_kind", "guess"), ("capture_id", "")):
            raw = raw_bundle()
            raw["documents"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                normalize_bundle(raw)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "catalog"
        self.store = Catalog(self.root)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, **kwargs):
        bundle = normalize_bundle(raw_bundle(**kwargs))
        self.store.import_bundle(bundle)
        return bundle["observations"][0]

    def test_idempotent_import_and_history_survive_reopen(self):
        bundle = normalize_bundle(raw_bundle())
        self.assertEqual(self.store.import_bundle(bundle)["added"], 1)
        self.assertEqual(self.store.import_bundle(bundle)["added"], 0)
        self.store.close()
        self.store = Catalog(self.root)
        detail = self.store.detail(bundle["observations"][0]["notice_id"])
        self.assertEqual(len(detail["history"]), 1)
        self.assertEqual(self.store.query()["total"], 0)  # 默认真实目录不能混入演示。
        self.assertEqual(self.store.query(simulation=True)["total"], 1)

    def test_late_old_data_does_not_roll_back_current(self):
        new = self.add(title="虚构软件采购招标公告", capture="new", observed="2026-09-30T20:00:00+08:00")
        old = self.add(title="历史标题采购公告", capture="old", observed="2026-09-30T10:00:00Z")
        detail = self.store.detail(new["notice_id"])
        self.assertEqual(detail["current"]["observation_id"], new["observation_id"])
        self.assertEqual(len(detail["history"]), 2)
        self.assertNotEqual(old["observation_id"], new["observation_id"])

    def test_snapshot_pagination_stable_while_updates_arrive(self):
        one = self.add(key="https://example.test/1", observed="2026-09-30T11:00:00Z")
        two = self.add(key="https://example.test/2", capture="two")
        first = self.store.query(simulation=True, page_size=1, as_of="2026-10-01T00:00:00Z")
        self.add(key="https://example.test/3", capture="new", observed="2026-10-01T11:00:00Z")
        self.add(key="https://example.test/2", title="后来变更的采购公告", capture="updated", observed="2026-10-01T12:00:00Z")
        second = self.store.query(simulation=True, page_size=1, cursor=first["next_cursor"])
        self.assertEqual(first["items"][0]["notice_id"], one["notice_id"])
        self.assertEqual(second["items"][0]["observation_id"], two["observation_id"])
        self.assertEqual(second["as_of"], first["as_of"])
        self.assertEqual(second["total"], 2)
        self.assertIsNone(second["next_cursor"])

    def test_cursor_tamper_and_changed_filters_rejected(self):
        self.add(key="https://example.test/1")
        self.add(key="https://example.test/2", capture="two")
        cursor = self.store.query(simulation=True, page_size=1)["next_cursor"]
        for kwargs in ({"cursor": cursor + "a", "simulation": True, "page_size": 1},
                       {"cursor": cursor, "simulation": True, "page_size": 2},
                       {"cursor": cursor, "simulation": False, "page_size": 1}):
            with self.assertRaises(ValueError):
                self.store.query(**kwargs)

    def test_invalid_second_record_leaves_no_first_record_committed(self):
        bundle = normalize_bundle(raw_bundle())
        bad = deepcopy(bundle["observations"][0])
        bad["title"] = "被篡改"
        bundle["observations"].append(bad)
        with self.assertRaises(ValueError):
            self.store.import_bundle(bundle)
        self.assertEqual(self.store.query(simulation=True)["total"], 0)

    def test_rehashed_bad_fact_still_rejected_by_contract(self):
        bundle = normalize_bundle(raw_bundle())
        bad = bundle["observations"][0]
        bad["facts"]["budget"]["value"] = 0
        bad["observation_id"] = fingerprint({k: v for k, v in bad.items() if k != "observation_id"})
        with self.assertRaises(ValueError):
            self.store.import_bundle(bundle)

    def test_explicit_correction_keeps_original_and_bound_versions(self):
        original = self.add()
        correction = self.add(title="虚构软件更正公告", key="https://example.test/correction", capture="correction",
                              links=[{"url": original["source_record_key"], "name": "原公告", "locator": "a[1]"}])
        detail = self.store.detail(correction["notice_id"])
        self.assertEqual(detail["relationships"][0]["status"], "evidenced")
        self.assertEqual(detail["relationships"][0]["target_observation_id"], original["observation_id"])
        self.assertEqual(self.store.detail(original["notice_id"])["current"], original)
        self.assertEqual(self.store.query(simulation=True)["total"], 2)

    def test_metadata_relation_is_candidate_then_ambiguous_not_merged(self):
        self.add()
        correction = self.add(title="虚构软件更正公告", key="https://example.test/correction", capture="correction")
        self.assertEqual(self.store.detail(correction["notice_id"])["relationships"][0]["status"], "candidate")
        self.add(key="https://example.test/second-original", capture="another")
        relations = self.store.detail(correction["notice_id"])["relationships"]
        self.assertEqual({rel["status"] for rel in relations}, {"ambiguous"})
        self.assertEqual(len(relations), 2)

    def test_missing_original_link_is_not_replaced_by_name_guess(self):
        self.add()
        correction = self.add(title="虚构软件更正公告", key="https://example.test/correction", capture="correction",
                              links=[{"url": "https://example.test/missing", "name": "原公告", "locator": "a[1]"}])
        self.assertEqual(self.store.detail(correction["notice_id"])["relationships"][0]["status"], "unresolved")

    def test_currentness_does_not_mean_eligibility(self):
        item = self.add()
        self.assertEqual(currentness(item, "2026-10-10T01:30:00Z")["status"], "deadline_passed")
        self.assertEqual(currentness(item, "2026-10-01T00:00:00Z")["status"], "deadline_not_reached")
        no_zone = normalize_bundle(raw_bundle(entries=[("投标截止时间", "2020-01-01 09:00")]))["observations"][0]
        self.assertEqual(currentness(no_zone, "2026-10-01T00:00:00Z")["status"], "unknown")

    def test_empty_failed_import_retains_failure_not_zero_success(self):
        raw = raw_bundle()
        raw.update(documents=[], run_status="blocked", failures=[{"capture_id": "x", "url": "https://example.test", "error": "blocked"}])
        result = self.store.import_bundle(normalize_bundle(raw))
        self.assertEqual(result["run_status"], "blocked")
        self.assertEqual(result["failures"], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM imports").fetchone()[0], 1)
        query = self.store.query(simulation=True)
        self.assertEqual(query["total"], 0)
        self.assertEqual(query["recent_runs"][0]["run_status"], "blocked")


class EndToEndTests(unittest.TestCase):
    def test_three_boundaries_with_real_local_stores_no_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            ingestion = Store(Path(temporary) / "ingestion")
            catalog = Catalog(Path(temporary) / "catalog")
            try:
                request = {**demo_request(), "simulation": True}
                run, _ = ingestion.create_run(request, "demo-chain")
                with patch("socket.socket", side_effect=AssertionError("离线验收不得联网")):
                    execute(ingestion, run, DemoTransport())
                    exported = export_bundle(ingestion, run)
                    # JSON序列化跨边界，processing/catalog没有取得原件路径或Store。
                    normalized = normalize_bundle(json.loads(json.dumps(exported)))
                    result = catalog.import_bundle(json.loads(json.dumps(normalized)))
                self.assertEqual(result["added"], 1)
                self.assertEqual(catalog.query(simulation=True)["total"], 1)
                self.assertEqual(exported["documents"][0]["content"]["attachments"][0]["fetch_status"], "fetched")
                digest = exported["documents"][0]["raw_sha256"]
                (ingestion.blobs / (digest + ".bin")).write_bytes(b"tampered")
                with self.assertRaises(StoreError):
                    export_bundle(ingestion, run)
            finally:
                catalog.close()
                ingestion.close()

    def test_running_export_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            try:
                run, _ = store.create_run(demo_request(), "unstarted")
                with self.assertRaises(StoreError):
                    export_bundle(store, run)
            finally:
                store.close()

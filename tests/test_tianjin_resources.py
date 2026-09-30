"""天津三个已核实资源的离线契约验收；响应均为虚构，不证明真实API格式。"""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.catalog.store import Catalog, relationships
from services.ingestion import tianjin as tj
from services.ingestion.__main__ import main
from services.ingestion.archive import Store, StoreError
from services.ingestion.export import export_bundle
from services.ingestion.pipeline import replay
from services.ingestion.transport import FetchResult
from services.processing.normalize import normalize_bundle, fingerprint

CONSULTATION = "tj_procurement_consultation"
CORRECTION = "tj_procurement_correction"


def row_fields(title="虚构项目", **overrides):
    return {"公告标题": title, "项目编号": "DEMO-RESOURCE-001", "采购人名称": "虚构采购方", **overrides}


def legacy_bundle(v2):
    """按旧契约重建同一证据的v1包；旧解释没有日期/合同/材料新增字段。"""
    old = deepcopy(v2)
    old.update(schema_version=1, normalizer_version="procurement-facts-v1")
    for item in old["observations"]:
        item["normalizer_version"] = old["normalizer_version"]
        item.pop("material_reference_evidence")
        item["facts"].pop("original_published_at")
        item["facts"].pop("original_contract_reference")
        item["observation_id"] = fingerprint({k: v for k, v in item.items() if k != "observation_id"})
    return old


class ResourceTransport:
    def __init__(self, source_id, fields, *, total=1, interrupt_page=None):
        self.source_id, self.fields, self.total = source_id, fields, total
        self.interrupt_page, self.calls = interrupt_page, []

    def fetch_page(self, page_number, page_size, *, checkpoint):
        checkpoint()
        if page_number == self.interrupt_page:
            raise KeyboardInterrupt()
        self.calls.append(page_number)
        url = tj.page_url(page_number, page_size, self.source_id)
        body = json.dumps({"code": 200, "columnNames": list(self.fields), "list": [list(self.fields.values())],
                           "totalCount": self.total}, ensure_ascii=False).encode()
        return FetchResult(url, url, 200, {"content-type": "application/json"}, body,
                           "2026-09-30T12:00:00Z", 1)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ingestion = Store(self.root / "ingestion")
        self.catalog = Catalog(self.root / "catalog")
        self.sequence = 0

    def tearDown(self):
        self.ingestion.close()
        self.catalog.close()
        self.temp.cleanup()

    def collect(self, source, fields, *, simulation=True):
        self.sequence += 1
        run = self.ingestion.create_run({**tj.request_spec(source_id=source), "simulation": simulation},
                                        str(self.sequence))[0]
        with patch("socket.socket", side_effect=AssertionError("虚构资料不得联网")):
            tj.execute(self.ingestion, run, ResourceTransport(source, fields))
            raw = export_bundle(self.ingestion, run)
            bundle = normalize_bundle(raw)
            self.catalog.import_bundle(bundle)
        return raw, bundle

    def test_fixed_resource_routes_and_capture_identities_are_separate(self):
        ids = []
        for source, resource in tj.SOURCE_RESOURCES.items():
            raw, bundle = self.collect(source, row_fields())
            report = self.ingestion.report(raw["run_id"])
            capture = report["captures"][0]
            self.assertIn("/api/invoke/" + resource + "?", capture["url"])
            self.assertEqual(bundle["source_id"], source)
            self.assertEqual(replay(self.ingestion, capture["id"])["parsed"]["status"], "ok")
            ids.append(bundle["observations"][0]["notice_id"])
        # 即使三个API的行字节相同，也不是同一公告，不能覆盖不同资源的身份。
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(self.catalog.query(simulation=True)["total"], 3)
        sources = {r[0] for r in self.ingestion.db.execute("SELECT source_id FROM versions")}
        self.assertEqual(sources, set(tj.SOURCE_RESOURCES))

    def test_transport_sends_only_selected_allowlisted_resource(self):
        token = self.root / "fake-token.txt"
        token.write_text("fixture-not-a-real-token", encoding="utf-8")
        for source, resource in tj.SOURCE_RESOURCES.items():
            calls = []
            def exchange(url, *args):
                calls.append(url)
                return 200, {"content-type": "application/json"}, b'{"code":500,"msg":"fixture failure"}'
            with patch.object(tj, "utc_now", return_value="2026-09-30T12:00:00Z"):
                result = tj.TianjinTransport(source_id=source, allow_network=True, token_file=token,
                    resolver=lambda *args: ["1.1.1.1"], exchange=exchange).fetch_page(1, 2, checkpoint=lambda: None)
            self.assertEqual(len(calls), 1)
            self.assertIn("/api/invoke/" + resource + "?", calls[0])
            self.assertNotIn("fixture-not-a-real-token", repr(result))
        for invalid in ("tj_procurement_unreviewed", "https://other.example/api", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                tj.TianjinTransport(source_id=invalid)

    def test_transport_resource_mismatch_does_not_acquire_or_fetch(self):
        run = self.ingestion.create_run(tj.request_spec(source_id=CORRECTION), "mismatch")[0]
        wrong = ResourceTransport(tj.SOURCE_ID, row_fields())
        with self.assertRaises(StoreError) as error:
            tj.execute(self.ingestion, run, wrong)
        self.assertEqual(str(error.exception), "transport_source_mismatch")
        self.assertEqual(wrong.calls, [])
        self.assertEqual(self.ingestion.run(run)["status"], "queued")
        self.assertEqual(self.ingestion.report(run)["captures"], [])

    def test_cli_resume_keeps_resource_and_fetches_only_uncommitted_page(self):
        request = {**tj.request_spec(source_id=CORRECTION, pages=2, page_size=1), "simulation": True}
        run = self.ingestion.create_run(request, "resume-correction")[0]
        first = ResourceTransport(CORRECTION, row_fields(), total=2, interrupt_page=2)
        with self.assertRaises(KeyboardInterrupt):
            tj.execute(self.ingestion, run, first)
        resumed = ResourceTransport(CORRECTION, row_fields(), total=2)
        with patch.object(tj, "TianjinTransport", return_value=resumed) as factory, redirect_stdout(io.StringIO()):
            result = main(["--store", str(self.root / "ingestion"), "resume", run])
        self.assertEqual(result, 0)
        self.assertEqual(factory.call_args.kwargs["source_id"], CORRECTION)
        self.assertEqual(resumed.calls, [2])
        self.assertEqual(len(self.ingestion.report(run)["captures"]), 2)

    def test_cli_collect_binds_selected_resource_and_idempotency(self):
        # CLI入口不能默默回到默认谈判资源；同一个幂等键换资源必须拒绝。
        transport = ResourceTransport(CONSULTATION, row_fields())
        args = ["--store", str(self.root / "ingestion"), "collect-tianjin", "--key", "cli-resource",
                "--resource", "consultation"]
        with patch.object(tj, "TianjinTransport", return_value=transport) as factory, redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 0)
            self.assertEqual(factory.call_args.kwargs["source_id"], CONSULTATION)
            self.assertFalse(factory.call_args.kwargs["allow_network"])
            self.assertEqual(main(args), 0)
            self.assertEqual(main(args[:-1] + ["correction"]), 4)
        self.assertEqual(transport.calls, [1])

    def test_material_field_evidence_is_kept_without_downloading_any_address(self):
        text = '<a href="http://127.0.0.1/private">外部材料</a>'
        for source, label in ((CONSULTATION, "其他附件文件下载链接"), (CORRECTION, "更正附件链接")):
            raw, bundle = self.collect(source, row_fields(**{label: text}))
            doc = raw["documents"][0]
            item = self.catalog.detail(bundle["observations"][0]["notice_id"])["current"]
            self.assertEqual(item["material_status"], "reference_only")
            self.assertEqual(item["material_reference_evidence"],
                [{"label": label, "text": text, "locator": "$.list[0][3]"}])
            self.assertEqual(doc["content"]["attachments"], [])
            self.assertEqual(len(self.ingestion.report(raw["run_id"])["captures"]), 1)
        _, missing = self.collect(CONSULTATION, row_fields())
        self.assertEqual(missing["observations"][0]["material_status"], "attachment_reference_missing")
        for blank in (None, "", " \t\n"):
            _, empty = self.collect(CONSULTATION, row_fields(**{"其他附件文件下载链接": blank}))
            self.assertEqual(empty["observations"][0]["material_status"], "attachment_reference_missing")
            self.assertEqual(empty["observations"][0]["material_reference_evidence"], [])

    def test_resource_category_and_explicit_title_conflict_are_distinguished(self):
        _, original = self.collect(CONSULTATION, row_fields())
        _, correction = self.collect(CORRECTION, row_fields())
        _, contradictory = self.collect(CORRECTION, row_fields("虚构项目招标公告"))
        self.assertEqual(original["observations"][0]["notice_type"], "procurement")
        self.assertEqual(correction["observations"][0]["notice_type"], "correction")
        self.assertEqual(contradictory["observations"][0]["notice_type"], "unknown")

    def test_original_publication_date_does_not_become_correction_publication_date(self):
        _, bundle = self.collect(CORRECTION, row_fields(**{"首次公告日期": "2026-09-01", "公告发布时间": "2026-09-30"}))
        facts = bundle["observations"][0]["facts"]
        self.assertEqual(facts["original_published_at"]["value"]["local"], "2026-09-01")
        self.assertEqual(facts["published_at"]["status"], "unparsed")
        self.assertEqual(facts["published_at"]["evidence"][0]["text"], "2026-09-30")

    def test_correction_can_find_original_in_other_reviewed_tianjin_resource(self):
        _, original = self.collect(CONSULTATION, row_fields())
        correction_fields = {"公告标题": "虚构事项更正公告", "采购项目编号": "DEMO-RESOURCE-001", "采购人名称": "虚构采购方"}
        _, correction = self.collect(CORRECTION, correction_fields)
        item = correction["observations"][0]
        relations = self.catalog.detail(item["notice_id"])["relationships"]
        self.assertEqual(relations[0]["status"], "candidate")
        self.assertEqual(relations[0]["basis"], "same_publisher_buyer_project_number")
        self.assertEqual(relations[0]["target_observation_id"], original["observations"][0]["observation_id"])
        self.collect(tj.SOURCE_ID, row_fields())
        relations = self.catalog.detail(item["notice_id"])["relationships"]
        self.assertEqual(len(relations), 2)
        self.assertEqual({r["status"] for r in relations}, {"ambiguous"})

    def test_cross_resource_lot_mismatch_and_simulation_are_not_linked(self):
        self.collect(CONSULTATION, row_fields(**{"包号": "1"}))
        _, correction = self.collect(CORRECTION, row_fields(**{"包号": "2"}))
        relations = self.catalog.detail(correction["observations"][0]["notice_id"])["relationships"]
        self.assertEqual(relations[0]["status"], "unresolved")
        # 此处simulation=False只测目录隔离，传输仍是离线虚构对象，不是实网成绩。
        _, separate = self.collect(CORRECTION, row_fields(), simulation=False)
        self.assertEqual(self.catalog.detail(separate["observations"][0]["notice_id"])["relationships"][0]["status"], "unresolved")

    def test_contract_reference_prevents_false_procurement_relationship(self):
        self.collect(CONSULTATION, row_fields())
        _, correction = self.collect(CORRECTION, row_fields(**{"原合同公告链接": "https://example.test/contract"}))
        relations = self.catalog.detail(correction["observations"][0]["notice_id"])["relationships"]
        self.assertEqual(relations[0]["basis"], "original_contract_outside_scope")
        self.assertEqual(relations[0]["status"], "unresolved")
        self.assertNotIn("target_notice_id", relations[0])
        self.assertEqual(relations[0]["evidence"][0]["label"], "原合同公告链接")

    def test_same_number_and_buyer_do_not_link_different_publishers(self):
        _, original = self.collect(CONSULTATION, row_fields())
        _, correction = self.collect(CORRECTION, row_fields())
        other = {**original["observations"][0], "source_id": "cn_ccgp"}
        # 单测关系纯函数：换成另一发布方或未核实tj_资源，不能借相同项目号串联。
        for source in ("cn_ccgp", "tj_procurement_unreviewed"):
            other["source_id"] = source
            result = relationships(correction["observations"][0], [other])
            self.assertEqual(result[0]["status"], "unresolved")
            self.assertNotIn("target_notice_id", result[0])

    def test_v1_compatibility_does_not_rewrite_old_observation_or_accept_version_mix(self):
        _, v2 = self.collect(tj.SOURCE_ID, row_fields("虚构项目采购公告"))
        old = legacy_bundle(v2)
        item = old["observations"][0]
        self.catalog.import_bundle(old)
        self.assertIn(item, self.catalog.detail(item["notice_id"])["history"])
        self.assertEqual(self.catalog.detail(item["notice_id"])["current"], v2["observations"][0])
        for invalid in ({**v2, "schema_version": 1}, {**old, "schema_version": 2},
                        {**old, "source_id": CORRECTION}, {**v2, "source_id": []}):
            with self.assertRaises(ValueError):
                self.catalog.import_bundle(invalid)
        malformed = deepcopy(v2)
        bad = malformed["observations"][0]
        bad["material_reference_evidence"] = [{"label": "附件", "text": 123, "locator": "x"}]
        bad["observation_id"] = fingerprint({k: v for k, v in bad.items() if k != "observation_id"})
        with self.assertRaises(ValueError):
            self.catalog.import_bundle(malformed)

    def test_late_v1_cannot_restore_contract_as_procurement_candidate(self):
        self.collect(tj.SOURCE_ID, row_fields("虚构项目采购公告"))
        _, v2 = self.collect(tj.SOURCE_ID, row_fields("虚构事项更正公告",
            **{"原合同公告链接": "https://example.test/contract"}))
        current = v2["observations"][0]
        old = legacy_bundle(v2)
        self.catalog.import_bundle(old)
        # 迟到旧解释应进入历史，不能成为当前并丢失原合同语义；重开后规则相同。
        self.catalog.close()
        self.catalog = Catalog(self.root / "catalog")
        detail = self.catalog.detail(current["notice_id"])
        self.assertEqual(detail["current"], current)
        self.assertIn(old["observations"][0], detail["history"])
        self.assertEqual(detail["relationships"][0]["basis"], "original_contract_outside_scope")
        rows = self.catalog.query(simulation=True, kind="correction")["items"]
        self.assertEqual(rows[0]["observation_id"], current["observation_id"])

    def test_version_preference_preserves_snapshot_and_newer_acquisition(self):
        _, first = self.collect(tj.SOURCE_ID, row_fields("虚构一号采购公告"))
        _, second = self.collect(tj.SOURCE_ID, row_fields("虚构二号采购公告"))
        with_catalog = Catalog(self.root / "migration")
        try:
            with_catalog.import_bundle(legacy_bundle(first))
            with_catalog.import_bundle(legacy_bundle(second))
            page = with_catalog.query(simulation=True, page_size=1)
            with_catalog.import_bundle(first)
            with_catalog.import_bundle(second)
            next_page = with_catalog.query(simulation=True, page_size=1, cursor=page["next_cursor"])
            self.assertEqual(next_page["items"][0]["normalizer_version"], "procurement-facts-v1")
            self.assertEqual({r["normalizer_version"] for r in with_catalog.query(simulation=True)["items"]},
                             {"procurement-facts-v2"})
            # 规范版本只在获取时间相同时排序；不能让旧获取的v2压过新获取的v1。
            newer = legacy_bundle(first)
            item = newer["observations"][0]
            item.update(observed_at="2026-10-01T12:00:00+00:00", capture_id="fixture-newer-capture")
            item["observation_id"] = fingerprint({k: v for k, v in item.items() if k != "observation_id"})
            with_catalog.import_bundle(newer)
            self.assertEqual(with_catalog.detail(item["notice_id"])["current"], item)
        finally:
            with_catalog.close()


if __name__ == "__main__":
    unittest.main()

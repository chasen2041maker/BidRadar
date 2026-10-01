"""v3公开事实语义回归。所有正文/机构/编号均为虚构；不联网，不证明来源授权。"""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from services.catalog.store import Catalog
from services.processing.normalize import normalize_bundle, fingerprint
from test_data_chain import raw_bundle


def sample(*paragraphs, metadata=None, title="虚构项目竞争性磋商公告", source="cn_ccgp", key=None):
    raw = raw_bundle(title=title, entries=metadata or [("项目编号", "FICTION-2026-001"), ("采购单位", "虚构采购方")])
    raw["source_id"] = source
    document = raw["documents"][0]
    if key:
        document.update(source_url=key, source_record_key=key)
    document["content"]["segments"] = [{"text": text, "locator": f"fiction/p[{i}]"} for i, text in enumerate(paragraphs, 1)]
    return raw


def observation(*args, **kwargs):
    return normalize_bundle(sample(*args, **kwargs))["observations"][0]


def rehash(bundle):
    for item in bundle["observations"]:
        item["observation_id"] = fingerprint({k: v for k, v in item.items() if k != "observation_id"})


class EvidenceSemanticsTests(unittest.TestCase):
    def test_summary_and_body_table_budget_conflict_preserves_both_and_precedence(self):
        item = observation("预算金额：1万元，最高限价：1万元。", "采购条目名称 | 数量 | 采购预算（元）",
                           "虚构系统 | 1 | 1000000.00", "因系统问题，预算以采购需求中的金额为准。",
                           metadata=[("预算金额", "￥1.000000万元（人民币）")])
        self.assertEqual(item["facts"]["budget"]["status"], "conflicting")
        extra = item["evidence_fields"]
        self.assertEqual(extra["budget_assessment"]["status"], "conflicting")
        self.assertIn("source_precedence_statement", extra["budget_assessment"]["reasons"])
        self.assertEqual({Decimal(x["amount"]) for x in extra["money"] if x["role"] == "budget"},
                         {Decimal("10000"), Decimal("1000000")})
        self.assertIn("fiction/p[3]", {e["locator"] for e in extra["budget_assessment"]["evidence"]})

    def test_zero_budget_and_patient_unit_price_never_become_project_total(self):
        item = observation("预算金额：0元。最高限价：5元/患者影像检查人次。",
                           "合同履行期限：5年。履约保证金：30万元。", source="cn_hainan")
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
        extra = item["evidence_fields"]
        self.assertEqual(extra["budget_assessment"]["status"], "context_required")
        unit = next(x for x in extra["money"] if x["role"] == "unit_price")
        self.assertEqual((unit["amount"], unit["unit_basis"]), ("5", "患者影像检查人次"))
        self.assertEqual(next(x for x in extra["money"] if x["role"] == "deposit")["amount"], "300000")
        self.assertTrue(extra["delivery_evidence"])

    def test_multilot_total_and_c_package_remain_separate_with_qualification(self):
        item = observation("预算金额：7134800元。", "C包预算金额：255600元。",
                           "C包供应商须具备测绘乙级资质。", source="cn_hainan")
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
        claims = item["evidence_fields"]["money"]
        self.assertEqual([(x["role"], x["amount"], x["package"]) for x in claims],
                         [("budget", "7134800", None), ("package_budget", "255600", "C")])
        self.assertEqual(item["evidence_fields"]["qualification_evidence"][0]["text"], "C包供应商须具备测绘乙级资质。")

    def test_separate_ceiling_heading_followed_by_package_rows_and_other_section_access(self):
        item = observation("预算金额：7134800元", "最高限价：", "虚构遥感服务（FICTION-A包）：2732800元",
                           "虚构农业服务（FICTION-B包）：4146400元", "虚构分发服务（FICTION-C包）：255600元",
                           "三、获取招标文件", "售价：0（元）", "六、其他补充事宜", "须登记企业信息后登陆平台下载招标文件，使用数字身份认证锁。")
        extra = item["evidence_fields"]
        self.assertEqual(extra["budget_assessment"]["status"], "context_required")
        package = [x for x in extra["money"] if x["package"] == "C"][0]
        self.assertEqual((package["role"], package["amount"]), ("ceiling", "255600"))
        self.assertEqual([e["text"] for e in package["evidence"]], ["最高限价：", "虚构分发服务（FICTION-C包）：255600元"])
        self.assertEqual({x["kind"] for x in extra["access_conditions"]}, {"registration", "login", "ca_certificate"})
        self.assertEqual(next(x for x in extra["money"] if x["role"] == "file_fee")["amount"], "0")

    def test_unit_price_in_requirement_paragraph_and_uppercase_deposit(self):
        item = observation("预算金额：0元", "最高限价：", "虚构影像服务（FICTION-003）：5元",
                           "按患者影像检查人次报价，服务综合单价≤5.00元/人次，单位为“元/人次”。",
                           "投标人应提交投标保证金人民币叁拾万元整（￥300,000.00），到账截止时间与响应截止时间一致。")
        extra = item["evidence_fields"]
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
        self.assertEqual(next(x for x in extra["money"] if x["role"] == "unit_price")["unit_basis"], "人次")
        self.assertEqual(next(x for x in extra["money"] if x["role"] == "deposit")["amount"], "300000.00")

    def test_acquisition_dates_response_deadline_price_and_access_roles(self):
        item = observation("三、获取采购文件", "时间：2026年10月08日至2026年10月13日，每天上午9:00至12:00。（北京时间）",
                           "方式：在平台注册并登录，申请报名后支付文件费。", "售价：￥500.0元（人民币）",
                           "四、响应文件提交", "截止时间：2026年10月19日14点30分（北京时间）",
                           "五、开启", "时间：2026年10月20日09点00分（北京时间）", "预算金额：48万元。")
        extra = item["evidence_fields"]
        self.assertEqual(extra["acquisition_window"]["value"]["end"]["local"], "2026-10-13")
        self.assertEqual(item["facts"]["response_deadline"]["value"]["local"], "2026-10-19T14:30")
        self.assertEqual(item["facts"]["budget"]["value"]["amount"], "480000")
        self.assertEqual(next(x for x in extra["money"] if x["role"] == "file_fee")["amount"], "500.0")
        self.assertEqual({x["kind"] for x in extra["access_conditions"]}, {"registration", "login", "application", "payment"})
        self.assertEqual(extra["material_availability"]["status"], "not_obtained")

    def test_no_login_statement_is_evidence_not_required_boolean(self):
        item = observation("三、获取采购文件", "无需登录或支付即可下载。")
        conditions = item["evidence_fields"]["access_conditions"]
        self.assertTrue(conditions)
        self.assertTrue(all(condition["status"] == "mentioned" for condition in conditions))
        self.assertTrue(all("无需" in condition["evidence"][0]["text"] for condition in conditions))

    def test_buyer_section_does_not_leak_agency_name(self):
        item = observation("项目编号：FICTION-002", "1.采购人信息", "名称：虚构买方甲",
                           "2.采购代理机构信息", "名称：虚构代理乙", metadata=[("发布日期", "2026-09-30")])
        self.assertEqual(item["facts"]["buyer"]["value"], "虚构买方甲")
        self.assertEqual(item["facts"]["project_number"]["value"], "FICTION-002")

    def test_equivalent_units_do_not_create_false_conflict(self):
        item = observation("预算金额：10000元。", metadata=[("预算金额", "1万元")])
        self.assertEqual(item["evidence_fields"]["budget_assessment"]["status"], "known")
        self.assertEqual(item["facts"]["budget"]["status"], "known")

    def test_amount_range_approximate_overflow_or_fraction_stays_unparsed(self):
        for value in ("10-12万元", "约10万元", "10余万元", "10万元左右", "10万元以上", "10万元（暂定）",
                      "1234,567元", "1000000000000001元", "1.0000001元"):
            with self.subTest(value=value):
                item = observation("预算金额：" + value)
                self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
                self.assertIsNone(item["facts"]["budget"]["value"])

    def test_price_denominator_does_not_attach_to_previous_sentence_budget(self):
        extra = observation("预算金额：0元。按检查人次报价，单价5元/人次。")["evidence_fields"]
        self.assertFalse(any(x["role"] == "unit_price" and x["amount"] == "0" for x in extra["money"]))
        self.assertEqual(extra["budget_assessment"]["status"], "context_required")

    def test_prefix_parser_does_not_override_unsupported_currency_or_large_unit(self):
        for value in ("10元（美元）", "10亿元", "10亿美元", "10元 USD"):
            item = observation(metadata=[("预算（万元）", value)])
            self.assertEqual(item["facts"]["budget"]["status"], "unparsed")
            self.assertIsNone(item["evidence_fields"]["money"][0]["amount"])

    def test_source_precedence_without_recovered_body_amount_does_not_trust_summary(self):
        item = observation("因系统问题，预算金额以正文采购需求为准。", metadata=[("预算金额", "1万元")])
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")

    def test_correction_window_and_money_cannot_be_assumed_current(self):
        item = observation("三、获取采购文件", "时间：2026年10月1日至2026年10月5日", "预算金额：10万元",
                           title="虚构项目更正公告")
        self.assertEqual(item["evidence_fields"]["acquisition_window"]["status"], "unparsed")
        self.assertEqual(item["facts"]["budget"]["status"], "unparsed")

    def test_windows_conflict_and_invalid_order_remain_unknown(self):
        for paragraphs, state in ((["时间：2026-10-08至2026-10-13", "时间：2026-10-09至2026-10-13"], "conflicting"),
                                  (["时间：2026-10-13至2026-10-08"], "unparsed")):
            item = observation("三、获取采购文件", *paragraphs)
            self.assertEqual(item["evidence_fields"]["acquisition_window"]["status"], state)
            self.assertIsNone(item["evidence_fields"]["acquisition_window"]["value"])

    def test_invalid_or_unconfirmed_window_is_not_misrepresented_as_absent(self):
        for text in ("时间：详见文件", "时间：2026-02-30至2026-03-01"):
            window = observation("三、获取采购文件", text)["evidence_fields"]["acquisition_window"]
            self.assertEqual(window["status"], "unparsed")
            self.assertEqual(window["evidence"][0]["text"], text)

    def test_single_segment_multiple_windows_or_reschedule_do_not_choose_first_pair(self):
        for text in ("时间：2026年10月1日至2026年10月8日，第二批：2026年10月10日至2026年10月11日",
                     "时间：2026年10月1日至2026年10月8日，补充说明：2026年10月10日",
                     "时间：2026年10月1日至2026年10月8日，延期安排另行通知"):
            with self.subTest(text=text):
                window = observation("三、获取采购文件", text)["evidence_fields"]["acquisition_window"]
                self.assertEqual(window["status"], "unparsed")
                self.assertIsNone(window["value"])
                # 所有日期/延期措辞均留在同一原文证据，不能裁成看似确定的第一批窗口。
                self.assertEqual(window["evidence"], [{"label": "正文", "text": text, "locator": "fiction/p[2]"}])

    def test_invalid_start_date_cannot_be_replaced_by_later_valid_date(self):
        for ending in ("延期至2026年3月4日", "补充说明：2026年3月4日"):
            text = "时间：2026年2月30日至2026年3月2日，" + ending
            with self.subTest(text=text):
                window = observation("三、获取采购文件", text)["evidence_fields"]["acquisition_window"]
                self.assertEqual(window["status"], "unparsed")
                self.assertIsNone(window["value"])
                self.assertEqual(window["evidence"], [{"label": "正文", "text": text, "locator": "fiction/p[2]"}])

    def test_original_project_number_and_short_method_title_are_explicit_evidence(self):
        item = observation("原公告的采购项目编号：FICTION-001", metadata=[("采购人", "虚构采购方")], title="虚构项目更正公告")
        self.assertEqual(item["facts"]["project_number"]["value"], "FICTION-001")
        self.assertEqual(observation(title="虚构项目竞争性磋商")["notice_type"], "procurement")

    def test_attachments_require_archive_evidence_and_keep_reference_gaps(self):
        raw = sample()
        attachments = raw["documents"][0]["content"]["attachments"] = [
            {"url": "https://example.test/a.pdf", "name": "虚构附件", "locator": "a[1]", "status": "fetched"}]
        self.assertEqual(normalize_bundle(raw)["observations"][0]["evidence_fields"]["material_availability"]["status"], "not_obtained")
        attachments[0].update(fetch_status="fetched", capture_id="fiction-capture", sha256="a" * 64)
        fields = normalize_bundle(raw)["observations"][0]["evidence_fields"]
        self.assertEqual(fields["material_availability"]["status"], "obtained")
        self.assertEqual(fields["material_availability"]["references"][0]["raw_sha256"], "a" * 64)
        attachments.append({"url": "https://example.test/b.pdf", "name": "另一个附件", "locator": "a[2]", "status": "not_fetched"})
        self.assertEqual(normalize_bundle(raw)["observations"][0]["evidence_fields"]["material_availability"]["status"], "partially_obtained")
        del attachments[0]["sha256"]
        with self.assertRaises(ValueError):
            normalize_bundle(raw)


class EvidenceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.temp.name) / "catalog")

    def tearDown(self):
        self.catalog.close()
        self.temp.cleanup()

    def test_body_technical_keyword_matches_even_when_category_is_meteorology(self):
        bundle = normalize_bundle(sample("采购需求：软件开发及智能体建设。", title="虚构气象项目采购公告",
                                         metadata=[("品目", "气象服务")]))
        self.catalog.import_bundle(bundle)
        page = self.catalog.query(keyword="智能体", simulation=True)
        self.assertEqual(page["total"], 1)
        extra = self.catalog.detail(page["items"][0]["notice_id"])["current"]["evidence_fields"]
        self.assertEqual(extra["category_evidence"][0]["text"], "气象服务")
        self.assertIn("软件开发", extra["technical_evidence"][0]["text"])

    def test_v1_v2_v3_rank_without_rewriting_history_or_accepting_version_mix(self):
        newest = normalize_bundle(sample("预算金额：10万元"))
        old2 = deepcopy(newest)
        old2.update(schema_version=2, normalizer_version="procurement-facts-v2")
        for item in old2["observations"]:
            item.update(normalizer_version=old2["normalizer_version"])
            item.pop("evidence_fields")
        rehash(old2)
        old1 = deepcopy(old2)
        old1.update(schema_version=1, normalizer_version="procurement-facts-v1")
        for item in old1["observations"]:
            item.update(normalizer_version=old1["normalizer_version"])
            item.pop("material_reference_evidence")
            item["facts"].pop("original_published_at")
            item["facts"].pop("original_contract_reference")
        rehash(old1)
        for bundle in (newest, old2, old1):
            self.catalog.import_bundle(bundle)
        detail = self.catalog.detail(newest["observations"][0]["notice_id"])
        self.assertEqual(detail["current"], newest["observations"][0])
        self.assertEqual(detail["history"], [newest["observations"][0], old2["observations"][0], old1["observations"][0]])
        with self.assertRaises(ValueError):
            self.catalog.import_bundle({**newest, "schema_version": 2})
        with self.assertRaises(ValueError):
            self.catalog.import_bundle({**old2, "source_id": "cn_hainan"})

    def test_correction_and_award_lifecycle_link_with_candidate_evidence_only(self):
        notices = []
        for number, title in (("1", "虚构项目更正公告"), ("2", "虚构项目成交结果公告")):
            bundle = normalize_bundle(sample(title=title, source="cn_hainan", key="https://ggzy.hainan.gov.cn/" + number))
            self.catalog.import_bundle(bundle)
            notices.append(bundle["observations"][0])
        detail = self.catalog.detail(notices[1]["notice_id"], as_of="2026-10-01T00:00:00Z")
        self.assertEqual(detail["currentness"]["status"], "not_opening_notice")
        self.assertEqual(detail["relationships"][0]["status"], "candidate")
        self.assertEqual(detail["relationships"][0]["target_observation_id"], notices[0]["observation_id"])
        self.assertEqual(detail["relationships"][0]["target_notice_type"], "correction")
        self.assertNotEqual(notices[0]["notice_id"], notices[1]["notice_id"])

    def test_invalid_extra_fields_rejected_atomically_even_with_recomputed_hash(self):
        base = normalize_bundle(sample("预算金额：10万元", "三、获取采购文件", "时间：2026-10-01至2026-10-08"))
        for mutate in (lambda extra: extra.update(eligibility=True),
                       lambda extra: extra["money"][0].update(amount="nan"),
                       lambda extra: extra["money"][0].update(role="guaranteed_revenue"),
                       lambda extra: extra["money"][0].update(evidence=[]),
                       lambda extra: extra["acquisition_window"]["value"]["end"].update(local="2026-10-99"),
                       lambda extra: extra["material_availability"].update(status="obtained")):
            bundle = deepcopy(base)
            mutate(bundle["observations"][0]["evidence_fields"])
            rehash(bundle)
            with self.assertRaises(ValueError):
                self.catalog.import_bundle(bundle)
            self.assertEqual(self.catalog.query(simulation=True)["total"], 0)


if __name__ == "__main__":
    unittest.main()

"""catalog 的领域规则：规范化公告的格式检查、"当前版本"怎么选、截止时间状态、更正和结果公告的关联。

纯 Python，不 import FastAPI 或 SQLAlchemy。大部分规则从旧版 services/catalog/store.py 原样搬来，
旧版的 486 项测试覆盖过它们；v2 只换了存储，没换规则。

几个核心概念：
    - 公告（notice）：一条采购公告，ID 由"来源 + 是否演示 + 来源里的记录键"算出，同一公告 ID 不变。
    - 观察（observation，表里叫 notice_versions）：某次抓取得到的公告内容。同一公告会有多个观察，每个都不可修改。
    - 当前版本：抓取时间最新的那个观察。晚到的旧观察不会把当前版本"倒退"回去。
    - 快照（snapshot）：观察的接收序号上限。搜索翻页、研究取材都固定在一个快照上，
      期间导入的新数据不会让结果前后不一致。
    - 关联（relationships）：更正公告、结果公告和原采购公告之间的关系，需要有依据才算确认。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re

from bidradar.infra.errors import AppError

# 公告类型：采购、更正、结果、终止、意向、未知。
KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}
# 规范化事实字段（v1），v2 加了原发布时间和原合同引用。
FIELDS = {"project_number", "buyer", "published_at", "budget", "response_deadline", "lot_identifier"}
V2_FIELDS = FIELDS | {"original_published_at", "original_contract_reference"}
# 来源 → 发布方。只有同一发布方的公告之间才会建立更正、结果关联。
# 这里只列已经核实过的来源，不按名字前缀（比如 tj_）推断。
SOURCE_FAMILIES = {
    "cn_ccgp": "cn_ccgp",
    "cn_hainan": "cn_hainan",
    "tj_procurement_negotiation": "tj_finance_procurement",
    "tj_procurement_consultation": "tj_finance_procurement",
    "tj_procurement_correction": "tj_finance_procurement",
}
# 每个格式版本接受哪些规范化器版本，以及允许的来源。
# v4 只改了公告类型的判断规则，包的结构和 v3 一样，所以都是格式版本 3。
SCHEMAS = {
    1: ({"procurement-facts-v1"}, {"cn_ccgp", "tj_procurement_negotiation"}),
    2: ({"procurement-facts-v2"}, set(SOURCE_FAMILIES) - {"cn_hainan"}),
    3: ({"procurement-facts-v3", "procurement-facts-v4"}, set(SOURCE_FAMILIES)),
}
# 选"当前版本"的排序：先比抓取时间；同一时刻抓到的，选规范化器版本高的；
# 最后才比接收序号。这样晚到的 v1 数据不会把已有的 v2 结果降级。
# 版本高低按这张表查，不按字符串比较。排序本身写在 repo.py 的 SQL 里（current_versions）。
NORMALIZER_RANK = {
    "procurement-facts-v1": 1,
    "procurement-facts-v2": 2,
    "procurement-facts-v3": 3,
    "procurement-facts-v4": 4,
}
MAX_BUNDLE_BYTES = 32 * 1024 * 1024


def _json(value):
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    """对象的 SHA-256，用来校验观察 ID 和公告 ID。"""
    return sha256(_json(value).encode()).hexdigest()


def _time(value):
    """解析带时区的 ISO 8601 时间，统一转成 UTC。"""
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("invalid_timestamp") from None


def _string(value, limit=4096, empty=False):
    """检查是不超长的字符串；empty=False 时不能全是空白。"""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_string")


def _evidence(value):
    """检查证据列表：每条都是 {label, text, locator}。"""
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError("invalid_evidence")
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
            raise ValueError("invalid_evidence")
        _string(entry["label"], 100)
        _string(entry["locator"], 512)
        _string(entry["text"], 200000)


def _date_value(value):
    """检查日期事实：精度（日、分、秒）和格式一致；只有原文写明北京时间才带 +08:00。"""
    if (
        not isinstance(value, dict)
        or set(value) != {"local", "precision", "timezone", "timezone_basis"}
        or value["precision"] not in ("date", "minute", "second")
        or value["timezone"] not in (None, "+08:00")
    ):
        raise ValueError("invalid_date_fact")
    _string(value["local"], 32)
    patterns = {
        "date": r"\d{4}-\d{2}-\d{2}",
        "minute": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
        "second": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
    }
    if not re.fullmatch(patterns[value["precision"]], value["local"]):
        raise ValueError("date_precision_mismatch")
    datetime.fromisoformat(value["local"])
    if value["timezone_basis"] != ("原文明确北京时间" if value["timezone"] else None):
        raise ValueError("date_timezone_basis_mismatch")


def _validate_evidence_fields(value):
    """检查 v3 格式里的 evidence_fields（金额、获取窗口、资格、交付等证据）。

    目录自己做一遍完整检查，不依赖 ingestion 的规范化代码：两边各自守住格式，任何一边出错都能被另一边挡住。
    """
    keys = {
        "money",
        "budget_assessment",
        "acquisition_window",
        "access_conditions",
        "material_availability",
        "category_evidence",
        "technical_evidence",
        "qualification_evidence",
        "delivery_evidence",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_evidence_fields")
    for name in ("category_evidence", "technical_evidence", "qualification_evidence", "delivery_evidence"):
        _evidence(value[name])
    money = value["money"]
    if not isinstance(money, list) or len(money) > 10000:
        raise ValueError("invalid_money_claims")
    for claim in money:
        if (
            not isinstance(claim, dict)
            or set(claim)
            != {"role", "status", "amount", "currency", "source_unit", "package", "unit_basis", "evidence"}
            or claim["role"] not in ("budget", "ceiling", "unit_price", "package_budget", "file_fee", "deposit")
            or claim["status"] not in ("parsed", "unparsed")
            or claim["currency"] != "CNY"
        ):
            raise ValueError("invalid_money_claim")
        _evidence(claim["evidence"])
        if not claim["evidence"]:
            raise ValueError("money_without_evidence")
        if claim["status"] == "parsed":
            _string(claim["amount"], 40)
            if (
                not re.fullmatch(r"\d+(?:\.\d{1,6})?", claim["amount"])
                or Decimal(claim["amount"]) > 10**15
                or claim["source_unit"] not in ("元", "万元")
            ):
                raise ValueError("invalid_money_value")
        elif claim["amount"] is not None or claim["source_unit"] is not None:
            raise ValueError("unparsed_money_has_value")
        for name in ("package", "unit_basis"):
            if claim[name] is not None:
                _string(claim[name], 2000)
        if (claim["role"] == "unit_price") != (claim["unit_basis"] is not None):
            raise ValueError("invalid_unit_price_basis")
        if claim["role"] == "package_budget" and claim["package"] is None:
            raise ValueError("package_budget_without_package")
    assessment = value["budget_assessment"]
    if (
        not isinstance(assessment, dict)
        or set(assessment) != {"status", "reasons", "evidence"}
        or assessment["status"] not in ("known", "missing", "conflicting", "unparsed", "context_required")
        or not isinstance(assessment["reasons"], list)
        or len(assessment["reasons"]) > 6
        or any(
            reason
            not in (
                "different_budget_amounts",
                "package_amounts_present",
                "unit_pricing_present",
                "unparsed_budget_evidence",
                "source_precedence_statement",
                "correction_context",
            )
            for reason in assessment["reasons"]
        )
    ):
        raise ValueError("invalid_budget_assessment")
    _evidence(assessment["evidence"])
    window = value["acquisition_window"]
    if (
        not isinstance(window, dict)
        or set(window) != {"status", "value", "evidence"}
        or window["status"] not in ("known", "missing", "conflicting", "unparsed")
        or (window["status"] == "known") != (window["value"] is not None)
    ):
        raise ValueError("invalid_acquisition_window")
    _evidence(window["evidence"])
    if (window["status"] == "missing") != (not window["evidence"]):
        raise ValueError("invalid_acquisition_evidence")
    if window["status"] == "known":
        dates = window["value"]
        if not isinstance(dates, dict) or set(dates) != {"start", "end"}:
            raise ValueError("invalid_acquisition_dates")
        for date in dates.values():
            _date_value(date)
        if dates["start"]["local"] > dates["end"]["local"]:
            raise ValueError("reversed_acquisition_dates")
    conditions = value["access_conditions"]
    if not isinstance(conditions, list) or len(conditions) > 10000:
        raise ValueError("invalid_access_conditions")
    for condition in conditions:
        if (
            not isinstance(condition, dict)
            or set(condition) != {"kind", "status", "evidence"}
            or condition["kind"] not in ("registration", "login", "application", "payment", "ca_certificate")
            or condition["status"] != "mentioned"
        ):
            raise ValueError("invalid_access_condition")
        _evidence(condition["evidence"])
        if not condition["evidence"]:
            raise ValueError("condition_without_evidence")
    materials = value["material_availability"]
    if (
        not isinstance(materials, dict)
        or set(materials) != {"status", "references"}
        or materials["status"] not in ("not_obtained", "partially_obtained", "obtained")
        or not isinstance(materials["references"], list)
        or len(materials["references"]) > 1000
    ):
        raise ValueError("invalid_material_availability")
    for reference in materials["references"]:
        if not isinstance(reference, dict) or set(reference) != {
            "url",
            "name",
            "locator",
            "status",
            "capture_id",
            "raw_sha256",
        }:
            raise ValueError("invalid_material_reference")
        for name in ("url", "name", "locator", "status"):
            _string(reference[name], empty=True)
        if reference["status"] == "fetched":
            _string(reference["capture_id"], 128)
            if not isinstance(reference["raw_sha256"], str) or not re.fullmatch(
                "[0-9a-f]{64}", reference["raw_sha256"]
            ):
                raise ValueError("fetched_attachment_without_hash")
        elif reference["capture_id"] is not None or reference["raw_sha256"] is not None:
            raise ValueError("unfetched_attachment_has_capture")
    acquired = sum(ref["status"] == "fetched" for ref in materials["references"])
    expected = (
        "obtained"
        if acquired and acquired == len(materials["references"])
        else "partially_obtained"
        if acquired
        else "not_obtained"
    )
    if materials["status"] != expected:
        raise ValueError("material_status_without_acquisition")


def _validate(observation, bundle):
    """导入前检查一条观察：字段齐全、类型正确、ID 和内容哈希对得上、事实格式合法。"""
    if not isinstance(observation, dict):
        raise ValueError("invalid_observation")
    required = {
        "observation_id",
        "notice_id",
        "source_id",
        "simulation",
        "source_record_key",
        "identity_kind",
        "source_url",
        "capture_id",
        "raw_sha256",
        "parser_version",
        "normalizer_version",
        "observed_at",
        "title",
        "notice_type",
        "facts",
        "references",
        "material_status",
        "attribution",
        "run_id",
    }
    if bundle["schema_version"] >= 2:
        required.add("material_reference_evidence")
    if bundle["schema_version"] == 3:
        required.add("evidence_fields")
    if set(observation) != required:
        raise ValueError("unsupported_observation_fields")
    value = {k: v for k, v in observation.items() if k != "observation_id"}
    if observation["observation_id"] != _hash(value):
        raise ValueError("observation_hash_mismatch")
    for name in ("observation_id", "notice_id", "raw_sha256"):
        if not isinstance(observation[name], str) or not re.fullmatch("[0-9a-f]{64}", observation[name]):
            raise ValueError("invalid_hash")
    for name in ("source_id", "run_id", "simulation", "normalizer_version"):
        if observation[name] != bundle[name] or type(observation[name]) is not type(bundle[name]):
            raise ValueError("observation_bundle_mismatch")
    if observation["notice_id"] != _hash(
        [observation["source_id"], observation["simulation"], observation["source_record_key"]]
    ):
        raise ValueError("notice_identity_mismatch")
    if (
        observation["identity_kind"] not in ("source_url", "content_snapshot")
        or observation["notice_type"] not in KINDS
    ):
        raise ValueError("invalid_observation_kind")
    for name in ("source_record_key", "source_url", "capture_id", "parser_version", "material_status"):
        _string(observation[name])
    _string(observation["title"], 2000, empty=True)
    _time(observation["observed_at"])
    facts = observation["facts"]
    expected_fields = V2_FIELDS if bundle["schema_version"] >= 2 else FIELDS
    if not isinstance(facts, dict) or set(facts) != expected_fields:
        raise ValueError("invalid_facts")
    for name, fact in facts.items():
        if not isinstance(fact, dict) or set(fact) != {"status", "value", "evidence"}:
            raise ValueError("invalid_fact")
        state, value, evidence = fact["status"], fact["value"], fact["evidence"]
        if state not in ("known", "missing", "unparsed", "conflicting") or (state == "known") != (value is not None):
            raise ValueError("invalid_fact_state")
        if not isinstance(evidence, list) or len(evidence) > 10000 or (state == "missing") != (len(evidence) == 0):
            raise ValueError("invalid_fact_evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
                raise ValueError("invalid_fact_evidence")
            for item in entry.values():
                _string(item, 200000)
        if state != "known":
            continue
        if name in ("published_at", "response_deadline", "original_published_at"):
            if (
                not isinstance(value, dict)
                or set(value) != {"local", "precision", "timezone", "timezone_basis"}
                or value["precision"] not in ("date", "minute", "second")
                or value["timezone"] not in (None, "+08:00")
            ):
                raise ValueError("invalid_date_fact")
            _string(value["local"], 32)
            date_patterns = {
                "date": r"\d{4}-\d{2}-\d{2}",
                "minute": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
                "second": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}",
            }
            if not re.fullmatch(date_patterns[value["precision"]], value["local"]):
                raise ValueError("date_precision_mismatch")
            datetime.fromisoformat(value["local"])
            if value["timezone_basis"] != ("原文明确北京时间" if value["timezone"] else None):
                raise ValueError("date_timezone_basis_mismatch")
        elif name == "budget":
            if (
                not isinstance(value, dict)
                or set(value) != {"amount", "currency", "source_unit", "scope"}
                or not isinstance(value["amount"], str)
                or not re.fullmatch(r"\d+(?:\.\d+)?", value["amount"])
                or value["currency"] != "CNY"
                or value["source_unit"] not in ("元", "万元")
                or value["scope"] != "unspecified"
            ):
                raise ValueError("invalid_budget_fact")
        else:
            _string(value, 200000)
    if bundle["schema_version"] == 3:
        _validate_evidence_fields(observation["evidence_fields"])
        if (
            observation["evidence_fields"]["budget_assessment"]["status"]
            in ("conflicting", "context_required", "unparsed")
            and facts["budget"]["status"] == "known"
        ):
            raise ValueError("unsafe_budget_projection")
    if bundle["schema_version"] >= 2:
        evidence = observation["material_reference_evidence"]
        if not isinstance(evidence, list) or len(evidence) > 10000:
            raise ValueError("invalid_material_reference_evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
                raise ValueError("invalid_material_reference_evidence")
            for item in entry.values():
                _string(item, 200000)
    refs = observation["references"]
    if not isinstance(refs, list) or len(refs) > 1000:
        raise ValueError("invalid_references")
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"url", "text", "locator"}:
            raise ValueError("invalid_reference")
        for item in ref.values():
            _string(item)


def currentness(observation, as_of):
    """按 as_of 时刻判断公告的时间状态。

    只看截止时间是否已过，不代表还能不能参与。原文没写时区的截止时间不做判断，记为 unknown。
    更正、结果、终止、意向这几类公告不是报名公告，直接标出来。
    """
    kind = observation["notice_type"]
    if kind in ("award", "termination", "intention", "correction"):
        return {"status": "not_opening_notice", "reason": kind, "as_of": as_of}
    fact = observation["facts"]["response_deadline"]
    if fact["status"] == "known":
        date = fact["value"]
        if date["precision"] != "date" and date["timezone"]:
            end = _time(date["local"] + date["timezone"])
            return {
                "status": "deadline_passed" if end <= _time(as_of) else "deadline_not_reached",
                "reason": "explicit_deadline_only_not_eligibility",
                "as_of": as_of,
            }
    return {"status": "unknown", "reason": "deadline_or_timezone_unconfirmed", "as_of": as_of}


def search_text(observation):
    """搜索用的文本：标题、项目编号、采购人、技术要求原文。存成一列，用 ILIKE 做关键词匹配。

    只做字符串匹配，不做任何判断。数据量上来以后再考虑 pg_trgm 或中文分词。
    """
    texts = [observation["title"]]
    for name in ("project_number", "buyer"):
        fact = observation["facts"][name]
        if fact["status"] == "known":
            texts.append(fact["value"])
    texts.extend(entry["text"] for entry in observation.get("evidence_fields", {}).get("technical_evidence", []))
    return "\n".join(texts)


def validate_bundle(bundle):
    """检查一批规范化观察的格式。任何一条不对，整批拒绝：目录不会导入半个包。"""
    if (
        not isinstance(bundle, dict)
        or type(bundle.get("schema_version")) is not int
        or bundle["schema_version"] not in SCHEMAS
        or bundle.get("kind") != "normalized_observations"
        or not isinstance(bundle.get("normalizer_version"), str)
        or bundle["normalizer_version"] not in SCHEMAS[bundle["schema_version"]][0]
        or not isinstance(bundle.get("source_id"), str)
        or bundle.get("source_id") not in SCHEMAS[bundle["schema_version"]][1]
        or type(bundle.get("simulation")) is not bool
        or bundle.get("coverage") != "bounded_sample"
        or bundle.get("run_status") not in ("succeeded", "partial", "failed", "blocked", "cancelled")
    ):
        raise AppError("unsupported_normalized_bundle")
    try:
        _string(bundle.get("run_id"), 128)
        observations, failures = bundle.get("observations"), bundle.get("failures")
        if (
            not isinstance(observations, list)
            or len(observations) > 100
            or not isinstance(failures, list)
            or len(failures) > 100
        ):
            raise ValueError("invalid_bundle_items")
        if len(_json(bundle).encode()) > MAX_BUNDLE_BYTES:
            raise ValueError("bundle_too_large")
        for observation in observations:
            _validate(observation, bundle)
    except ValueError as exc:
        raise AppError(str(exc)) from None
    return _hash(bundle)


def relationships(current, candidates):
    """找出一条更正或结果公告对应的原采购公告。

    判断依据，按可靠程度从高到低：
        1. 原文里有指向原公告的链接，并且目录里能找到 → evidenced（确认）
        2. 同一发布方、同一采购人、同一项目编号 → candidate（候选，需要人工确认）
        3. 都找不到 → unresolved
    包号对不上时标为 conflicting；匹配到多个时标为 ambiguous。
    只建立关系，不合并两条公告，也不改写原公告的内容。
    """
    if current["notice_type"] not in ("correction", "award"):
        return []
    contract_reference = current["facts"].get("original_contract_reference", {}).get("evidence", [])
    if contract_reference:
        # 引用的是"原合同"而不是"原采购公告"，不能当成采购更正来关联，哪怕项目编号相同。
        return [
            {
                "status": "unresolved",
                "basis": "original_contract_outside_scope",
                "source_observation_id": current["observation_id"],
                "evidence": contract_reference,
            }
        ]

    def value(item, name):
        fact = item["facts"][name]
        return fact["value"] if fact["status"] == "known" else None

    def different_lot(other):
        return (
            value(current, "lot_identifier")
            and value(other, "lot_identifier")
            and value(current, "lot_identifier") != value(other, "lot_identifier")
        )

    family = SOURCE_FAMILIES.get(current["source_id"])
    target_kinds = ("procurement", "correction") if current["notice_type"] == "award" else ("procurement",)
    pool = [
        c
        for c in candidates
        if family is not None
        and SOURCE_FAMILIES.get(c["source_id"]) == family
        and c["simulation"] == current["simulation"]
        and c["notice_type"] in target_kinds
    ]
    explicit = [
        (other, ref)
        for other in pool
        for ref in current["references"]
        if other["identity_kind"] == "source_url" and other["source_record_key"] == ref["url"]
    ]
    if explicit:
        conflicts = [(other, ref) for other, ref in explicit if different_lot(other)]
        if conflicts:
            # 链接对上了但包号不一致：标为冲突，两边的原文都保留，不建立关联。
            return [
                {
                    "status": "conflicting",
                    "basis": "original_link_lot_mismatch",
                    "source_observation_id": current["observation_id"],
                    "target_observation_id": other["observation_id"],
                    "evidence": [ref]
                    + current["facts"]["lot_identifier"]["evidence"]
                    + other["facts"]["lot_identifier"]["evidence"],
                }
                for other, ref in conflicts
            ]
        matches = {other["notice_id"]: (other, ref) for other, ref in explicit}
        return [
            {
                "target_notice_id": other["notice_id"],
                "source_observation_id": current["observation_id"],
                "target_observation_id": other["observation_id"],
                "status": "evidenced" if len(matches) == 1 else "ambiguous",
                "basis": "explicit_original_link",
                "evidence": [ref],
            }
            for other, ref in matches.values()
        ]
    if current["references"]:
        return [{"status": "unresolved", "basis": "original_link_not_in_catalog", "evidence": current["references"]}]
    buyer, number = value(current, "buyer"), value(current, "project_number")
    if not buyer or not number:
        return [{"status": "unresolved", "basis": "missing_buyer_or_project_number", "evidence": []}]
    possible = [
        other
        for other in pool
        if value(other, "buyer") == buyer and value(other, "project_number") == number and not different_lot(other)
    ]
    if not possible:
        return [{"status": "unresolved", "basis": "no_matching_original", "evidence": []}]
    return [
        {
            "target_notice_id": other["notice_id"],
            "source_observation_id": current["observation_id"],
            "target_observation_id": other["observation_id"],
            "status": "candidate" if len(possible) == 1 else "ambiguous",
            "target_notice_type": other["notice_type"],
            "source_notice_type": current["notice_type"],
            "basis": (
                "same_source_buyer_project_number"
                if other["source_id"] == current["source_id"]
                else "same_publisher_buyer_project_number"
            ),
            "evidence": current["facts"]["project_number"]["evidence"] + current["facts"]["buyer"]["evidence"],
        }
        for other in possible
    ]

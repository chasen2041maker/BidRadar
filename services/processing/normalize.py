"""规范化：把采集导出的证据包转换成目录能导入的"观察"（observation）。

流程：检查证据包格式 → 按字段标签（"项目编号""预算金额""投标截止时间"等）从原文取值
→ 取不到记为 missing、取到多个不同的值记为 conflicting → 计算观察 ID 和公告 ID。

每个字段都保留原文证据和位置。金额和日期只在有明确依据时才转换成结构化的值，
范围、约数、没写时区的时刻都保留原文、不强行解析。
这里不判断项目适不适合谁，也不判断还能不能投。纯函数，不联网、不调模型。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit, urlunsplit
from .evidence_fields import body_entries, extract as extract_evidence

VERSION = "procurement-facts-v3"
MAX_JSON_BYTES = 32 * 1024 * 1024
SOURCES = {
    "cn_ccgp",
    "cn_hainan",
    "tj_procurement_negotiation",
    "tj_procurement_consultation",
    "tj_procurement_correction",
}
KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}
LABELS = {
    "project_number": ("项目编号", "采购项目编号", "原公告的采购项目编号"),
    "buyer": ("采购人名称", "采购单位", "采购人"),
    "published_at": ("公告发布时间", "公告时间", "发布日期"),
    "budget": ("预算（万元）", "预算(万元)", "预算金额", "项目预算", "采购预算", "预算金额（元）", "预算金额（万元）"),
    "response_deadline": ("响应文件提交的截止时间", "响应文件提交截止时间", "投标截止时间", "提交投标文件截止时间"),
    "lot_identifier": ("包号", "标段编号"),
    # 更正公告里提到的"首次公告日期"是原公告的发布日期，不能当成这条更正的发布日期。
    "original_published_at": ("首次公告日期",),
    "original_contract_reference": ("原合同公告链接",),
}
DATE_FIELDS = {"published_at", "response_deadline", "original_published_at"}
MATERIAL_LABELS = {"其他附件文件下载链接", "更正附件链接"}


def canonical(value):
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    """对象的 SHA-256。"""
    return sha256(canonical(value).encode()).hexdigest()


def require_string(value, name, limit=4096, *, empty=False):
    """检查是不超长的字符串，否则抛 ValueError(name)。"""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_" + name)
    return value


def timestamp(value):
    """检查是带时区的 ISO 8601 时间。"""
    require_string(value, "timestamp", 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise ValueError("timestamp_requires_offset") from None


def public_url(value):
    """检查是合法的 http/https 地址。只做检查，不发请求。"""
    require_string(value, "url")
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise ValueError()
        if any(ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError()
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    except ValueError:
        raise ValueError("invalid_url") from None


def _entries(content):
    """把一个文档的元数据和正文段落整理成 {label, text, locator} 列表。"""
    entries = []
    for field in ("metadata", "segments"):
        values = content.get(field, [])
        if not isinstance(values, (list, tuple)) or len(values) > 10000:
            raise ValueError("invalid_evidence_entries")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("invalid_evidence_entry")
            text = require_string(item.get("text"), "evidence_text", 200000, empty=True).strip()
            locator = require_string(item.get("locator"), "locator", 512)
            if not text:
                continue
            if field == "metadata":
                label = require_string(item.get("label"), "label", 100).strip().rstrip("：:")
                entries.append({"label": label, "text": text, "locator": locator})
    entries.extend(body_entries(content.get("segments", []), LABELS, _date))
    return entries


def _date(text):
    """解析日期文本，返回 {local, precision, timezone, timezone_basis}，解析不了返回 None。

    只有日期就记日期精度，不补成零点；原文没写"北京时间"就不加时区。
    """
    match = re.fullmatch(
        r"\s*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?"
        r"(?:[ T\s]*(\d{1,2})[:时点](\d{1,2})(?:[:分](\d{1,2})秒?)?分?)?"
        r"\s*(?:[（(]?(北京时间)[）)]?)?\s*",
        text,
    )
    if not match:
        return None
    year, month, day, hour, minute, second, beijing = match.groups()
    try:
        date = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    precision = "second" if second else ("minute" if hour else "date")
    value = date.strftime("%Y-%m-%d") if hour is None else date.isoformat(timespec=precision + "s")
    return {
        "local": value,
        "precision": precision,
        "timezone": "+08:00" if beijing else None,
        "timezone_basis": "原文明确北京时间" if beijing else None,
    }


def _money(text, label):
    """解析金额，返回 {amount（单位元）, currency, source_unit, scope}，不是明确的单一金额就返回 None。"""
    # 区间、约数、最高限价、外币、一段里有多个金额，都不解析，保留原文让人看。
    match = re.fullmatch(
        r"\s*(?:人民币\s*)?[￥¥]?\s*((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)\s*(万元|元)?(?:\s*[（(]人民币[）)])?\s*",
        text,
    )
    if not match:
        return None
    number, unit = match.groups()
    unit = unit or ("万元" if "万元" in label else "元" if "元" in label else None)
    if unit is None:
        return None
    try:
        amount = Decimal(number.replace(",", "")) * (10000 if unit == "万元" else 1)
        if amount > Decimal("1000000000000000") or amount.as_tuple().exponent < -6:
            return None
        return {"amount": format(amount, "f"), "currency": "CNY", "source_unit": unit, "scope": "unspecified"}
    except InvalidOperation:
        return None


def _fact(entries, name):
    """从条目里取某个字段：返回 {status, value, evidence}，状态是 known / missing / unparsed / conflicting。"""
    evidence = [entry for entry in entries if entry["label"] in LABELS[name]]
    if not evidence:
        return {"status": "missing", "value": None, "evidence": []}
    values = []
    for entry in evidence:
        value = (
            _date(entry["text"])
            if name in DATE_FIELDS
            else (_money(entry["text"], entry["label"]) if name == "budget" else entry["text"])
        )
        if value is not None and value not in values:
            values.append(value)
    if len(values) > 1:
        return {"status": "conflicting", "value": None, "evidence": evidence}
    # 一条能解析、另一条解析不了，也不算确定：不能只挑对自己有利的那条。
    if not values or any(
        (_date(e["text"]) if name in DATE_FIELDS else _money(e["text"], e["label"]) if name == "budget" else e["text"])
        is None
        for e in evidence
    ):
        return {"status": "unparsed", "value": None, "evidence": evidence}
    return {"status": "known", "value": values[0], "evidence": evidence}


def _notice_kind(title, source=None):
    """根据标题（以及来源）判断公告类型：采购、更正、结果、终止、意向或未知。"""
    kind = "unknown"
    for pattern, kind in (
        (r"(更正|变更|澄清)公告", "correction"),
        (r"(废标|终止|中止)公告", "termination"),
        (r"(中标|成交)(结果)?公告", "award"),
        (r"采购意向", "intention"),
        (r"(?:(?:竞争性谈判|竞争性磋商|询价)(?:公告)?|(?:招标|采购)公告)", "procurement"),
    ):
        if re.search(pattern + r"(?:[（(].*[）)])?$", title):
            break
    else:
        kind = "unknown"
    # 天津的资源本身就说明了类型；如果和标题明显矛盾，就记为 unknown，不去猜。
    if source == "tj_procurement_correction":
        return "correction" if kind in ("unknown", "correction") else "unknown"
    if kind == "unknown" and source in ("tj_procurement_negotiation", "tj_procurement_consultation"):
        return "procurement"
    return kind


def normalize_bundle(bundle):
    """把一个证据包整体规范化。任何一个文档格式不对，整个包都拒绝，目录不会导入半个包。"""
    if (
        not isinstance(bundle, dict)
        or type(bundle.get("schema_version")) is not int
        or bundle["schema_version"] != 1
        or bundle.get("kind") != "raw_evidence_bundle"
        or not isinstance(bundle.get("source_id"), str)
        or bundle.get("source_id") not in SOURCES
        or type(bundle.get("simulation")) is not bool
    ):
        raise ValueError("invalid_bundle_header")
    if len(canonical(bundle).encode()) > MAX_JSON_BYTES:
        raise ValueError("bundle_too_large")
    run_id = require_string(bundle.get("run_id"), "run_id", 128)
    if bundle.get("run_status") not in ("succeeded", "partial", "failed", "blocked", "cancelled"):
        raise ValueError("run_not_terminal")
    docs, failures = bundle.get("documents"), bundle.get("failures")
    if not isinstance(docs, list) or len(docs) > 100 or not isinstance(failures, list) or len(failures) > 100:
        raise ValueError("invalid_bundle_items")
    observations = []
    for doc in docs:
        if not isinstance(doc, dict):
            raise ValueError("invalid_document")
        capture = require_string(doc.get("capture_id"), "capture_id", 128)
        digest = doc.get("raw_sha256")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("invalid_raw_hash")
        parser = require_string(doc.get("parser_version"), "parser_version", 128)
        observed = timestamp(doc.get("observed_at")).astimezone(timezone.utc).isoformat()
        url = public_url(doc.get("source_url"))
        key = require_string(doc.get("source_record_key"), "record_key")
        identity = doc.get("identity_kind")
        if identity == "source_url":
            key = public_url(key)
        elif identity != "content_snapshot" or not re.fullmatch("snapshot:[0-9a-f]{64}", key):
            raise ValueError("invalid_identity")
        content = doc.get("content")
        if not isinstance(content, dict) or content.get("status") not in ("ok", "partial"):
            raise ValueError("unusable_document")
        title = content.get("title") or ""
        require_string(title, "title", 2000, empty=True)
        entries = _entries(content)
        facts = {name: _fact(entries, name) for name in LABELS}
        notice_type = _notice_kind(title, bundle["source_id"])
        extra = extract_evidence(content, entries, _date, _money, notice_type)
        assessment = extra["budget_assessment"]
        if assessment["status"] == "known" and facts["budget"]["status"] != "known":
            claim = next(claim for claim in extra["money"] if claim["role"] == "budget" and claim["status"] == "parsed")
            facts["budget"] = {
                "status": "known",
                "value": {
                    "amount": claim["amount"],
                    "currency": "CNY",
                    "source_unit": claim["source_unit"],
                    "scope": "unspecified",
                },
                "evidence": assessment["evidence"],
            }
        if assessment["status"] in ("conflicting", "context_required", "unparsed"):
            # 金额证据有冲突、需要结合上下文、或解析不了时，不输出项目预算。
            proof = assessment["evidence"] or [entry for claim in extra["money"] for entry in claim["evidence"]]
            if proof:
                facts["budget"] = {
                    "status": "conflicting" if assessment["status"] == "conflicting" else "unparsed",
                    "value": None,
                    "evidence": proof,
                }
        if notice_type == "correction":
            # 更正公告里常常同时出现旧的和新的日期、金额，分不清哪个是现在的值，所以只保留证据、不填值。
            # 项目编号和采购人仍然保留，用来和原公告建立关联。
            for name in ("published_at", "response_deadline", "budget"):
                if facts[name]["evidence"]:
                    facts[name].update(status="unparsed", value=None)
        links = content.get("links", [])
        if not isinstance(links, (list, tuple)) or len(links) > 1000:
            raise ValueError("invalid_links")
        references = []
        for link in links:
            if not isinstance(link, dict):
                raise ValueError("invalid_link")
            name = require_string(link.get("name", ""), "link_name", 2000, empty=True)
            locator = require_string(link.get("locator"), "locator", 512)
            if any(word in name for word in ("原公告", "首次公告", "原采购公告")):
                references.append({"url": public_url(link.get("url")), "text": name, "locator": locator})
        # 公告 ID 由来源、是否演示、来源里的记录键共同决定：演示数据和真实数据永远不会混成同一条公告。
        notice_id = fingerprint([bundle["source_id"], bundle["simulation"], key])
        observation = {
            "notice_id": notice_id,
            "source_id": bundle["source_id"],
            "simulation": bundle["simulation"],
            "source_record_key": key,
            "identity_kind": identity,
            "source_url": url,
            "capture_id": capture,
            "raw_sha256": digest,
            "parser_version": parser,
            "normalizer_version": VERSION,
            "observed_at": observed,
            "title": title,
            "notice_type": notice_type,
            "facts": facts,
            "references": references,
            "evidence_fields": extra,
            # 只记下附件引用的原文和位置，不访问链接，也不会把"有链接"写成"已下载"。
            "material_reference_evidence": [entry for entry in entries if entry["label"] in MATERIAL_LABELS],
            "material_status": content.get("material_status", "see_ingestion_evidence"),
            "attribution": content.get("attribution"),
            "run_id": run_id,
        }
        # 观察 ID 是整个观察内容的哈希：内容（包括解析结果）一变，ID 就变。目录导入时会重新核对。
        observation["observation_id"] = fingerprint(observation)
        observations.append(observation)
    return {
        "schema_version": 3,
        "kind": "normalized_observations",
        "normalizer_version": VERSION,
        "run_id": run_id,
        "run_status": bundle["run_status"],
        "source_id": bundle["source_id"],
        "simulation": bundle["simulation"],
        "coverage": "bounded_sample",
        "observations": observations,
        "failures": failures,
    }

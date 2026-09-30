"""将公开证据转换为目录观察；纯函数，不联网、不调用模型、不访问原件库。

阅读重点：先验证包 → 按标签取证 → 保留未知/冲突 → 生成不可变观察身份。
金额和日期只做有依据的规范化，不把采购需求、资格或截止状态变成投标结论。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from urllib.parse import urlsplit, urlunsplit

VERSION = "procurement-facts-v2"
MAX_JSON_BYTES = 32 * 1024 * 1024
SOURCES = {"cn_ccgp", "tj_procurement_negotiation", "tj_procurement_consultation", "tj_procurement_correction"}
KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}
LABELS = {
    "project_number": ("项目编号", "采购项目编号"),
    "buyer": ("采购人名称", "采购单位", "采购人"),
    "published_at": ("公告发布时间", "公告时间", "发布日期"),
    "budget": ("预算（万元）", "预算(万元)", "预算金额", "项目预算", "采购预算"),
    "response_deadline": ("响应文件提交的截止时间", "响应文件提交截止时间", "投标截止时间", "提交投标文件截止时间"),
    "lot_identifier": ("包号", "标段编号"),
    # 首次公告属于被引用的原公告；不能写进本次更正的published_at。
    "original_published_at": ("首次公告日期",),
    "original_contract_reference": ("原合同公告链接",),
}
DATE_FIELDS = {"published_at", "response_deadline", "original_published_at"}
MATERIAL_LABELS = {"其他附件文件下载链接", "更正附件链接"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return sha256(canonical(value).encode()).hexdigest()


def require_string(value, name, limit=4096, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_" + name)
    return value


def timestamp(value):
    require_string(value, "timestamp", 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed
    except ValueError:
        raise ValueError("timestamp_requires_offset") from None


def public_url(value):
    """这里只验证证据定位，不发起请求；正文中的URL永远不能指挥传输层。"""
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
            else:
                # 只认段首明确标签；长正文里的“原截止/更正后截止”不混成同一事实。
                for label in (label for labels in LABELS.values() for label in labels):
                    match = re.fullmatch(r"\s*" + re.escape(label) + r"\s*[:：]\s*(.+)", text, re.S)
                    if match:
                        entries.append({"label": label, "text": match.group(1).strip(), "locator": locator})
                        break
    return entries


def _date(text):
    """自然日期不补零点；无时区的时刻不暗中升级成北京时间/UTC。"""
    match = re.fullmatch(r"\s*(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?"
                         r"(?:[ T\s]+(\d{1,2})[:时](\d{1,2})(?:[:分](\d{1,2})秒?)?分?)?"
                         r"\s*(?:[（(]?(北京时间)[）)]?)?\s*", text)
    if not match:
        return None
    year, month, day, hour, minute, second, beijing = match.groups()
    try:
        date = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0))
    except ValueError:
        return None
    precision = "second" if second else ("minute" if hour else "date")
    value = date.strftime("%Y-%m-%d") if hour is None else date.isoformat(timespec=precision + "s")
    return {"local": value, "precision": precision, "timezone": "+08:00" if beijing else None,
            "timezone_basis": "原文明确北京时间" if beijing else None}


def _money(text, label):
    # 不接受区间、约数、最高限价、多币种或一段多金额；保持原文供人工核对。
    match = re.fullmatch(r"\s*(?:人民币\s*)?((?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?)\s*(万元|元)?(?:\s*（人民币）)?\s*", text)
    if not match:
        return None
    number, unit = match.groups()
    unit = unit or ("万元" if "万元" in label else None)
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
    evidence = [entry for entry in entries if entry["label"] in LABELS[name]]
    if not evidence:
        return {"status": "missing", "value": None, "evidence": []}
    values = []
    for entry in evidence:
        value = _date(entry["text"]) if name in DATE_FIELDS else (
            _money(entry["text"], entry["label"]) if name == "budget" else entry["text"])
        if value is not None and value not in values:
            values.append(value)
    if len(values) > 1:
        return {"status": "conflicting", "value": None, "evidence": evidence}
    # 一条可解析、另一条不可解析仍不是已确认值；不能抛掉不利证据。
    if not values or any((_date(e["text"]) if name in DATE_FIELDS else
                         _money(e["text"], e["label"]) if name == "budget" else e["text"]) is None for e in evidence):
        return {"status": "unparsed", "value": None, "evidence": evidence}
    return {"status": "known", "value": values[0], "evidence": evidence}


def _notice_kind(title, source=None):
    kind = "unknown"
    for pattern, kind in ((r"(更正|变更|澄清)公告", "correction"), (r"(废标|终止|中止)公告", "termination"),
                          (r"(中标|成交)(结果)?公告", "award"), (r"采购意向", "intention"),
                          (r"(招标|竞争性谈判|竞争性磋商|询价|采购)公告", "procurement")):
        if re.search(pattern + r"(?:[（(].*[）)])?$", title):
            break
    else:
        kind = "unknown"
    # 固定资源本身有分类依据；来源与明确标题相互矛盾时不猜哪个正确。
    if source == "tj_procurement_correction":
        return "correction" if kind in ("unknown", "correction") else "unknown"
    if kind == "unknown" and source in ("tj_procurement_negotiation", "tj_procurement_consultation"):
        return "procurement"
    return kind


def normalize_bundle(bundle):
    """每次输出完整规范包；一个文档类型错误导致整包拒绝，目录写入不会半成功。"""
    if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int or bundle["schema_version"] != 1
            or bundle.get("kind") != "raw_evidence_bundle" or not isinstance(bundle.get("source_id"), str)
            or bundle.get("source_id") not in SOURCES
            or type(bundle.get("simulation")) is not bool):
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
        if notice_type == "correction":
            # 更正正文可能同时引用旧/新日期金额；未有明确作用域契约前只保留证据。
            # 项目号/采购方仍可支持候选关联，但不让旧截止或更正前预算进入当前值。
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
        # 来源、模拟标记都进入身份，演示不能与真实公告共享当前指针。
        notice_id = fingerprint([bundle["source_id"], bundle["simulation"], key])
        observation = {"notice_id": notice_id, "source_id": bundle["source_id"], "simulation": bundle["simulation"],
            "source_record_key": key, "identity_kind": identity, "source_url": url,
            "capture_id": capture, "raw_sha256": digest, "parser_version": parser,
            "normalizer_version": VERSION, "observed_at": observed, "title": title,
            "notice_type": notice_type, "facts": facts, "references": references,
            # 只保留引用字段原文和位置；没有请求外部地址，也没有生成“下载成功”。
            "material_reference_evidence": [entry for entry in entries if entry["label"] in MATERIAL_LABELS],
            "material_status": content.get("material_status", "see_ingestion_evidence"),
            "attribution": content.get("attribution"), "run_id": run_id}
        # 观察绑定同一获取+解析内容；新解析不能冒用旧ID。目录会再次校验哈希。
        observation["observation_id"] = fingerprint(observation)
        observations.append(observation)
    return {"schema_version": 2, "kind": "normalized_observations", "normalizer_version": VERSION,
            "run_id": run_id, "run_status": bundle["run_status"], "source_id": bundle["source_id"],
            "simulation": bundle["simulation"], "coverage": "bounded_sample",
            "observations": observations, "failures": failures}

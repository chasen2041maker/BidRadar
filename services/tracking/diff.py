"""确定性版本比较：程序决定变化范围，模型只能解释已冻结的差异。

观察ID还包含获取时间/运行编号，不能直接作为业务变化；当前规范数据没有完整
正文指纹，所以未命中字段的原文变化必须保守保留，不能宣布只是排版。
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

RULE_VERSION = "tracking-diff-v1"
SCOPES = frozenset(("deadline", "money", "qualification", "delivery", "materials", "lifecycle", "profile", "other"))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def _semantic(value):
    """只去定位/获取元数据；保留否定、状态、包号、计价口径、时区和时间精度。"""
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in sorted(value.items())
                if key not in ("locator", "capture_id", "source_observation_id", "target_observation_id")}
    if isinstance(value, list):
        return sorted((_semantic(item) for item in value), key=canonical)
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def _projection(observation):
    facts, extra = observation.get("facts", {}), observation.get("evidence_fields", {})
    return {
        "deadline": _semantic({key: facts.get(key) for key in ("published_at", "response_deadline", "original_published_at")}
                               | {"acquisition_window": extra.get("acquisition_window")}),
        "money": _semantic({"budget": facts.get("budget"), "claims": extra.get("money"), "assessment": extra.get("budget_assessment")}),
        "qualification": _semantic(extra.get("qualification_evidence")),
        "delivery": _semantic(extra.get("delivery_evidence")),
        "materials": _semantic({"availability": extra.get("material_availability"), "access": extra.get("access_conditions"),
                                 "status": observation.get("material_status"), "references": observation.get("material_reference_evidence")}),
        "lifecycle": _semantic({"notice_type": observation.get("notice_type"), "references": observation.get("references")}),
        "other": _semantic({"title": observation.get("title"), "identity": {key: facts.get(key) for key in ("project_number", "buyer", "lot_identifier")},
                             "technical": extra.get("technical_evidence"), "category": extra.get("category_evidence")}),
    }


def input_fingerprint(notice_id, profile_revision, snapshot, observation_ids):
    return fingerprint({"notice_id": notice_id, "profile_revision": profile_revision,
                        "catalog_snapshot": snapshot, "observation_ids": observation_ids})


def compare_bundles(before, after):
    """输入由catalog固定snapshot提供；incoming只有evidenced项进入observations。"""
    old = {item["notice_id"]: item for item in before["observations"]}
    new = {item["notice_id"]: item for item in after["observations"]}
    entries, unclassified = [], []
    for notice_id in sorted(old.keys() | new.keys()):
        left, right = old.get(notice_id), new.get(notice_id)
        if left is None or right is None:
            entries.append({"scope": "lifecycle", "notice_id": notice_id, "path": "observations",
                            "before": left, "after": right})
            continue
        first, second = _projection(left), _projection(right)
        for name in sorted(first):
            if first[name] != second[name]:
                # 原始片段与定位继续保存在固定观察内；差异项提供两端观察引用便于回核。
                entries.append({"scope": name, "notice_id": notice_id, "path": name,
                                "before": first[name], "after": second[name],
                                "before_observation_id": left["observation_id"], "after_observation_id": right["observation_id"]})
        if first == second and left.get("raw_sha256") != right.get("raw_sha256"):
            unclassified.append({"notice_id": notice_id, "before_observation_id": left["observation_id"],
                                 "after_observation_id": right["observation_id"]})
    relations_changed = _semantic(before.get("relationships", [])) != _semantic(after.get("relationships", []))
    if relations_changed:
        entries.append({"scope": "lifecycle", "notice_id": after["notice_id"], "path": "relationships",
                        "before": before.get("relationships", []), "after": after.get("relationships", [])})
    kind = "material_change" if entries else "unclassified_content_change" if unclassified else "no_business_change"
    return {"schema_version": 1, "rule_version": RULE_VERSION, "kind": kind, "entries": entries,
            "unclassified": unclassified, "scopes": sorted({entry["scope"] for entry in entries} | ({"other"} if unclassified else set())),
            "before_observation_ids": [item["observation_id"] for item in before["observations"]],
            "after_observation_ids": [item["observation_id"] for item in after["observations"]]}

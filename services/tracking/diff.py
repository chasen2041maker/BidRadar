"""比较同一公告的两个版本，找出业务上有意义的变化。

变化范围完全由程序决定，不交给模型判断。做法是把每个版本投影成几个业务维度
（截止时间、金额、资格、交付、材料、公告状态、其他），逐个维度比较。

两个容易误判的地方：
    - 观察 ID 里包含抓取时间和批次，ID 变了不代表内容变了，所以不直接比 ID。
    - 现在的规范化数据没有完整正文的指纹。如果原文的哈希变了，但各个维度都没变，
      就记为 unclassified_content_change（原文有变化但没归到具体维度），
      而不是断言"只是排版变了"。
"""
from __future__ import annotations

from hashlib import sha256
import json
import re

# 比较规则的版本。规则一变，同样的变化会得到新的指纹，避免和旧规则的结果混在一起。
RULE_VERSION = "tracking-diff-v1"
# 变化可能落在的业务维度。用户关注一个公告时，可以只选其中几个维度。
SCOPES = frozenset(("deadline", "money", "qualification", "delivery", "materials", "lifecycle", "profile", "other"))


def canonical(value):
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    """对象的 SHA-256 指纹，用来给变化、命令去重。"""
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def _semantic(value):
    """去掉不影响业务的部分，得到可以直接比较的值。

    去掉的：定位信息、抓取批次、观察 ID；字符串里多余的空白；列表的顺序。
    保留的：否定词、状态、包号、计价口径、时区和时间精度等一切有业务含义的内容。
    """
    if isinstance(value, dict):
        return {key: _semantic(item) for key, item in sorted(value.items())
                if key not in ("locator", "capture_id", "source_observation_id", "target_observation_id")}
    if isinstance(value, list):
        return sorted((_semantic(item) for item in value), key=canonical)
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def _projection(observation):
    """把一条公告观察投影成各个业务维度的可比较值。"""
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
    """一次复核的输入指纹：哪个公告、哪版档案、目录快照、哪几条观察。委托里会记下它。"""
    return fingerprint({"notice_id": notice_id, "profile_revision": profile_revision,
                        "catalog_snapshot": snapshot, "observation_ids": observation_ids})


def compare_bundles(before, after):
    """比较同一公告在两个目录快照下的版本，返回差异。

    before / after 是 catalog 提供的 bundle（主公告加上有依据关联的更正、结果公告）。
    返回值里的 kind：
        material_change              至少有一个业务维度变了
        unclassified_content_change  原文变了，但没有落到任何维度上
        no_business_change           没有变化
    """
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
                # 差异项记下前后两个观察的 ID，需要时可以回到原文核对。
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

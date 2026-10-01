"""同一冻结输入上的规则/Agent对照；机器可验证引用范围，语义效果仍需人工标签。

本模块不运行真实模型、不造人工真值。没有标签时指标保持None，不以自评分冒充质量。
"""
from __future__ import annotations

from .evidence import EvidenceIndex, EvidenceError, STATUSES, baseline_report, validate_report


def run_baseline(manifest):
    return {"report": baseline_report(manifest), "trace": [], "state": "partial",
            "quality": {"verified": False, "verification": "rule_baseline", "model_calls": 0,
                        "human_reviewed": False}}


def evaluate_report(manifest, report, *, labels=None):
    """labels={provenance,requirements:[{evidence_id,status}]}，标签来源由评测主持人记录。

    status_match仅是固定标签对照，citation_reference_valid只代表引用身份/原片段正确；
    两者均不自动证明中文语义被支持。真实/合成样本标记来自冻结观察。
    """
    index = EvidenceIndex(manifest)
    if not isinstance(report, dict):
        raise EvidenceError("invalid_report")
    proposal = {key: report.get(key) for key in ("summary", "findings", "questions", "answer")}
    references = report.get("references", [])
    reference_ids, reference_valid = set(), True
    if not isinstance(references, list) or len(references) > 1000:
        reference_valid = False
        references = []
    for reference in references:
        if (not isinstance(reference, dict) or not isinstance(reference.get("evidence_id"), str)
                or reference.get("evidence_id") not in index.by_id
                or reference != index.by_id[reference["evidence_id"]]):
            reference_valid = False
        else:
            reference_ids.add(reference["evidence_id"])
    errors = validate_report(proposal, index, reference_ids)
    if report.get("scope") != index.scope:
        errors.append("evaluation_scope_mismatch")
    findings = report.get("findings", []) if isinstance(report.get("findings"), list) else []
    cited = {e for finding in findings if isinstance(finding, dict) and isinstance(finding.get("evidence_ids"), list)
             for e in finding["evidence_ids"] if isinstance(e, str)}
    result = {"manifest_digest": index.manifest_digest,
              "sample_type": "simulation" if all(x.get("simulation") is True for x in manifest["observations"]) else "public_frozen",
              "local_errors": errors, "citation_reference_valid": reference_valid and not (cited - reference_ids),
              "semantic_support_rate": None, "labels_provenance": None, "condition_count": None,
              "condition_covered_count": None, "status_match_count": None, "unknown_as_known_count": None,
              "missing_evidence_ids": None}
    if labels is None:
        return result
    if (not isinstance(labels, dict) or set(labels) != {"provenance", "requirements"}
            or not isinstance(labels["provenance"], str) or not 1 <= len(labels["provenance"]) <= 200
            or not isinstance(labels["requirements"], list) or not 1 <= len(labels["requirements"]) <= 100):
        raise EvidenceError("invalid_evaluation_labels")
    expected = {}
    for label in labels["requirements"]:
        if (not isinstance(label, dict) or set(label) != {"evidence_id", "status"}
                or not isinstance(label["evidence_id"], str) or label["evidence_id"] not in index.by_id
                or label["evidence_id"] in expected or not isinstance(label["status"], str) or label["status"] not in STATUSES):
            raise EvidenceError("invalid_evaluation_labels")
        expected[label["evidence_id"]] = label["status"]
    matched, bad_unknown = 0, 0
    for eid, expected_status in expected.items():
        actual = [x.get("status") for x in findings if isinstance(x, dict)
                  and isinstance(x.get("evidence_ids"), list) and eid in x["evidence_ids"]]
        if actual and all(value == expected_status for value in actual):
            matched += 1
        if expected_status == "unknown" and any(value in ("met", "unmet") for value in actual):
            bad_unknown += 1
    return {**result, "labels_provenance": labels["provenance"], "condition_count": len(expected),
            "condition_covered_count": len(set(expected) & cited), "status_match_count": matched,
            "unknown_as_known_count": bad_unknown, "missing_evidence_ids": sorted(set(expected) - cited)}


def compare_reports(manifest, baseline, agent, *, labels=None):
    """保证两份报告使用同一manifest；耗时/费用由真实调用账本另附，不在这里猜测。"""
    index = EvidenceIndex(manifest)
    if any(report.get("scope") != index.scope for report in (baseline, agent) if isinstance(report, dict)):
        raise EvidenceError("evaluation_scope_mismatch")
    return {"baseline": evaluate_report(manifest, baseline, labels=labels),
            "agent": evaluate_report(manifest, agent, labels=labels),
            "cost_and_latency": "record_from_actual_run_ledger"}

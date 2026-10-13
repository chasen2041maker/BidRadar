"""评测：在同一份冻结输入上，对比规则基线和 Agent 的报告。

程序能自动检查的只有"引用对不对得上"。结论对不对，需要人工标注的标准答案（labels）。
没有标签时，所有质量指标都返回 None，不拿模型给自己打的分冒充质量。
这个模块不调真实模型，也不会自己生成标准答案。
"""
from __future__ import annotations

from .evidence import EvidenceIndex, EvidenceError, STATUSES, baseline_report, validate_report


def run_baseline(manifest):
    """跑规则基线，返回和 Agent 相同格式的结果。"""
    return {"report": baseline_report(manifest), "trace": [], "state": "partial",
            "quality": {"verified": False, "verification": "rule_baseline", "model_calls": 0,
                        "human_reviewed": False}}


def evaluate_report(manifest, report, *, labels=None):
    """评估一份报告。

    labels 是人工标注的标准答案：
        {"provenance": "谁标注的、怎么标的", "requirements": [{"evidence_id": ..., "status": ...}]}
    返回的主要指标：
        citation_reference_valid  报告引用的片段是否都真实存在且未被改动
        condition_covered_count   标准答案里的要求，报告覆盖了几条
        status_match_count        状态和标准答案一致的有几条
        unknown_as_known_count    应该是"未知"却被判成满足或不满足的条数（最严重的错误）
    这些都是和固定标签的机械对照，不等于中文语义层面的正确。
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
    """在同一份输入上评估基线和 Agent 两份报告。耗时和费用以真实运行的账本为准，这里不估算。"""
    index = EvidenceIndex(manifest)
    if any(report.get("scope") != index.scope for report in (baseline, agent) if isinstance(report, dict)):
        raise EvidenceError("evaluation_scope_mismatch")
    return {"baseline": evaluate_report(manifest, baseline, labels=labels),
            "agent": evaluate_report(manifest, agent, labels=labels),
            "cost_and_latency": "record_from_actual_run_ledger"}

"""② 抽取：分批、跳过模板、原文子串检查、打回重问、跨批合并、漏检复查、规则基线。"""

import json

from bidradar.agent.llm import FakeLLM, LLMRequest
from bidradar.agent.schemas import Dimension, Kind, MoneyRole
from bidradar.agent.steps import extract
from bidradar.agent.steps.parse import prepare
from tests.agent.support import extract_responder, tender_chunks


def material():
    return prepare(list(tender_chunks()))


def test_batches_follow_sections_and_skip_templates() -> None:
    planned, skipped = extract.batches(material())
    assert skipped == [("第六章 投标文件格式",)]
    sections = {b.section for b in planned}
    assert ("第三章 采购需求", "二、交付要求") in sections
    assert all(c.kind != "heading" for b in planned for c in b.chunks)
    assert all(not any("投标文件格式" in p for p in b.section) for b in planned)


def test_batch_keys_are_stable() -> None:
    first, _ = extract.batches(material())
    again, _ = extract.batches(material())
    assert [b.key for b in first] == [b.key for b in again]


def test_long_sections_are_split() -> None:
    planned, _ = extract.batches(material(), max_chars=200)
    assert all(sum(len(c.text) for c in b.chunks) <= 200 or len(b.chunks) == 1 for b in planned)


def run_extract(responder):
    m = material()
    planned, _ = extract.batches(m)
    llm = FakeLLM(responder)
    found, issues = [], []
    for batch in planned:
        good, bad = extract.extract_batch(batch, m, llm, f"run:{batch.key}")
        found += good
        issues += bad
    return extract.merge(found), issues, llm


def test_extracts_obligations_and_facts_with_real_chunk_ids() -> None:
    requirements, issues, llm = run_extract(extract_responder)
    assert issues == []
    by_quote = {r.quote: r for r in requirements}
    schedule = by_quote["合同签订后60日内完成系统开发、部署和试运行"]
    assert (schedule.kind, schedule.dimension) == (Kind.OBLIGATION, Dimension.SCHEDULE)
    assert schedule.chunk_ids[0] in {c.id for c in tender_chunks()}  # 别名已经换回真实 ID
    assert by_quote["最高限价：48万元"].money_role is MoneyRole.MAX_PRICE
    assert by_quote["预算金额：48万元"].dimension is Dimension.FACT
    # 发给模型的是短别名和去掉联系方式的原文。
    sent = "".join(r.user for r in llm.calls)
    assert '"ref": "c' in sent and "0000-12345678" not in sent and "contact@example.invalid" not in sent


def test_paraphrased_quote_is_rejected_then_fixed_on_retry() -> None:
    def responder(request: LLMRequest):
        if request.purpose == "extract_retry":
            return extract_responder(request)
        result = extract_responder(request)
        for item in result["items"]:
            if item["dimension"] == "schedule":
                item["quote"] = "60天内完成开发"  # 模型改写了原文
        return result

    requirements, issues, llm = run_extract(responder)
    assert issues == []
    assert "合同签订后60日内完成系统开发、部署和试运行" in {r.quote for r in requirements}
    retry = next(r for r in llm.calls if r.purpose == "extract_retry")
    assert "quote_not_in_chunk" in retry.user


def test_still_wrong_after_retry_is_recorded_not_kept() -> None:
    def responder(request: LLMRequest):
        result = extract_responder(request)
        for item in result["items"]:
            if item["dimension"] == "schedule":
                item["quote"] = "60天内完成开发"
        return result

    requirements, issues, _ = run_extract(responder)
    assert "60天内完成开发" not in {r.quote for r in requirements}
    assert [i.code for i in issues] == ["quote_not_in_chunk"]


def test_unknown_refs_and_bad_enums_are_rejected() -> None:
    m = material()
    batch = extract.batches(m)[0][0]
    allowed = {m.alias(c.id): c for c in batch.chunks}
    item = extract._Item(kind="obligation", category="technical", dimension="capability", quote="x", refs=["c999"])
    assert extract._check_item(item, allowed, "model") == "unknown_ref"
    ref = next(iter(allowed))
    quote = allowed[ref].text[:8]
    item = extract._Item(kind="obligation", category="nonsense", dimension="capability", quote=quote, refs=[ref])
    assert extract._check_item(item, allowed, "model") == "invalid_enum"


def test_invalid_json_is_an_issue_not_a_crash() -> None:
    requirements, issues, _ = run_extract(lambda request: "这不是 JSON")
    assert requirements == [] and issues and issues[0].code == "invalid_model_output"


def test_same_requirement_from_two_batches_merges() -> None:
    m = material()
    chunk = next(c for c in m.chunks if "48万元" in c.text)
    first = extract._check_item(
        extract._Item(
            kind="fact", category="budget", dimension="fact", quote="预算金额：48万元", refs=[m.alias(chunk.id)]
        ),
        {m.alias(chunk.id): chunk},
        "model",
    )
    merged = extract.merge([first, first])
    assert len(merged) == 1


def test_recall_finds_uncited_requirement_like_chunks() -> None:
    requirements, _, _ = run_extract(extract_responder)
    m = material()
    planned, _ = extract.batches(m)
    candidates = extract.recall_candidates(m, requirements, planned)
    texts = [c.text for c in candidates]
    assert any("付款方式" in t for t in texts)  # 假模型没抽付款条款，复查要把它找出来
    assert not any("合同签订后60日内" in t for t in texts)  # 已经被引用的不再复查

    def recall_responder(request: LLMRequest):
        payload = json.loads(request.user)
        chunk = next(c for c in payload["chunks"] if "付款方式" in c["text"])
        return {
            "items": [
                {
                    "kind": "obligation",
                    "category": "commercial",
                    "dimension": "commercial",
                    "quote": "项目终验合格后支付60%",
                    "refs": [chunk["ref"]],
                }
            ]
        }

    recalled = extract.extract_recall(candidates, m, FakeLLM(recall_responder), "run:recall")
    assert [(r.quote, r.source) for r in recalled] == [("项目终验合格后支付60%", "recall")]


def test_rule_baseline_separates_schedule_from_onsite() -> None:
    planned, _ = extract.batches(material())
    found = {r.quote: r for r in extract.rule_extract(planned)}
    schedule = next(r for q, r in found.items() if "60日内完成系统开发" in q)
    onsite = next(r for q, r in found.items() if "驻场服务" in q)
    assert schedule.dimension is Dimension.SCHEDULE
    assert onsite.dimension is Dimension.ONSITE
    assert all(r.source == "rule" for r in found.values())
    assert any(r.money_role is MoneyRole.MAX_PRICE for r in found.values())

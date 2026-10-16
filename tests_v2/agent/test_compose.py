"""汇总：排序、事实行、待核查清单去重、模板摘要、模型摘要的数字检查。"""

from bidradar.agent.dimensions import Dimension
from bidradar.agent.llm import FakeLLM
from bidradar.agent.schemas import Category, ExtractResult, Finding, Kind, MoneyRole, Requirement, Status
from bidradar.agent.steps import compose
from bidradar.agent.steps.parse import prepare
from tests_v2.agent.support import tender_chunks


def requirement(rid: str, quote: str, dimension=Dimension.SCHEDULE, kind=Kind.OBLIGATION, money_role=None):
    chunk = next(c for c in tender_chunks() if quote in c.text)
    return Requirement(
        id=rid,
        kind=kind,
        category=Category.BUDGET if kind is Kind.FACT else Category.DELIVERY,
        dimension=dimension,
        quote=quote,
        chunk_ids=(chunk.id,),
        money_role=money_role,
        source="model",
    )


SCHEDULE = requirement("r1", "合同签订后60日内完成系统开发、部署和试运行")
ONSITE = requirement("r2", "中标人须安排不少于2名工程师驻场服务", Dimension.ONSITE)
CONSORTIUM = requirement("r3", "本项目不接受联合体投标", Dimension.QUALIFICATION)
BUDGET = requirement("r4", "预算金额：48万元", Dimension.FACT, Kind.FACT, MoneyRole.BUDGET)
GAP = "补充驻场和排期安排"


def unknown(r: Requirement, gap: str = GAP) -> Finding:
    return Finding(requirement_id=r.id, status=Status.UNKNOWN, reason="档案没有写", gap=gap)


def build(findings, requirements=(SCHEDULE, ONSITE, CONSORTIUM, BUDGET), **kwargs):
    return compose.compose(
        mode="agent",
        extracted=ExtractResult(requirements=tuple(requirements)),
        findings=findings,
        material=prepare(list(tender_chunks())),
        versions={"config_version": "x"},
        **kwargs,
    )


def test_findings_sorted_by_severity_and_facts_listed() -> None:
    met = Finding(
        requirement_id=CONSORTIUM.id, status=Status.MET, reason="档案未声明联合体", profile_fields=("qualifications",)
    )
    unmet = Finding(requirement_id=ONSITE.id, status=Status.UNMET, reason="不接受驻场", profile_fields=())
    report = build([met, unknown(SCHEDULE), unmet])
    assert [f.status for f in report.findings] == [Status.UNMET, Status.UNKNOWN, Status.MET]
    [fact] = report.facts
    assert (fact.label, fact.quote) == ("预算金额", BUDGET.quote)
    assert {c.id for c in report.chunks} == {cid for r in report.requirements for cid in r.chunk_ids}


def test_checklist_merges_same_gap_and_never_comes_from_the_model() -> None:
    report = build([unknown(SCHEDULE), unknown(ONSITE), unknown(CONSORTIUM, "确认是否组成联合体")])
    assert [(i.text, i.requirement_ids) for i in report.checklist] == [
        (GAP, ("r1", "r2")),
        ("确认是否组成联合体", ("r3",)),
    ]
    assert {i.origin for i in report.checklist} == {"gap"}


def test_template_summary_counts() -> None:
    text = compose.template_summary([SCHEDULE, ONSITE], [unknown(SCHEDULE), unknown(ONSITE)])
    assert text.startswith("共识别 2 条投标要求") and "待核查 2 条，满足 0 条" in text


def test_model_summary_rejected_when_numbers_have_no_source() -> None:
    findings = [unknown(SCHEDULE)]
    ok = FakeLLM(lambda r: {"summary": "排期要求60日内完成，档案没有对应信息，需要补充。"})
    assert compose.model_summary([SCHEDULE], findings, ok, "k") is not None
    invented = FakeLLM(lambda r: {"summary": "排期要求90日内完成。"})
    assert compose.model_summary([SCHEDULE], findings, invented, "k") is None
    promise = FakeLLM(lambda r: {"summary": "建议直接投标，一定能中标。"})
    assert compose.model_summary([SCHEDULE], findings, promise, "k") is None


def test_report_uses_template_when_no_model_summary() -> None:
    report = build([unknown(SCHEDULE)], requirements=(SCHEDULE,))
    assert report.summary.source == "template"
    assert build([unknown(SCHEDULE)], requirements=(SCHEDULE,), summary_text="摘要").summary.source == "model"

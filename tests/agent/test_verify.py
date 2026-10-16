"""④ 核验：程序检查挡住的典型错误、模型核验的三种结果、降级。"""

from bidradar.agent.llm import FakeLLM
from bidradar.agent.schemas import Category, Dimension, Finding, Kind, MoneyRole, Requirement, Status
from bidradar.agent.steps import verify
from tests.agent.support import profile, tender_chunks


def chunk_with(text: str):
    return next(c for c in tender_chunks() if text in c.text)


def requirement(quote: str, dimension: Dimension, role: MoneyRole | None = None) -> Requirement:
    chunk = chunk_with(quote)
    return Requirement(
        id="r1",
        kind=Kind.OBLIGATION,
        category=Category.DELIVERY,
        dimension=dimension,
        quote=quote,
        chunk_ids=(chunk.id,),
        money_role=role,
    )


SCHEDULE = requirement("合同签订后60日内完成系统开发、部署和试运行", Dimension.SCHEDULE)
ONSITE = requirement("中标人须安排不少于2名工程师驻场服务，驻场时间不少于30个工作日", Dimension.ONSITE)


def finding(**kwargs) -> Finding:
    values = {
        "requirement_id": "r1",
        "status": Status.UNKNOWN,
        "reason": "档案没有排期信息",
        "gap": "补充 60 日内的开发排期",
    }
    return Finding(**{**values, **kwargs})


def checks(f: Finding, r: Requirement = SCHEDULE, p=None) -> list[str]:
    return verify.program_checks(f, r, [chunk_with(r.quote)], p or profile())


def test_good_unknown_passes() -> None:
    assert checks(finding(gap="需要补充合同签订后60日内完成开发的排期")) == []


def test_cross_dimension_profile_field_is_rejected() -> None:
    """旧版的经典错误：拿驻场限制判断开发期限。工期维度不允许用 delivery_constraints。"""
    wrong = finding(
        status=Status.UNMET,
        reason="企业不接受连续60天驻场",
        profile_fields=("delivery_constraints",),
        profile_quote="不接受连续60天驻场",
        gap=None,
    )
    assert "profile_field_outside_dimension" in checks(wrong)


def test_met_needs_a_real_profile_quote() -> None:
    made_up = finding(
        status=Status.MET, reason="团队足够", profile_fields=("staffing",), profile_quote="8名工程师随时待命", gap=None
    )
    assert "profile_quote_not_in_profile" in checks(made_up)
    missing = made_up.model_copy(update={"profile_quote": None})
    assert "profile_quote_missing" in checks(missing)
    real = made_up.model_copy(update={"profile_quote": "4名软件工程师"})
    assert checks(real) == []


def test_unknown_needs_a_gap() -> None:
    assert "gap_missing" in checks(finding(gap=None))


def test_numbers_must_come_from_the_sources() -> None:
    wrong = finding(reason="要求90日内完成开发")
    assert "unsupported_number:90日" in checks(wrong)


def test_money_role_must_match() -> None:
    """预算和最高限价同为 48 万元，但不能互相证明。"""
    cap = requirement("最高限价：48万元", Dimension.FACT, MoneyRole.MAX_PRICE)
    wrong = finding(reason="预算金额为48万元", gap="需要核对金额口径")
    assert checks(wrong, cap) == ["money_role_mismatch"]
    right = finding(reason="最高限价为48万元", gap="需要核对金额口径")
    assert checks(right, cap) == []


def test_contact_and_forbidden_claims_rejected() -> None:
    assert "contact_in_reason" in checks(finding(reason="请致电0000-12345678核实"))
    assert "forbidden_claim" in checks(finding(reason="中标概率较高"))


def met(**kwargs) -> Finding:
    return finding(
        status=Status.MET,
        reason="档案声明有4名软件工程师",
        profile_fields=("staffing",),
        profile_quote="4名软件工程师",
        gap=None,
        **kwargs,
    )


def run_verify(f: Finding, verdict: str):
    llm = FakeLLM(lambda request: {"verdict": verdict, "note": "测试意见"})
    result = verify.verify(f, SCHEDULE, [chunk_with(SCHEDULE.quote)], profile(), llm, call_key="k", attempt=1)
    return result, llm


def test_supported_met_is_kept() -> None:
    result, llm = run_verify(met(), "supported")
    assert result.ok and result.finding.status is Status.MET and result.finding.verification.verdict == "supported"
    # 核验时只给这一条的材料：档案里只有它用到的字段。
    assert '"staffing"' in llm.calls[0].user and "delivery_constraints" not in llm.calls[0].user


def test_unsupported_asks_for_a_redo() -> None:
    result, _ = run_verify(met(), "unsupported")
    assert not result.ok and "测试意见" in result.feedback


def test_uncertain_met_becomes_unknown() -> None:
    result, _ = run_verify(met(), "uncertain")
    assert result.ok and result.finding.status is Status.UNKNOWN and result.finding.verification.downgraded


def test_unknown_is_not_sent_to_the_judge_by_default() -> None:
    llm = FakeLLM(lambda request: {"verdict": "unsupported"})
    result = verify.verify(
        finding(gap="需要补充排期"), SCHEDULE, [chunk_with(SCHEDULE.quote)], profile(), llm, call_key="k", attempt=1
    )
    assert result.ok and llm.calls == []


def test_downgrade_hides_the_unverified_reason() -> None:
    result, _ = run_verify(met(), "unsupported")
    final = verify.downgrade(result, attempts=3)
    assert final.status is Status.UNKNOWN and final.reason == verify.DOWNGRADE_REASON
    assert final.profile_quote is None and final.verification.downgraded and final.verification.attempts == 3
    assert "档案声明有4名软件工程师" in final.verification.judge_note


def test_rule_findings_are_not_sent_to_the_judge() -> None:
    """规则的结论只做程序检查：既省钱，也保证基线模式不调模型。"""
    result, llm = run_verify(met(basis="rule", rule_id="test-rule"), "unsupported")
    assert result.ok and llm.calls == [] and result.finding.verification.verdict == "skipped"
    rejected, _ = run_verify(met(basis="rule").model_copy(update={"profile_quote": "8名软件工程师"}), "supported")
    assert not rejected.ok  # 程序检查照做

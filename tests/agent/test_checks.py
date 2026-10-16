"""程序检查：子串、数字和单位、金额角色、联系方式、不允许的断言。"""

import pytest

from bidradar.agent import checks
from bidradar.agent.schemas import MoneyRole


def test_quote_matching_ignores_whitespace_and_width() -> None:
    assert checks.quote_in("合同签订后 60 日内", ["1. 合同签订后60日内完成系统开发"])
    assert checks.quote_in("（北京时间）", ["截止时间：2026年10月29日09:30(北京时间)"])
    assert not checks.quote_in("合同签订后30日内", ["合同签订后60日内"])
    assert not checks.quote_in("", ["anything"])


def test_number_with_unit_must_match_exactly() -> None:
    """ "2026年10月8日至13日"里的"13日"不能被说成"13天"。"""
    source = ["时间：2026年10月8日至13日"]
    assert checks.unsupported_numbers("获取文件截止到13日", source) == []
    assert checks.unsupported_numbers("获取文件的时间只有13天", source) == ["13天"]


def test_bare_numbers_only_need_the_number() -> None:
    assert checks.unsupported_numbers("第2条要求", ["不少于2名工程师"]) == []
    assert checks.unsupported_numbers("需要3名工程师", ["不少于2名工程师"]) == ["3名"]


def test_number_normalization() -> None:
    assert checks.number_tokens("60 日内、30％、1,200元") == {"60日", "30%", "1200元"}


@pytest.mark.parametrize(
    ("text", "roles"),
    [
        ("预算金额：48万元", {MoneyRole.BUDGET}),
        ("最高限价：48万元", {MoneyRole.MAX_PRICE}),
        ("履约保证金：合同金额的5%", {MoneyRole.PERFORMANCE_BOND}),
        ("不收取投标保证金", {MoneyRole.DEPOSIT}),
        ("预算金额和最高限价都是48万元", {MoneyRole.BUDGET, MoneyRole.MAX_PRICE}),
    ],
)
def test_money_roles(text: str, roles: set[MoneyRole]) -> None:
    assert checks.money_roles(text) == roles


def test_contacts_are_redacted() -> None:
    text = "联系人：张工，电话：0000-12345678，手机13800000000，邮箱：contact@example.invalid"
    redacted = checks.redact(text)
    assert "12345678" not in redacted and "13800000000" not in redacted and "@" not in redacted
    assert checks.has_contact(text) and not checks.has_contact(redacted)


def test_project_numbers_are_not_mistaken_for_phones() -> None:
    assert not checks.has_contact("项目编号：XG-2026-0815")


def test_forbidden_claims() -> None:
    assert checks.forbidden_claims("中标概率很高") == ["中标概率"]
    assert checks.forbidden_claims("完全满足全部要求")
    assert checks.forbidden_claims("这条要求待核查") == []

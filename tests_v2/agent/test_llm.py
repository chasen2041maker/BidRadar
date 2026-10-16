"""模型调用层：先预留后结算、结果不确定时不重试、续跑取回已付费的响应、额度。"""

import pytest

from bidradar.agent.llm import (
    BudgetedLLM,
    BudgetExceeded,
    CallFailed,
    CallOutcomeUnknown,
    FakeLLM,
    InvalidModelOutput,
    LLMRequest,
    LLMResponse,
    Pricing,
    Usage,
    parse_json,
)
from bidradar.agent.schemas import Frozen


class MemoryLedger:
    def __init__(self, cap: int = 10**9) -> None:
        self.rows: dict[str, dict] = {}
        self.cap = cap

    def spent(self) -> int:
        return sum(
            r["charged"] if r["state"] == "settled" else r["reserved"]
            for r in self.rows.values()
            if r["state"] != "released"
        )

    def lookup(self, call_key):
        row = self.rows.get(call_key)
        if row is None or row["state"] == "released":
            return None
        if row["state"] in ("pending", "unknown"):
            raise CallOutcomeUnknown()
        return row["response"]

    def reserve(self, request, amount):
        if self.spent() + amount > self.cap:
            raise BudgetExceeded()
        self.rows[request.call_key] = {"state": "pending", "reserved": amount, "charged": 0}

    def settle(self, request, response, cost):
        self.rows[request.call_key].update(state="settled", charged=cost, response=response)

    def mark_unknown(self, request):
        self.rows[request.call_key]["state"] = "unknown"

    def release(self, request):
        self.rows[request.call_key].update(state="released", reserved=0)


def request(key: str = "run:extract:1") -> LLMRequest:
    return LLMRequest(purpose="extract", call_key=key, system="系统提示", user="用户内容" * 100)


def test_cost_is_reserved_then_settled() -> None:
    ledger = MemoryLedger()
    pricing = Pricing()
    llm = BudgetedLLM(FakeLLM(lambda r: {"items": []}), ledger, pricing)
    llm.complete(request())
    row = ledger.rows["run:extract:1"]
    assert row["state"] == "settled" and 0 < row["charged"] <= pricing.estimate(request())


def test_resume_returns_the_paid_response_without_sending() -> None:
    ledger = MemoryLedger()
    fake = FakeLLM(lambda r: {"items": []})
    llm = BudgetedLLM(fake, ledger, Pricing())
    first = llm.complete(request())
    again = llm.complete(request())
    assert len(fake.calls) == 1 and again.replayed and again.text == first.text


def test_unknown_outcome_is_never_retried() -> None:
    def explode(r):
        raise CallOutcomeUnknown()

    ledger = MemoryLedger()
    llm = BudgetedLLM(FakeLLM(explode), ledger, Pricing())
    with pytest.raises(CallOutcomeUnknown):
        llm.complete(request())
    assert ledger.rows["run:extract:1"]["state"] == "unknown"
    assert ledger.spent() > 0  # 预留额照算已花
    healthy = FakeLLM(lambda r: {"items": []})
    with pytest.raises(CallOutcomeUnknown):
        BudgetedLLM(healthy, ledger, Pricing()).complete(request())
    assert healthy.calls == []  # 同一个调用不会再发一次


def test_definite_failure_releases_the_reservation() -> None:
    def refuse(r):
        raise CallFailed("model_http_400")

    ledger = MemoryLedger()
    with pytest.raises(CallFailed):
        BudgetedLLM(FakeLLM(refuse), ledger, Pricing()).complete(request())
    assert ledger.rows["run:extract:1"]["state"] == "released" and ledger.spent() == 0


def test_over_budget_is_not_sent() -> None:
    fake = FakeLLM(lambda r: {"items": []})
    with pytest.raises(BudgetExceeded):
        BudgetedLLM(fake, MemoryLedger(cap=1), Pricing()).complete(request())
    assert fake.calls == []


def test_pricing_rounds_up_and_estimate_is_conservative() -> None:
    pricing = Pricing()
    assert pricing.cost(Usage(input_tokens=1, output_tokens=0)) == 2  # 2 元/百万 token，一个 token 向上取整
    big = request()
    actual = Usage(input_tokens=len(big.user), output_tokens=100)
    assert pricing.estimate(big) >= pricing.cost(actual)


class Shape(Frozen):
    value: int


def test_parse_json_strict_but_tolerates_code_fence() -> None:
    fenced = LLMResponse(text='```json\n{"value": 3}\n```', usage=None, model="m")
    assert parse_json(fenced, Shape).value == 3
    for bad in ("not json", '{"value": "x"}', '{"value": 1, "extra": 2}'):
        with pytest.raises(InvalidModelOutput):
            parse_json(LLMResponse(text=bad, usage=None, model="m"), Shape)

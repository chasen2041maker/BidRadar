"""④ 核验：每条结论单独核，只给它自己的材料。这是整套设计的重心。

旧版把整份报告交给核验模型，模型容易拿 A 条的原文去支持 B 条。新版逐条核验，核验时看不到别的结论。

两层：
    第一层 程序检查（program_checks）：不花钱，每条都做。失败就打回重做。
    第二层 模型核验（judge）：只对模型给出的 met、unmet、conflicting、not_applicable 做。
          unknown 已经是最保守的结果，默认不核（设计文档第 14 节问题 3），省下的钱用在更关键的结论上。
          规则给出的结论也不核：规则是确定的、有单测，让模型去核既花钱又没有新信息；
          这也保证了基线模式（模型没开）从头到尾不会调模型。

结果怎么用（由 pipeline 决定重做几次）：
    程序检查不通过、或模型判 unsupported → 带着原因重做这一条
    模型判 uncertain → met / unmet 直接降为 unknown（拿不准就别下结论）；其余保留并标注
    重做用完仍不通过 → downgrade() 降为 unknown，标注"未通过核验"
"""

import json

from pydantic import BaseModel, ConfigDict

from bidradar.agent import prompts
from bidradar.agent.checks import (
    forbidden_claims,
    has_contact,
    money_roles,
    quote_in,
    redact,
    unsupported_numbers,
)
from bidradar.agent.dimensions import allowed_fields, spec
from bidradar.agent.llm import InvalidModelOutput, LLMClient, LLMRequest, parse_json
from bidradar.agent.schemas import Chunk, Finding, ProfileSnapshot, Requirement, Status, Verification, VerifyResult

JUDGED = frozenset({Status.MET, Status.UNMET, Status.CONFLICTING, Status.NOT_APPLICABLE})
DOWNGRADE_REASON = "自动判断没有通过核验，这条暂按未知处理。"
DOWNGRADE_GAP = "需要人工对照原文和企业档案核对这条要求。"
UNCERTAIN_GAP = "核验时无法确认这条判断，需要人工对照原文和企业档案核对。"


def program_checks(
    finding: Finding, requirement: Requirement, chunks: list[Chunk], profile: ProfileSnapshot
) -> list[str]:
    """程序检查，返回没通过的检查项。空列表表示全部通过。"""
    failed = []
    texts = [c.text for c in chunks]
    if not quote_in(requirement.quote, texts):
        failed.append("quote_not_in_chunks")
    allowed = set(allowed_fields(requirement.dimension))
    if not set(finding.profile_fields) <= allowed:
        failed.append("profile_field_outside_dimension")
    used = [profile.fields.get(name) or "" for name in finding.profile_fields if name in allowed]
    if finding.status in (Status.MET, Status.UNMET):
        if not finding.profile_quote:
            failed.append("profile_quote_missing")
        elif not quote_in(finding.profile_quote, used):
            failed.append("profile_quote_not_in_profile")
    if finding.status is Status.UNKNOWN and not finding.gap:
        failed.append("gap_missing")
    sources = [requirement.quote, *texts, finding.profile_quote or ""]
    for token in unsupported_numbers(finding.reason + " " + (finding.gap or ""), sources):
        failed.append(f"unsupported_number:{token}")
    # 金额角色只看这条要求自己的摘录：一个片段里常常并列着预算和最高限价，同额也不能互相证明。
    claimed = money_roles(finding.reason)
    available = money_roles(requirement.quote) | ({requirement.money_role} if requirement.money_role else set())
    if claimed - available:
        failed.append("money_role_mismatch")
    if has_contact(finding.reason) or has_contact(finding.gap or ""):
        failed.append("contact_in_reason")
    if forbidden_claims(finding.reason):
        failed.append("forbidden_claim")
    return failed


def dimension_suspicious(requirement: Requirement) -> bool:
    """摘录里一个这个维度的典型词都没有：维度可能标错了。不直接打回，交给模型核验时重点看。"""
    keywords = spec(requirement.dimension).keywords
    return bool(keywords) and not any(k in requirement.quote for k in keywords)


class _Judgement(BaseModel):
    model_config = ConfigDict(extra="ignore")

    verdict: str
    note: str = ""


def judge(
    finding: Finding,
    requirement: Requirement,
    chunks: list[Chunk],
    profile: ProfileSnapshot,
    llm: LLMClient,
    call_key: str,
) -> tuple[str, str | None]:
    """模型核验。只给这一条结论自己的引用和它用到的档案字段。返回 (verdict, 意见)。"""
    payload = {
        "requirement": {
            "quote": requirement.quote,
            "dimension": spec(requirement.dimension).label,
            "conditions": requirement.conditions,
            "dimension_may_be_wrong": dimension_suspicious(requirement),
        },
        "chunks": [{"page": c.page, "text": redact(c.text)} for c in chunks],
        "profile": {name: profile.fields.get(name) for name in finding.profile_fields},
        "finding": {"status": finding.status.value, "reason": finding.reason, "profile_quote": finding.profile_quote},
    }
    request = LLMRequest(
        purpose="verify",
        call_key=call_key,
        system=prompts.load("verify").text,
        user=json.dumps(payload, ensure_ascii=False),
        max_output_tokens=512,
    )
    try:
        result = parse_json(llm.complete(request), _Judgement)
    except InvalidModelOutput:
        return "uncertain", "核验输出格式不对"
    verdict = result.verdict if result.verdict in ("supported", "unsupported", "uncertain") else "uncertain"
    return verdict, (result.note or None)


def verify(
    finding: Finding,
    requirement: Requirement,
    chunks: list[Chunk],
    profile: ProfileSnapshot,
    llm: LLMClient,
    *,
    call_key: str,
    attempt: int,
    judge_unknown: bool = False,
) -> VerifyResult:
    failed = program_checks(finding, requirement, chunks, profile)
    if failed:
        verification = Verification(verdict="unsupported", checks_failed=tuple(failed), attempts=attempt)
        return VerifyResult(
            ok=False,
            finding=finding.model_copy(update={"verification": verification}),
            feedback="程序检查没通过：" + "、".join(failed),
        )
    if finding.basis != "model" or (finding.status not in JUDGED and not judge_unknown):
        verification = Verification(verdict="skipped", attempts=attempt)
        return VerifyResult(ok=True, finding=finding.model_copy(update={"verification": verification}))
    verdict, note = judge(finding, requirement, chunks, profile, llm, call_key)
    verification = Verification(verdict=verdict, judge_note=note, attempts=attempt)
    if verdict == "unsupported":
        return VerifyResult(
            ok=False,
            finding=finding.model_copy(update={"verification": verification}),
            feedback="核验意见：" + (note or ""),
        )
    if verdict == "uncertain" and finding.status in (Status.MET, Status.UNMET):
        downgraded = finding.model_copy(
            update={
                "status": Status.UNKNOWN,
                "gap": UNCERTAIN_GAP,
                "verification": verification.model_copy(update={"downgraded": True}),
            }
        )
        return VerifyResult(ok=True, finding=downgraded)
    return VerifyResult(ok=True, finding=finding.model_copy(update={"verification": verification}))


def downgrade(last: VerifyResult, attempts: int) -> Finding:
    """重做用完仍不通过：降为未知。

    没通过核验的理由不能当结论展示，所以理由换成固定说明；原来的判断和核验意见放进 verification.judge_note，
    方便人去看错在哪。
    """
    previous = last.finding
    old = previous.verification or Verification(verdict="unsupported")
    note = f"原判断（{previous.status.value}）：{previous.reason}"
    if old.judge_note:
        note += f"；核验意见：{old.judge_note}"
    verification = old.model_copy(update={"attempts": attempts, "downgraded": True, "judge_note": note[:1000]})
    return previous.model_copy(
        update={
            "status": Status.UNKNOWN,
            "reason": DOWNGRADE_REASON,
            "gap": DOWNGRADE_GAP,
            "profile_quote": None,
            "verification": verification,
        }
    )

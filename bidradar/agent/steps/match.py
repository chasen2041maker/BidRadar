"""③ 匹配：一条要求 × 这个维度允许看的档案字段 → 一条结论（Finding）。

TODO（练习 2，项目负责人主写）。要求、几种可选做法、验收测试见 docs/exercises.md 的"练习 2"，
设计见 docs/agent-design.md 第 4 节 ③。

现在这里是一个**保守的占位实现**：所有要求一律判"未知"，理由写明"匹配规则尚未实现"。
这样整条流水线能跑通，报告也绝不会出现错误的"满足"或"不满足"（错误不对称：宁可多报未知）。

接口是固定的，实现时不要改：
    match(requirement, chunks, profile, llm, *, call_key, feedback=None) -> Finding

    requirement  一条要求（kind 一定是 obligation；项目事实不进入匹配）
    chunks       这条要求引用的片段原文
    profile      已经按维度表切好的档案：只含这个维度允许看的字段（ProfileSlice）。函数里拿不到别的字段
    llm          要问模型时用它。call_key 作为 LLMRequest.call_key，续跑时靠它找回已付费的响应
    feedback     ④ 核验没通过时的意见。第一次匹配时是 None；重做这一条时传进来

输出的 Finding 会被 ④ 逐条检查，几条硬规则（不满足就会被打回重做，重做 2 次仍不过就降为未知）：
    - status 是 met 或 unmet 时，profile_quote 必须是 profile 里某个字段原文的子串
    - status 是 unknown 时，gap 必须写明还缺什么
    - profile_fields 只能是 profile 里有的字段
    - reason 里的数字和单位，要能在要求摘录、片段原文或 profile_quote 里找到

出错时怎么办（验收测试会检查）：
    - 模型输出不合格（不是 JSON、状态不认识、引用的档案原话档案里没有……）：不抛异常，返回 unknown
    - 计费和调用错误（BudgetExceeded、CallOutcomeUnknown、CallFailed、ModelDisabled）：原样往外抛，
      不能吞掉。吞掉了，"结果不确定、可能已经扣费"就会被当成一条普通的未知结论
"""

from pydantic import BaseModel, ConfigDict, Field

from bidradar.agent.llm import LLMClient
from bidradar.agent.schemas import Chunk, Finding, ProfileSlice, Requirement, Status


class MatchAnswer(BaseModel):
    """模型每次匹配调用要返回的 JSON 结构。

    结构是固定的：验收测试里的假模型按它应答，提示词（match.v1.md）里要把它写给模型看。
    可以加字段（比如"置信度"），已有的字段别删、别改名。用 llm.parse_json(response, MatchAnswer) 解析。
    """

    model_config = ConfigDict(extra="ignore")

    status: str  # met / unmet / unknown / conflicting / not_applicable，别的值按不合格处理
    reason: str = ""
    profile_fields: list[str] = Field(default_factory=list)
    profile_quote: str | None = None
    gap: str | None = None


PLACEHOLDER_REASON = "自动匹配尚未实现，暂按未知处理。"
PLACEHOLDER_GAP = "需要人工对照企业档案判断这条要求是否满足。"


def match(
    requirement: Requirement,
    chunks: list[Chunk],
    profile: ProfileSlice,
    llm: LLMClient,
    *,
    call_key: str,
    feedback: str | None = None,
) -> Finding:
    # TODO（练习 2）：先用规则判能判的（比如维度表里的默认规则），剩下的再问模型。
    return Finding(
        requirement_id=requirement.id,
        status=Status.UNKNOWN,
        reason=PLACEHOLDER_REASON,
        profile_fields=(),
        gap=PLACEHOLDER_GAP,
        basis="placeholder",
    )

"""评测指标。

TODO（练习 2，项目负责人主写）：align()、extraction_scores()、match_scores() 三个函数。
要求和验收测试见 docs/exercises.md 的"练习 2"和 tests_v2/evals/test_metrics.py；为什么这么设计见
docs/agent-design.md 第 11 节。下面每个函数的 docstring 就是它的规格，结构（输入、输出的字段）是固定的，
evals/run.py 按这个结构打印报表。

已经写好的：verify_scores()（核验拦截率）、citation_validity()（引用有效率）。

所有比例在分母为 0 时返回 None（报表里显示"—"），不要返回 0 或 1：
"没有可算的"和"算出来是 0"是两回事。
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from bidradar.agent.checks import quote_in
from bidradar.agent.schemas import Chunk, Finding, Requirement, Status, VerifyResult
from evals.cases import GoldDocument, GoldFinding, GoldRequirement


def ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


# ---- 对齐（TODO 练习 2） ----


@dataclass(frozen=True)
class Alignment:
    pairs: tuple[tuple[int, int], ...]  # (标准要求的下标, 预测要求的下标)
    missed: tuple[int, ...]  # 没有对上的标准要求
    extra: tuple[int, ...]  # 没有对上的预测要求


def align(gold: Sequence[str], predicted: Sequence[Requirement], chunks: Mapping[str, Chunk]) -> Alignment:
    """把预测的要求和标准要求一一对上。gold 是标准摘录的列表，chunks 是 {片段 ID: 片段}。

    规格（验收测试逐条检查）：
    - 一对一：一条预测最多对上一条标准，一条标准最多对上一条预测。
    - 摘录完全相同，一定对上。
    - 一条是另一条的子串（预测比标准短或长），算对上。比较前去掉空白、统一全角半角（checks.squash 就是做这个的）。
    - 两条只是开头几个字一样（比如都以"平台应支持"开头），不算对上。
    - 一条预测同时包含两条标准（抽取把两条要求合成了一条），只能对上其中一条，另一条算漏。
    - 有多种对法时，选让对上的总数最多的那种；总数一样时，优先对上摘录更接近的。
    - pairs 按标准下标排序；missed、extra 按下标排序。

    做法自己定：摘录的子串关系、字符级相似度、引用的片段有没有重叠，或者几种结合。
    想清楚每种做法在什么情况下会对错：这是指标里最难、也最值得想透的部分（设计文档第 11 节）。
    """
    raise NotImplementedError("练习 2：evals/metrics.py 的 align()")


# ---- 抽取指标（TODO 练习 2） ----


@dataclass(frozen=True)
class ExtractionScores:
    gold: int  # 标准要求条数（不含 optional）
    predicted: int  # 预测条数（不含对上 optional 的）
    aligned: int  # 对上的条数
    recall: float | None  # aligned / gold
    precision: float | None  # aligned / predicted
    dimension_accuracy: float | None  # 对上的里面，维度标对的比例
    kind_accuracy: float | None  # 对上的里面，事实 / 要求分对的比例
    forbidden: int  # 抽出了 not_requirements 里的内容（模板、联系方式）多少条
    missed: tuple[str, ...] = ()  # 漏掉的标准摘录，报表里列出来，方便看漏了什么
    wrong_dimension: tuple[tuple[str, str, str], ...] = ()  # (摘录, 标准维度, 预测维度)


def extraction_scores(
    document: GoldDocument, predicted: Sequence[Requirement], chunks: Mapping[str, Chunk]
) -> ExtractionScores:
    """抽取评测。

    规格：
    - 先用 align() 把预测和 document.requirements 对齐。
    - 对上 document.optional 里某一条的预测，既不算对也不算错：不进 predicted，也不进 aligned。
      同一条预测既能对上标准要求、又能对上 optional 时，算对上标准要求。
    - 对上 document.not_requirements 里某一条的预测，计入 forbidden，同时照常算进 predicted（它是一条错的预测）。
    - recall、precision、dimension_accuracy、kind_accuracy 的分母为 0 时是 None。
    """
    raise NotImplementedError("练习 2：evals/metrics.py 的 extraction_scores()")


# ---- 匹配指标（TODO 练习 2） ----


@dataclass(frozen=True)
class MatchScores:
    total: int  # 标准结论条数
    missing: int  # 没有对应预测的（端到端评测时，要求没被抽出来）
    correct: int
    status_accuracy: float | None  # correct / (total - missing)
    severe: int  # 严重错误：预测状态在 GoldFinding.must_not 里
    severe_rate: float | None  # severe / (total - missing)
    unknown_rate: float | None  # 预测为 unknown 的比例：太高说明过分保守，报告没用
    confusion: Mapping[tuple[str, str], int] = field(default_factory=dict)  # (标准状态, 预测状态) → 条数
    severe_quotes: tuple[str, ...] = ()  # 出了严重错误的要求摘录


def match_scores(gold: Sequence[GoldFinding], predicted: Sequence[Finding | None]) -> MatchScores:
    """匹配评测。predicted[i] 是 gold[i] 那条要求的预测结论；None 表示没有（要求没抽出来）。

    规格：
    - 对不对用 GoldFinding.correct()（标准状态或 acceptable 里的状态都算对）。
    - 严重错误用 GoldFinding.severe()。
    - None 只计入 missing，不进其他任何分子分母。
    - confusion 的键用状态的字符串值，比如 ("unknown", "met")；只记有预测的。
    """
    raise NotImplementedError("练习 2：evals/metrics.py 的 match_scores()")


# ---- 核验指标（已实现） ----


@dataclass(frozen=True)
class Injected:
    """故意造的一条结论：kind 是造错的方式；clean=True 表示这是一条正确的结论，用来看核验会不会误杀。"""

    kind: str
    requirement: Requirement
    finding: Finding
    clean: bool = False


@dataclass(frozen=True)
class VerifyScores:
    injected: int
    caught: int
    catch_rate: float | None
    by_kind: Mapping[str, tuple[int, int]]  # 造错方式 → (拦下, 总数)
    clean: int
    false_rejections: int
    false_rejection_rate: float | None


def stopped(result: VerifyResult) -> bool:
    """核验"拦下"了这条结论：打回重做，或者降为未知。"""
    verification = result.finding.verification
    return not result.ok or bool(verification and verification.downgraded)


def verify_scores(injected: Sequence[Injected], results: Sequence[VerifyResult]) -> VerifyScores:
    totals: Counter[str] = Counter()
    caught_by: Counter[str] = Counter()
    clean = false_rejections = 0
    for item, result in zip(injected, results, strict=True):
        if item.clean:
            clean += 1
            false_rejections += stopped(result)
            continue
        totals[item.kind] += 1
        caught_by[item.kind] += stopped(result)
    caught = sum(caught_by.values())
    return VerifyScores(
        injected=sum(totals.values()),
        caught=caught,
        catch_rate=ratio(caught, sum(totals.values())),
        by_kind={kind: (caught_by[kind], totals[kind]) for kind in sorted(totals)},
        clean=clean,
        false_rejections=false_rejections,
        false_rejection_rate=ratio(false_rejections, clean),
    )


def citation_validity(requirements: Sequence[Requirement], chunks: Mapping[str, Chunk]) -> float | None:
    """引用有效率：摘录确实出现在它引用的片段里的比例。抽取步骤已经做了这个检查，这里是复查。"""
    valid = sum(quote_in(r.quote, [chunks[cid].text for cid in r.chunk_ids if cid in chunks]) for r in requirements)
    return ratio(valid, len(requirements))


def gold_quotes(requirements: Sequence[GoldRequirement]) -> list[str]:
    return [r.quote for r in requirements]


STATUS_ORDER = [s.value for s in Status]

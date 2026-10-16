"""练习 2 的验收测试：评测指标（evals/metrics.py 的 align、extraction_scores、match_scores）。

规格写在 metrics.py 每个函数的 docstring 里，这里逐条检查。做完把下面的 pytestmark 删掉，全部通过即完成。
"""

import pytest

from bidradar.agent.dimensions import Dimension
from bidradar.agent.schemas import Category, Chunk, Finding, Kind, Requirement, Status
from evals import metrics
from evals.cases import GoldDocument, GoldFinding, GoldRequirement, default_must_not

pytestmark = pytest.mark.skip(reason="练习 2：evals/metrics.py 还没实现，见 docs/exercises.md")

TEXT = (
    "合同签订后60日内完成系统开发、部署和试运行。"
    "中标人须安排不少于2名工程师驻场服务，驻场时间不少于30个工作日，服务地点为虚构市政务服务中心。"
    "平台应支持私有化部署，所有数据存储在采购人指定的政务云环境内，不得传输至境外。"
    "平台应支持PDF、Word、Excel等常见格式文档的批量导入和自动解析。"
    "预算金额：48万元。系统开发工作由中标人负责。"
    "我单位承诺：参加本次政府采购活动前三年内，在经营活动中没有重大违法记录。"
)
CHUNKS = {"c1": Chunk(id="c1", document_id="d", seq=0, page=1, section_path=(), kind="text", text=TEXT)}


def predicted(quote: str, dimension: Dimension = Dimension.OTHER, kind: Kind = Kind.OBLIGATION) -> Requirement:
    return Requirement(
        id=quote, kind=kind, category=Category.OTHER, dimension=dimension, quote=quote, chunk_ids=("c1",)
    )


def aligned(gold: list[str], quotes: list[str]) -> metrics.Alignment:
    return metrics.align(gold, [predicted(q) for q in quotes], CHUNKS)


# ---- align ----

SCHEDULE = "合同签订后60日内完成系统开发、部署和试运行"
ONSITE = "中标人须安排不少于2名工程师驻场服务，驻场时间不少于30个工作日"
LOCATION = "服务地点为虚构市政务服务中心"
PRIVATE = "平台应支持私有化部署，所有数据存储在采购人指定的政务云环境内，不得传输至境外"
IMPORT = "平台应支持PDF、Word、Excel等常见格式文档的批量导入和自动解析"


def test_identical_quotes_align() -> None:
    result = aligned([SCHEDULE, PRIVATE], [PRIVATE, SCHEDULE])
    assert (result.pairs, result.missed, result.extra) == (((0, 1), (1, 0)), (), ())


def test_shorter_or_longer_prediction_aligns() -> None:
    assert aligned([SCHEDULE], ["60日内完成系统开发"]).pairs == ((0, 0),)
    assert aligned(["平台应支持私有化部署"], [PRIVATE]).pairs == ((0, 0),)


def test_whitespace_and_full_width_do_not_matter() -> None:
    """PDF 折行带进来的空格、全角数字，都不是摘录的差别。"""
    assert aligned([SCHEDULE], ["合同签订后 ６０ 日内完成系统开发、\n部署和试运行"]).pairs == ((0, 0),)


def test_a_shared_opening_is_not_enough() -> None:
    """都以"平台应支持"开头的两条不同要求，不能对上。"""
    result = aligned([IMPORT], ["平台应支持私有化部署"])
    assert (result.pairs, result.missed, result.extra) == ((), (0,), (0,))


def test_one_prediction_covers_at_most_one_gold() -> None:
    """抽取把驻场和服务地点合成了一条：只能算对上一条，另一条算漏，这样召回率才能反映"合并"这种错。"""
    merged = ONSITE + "，" + LOCATION
    result = aligned([ONSITE, LOCATION], [merged])
    assert len(result.pairs) == 1 and len(result.missed) == 1 and result.extra == ()


def test_alignment_maximizes_the_number_of_pairs() -> None:
    """贪心会掉进的坑：第一条标准先抢走了第二条唯一能对上的预测。"""
    gold = ["系统开发", "60日内完成系统开发、部署"]
    result = aligned(gold, ["60日内完成系统开发、部署和试运行", "系统开发工作"])
    assert result.pairs == ((0, 1), (1, 0))


def test_closer_quote_wins_a_tie() -> None:
    gold = ["平台应支持私有化部署"]
    result = aligned(gold, [PRIVATE, "平台应支持私有化部署，所有数据存储在采购人指定的政务云环境内"])
    assert (result.pairs, result.extra) == (((0, 1),), (0,))


def test_empty_inputs() -> None:
    assert aligned([], []) == metrics.Alignment(pairs=(), missed=(), extra=())
    assert aligned([SCHEDULE], []).missed == (0,)
    assert aligned([], [SCHEDULE]).extra == (0,)


# ---- extraction_scores ----

DOCUMENT = GoldDocument(
    id="d",
    files=(),
    requirements=(
        GoldRequirement(SCHEDULE, Kind.OBLIGATION, Dimension.SCHEDULE),
        GoldRequirement(ONSITE, Kind.OBLIGATION, Dimension.ONSITE),
        GoldRequirement(PRIVATE, Kind.OBLIGATION, Dimension.CAPABILITY),
        GoldRequirement("预算金额：48万元", Kind.FACT, Dimension.FACT),
    ),
    optional=("系统开发工作由中标人负责",),
    not_requirements=("我单位承诺：参加本次政府采购活动前三年内，在经营活动中没有重大违法记录",),
)


def test_extraction_scores() -> None:
    result = metrics.extraction_scores(
        DOCUMENT,
        [
            predicted(SCHEDULE, Dimension.ONSITE),  # 维度标错：旧版典型错误
            predicted(PRIVATE, Dimension.CAPABILITY),
            predicted("预算金额：48万元", Dimension.FACT, Kind.OBLIGATION),  # 事实当成了要求
            predicted("系统开发工作由中标人负责"),  # optional：不算对也不算错
            predicted("我单位承诺：参加本次政府采购活动前三年内，在经营活动中没有重大违法记录"),  # 模板
        ],
        CHUNKS,
    )
    assert (result.gold, result.predicted, result.aligned) == (4, 4, 3)
    assert result.recall == 3 / 4 and result.precision == 3 / 4
    assert result.dimension_accuracy == 2 / 3 and result.kind_accuracy == 2 / 3
    assert result.forbidden == 1
    assert result.missed == (ONSITE,)
    assert result.wrong_dimension == ((SCHEDULE, "schedule", "onsite"),)


def test_gold_beats_optional() -> None:
    """一条预测既对得上标准要求、又对得上 optional（标准摘录比 optional 长），算对上标准要求。"""
    document = GoldDocument(
        id="d",
        files=(),
        requirements=(GoldRequirement(SCHEDULE, Kind.OBLIGATION, Dimension.SCHEDULE),),
        optional=("60日内完成系统开发",),
    )
    result = metrics.extraction_scores(document, [predicted(SCHEDULE, Dimension.SCHEDULE)], CHUNKS)
    assert (result.predicted, result.aligned, result.recall, result.precision) == (1, 1, 1.0, 1.0)


def test_nothing_predicted() -> None:
    result = metrics.extraction_scores(DOCUMENT, [], CHUNKS)
    assert (result.recall, result.precision, result.dimension_accuracy, result.kind_accuracy) == (0.0, None, None, None)
    assert len(result.missed) == 4


# ---- match_scores ----


def gold(status: Status, acceptable=(), must_not=None) -> GoldFinding:
    acceptable = frozenset(acceptable)
    return GoldFinding(
        quote="q",
        status=status,
        acceptable=acceptable,
        must_not=default_must_not(status, acceptable) if must_not is None else frozenset(must_not),
    )


def found(status: Status) -> Finding:
    return Finding(requirement_id="r", status=status, reason="理由", gap="缺口" if status is Status.UNKNOWN else None)


def test_status_accuracy_counts_acceptable_answers() -> None:
    result = metrics.match_scores(
        [gold(Status.UNKNOWN, acceptable=[Status.MET]), gold(Status.MET), gold(Status.UNMET)],
        [found(Status.MET), found(Status.MET), found(Status.UNKNOWN)],
    )
    assert (result.total, result.correct, result.status_accuracy) == (3, 2, 2 / 3)


def test_severe_errors_follow_must_not() -> None:
    result = metrics.match_scores(
        [gold(Status.UNKNOWN), gold(Status.UNKNOWN, must_not=[Status.MET]), gold(Status.MET), gold(Status.UNMET)],
        [found(Status.UNMET), found(Status.UNMET), found(Status.UNMET), found(Status.UNKNOWN)],
    )
    # 第一条：标准未知却判不满足（默认就是严重错误）；第二条：只禁止 met；第三条：满足判成不满足。
    assert (result.severe, result.severe_rate) == (2, 2 / 4)


def test_missing_predictions_only_count_as_missing() -> None:
    result = metrics.match_scores([gold(Status.UNKNOWN), gold(Status.MET)], [None, found(Status.MET)])
    assert (result.missing, result.correct, result.status_accuracy, result.severe_rate) == (1, 1, 1.0, 0.0)
    everything_missing = metrics.match_scores([gold(Status.MET)], [None])
    assert (everything_missing.status_accuracy, everything_missing.severe_rate) == (None, None)
    assert everything_missing.unknown_rate is None


def test_confusion_and_unknown_rate() -> None:
    result = metrics.match_scores(
        [gold(Status.UNKNOWN), gold(Status.UNKNOWN), gold(Status.MET)],
        [found(Status.UNKNOWN), found(Status.MET), found(Status.UNKNOWN)],
    )
    assert dict(result.confusion) == {("unknown", "unknown"): 1, ("unknown", "met"): 1, ("met", "unknown"): 1}
    assert result.unknown_rate == 2 / 3

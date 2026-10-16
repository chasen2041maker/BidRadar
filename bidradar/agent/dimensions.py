"""维度表：每条要求属于哪个维度，匹配时只能看这个维度对应的档案字段。

这是研究流水线里最重要的一张表（设计见 docs/agent-design.md 第 3 节）。
旧版最典型的错误是跨维度比较：拿"不接受连续 60 天驻场"去判"合同签订后 60 日内完成开发"。
提示词劝了很多次，模型仍然会犯。新版的做法是让匹配步骤**根本看不到**别的维度的档案字段。

这张表是数据，不是提示词：
    - ② 抽取的提示词从这里生成"有哪些维度、各是什么意思"
    - ③ 匹配按这里给每条要求切出允许看的档案字段（ProfileSlice）
    - ④ 核验按这里检查"用到的档案字段是否越界"，并用关键词做维度一致性的可疑标记
以后发现新的跨维度错误，改的是这张表，不是提示词。改了要跑全量评测。

档案字段名和 workspace 模块的九个字段一致。agent 不能 import 业务模块，所以这里写死；
research 模块在把档案交给流水线之前会核对字段名（见 research.service._profile_snapshot）。
"""

from dataclasses import dataclass
from enum import StrEnum


class Dimension(StrEnum):
    QUALIFICATION = "qualification"
    CAPABILITY = "capability"
    EXPERIENCE = "experience"
    SCHEDULE = "schedule"
    ONSITE = "onsite"
    LOCATION = "location"
    STAFFING = "staffing"
    COMMERCIAL = "commercial"
    MATERIALS = "materials"
    SCORING = "scoring"
    FACT = "fact"
    OTHER = "other"


PROFILE_FIELDS = (
    "company_name",
    "city",
    "project_types",
    "capabilities",
    "delivery_constraints",
    "cases",
    "qualifications",
    "staffing",
    "commercial_constraints",
)


@dataclass(frozen=True)
class DimensionSpec:
    dimension: Dimension
    label: str
    # 给抽取模型看的一句话解释，以及一两个典型写法。
    description: str
    # 匹配时能看的档案字段。空表示这个维度不做匹配（比如项目事实、投标材料）。
    profile_fields: tuple[str, ...]
    # 摘录里一般会出现的词。一个都没有时打一个"维度可疑"的标记，核验时重点看。空表示不检查。
    keywords: tuple[str, ...] = ()

    @property
    def matchable(self) -> bool:
        return bool(self.profile_fields)


DIMENSIONS: dict[Dimension, DimensionSpec] = {
    spec.dimension: spec
    for spec in (
        DimensionSpec(
            Dimension.QUALIFICATION,
            "资格",
            "投标人必须具备的法定资格、资质证书、信用记录、参与限制。例：具有独立承担民事责任的能力；不接受联合体投标",
            ("qualifications",),
        ),
        DimensionSpec(
            Dimension.CAPABILITY,
            "能力与技术",
            "要交付的系统、功能、技术指标和技术约束。例：平台应支持私有化部署；回答须标注引用出处",
            ("capabilities", "project_types", "cases"),
        ),
        DimensionSpec(
            Dimension.EXPERIENCE,
            "业绩案例",
            "对过往类似项目、合同、案例的要求。例：近三年至少 2 个同类项目",
            ("cases",),
            ("业绩", "案例", "合同", "项目经验", "类似项目"),
        ),
        DimensionSpec(
            Dimension.SCHEDULE,
            "工期",
            "完成某项交付的期限和进度节点（不是驻场）。例：合同签订后 60 日内完成系统开发",
            ("staffing",),
            ("日内", "天内", "工期", "完成", "期限", "交付", "上线", "之前", "以前"),
        ),
        DimensionSpec(
            Dimension.ONSITE,
            "驻场与现场",
            "要求人员驻场、现场服务、常驻的时长和人数。例：须安排 2 名工程师驻场不少于 30 个工作日",
            ("delivery_constraints",),
            ("驻场", "现场", "常驻", "到岗", "派驻", "进场"),
        ),
        DimensionSpec(
            Dimension.LOCATION,
            "服务地点",
            "服务、交付或部署的地点要求。例：服务地点为某市政务服务中心",
            ("city", "delivery_constraints"),
            ("地点", "地址", "所在地", "本地", "市", "区", "省"),
        ),
        DimensionSpec(
            Dimension.STAFFING,
            "人员要求",
            "对项目人员的数量、资历、证书、社保等要求。例：项目经理须具有高级证书",
            ("staffing", "qualifications"),
            ("人员", "项目经理", "工程师", "团队", "证书", "社保", "名"),
        ),
        DimensionSpec(
            Dimension.COMMERCIAL,
            "商务与付款",
            "付款方式、保证金、质保、违约责任等商务条款。例：验收合格后支付 60%",
            ("commercial_constraints",),
            ("付款", "支付", "保证金", "质保", "违约", "结算", "发票", "%"),
        ),
        DimensionSpec(
            Dimension.MATERIALS,
            "投标材料",
            "投标文件里必须提交的材料、证明和格式要求。例：须提供营业执照副本",
            (),
        ),
        DimensionSpec(
            Dimension.SCORING,
            "评分项",
            "评标打分的项目和分值（不是门槛）。例：类似业绩 15 分，每个项目 5 分",
            ("capabilities", "cases"),
        ),
        DimensionSpec(
            Dimension.FACT,
            "项目事实",
            "项目本身的信息，不存在企业满足与否：预算、最高限价、截止时间、获取文件的时间和方式、项目编号",
            (),
        ),
        DimensionSpec(Dimension.OTHER, "其他", "放不进上面任何一类的要求", ("capabilities", "delivery_constraints")),
    )
}


def spec(dimension: Dimension | str) -> DimensionSpec:
    return DIMENSIONS[Dimension(dimension)]


def allowed_fields(dimension: Dimension | str) -> tuple[str, ...]:
    return spec(dimension).profile_fields


def describe_for_prompt() -> list[dict[str, str]]:
    """给抽取模型的维度说明。"""
    return [{"dimension": s.dimension.value, "label": s.label, "meaning": s.description} for s in DIMENSIONS.values()]


def table_fingerprint_source() -> list[list[object]]:
    """维度表的内容，参与配置版本的计算：表一改，配置版本就变，旧报告知道自己是按哪版表生成的。"""
    return [
        [s.dimension.value, s.label, s.description, list(s.profile_fields), list(s.keywords)]
        for s in DIMENSIONS.values()
    ]

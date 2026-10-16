"""各步骤共用的程序检查：摘录必须是原文子串、数字和单位要有出处、金额角色要对得上、不能含联系方式。

这些检查替代了旧版提示词里的大部分补丁规则（对照表见 docs/agent-design.md 第 12 节）。
它们不花钱、结果确定，每条结论都做一遍。全部是纯函数。
"""

import re
import unicodedata

from bidradar.agent.schemas import MoneyRole

# ---- 文本规范化和子串检查 ----


def squash(text: str) -> str:
    """比较用的规范形式：全角转半角（NFKC），去掉所有空白。

    中文没有词间空格，PDF 折行、Word 排版带进来的空格和换行都不是原文的一部分。
    "合同签订后 60 日内"和"合同签订后60日内"应当算同一段话。
    """
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def quote_in(quote: str, texts: list[str]) -> bool:
    """摘录是否原样出现在这些原文里（规范化后比较）。"""
    needle = squash(quote)
    return bool(needle) and any(needle in squash(text) for text in texts)


# ---- 联系方式 ----

CONTACT = re.compile(
    r"(?:[\w.+-]+@[\w-]+(?:\.[\w-]+)+)"  # 邮箱
    r"|(?:(?<!\d)1[3-9]\d{9}(?!\d))"  # 手机号
    r"|(?:(?<!\d)0\d{2,3}[-－]?\d{7,8}(?!\d))"  # 带区号的座机
    r"|(?:(?<!\d)\d{3,4}[-－]\d{7,8}(?!\d))"  # 其他"区号-号码"写法
)


def redact(text: str) -> str:
    """发给模型之前把联系方式换成占位符。模型不需要它们，也不该在报告里复述。"""
    return CONTACT.sub("[联系方式]", text or "")


def has_contact(text: str) -> bool:
    return CONTACT.search(text or "") is not None


# ---- 数字和单位 ----

NUMBER = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*(万元|元|个工作日|工作日|日历天|个月|月|日|天|年|小时|分钟|%|％|名|人|份|分|套|个)?"
)


def number_tokens(text: str) -> set[str]:
    """文本里的"数字 + 单位"。"60 日"和"60日"是同一个；单位不同的同一个数字（13 日 / 13 天）不是同一个。"""
    tokens = set()
    for value, unit in NUMBER.findall(unicodedata.normalize("NFKC", text or "")):
        value = value.replace(",", "")
        tokens.add(value + (unit or "").replace("％", "%"))
    return tokens


def unsupported_numbers(claim: str, sources: list[str]) -> list[str]:
    """结论里出现、但在依据里找不到的数字。

    规则：带单位的数字，依据里要有同样的数字和同样的单位；不带单位的数字，依据里有这个数字就行。
    这条规则挡住的典型错误："2026年10月8日至13日"被说成"13 天"。
    """
    available = set()
    bare = set()
    for source in sources:
        for token in number_tokens(source):
            available.add(token)
            bare.add(re.match(r"[\d.]+", token).group())
    missing = []
    for token in sorted(number_tokens(claim)):
        number = re.match(r"[\d.]+", token).group()
        if token == number:
            if number not in bare:
                missing.append(token)
        elif token not in available:
            missing.append(token)
    return missing


# ---- 金额角色 ----

MONEY_LABELS: dict[str, MoneyRole] = {
    "预算金额": MoneyRole.BUDGET,
    "项目预算": MoneyRole.BUDGET,
    "采购预算": MoneyRole.BUDGET,
    "预算": MoneyRole.BUDGET,
    "最高限价": MoneyRole.MAX_PRICE,
    "最高投标限价": MoneyRole.MAX_PRICE,
    "控制价": MoneyRole.MAX_PRICE,
    "招标文件售价": MoneyRole.FILE_FEE,
    "文件售价": MoneyRole.FILE_FEE,
    "售价": MoneyRole.FILE_FEE,
    "投标保证金": MoneyRole.DEPOSIT,
    "保证金": MoneyRole.DEPOSIT,
    "履约保证金": MoneyRole.PERFORMANCE_BOND,
    "单价": MoneyRole.UNIT_PRICE,
}


def money_roles(text: str) -> set[MoneyRole]:
    """文本里提到了哪几种钱。按标签识别，"履约保证金"不会被当成"保证金"之外的另一笔投标保证金。"""
    found = set()
    remaining = text or ""
    for label in sorted(MONEY_LABELS, key=len, reverse=True):
        if label in remaining:
            found.add(MONEY_LABELS[label])
            remaining = remaining.replace(label, "")
    return found


# ---- 不允许的断言 ----

FORBIDDEN = re.compile(r"中标概率|一定(?:能)?中标|保证中标|完全满足(?:全部|所有)|满足全部资格|已读完(?:全部|完整)")


def forbidden_claims(text: str) -> list[str]:
    """报告里不允许出现的说法：预测中标、声称满足全部资格、声称读完了完整标书。"""
    return FORBIDDEN.findall(text or "")

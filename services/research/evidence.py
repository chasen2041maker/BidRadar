"""证据索引和报告校验：研究 Agent 的"原文库"和"程序检查员"。

这个文件做三件事：
    1. EvidenceIndex    把任务冻结的公告观察切成带 ID 的原文片段，供模型检索和阅读。
    2. validate_report  用程序检查模型提交的报告：引用是否存在、资格是否逐字摘录、
                        金额和数字是否都能在引用的原文里找到。
    3. baseline_report  不调模型的"规则基线"：只列原文、全部标为待核查，用来和 Agent 对照。

几个前提：
    - 只处理任务里已经冻结的输入，不联网，也不去读原件库。
    - 片段来自规范化后的公告字段，不是完整招标文件；PDF 归档了也不代表读过全文。
    - 联系方式在切片时就去掉了，剩下的每个片段仍是原文里连续的一段，可以原样引用。

阅读顺序：EvidenceIndex → search_result / read → validate_report。
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from hashlib import sha256
from itertools import combinations
import json
import math
import re

# 切片和校验规则的版本，会写进任务的冻结配置。
VERSION = "frozen-evidence-v13-question-gaps"
HEX = re.compile(r"[0-9a-f]{64}\Z")
# 企业档案的九个字段、要求的八个类别、结论的五种状态。
PROFILE_FIELDS = frozenset(
    (
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
)
CATEGORIES = frozenset(
    ("technical", "qualification", "delivery", "commercial", "budget", "deadline", "materials", "other")
)
STATUSES = frozenset(("met", "unmet", "unknown", "conflicting", "not_applicable"))
# 企业档案只是企业自己填的声明，资格证明没有核验过，所以资格类要求一律判"未知"，
# 而且理由只能用下面这两句固定的话。以前让模型自由写，它常从别的条款借材料名称，
# 写出原文里没有的要求。这条规则只管资格类，技术、交付等类别仍由模型自己写理由。
QUALIFICATION_REASON = "企业资格资料尚未核验，本条要求及适用分支需结合原文和企业证明逐项核对。"
QUALIFICATION_UNKNOWN_REASON = "缺少已核验的企业资格资料，当前不能确认本条的适用情况。"
# 金额标签 → 金额角色（名称和规范化数据里的 money.role 一致）。
# 预算、最高限价、文件售价是三种不同的钱，哪怕数额相同也不能互相证明。
# 这里只识别"标签紧跟金额"的明确写法，更复杂的说法交给语义核验。
MONEY_LABELS = {
    "预算金额": "budget",
    "项目预算": "budget",
    "采购预算": "budget",
    "预算": "budget",
    "最高投标限价": "ceiling",
    "最高限价": "ceiling",
    "限价": "ceiling",
    "招标文件售价": "file_fee",
    "采购文件售价": "file_fee",
    "文件售价": "file_fee",
    "售价": "file_fee",
    "文件费": "file_fee",
}
MONEY_ROLES = frozenset(MONEY_LABELS.values())
# 识别联系方式：邮箱、"联系人/电话"后面的内容、手机号、座机号。
# 发给模型之前要去掉，报告里也不能出现。后面紧跟"元/万元"的数字是金额，不当成电话。
CONTACT = re.compile(
    r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
    r"(?:联系人|联系电话|联系手机|电子邮箱|电子邮件|联系方式|项目联系人|电话|手机|邮箱)(?!已省略)(?:\s*[:：]\s*|\s+)"
    r"(?:(?!预算|金额|截止|交付|采购|合同|期限|获取|响应)[^；;。\n]){0,100}|"
    r"(?<![A-Za-z0-9.])(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?![A-Za-z0-9.]|\s*(?:元|万元|亿元|人民币|CNY))|"
    r"(?<![A-Za-z0-9.])(?:[（(]0\d{2,3}[)）]|0\d{2,3}[-－ ])[-－ ]?\d{7,8}(?![A-Za-z0-9.]|\s*(?:元|万元|亿元|人民币|CNY))"
)


class EvidenceError(ValueError):
    """证据或报告不合格时抛出。code 是给程序看的错误码，不含任何外部文本。"""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    """把对象序列化成固定格式的 JSON（键排序、无多余空格），同样的内容永远得到同样的字符串。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    """对象的 SHA-256 摘要。证据 ID、输入指纹都用它算。"""
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def strict_json(text, *, limit=512_000):
    """严格解析 JSON。

    模型输出和恢复数据都不能直接信任，所以这里会拒绝：重复的键、NaN/Infinity、
    嵌套太深或节点太多的结构，以及超过 limit 字节的文本。
    """
    if not isinstance(text, str) or len(text.encode("utf-8")) > limit:
        raise EvidenceError("invalid_json_size")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise EvidenceError("duplicate_json_key")
            result[key] = value
        return result

    def invalid(_):
        raise EvidenceError("non_finite_json")

    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, RecursionError) as exc:
        raise EvidenceError("invalid_json") from None
    queue, nodes = [(result, 0)], 0
    while queue:
        value, depth = queue.pop()
        nodes += 1
        if depth > 32 or nodes > 30_000 or isinstance(value, float) and not math.isfinite(value):
            raise EvidenceError("json_resource_limit")
        if isinstance(value, dict):
            queue.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            queue.extend((v, depth + 1) for v in value)
    return result


def redact(text):
    """把文本里的联系方式替换成"[联系方式已省略]"。发给模型的文本都要先过一遍。"""
    return CONTACT.sub("[联系方式已省略]", text)


def _string(value, limit, *, empty=False):
    """是否为长度不超过 limit 的字符串；empty=False 时还要求不能全是空白。"""
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))


def _safe_parts(text):
    """跳过联系方式，把文本拆成若干段，逐段返回 (起始位置, 去掉首尾空白的原文)。

    不做替换，只是避开：这样每一段都还是原文里连续的一段，可以原样引用。
    """
    cursor = 0
    for match in CONTACT.finditer(text):
        part = text[cursor : match.start()]
        if part.strip():
            yield cursor + len(part) - len(part.lstrip()), part.strip()
        cursor = match.end()
    part = text[cursor:]
    if part.strip():
        yield cursor + len(part) - len(part.lstrip()), part.strip()


_MONEY_LABEL_PATTERN = "|".join(sorted(map(re.escape, MONEY_LABELS), key=len, reverse=True))
_CURRENCY_PATTERN = r"人民币|美元|美金|欧元|港元|港币|日元|日圆|英镑|CNY|RMB|USD|EUR|HKD|JPY|GBP"
_CURRENCIES = {
    "人民币": "CNY",
    "RMB": "CNY",
    "美元": "USD",
    "美金": "USD",
    "欧元": "EUR",
    "港元": "HKD",
    "港币": "HKD",
    "日元": "JPY",
    "日圆": "JPY",
    "英镑": "GBP",
}


def _structured_money(value, role):
    """把规范化数据里的结构化金额转成 (角色, 数值, 原单位, 币种)。

    规范化数据统一按"元"存 amount；这里换回原文用的单位（元或万元），
    因为报告只能照原文的单位写，不允许自己换算。数据不完整时返回 None。
    """
    if (
        role not in MONEY_ROLES
        or not isinstance(value, dict)
        or value.get("currency") != "CNY"
        or value.get("source_unit") not in ("元", "万元")
        or not isinstance(value.get("amount"), str)
        or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value["amount"])
    ):
        return None
    number = Decimal(value["amount"])
    # 用 Decimal.scaleb 移动小数点，避免浮点误差。
    with localcontext() as context:
        context.prec = max(28, len(value["amount"]) + 4)
        number = number.scaleb(-4) if value["source_unit"] == "万元" else number
    return role, number, value["source_unit"], "CNY"


def _money_mentions(text, *, owner=None, report=False):
    """从文本里找出"金额标签 + 金额"形式的明确说法，返回 {(角色, 数值, 单位, 币种)}。

    例子：
        "预算金额：48万元"          -> {("budget", 48, "万元", "CNY")}
        "预算和最高限价均为48万元"   -> 预算、最高限价各一条
    规则：
        - 数额相同但角色不同，算两件事。
        - 只去掉数值末尾的 0，不换算单位；写了外币就按外币，不默认人民币。
        - owner="company" 只要企业自己的钱（"公司预算"），owner="procurement" 只要采购方的钱。
        - 原文里的"X 以内""X 至 Y""约 X"不是精确金额，单位记为 None，不能拿来支持报告里的"预算 X"。
        - report=True 时，报告里的"在限价 X 以内"只检查 X 本身有没有依据，比较关系交给语义核验。
    """
    labels = (
        r"(?P<labels>(?:" + _MONEY_LABEL_PATTERN + r")(?:\s*(?:、|和|与|及|/)\s*(?:" + _MONEY_LABEL_PATTERN + r"))*)"
    )
    pattern = (
        labels + r"\s*(?:[（(]\s*(?P<label_unit>万元|元)(?:人民币)?\s*[）)])?"
        r"\s*(?:[（(]如有[）)])?\s*(?:[:：|]\s*)?(?:(?:均)?(?:为|是)\s*)?"
        r"(?P<currency>" + _CURRENCY_PATTERN + r")?\s*[￥¥]?\s*"
        r"(?P<number>[+-]?\d+(?:\.\d+)?)(?![\d.])\s*(?P<unit>亿元|万元|元)?"
        r"\s*(?:[（(]?\s*(?P<suffix>" + _CURRENCY_PATTERN + r")\s*[）)]?)?"
    )
    result = set()
    for match in re.finditer(pattern, text, re.I):
        # "公司预算""企业自有预算"是企业自己的钱；"采购预算""项目预算"是采购方的钱。
        company = re.search(
            r"(?:本公司|本企业|公司|企业)(?:自有|内部|可用)?\s*$", text[: match.start()]
        ) is not None and not re.search(r"采购|项目", match["labels"])
        if owner == "company" and not company or owner == "procurement" and company:
            continue
        unit = match["unit"] or match["label_unit"]
        currencies = {_CURRENCIES.get(raw.upper(), raw.upper()) for raw in (match["suffix"], match["currency"]) if raw}
        currency = next(iter(currencies)) if len(currencies) == 1 else "ambiguous" if currencies else "CNY"
        # 后面跟着"至/~/左右/以上"之类的，说明这是区间或约数，不当作精确金额。
        tail = text[match.end() :]
        if (
            re.match(r"(?:[-~～—至到余多]|\.(?=\s*\d)|左右|上下|[/／])", tail.lstrip())
            or not report
            and re.match(r"以上|以下|以内", tail.lstrip())
        ):
            unit = None
        for label in re.findall(_MONEY_LABEL_PATTERN, match["labels"]):
            result.add((MONEY_LABELS[label], Decimal(match["number"]), unit, currency))
    return result


def _category(path):
    """根据证据在规范化数据里的字段路径，判断它属于哪一类要求。"""
    if "qualification" in path:
        return "qualification"
    if "technical" in path or "category_evidence" in path:
        return "technical"
    if "delivery" in path:
        return "delivery"
    if "budget" in path or "money" in path:
        return "budget"
    if any(word in path for word in ("deadline", "published", "acquisition_window")):
        return "deadline"
    if any(word in path for word in ("material", "acquisition_conditions")):
        return "materials"
    return "other"


def _terms(text):
    """把文本拆成检索用的词：英文数字按单词，中文按相邻两个字（二元组）。"""
    result = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(span[i : i + 2] for i in range(len(span) - 1))
    return result


class EvidenceIndex:
    """一次研究可用的全部原文片段。

    只包含本任务 manifest 里冻结的公告观察。别的项目哪怕内容很像，也永远检索不到。
    每个片段都有一个由内容算出来的 evidence_id，报告里的引用必须是这些 ID。
    构造时会严格检查 manifest 的结构，不合格就抛 EvidenceError。
    """

    def __init__(self, manifest):
        if (
            not isinstance(manifest, dict)
            or type(manifest.get("schema_version")) is not int
            or manifest["schema_version"] != 1
        ):
            raise EvidenceError("invalid_manifest")
        try:
            raw = canonical(manifest)
        except (ValueError, TypeError, RecursionError):
            raise EvidenceError("invalid_manifest") from None
        if len(raw.encode("utf-8")) > 2_000_000:
            raise EvidenceError("manifest_too_large")
        self.manifest_digest = digest(manifest)
        self.manifest = deepcopy(manifest)
        self.scope_id = manifest.get("scope_notice_id")
        observations, profile = manifest.get("observations"), manifest.get("profile")
        if (
            not isinstance(self.scope_id, str)
            or not HEX.fullmatch(self.scope_id)
            or any(not _string(manifest.get(field), 128) for field in ("workspace_id", "actor_id"))
            or not isinstance(observations, list)
            or not 1 <= len(observations) <= 50
            or not isinstance(profile, dict)
            or not isinstance(profile.get("payload"), dict)
            or type(profile.get("revision")) is not int
            or profile["revision"] < 1
            or profile.get("workspace_id") != manifest.get("workspace_id")
            or set(profile["payload"]) != PROFILE_FIELDS
            or any(value is not None and not _string(value, 4000, empty=True) for value in profile["payload"].values())
            or type(manifest.get("catalog_snapshot")) is not int
            or manifest["catalog_snapshot"] < 0
            or not isinstance(manifest.get("config"), dict)
            or manifest.get("kind") not in ("analysis", "question", "reassessment")
        ):
            raise EvidenceError("invalid_manifest")
        question = manifest.get("question")
        if question is not None and not _string(question, 2000):
            raise EvidenceError("invalid_question")
        if manifest["kind"] == "question" and question is None:
            raise EvidenceError("invalid_question")
        previous = manifest.get("previous_report")
        if previous is not None and (
            not isinstance(previous, dict)
            or not isinstance(previous.get("scope"), dict)
            or previous["scope"].get("notice_id") != self.scope_id
        ):
            raise EvidenceError("previous_report_scope_mismatch")
        self.profile = {key: redact(value) if value is not None else None for key, value in profile["payload"].items()}
        self.entries, self.by_id, self.money_claims, seen_observations = [], {}, [], set()
        for position, observation in enumerate(observations):
            if not isinstance(observation, dict) or any(
                not isinstance(observation.get(key), str) or not HEX.fullmatch(observation[key])
                for key in ("notice_id", "observation_id", "raw_sha256")
            ):
                raise EvidenceError("invalid_observation")
            if position == 0 and observation["notice_id"] != self.scope_id:
                raise EvidenceError("manifest_scope_mismatch")
            if observation["observation_id"] in seen_observations:
                raise EvidenceError("duplicate_observation")
            seen_observations.add(observation["observation_id"])
            for root in ("facts", "evidence_fields", "material_reference_evidence"):
                self._walk(observation.get(root, {}), root, observation)
        self.observation_ids = [x["observation_id"] for x in observations]
        self.scope = {
            "notice_id": self.scope_id,
            "observation_ids": self.observation_ids,
            "profile_revision": profile["revision"],
            "catalog_snapshot": manifest["catalog_snapshot"],
        }

    def _walk(self, value, path, observation, depth=0, money_role=None):
        """递归遍历一条观察的规范化数据，把每个 {text, locator} 节点切成片段放进索引。"""
        if depth > 12:
            raise EvidenceError("evidence_too_deep")
        if isinstance(value, dict):
            # 金额角色只从两个固定位置读取：evidence_fields.money.N 和 facts.budget。
            # 不能因为路径里碰巧有 "budget" 字样，就把别的金额当成预算。
            structured = None
            if re.fullmatch(r"evidence_fields\.money\.\d+", path):
                raw_role = value.get("role")
                money_role = (
                    raw_role
                    if isinstance(raw_role, str)
                    and raw_role in MONEY_ROLES | {"package_budget", "unit_price", "deposit"}
                    else "unknown"
                    if "role" in value
                    else None
                )
                if value.get("status") == "parsed" and not value.get("package") and not value.get("unit_basis"):
                    structured = _structured_money(value, money_role)
            elif path == "facts.budget" and value.get("status") == "known":
                money_role = "budget"
                structured = _structured_money(value.get("value"), money_role)
            first_entry = len(self.entries)
            if "text" in value and "locator" in value:
                text, locator = value["text"], value["locator"]
                if not _string(text, 200_000, empty=True) or not _string(locator, 512):
                    raise EvidenceError("invalid_evidence")
                # 跳过联系方式，再按 2400 字切段。每段都是原文里连续的一段，不改写任何字。
                for offset, part in _safe_parts(text):
                    for start in range(0, len(part), 2400):
                        snippet = part[start : start + 2400]
                        entry = {
                            "notice_id": observation["notice_id"],
                            "observation_id": observation["observation_id"],
                            "raw_sha256": observation["raw_sha256"],
                            "locator": locator,
                            "field": path,
                            "category": _category(path),
                            "text": snippet,
                            "offset": offset + start,
                        }
                        if entry["category"] == "qualification":
                            # 资格条款要逐字摘录。被切段或删过联系方式的条款不完整（可能丢了后半段的替代条件），
                            # 标记为不可摘录，不能当成完整条款引用。
                            entry["qualification_extractable"] = (
                                len(text.strip()) <= 2400 and CONTACT.search(text) is None
                            )
                        # 带上字段标签（如"预算金额"），模型才知道"48万元"指的是哪种钱。
                        label = value.get("label")
                        if _string(label, 512):
                            entry["label"] = redact(label)
                        if money_role is not None:
                            entry["money_role"] = money_role
                        entry["evidence_id"] = digest(entry)
                        if entry["evidence_id"] not in self.by_id:
                            self.entries.append(entry)
                            self.by_id[entry["evidence_id"]] = entry
                        if len(self.entries) > 1000 or sum(len(x["text"]) for x in self.entries) > 500_000:
                            raise EvidenceError("evidence_limit")
                return
            for key, item in value.items():
                self._walk(item, path + "." + str(key), observation, depth + 1, money_role)
            if structured is not None and len(self.entries) > first_entry:
                # 表格里的金额常常是"表头写单位、表行写数字"，要整组一起引用才算有依据。
                self.money_claims.append((structured, frozenset(e["evidence_id"] for e in self.entries[first_entry:])))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._walk(item, path + "." + str(i), observation, depth + 1, money_role)

    def structured_money_support(self, ids):
        """返回这些引用能完整支持的结构化金额（表头和表行都被引用了才算）。"""
        ids = set(ids)
        return {claim for claim, required in self.money_claims if required.issubset(ids)}

    def money_support(self, ids):
        """返回这些引用能支持的全部采购金额。

        只看传进来的这些 ID：企业自己的钱、别的结论的引用、读过但没引用的片段，都不算。
        """
        ids = set(ids)
        result = self.structured_money_support(ids)
        for eid in ids:
            entry = self.by_id[eid]
            # 分包预算、单价、保证金这类片段跳过，免得因为原文里有"预算"二字被当成项目总预算。
            if entry.get("money_role") not in MONEY_ROLES | {None}:
                continue
            result.update(
                claim for claim in _money_mentions(entry["text"], owner="procurement") if claim[2] is not None
            )
            label = entry.get("label", "").strip().rstrip(":：")
            if label in MONEY_LABELS or re.fullmatch(r"(?:" + _MONEY_LABEL_PATTERN + r")[（(](?:万元|元)[）)]", label):
                result.update(claim for claim in _money_mentions(label + "：" + entry["text"]) if claim[2] is not None)
        return result

    def search(self, query, category=None, limit=6):
        """只返回命中的片段列表。Agent 工具用的是信息更全的 search_result。"""
        return self.search_result(query, category, limit)["evidence"]

    @property
    def category_available_count(self):
        """每个类别各有多少片段。告诉模型"这一类有材料"，避免它一次没搜到就断定没有要求。"""
        return {
            category: sum(entry["category"] == category for entry in self.entries) for category in sorted(CATEGORIES)
        }

    def search_result(self, query, category=None, limit=6):
        """按关键词在片段里检索，返回命中的片段和检索情况。

        打分很简单：中文二元组重合的个数，整句出现再加 5 分。
        指定了类别但关键词一个都没命中时，退回返回该类别的全部片段（match_mode 会写明），
        因为原文措辞可能和关键词不同，零命中不代表没有这项要求。
        query 为空字符串时表示"浏览这个类别"，必须同时指定类别。
        """
        if (
            not _string(query, 200, empty=True)
            or not query.strip()
            and category is None
            or category is not None
            and (not isinstance(category, str) or category not in CATEGORIES)
            or type(limit) is not int
            or not 1 <= limit <= 8
        ):
            raise EvidenceError("invalid_search")
        query = query.strip()
        terms = _terms(query)
        ranked, candidates = [], []
        for entry in self.entries:
            if category is not None and entry["category"] != category:
                continue
            candidates.append(entry)
            score = len(terms & _terms(entry["text"])) + (5 if query and query.lower() in entry["text"].lower() else 0)
            if score:
                ranked.append((score, entry["evidence_id"], entry))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        if ranked:
            selected, mode = [x[2] for x in ranked], "keyword"
        elif category is not None:
            selected, mode = candidates, "category_fallback" if query else "category_browse"
        else:
            selected, mode = [], "no_match"
        return {
            "evidence": deepcopy(selected[:limit]),
            "match_mode": mode,
            "category": category,
            "keyword_match_count": len(ranked),
            "category_available_count": self.category_available_count,
            "selected_category_available_count": len(candidates) if category is not None else None,
            "truncated": len(selected) > limit,
            "retrieval_note": "零关键词命中不代表要求不存在。分类回退/浏览是原始候选片段，仍须逐条判断；可用空query浏览指定分类。",
        }

    def read(self, ids):
        """按 ID 读取片段原文，一次 1–8 个。ID 不在本任务范围内就报错。"""
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 8
            or any(not isinstance(x, str) for x in ids)
            or len(set(ids)) != len(ids)
            or any(x not in self.by_id for x in ids)
        ):
            raise EvidenceError("evidence_not_in_manifest")
        return [deepcopy(self.by_id[x]) for x in ids]

    def coverage(self, read_ids):
        """说明这次研究实际看了哪些材料：只有规范化片段，没有读完整招标文件。"""
        return {
            "mode": "normalized_snippets",
            "full_tender_read": False,
            "observation_ids": self.observation_ids,
            "evidence_ids": sorted(set(read_ids)),
            "available_evidence_count": len(self.entries),
            "material_status": [str(x.get("material_status", "unknown"))[:200] for x in self.manifest["observations"]],
        }


def _unsupported_completeness(text):
    """报告是否声称了做不到的事：读完了全部标书、满足全部资格、给出中标概率等。

    "未读完整标书""不预测中标概率"这类否定说法不算。
    否定只在同一分句内生效：前一句的免责声明不能替后一句的实际承诺开脱。
    更复杂的含义交给语义核验。
    """
    claims = re.compile(
        r"(?:已经|已|全部|完整)(?:完整|全部)?(?:阅读|读完|审阅|取得|读).{0,10}(?:全文|标书|招标文件|采购文件|PDF全文)"
        r"|(?:全文|全部标书)(?:已读|已阅读|已审阅)"
        r"|(?:全部|所有)(?:资格条件|资格|条件)(?:均|都)?(?:已)?满足"
        r"|(?:已|已经)满足(?:全部|所有)(?:资格|条件)|中标概率",
        re.I,
    )
    before_negative = re.compile(
        r"(?:不|未|没有)(?:会|能|可|应)?(?:承诺|保证|包含|提供|计算|评估|预测|估算|宣称|声称|认定|确认|判断|给出|输出|涉及)(?:任何|具体|精确)?$"
        r"|(?:并非|不是|不代表|不意味着|不等于|不能据此认为|不能认定|未能确认|尚无法确认)$"
    )
    after_negative = re.compile(r"^(?:暂不|不|未|尚未|无法|不能)(?:予以|予)?(?:提供|计算|评估|预测|估算|给出|确认)")
    for clause in re.split(r"[，,。；;！!？?\n]", text):
        for match in claims.finditer(clause):
            before, after = clause[: match.start()].rstrip(), clause[match.end() :].lstrip()
            direct_unread = re.search(r"(?:尚未|还未|未曾|从未|未)$", before)
            if direct_unread and not re.search(r"(?:否认|并非|不是|不承认|不能说)$", before[: direct_unread.start()]):
                continue
            if not before_negative.search(before) and not after_negative.search(after):
                return True
    return False


def _numbers(text):
    """提取文本里出现的所有数值（用 Decimal 表示，48 和 48.00 视为同一个数）。

    先把"2026-10-08""09:00-17:00"这类日期、时间段里的连字符换成空格，
    免得"-08"被读成负数。其他地方的负号保留：负数和正数是不同的值。
    """
    text = re.sub(r"(?<!\d)(\d{4})\s*-\s*(0?[1-9]|1[0-2])\s*-\s*(0?[1-9]|[12]\d|3[01])(?!\d)", r"\1 \2 \3", text)
    clock = r"(?:[01]?\d|2[0-3])[:：][0-5]\d(?:[:：][0-5]\d)?"
    text = re.sub(r"(?<!\d)(" + clock + r")\s*-\s*(" + clock + r")(?!\d)", r"\1 \2", text)
    return {Decimal(value) for value in re.findall(r"(?<!\d)[+-]?\d+(?:\.\d+)?", text)}


def _number_units(text):
    """提取"数值 + 单位"组合，如 (48, "万元")、(60, "天")。

    用来检查报告有没有偷换单位：原文"48万元"不能写成"48元"，"60天"不能写成"60小时"。
    只把"日"当成"天"、全角"％"当成"%"，不做万元和元之间的换算。
    日期（"10月8日"）和日期范围（"10月8日至13日"）会先去掉，免得"13日"被当成 13 天；
    但"13日内交付"是期限，会保留。
    """
    month, day = r"(?:0?[1-9]|1[0-2])", r"(?:0?[1-9]|[12]\d|3[01])"
    start = r"(?:\d{4}\s*年\s*)?" + month + r"\s*月\s*" + day + r"\s*[日号]?"
    end = r"(?:\d{4}\s*年\s*)?(?:" + month + r"\s*月\s*)?" + day + r"\s*[日号]"
    # 先去掉整个日期范围，再去掉单个日期。
    without_dates = re.sub(
        r"(?<!\d)" + start + r"\s*(?:至|到|[-－—~～])\s*" + end + r"(?!\s*(?:内|以内|之内))", "", text
    )
    without_dates = re.sub(r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]", "", without_dates)
    return {
        (Decimal(number), {"日": "天", "％": "%"}.get(unit, unit))
        for number, unit in re.findall(
            r"(?<!\d)([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|元|小时|分钟|天|日|周|%|％)", without_dates
        )
    }


def _without_cited_ids(text, cited_ids):
    """数字检查前，把文本里已引用的证据 ID 换成占位符，返回 (清理后的文本, 是否出现了未引用的 ID)。

    证据 ID 是 64 位十六进制，里面的数字不是事实，不该参与数字检查。
    但没被本报告引用的 ID 要报出来；ID 后面紧跟金额或时长单位时也照常检查。
    不改动报告本身。
    """
    unknown = False

    def replace(match):
        nonlocal unknown
        if match.group() not in cited_ids:
            unknown = True
            return match.group()
        if re.match(r"\s*(?:亿元|万元|元|小时|分钟|天|日|周|%|％)", text[match.end() :]):
            return match.group()
        return "[已引用证据]"

    clean = re.sub(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{64}(?![A-Za-z0-9_])", replace, text)
    return clean, unknown


def answer_reason_indices(report):
    """追问的回答必须由本报告里 1–3 条完整的理由按顺序拼成。找到就返回这些理由的下标，否则返回 None。

    为什么这么严：以前让模型自由写回答，它常在第二次概括时扩大或丢掉原文条件。
    比较时忽略空白差异。拼对了只说明文字来源对，理由本身是否正确仍要语义核验。
    """
    answer, findings = report.get("answer"), report.get("findings")
    if (
        not _string(answer, 3000)
        or not isinstance(findings, list)
        or not 1 <= len(findings) <= 30
        or any(not isinstance(f, dict) or not _string(f.get("reason"), 1600) for f in findings)
    ):
        return None
    text = " ".join(answer.split())
    reasons = [" ".join(f["reason"].split()) for f in findings]
    for count in range(1, min(3, len(reasons)) + 1):
        for positions in combinations(range(len(reasons)), count):
            selected = [reasons[i] for i in positions]
            if len(set(selected)) == count and text == " ".join(selected):
                return list(positions)
    return None


def question_finding_indices(report):
    """报告里的 questions（待核查清单）每一项都必须是某条结论的 unknown_reason 原文。

    找到就返回对应结论的下标列表，否则返回 None。
    字段名虽然叫 questions，实际展示的是"还缺什么要核查"，不要求写成问句。
    最多 12 项、顺序随意、不能重复；超过 600 字的说明不能截短放进来。
    """
    questions, findings = report.get("questions"), report.get("findings")
    if (
        not isinstance(questions, list)
        or len(questions) > 12
        or any(not _string(q, 600) for q in questions)
        or not isinstance(findings, list)
        or len(findings) > 30
    ):
        return None
    gaps = {}
    for i, finding in enumerate(findings):
        if isinstance(finding, dict) and _string(finding.get("unknown_reason"), 800):
            gaps.setdefault(" ".join(finding["unknown_reason"].split()), i)
    normalized = [" ".join(q.split()) for q in questions]
    if len(set(normalized)) != len(normalized) or any(q not in gaps for q in normalized):
        return None
    return [gaps[q] for q in normalized]


def validate_report(report, index, read_ids):
    """用程序检查一份候选报告，返回错误码列表；空列表表示通过。

    检查内容：
        - 结构和长度：字段齐全、类型正确、不超长。
        - 引用：每条结论都要有引用，引用必须是本任务里、并且模型实际读过的片段。
        - 资格：只能引用一条完整的资格原文，requirement 逐字照抄；状态只能是未知，理由用固定句子。
        - 满足/不满足：必须关联非空的企业档案字段。
        - 金额：按角色检查，每个金额都要有本条引用里相同角色、相同数值和单位的原文。
        - 数字和单位：结论、摘要、回答里出现的数字都要能在引用原文或档案里找到。
        - 其他：不能含联系方式，不能声称读完全部标书或满足全部资格，摘要里不能堆内部 ID。

    通过只说明"格式和引用对得上"，不说明结论正确，语义要另做核验。
    只返回错误码，不带模型或原文的文字，免得不可信内容进入修订请求。
    """
    errors = []
    required = {"summary", "findings", "questions", "answer"}
    if not isinstance(report, dict) or set(report) != required:
        return ["report_schema"]
    if (
        not _string(report["summary"], 1800)
        or not isinstance(report["findings"], list)
        or not 0 <= len(report["findings"]) <= 30
        or not isinstance(report["questions"], list)
        or len(report["questions"]) > 12
        or any(not _string(x, 600) for x in report["questions"])
        or report["answer"] is not None
        and not _string(report["answer"], 3000)
    ):
        return ["report_schema"]
    if index.manifest["kind"] == "question":
        if report["answer"] is None:
            errors.append("question_answer_missing")
        elif answer_reason_indices(report) is None:
            errors.append("answer_not_from_current_reasons")
    elif report["answer"] is not None:
        errors.append("unexpected_answer")
    if question_finding_indices(report) is None:
        errors.append("questions_not_from_current_unknown_reasons")
    if CONTACT.search(canonical(report)):
        errors.append("report_contact_information")
    if _unsupported_completeness(canonical(report)):
        errors.append("unsupported_completeness")
    if re.search(
        r"notice_id|observation_id|raw_sha256|\brevision\b|profile_revision|catalog_snapshot|category_available_count",
        report["summary"],
        re.I,
    ):
        errors.append("summary_contains_metadata")
    supported_numbers, supported_units, supported_company_money, cited_ids = set(), set(), set(), set()
    for i, finding in enumerate(report["findings"]):
        prefix = "finding_" + str(i) + "_"
        if (
            not isinstance(finding, dict)
            or set(finding)
            != {"category", "requirement", "status", "reason", "evidence_ids", "profile_fields", "unknown_reason"}
            or not isinstance(finding["category"], str)
            or finding["category"] not in CATEGORIES
            or not isinstance(finding["status"], str)
            or finding["status"] not in STATUSES
            or not _string(finding["requirement"], 2400)
            or not _string(finding["reason"], 1600)
            or not isinstance(finding["evidence_ids"], list)
            or len(finding["evidence_ids"]) > 12
            or any(not isinstance(x, str) for x in finding["evidence_ids"])
            or len(set(finding["evidence_ids"])) != len(finding["evidence_ids"])
            or not isinstance(finding["profile_fields"], list)
            or len(finding["profile_fields"]) > 9
            or any(not isinstance(x, str) or x not in PROFILE_FIELDS for x in finding["profile_fields"])
            or finding["unknown_reason"] is not None
            and not _string(finding["unknown_reason"], 800)
        ):
            errors.append(prefix + "schema")
            continue
        ids = finding["evidence_ids"]
        if any(x not in index.by_id or x not in read_ids for x in ids):
            errors.append(prefix + "citation_scope_or_unread")
            continue
        # 每条结论都要有原文依据。"还缺哪些材料"由服务器写进 limitations，不要写成结论。
        if not ids:
            errors.append(prefix + "missing_evidence")
        qualification_ids = [eid for eid in ids if index.by_id[eid]["category"] == "qualification"]
        if finding["category"] == "qualification" or qualification_ids:
            # 只要引用了资格原文就按资格规则检查：改个类别也绕不过去。
            if finding["category"] != "qualification":
                errors.append(prefix + "qualification_category_mismatch")
            if len(ids) != 1 or len(qualification_ids) != 1:
                errors.append(prefix + "qualification_single_evidence_required")
            else:
                entry = index.by_id[ids[0]]
                if not entry["qualification_extractable"]:
                    errors.append(prefix + "qualification_incomplete_source")
                # 忽略空白差异后必须和原文完全一样，子串也不行。
                if " ".join(finding["requirement"].split()) != " ".join(entry["text"].split()):
                    errors.append(prefix + "qualification_exact_quote_required")
        if finding["status"] == "unknown" and not finding["unknown_reason"]:
            errors.append(prefix + "missing_unknown_reason")
        if finding["category"] == "qualification" and finding["status"] == "unknown":
            if (
                " ".join(finding["reason"].split()) != QUALIFICATION_REASON
                or " ".join((finding["unknown_reason"] or "").split()) != QUALIFICATION_UNKNOWN_REASON
            ):
                errors.append(prefix + "qualification_unverified_text_required")
        if finding["status"] in ("met", "unmet"):
            fields = finding["profile_fields"]
            if not fields or any(not index.profile[field] for field in fields):
                errors.append(prefix + "missing_profile")
            if finding["category"] == "qualification":
                # 档案只是声明，没有证明材料，资格不能判满足或不满足。
                errors.append(prefix + "qualification_not_verified")
        evidence_text = " ".join(index.by_id[x]["text"] for x in ids)
        cited_ids.update(ids)
        profile_text = " ".join(index.profile[x] or "" for x in finding["profile_fields"])
        finding_text = " ".join(finding[field] or "" for field in ("requirement", "reason", "unknown_reason"))
        # 金额检查：采购方的钱看本条引用，企业的钱看本条关联的档案字段，两者不能互相借。
        missing_roles = {
            claim[0]
            for claim in _money_mentions(finding_text, owner="procurement", report=True) - index.money_support(ids)
        }
        errors.extend(prefix + "unsupported_money_role_" + role for role in sorted(missing_roles))
        company_money = _money_mentions(profile_text)
        supported_company_money.update(company_money)
        missing_company = {
            claim[0] for claim in _money_mentions(finding_text, owner="company", report=True) - company_money
        }
        errors.extend(prefix + "unsupported_company_money_role_" + role for role in sorted(missing_company))
        structured = index.structured_money_support(ids)
        number_pool = _numbers(evidence_text + " " + profile_text)
        number_pool.update(claim[1] for claim in structured)
        supported_numbers.update(number_pool)
        unit_pool = _number_units(evidence_text + " " + profile_text)
        # 表格金额：引用了完整的表头和表行，才能用"表头单位 + 表行数值"作为依据。
        unit_pool.update((claim[1], claim[2]) for claim in structured)
        supported_units.update(unit_pool)
        mentioned = _numbers(finding_text)
        if not mentioned.issubset(number_pool):
            errors.append(prefix + "unsupported_number")
        if not _number_units(finding_text).issubset(unit_pool):
            errors.append(prefix + "unsupported_numeric_unit")
    # 摘要和追问回答里的数字，也必须来自各条结论的引用或档案。
    for field in ("summary", "answer"):
        text = report[field] or ""
        missing_roles = {
            claim[0]
            for claim in _money_mentions(text, owner="procurement", report=True) - index.money_support(cited_ids)
        }
        errors.extend(field + "_unsupported_money_role_" + role for role in sorted(missing_roles))
        missing_company = {
            claim[0] for claim in _money_mentions(text, owner="company", report=True) - supported_company_money
        }
        errors.extend(field + "_unsupported_company_money_role_" + role for role in sorted(missing_company))
        assertions, unknown_id = _without_cited_ids(text, cited_ids)
        if unknown_id:
            errors.append(field + "_unsupported_citation_id")
        if not _numbers(assertions).issubset(supported_numbers):
            errors.append(field + "_unsupported_number")
        if not _number_units(assertions).issubset(supported_units):
            errors.append(field + "_unsupported_numeric_unit")
    return errors


def finalize_report(proposal, index, read_ids, *, limitations=None):
    """给通过核验的报告补上研究范围、材料覆盖、限制说明和全部已读原文，得到最终发布的报告。"""
    result = deepcopy(proposal)
    result["scope"] = deepcopy(index.scope)
    result["coverage"] = index.coverage(read_ids)
    result["limitations"] = [
        "本次研究输入为规范化证据片段，未阅读完整招标文件；这不表示系统全局未获取或未归档原件。",
        "企业档案为用户确认声明，未独立验证资质证明。",
    ] + list(limitations or [])
    result["references"] = [deepcopy(index.by_id[x]) for x in sorted(set(read_ids))]
    return result


def baseline_report(manifest):
    """规则基线：不调模型，把前 24 个片段原样列出，全部标为"未知、待核查"。

    两个用途：一是和 Agent 做对照评测；二是 Agent 没通过核验时，用它作为保守的降级报告。
    """
    index = EvidenceIndex(manifest)
    selected = index.entries[:24]
    findings = [
        {
            "category": e["category"],
            "requirement": e["text"] if e["category"] == "qualification" else e["text"][:1200],
            "status": "unknown",
            "reason": QUALIFICATION_REASON
            if e["category"] == "qualification"
            else "规则基线仅列出可得原文，尚未完成语义匹配或证明核验。",
            "evidence_ids": [e["evidence_id"]],
            "profile_fields": [],
            "unknown_reason": QUALIFICATION_UNKNOWN_REASON
            if e["category"] == "qualification"
            else "需核对要求与企业证明，当前不判定满足或不满足。",
        }
        for e in selected
    ]
    proposal = {
        "summary": "规则基线：资料待核查，不构成参与资格结论。",
        "findings": findings,
        "questions": list(dict.fromkeys(f["unknown_reason"] for f in findings if f["unknown_reason"]))[:12],
        "answer": findings[0]["reason"] if manifest.get("question") and findings else None,
    }
    limitations = (
        ["资格来源经长度或隐私处理形成片段，基线展示不代表完整资格条款。"]
        if any(e["category"] == "qualification" and not e["qualification_extractable"] for e in selected)
        else []
    )
    return finalize_report(proposal, index, [e["evidence_id"] for e in selected], limitations=limitations)

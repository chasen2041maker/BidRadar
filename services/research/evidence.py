"""冻结观察的证据索引与报告校验；只处理已授权输入，不联网、不读取原件库。

阅读顺序：EvidenceIndex → search/read → validate_report。证据仍是规范化片段，
PDF 已归档不能变成全文已读；去除联系信息后的每个片段仍是原文连续子串。
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import math
import re

VERSION = "frozen-evidence-v10-money-comparison-references"
HEX = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_FIELDS = frozenset(("company_name", "city", "project_types", "capabilities", "delivery_constraints",
                            "cases", "qualifications", "staffing", "commercial_constraints"))
CATEGORIES = frozenset(("technical", "qualification", "delivery", "commercial", "budget", "deadline", "materials", "other"))
STATUSES = frozenset(("met", "unmet", "unknown", "conflicting", "not_applicable"))
# 与规范化money.role保持同名；这里只保护明确金额断言，不代替上下文语义核验。
MONEY_LABELS = {"预算金额": "budget", "项目预算": "budget", "采购预算": "budget", "预算": "budget",
                "最高投标限价": "ceiling", "最高限价": "ceiling", "限价": "ceiling",
                "招标文件售价": "file_fee", "采购文件售价": "file_fee", "文件售价": "file_fee",
                "售价": "file_fee", "文件费": "file_fee"}
MONEY_ROLES = frozenset(MONEY_LABELS.values())
CONTACT = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|"
                     r"(?:联系人|联系电话|联系手机|电子邮箱|电子邮件|联系方式|项目联系人|电话|手机|邮箱)(?!已省略)(?:\s*[:：]\s*|\s+)"
                     r"(?:(?!预算|金额|截止|交付|采购|合同|期限|获取|响应)[^；;。\n]){0,100}|"
                     r"(?<![A-Za-z0-9.])(?:\+?86[- ]?)?1[3-9]\d[- ]?\d{4}[- ]?\d{4}(?![A-Za-z0-9.]|\s*(?:元|万元|亿元|人民币|CNY))|"
                     r"(?<![A-Za-z0-9.])(?:[（(]0\d{2,3}[)）]|0\d{2,3}[-－ ])[-－ ]?\d{7,8}(?![A-Za-z0-9.]|\s*(?:元|万元|亿元|人民币|CNY))")


class EvidenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def strict_json(text, *, limit=512_000):
    """模型/恢复 JSON 不是可信对象：拒绝重复键、非有限数、过深和过大的结构。"""
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
    """外发普通文本去联系方式；金额带货币单位时不当成手机号删除。"""
    return CONTACT.sub("[联系方式已省略]", text)


def _string(value, limit, *, empty=False):
    return isinstance(value, str) and len(value) <= limit and (empty or bool(value.strip()))


def _safe_parts(text):
    cursor = 0
    for match in CONTACT.finditer(text):
        part = text[cursor:match.start()]
        if part.strip():
            yield cursor + len(part) - len(part.lstrip()), part.strip()
        cursor = match.end()
    part = text[cursor:]
    if part.strip():
        yield cursor + len(part) - len(part.lstrip()), part.strip()


_MONEY_LABEL_PATTERN = "|".join(sorted(map(re.escape, MONEY_LABELS), key=len, reverse=True))
_CURRENCY_PATTERN = r"人民币|美元|美金|欧元|港元|港币|日元|日圆|英镑|CNY|RMB|USD|EUR|HKD|JPY|GBP"
_CURRENCIES = {"人民币": "CNY", "RMB": "CNY", "美元": "USD", "美金": "USD", "欧元": "EUR",
               "港元": "HKD", "港币": "HKD", "日元": "JPY", "日圆": "JPY", "英镑": "GBP"}


def _structured_money(value, role):
    """规范化amount以元存储；还原其source_unit数值，不授权报告自行换算单位。"""
    if (role not in MONEY_ROLES or not isinstance(value, dict) or value.get("currency") != "CNY"
            or value.get("source_unit") not in ("元", "万元") or not isinstance(value.get("amount"), str)
            or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value["amount"])):
        return None
    number = Decimal(value["amount"])
    # scaleb移动十进制位，不做浮点近似；规范化器的金额长度本已受manifest大小限制。
    with localcontext() as context:
        context.prec = max(28, len(value["amount"]) + 4)
        number = number.scaleb(-4) if value["source_unit"] == "万元" else number
    return role, number, value["source_unit"], "CNY"


def _money_mentions(text, *, owner=None, report=False):
    """仅识别标签紧邻金额的明确断言；未知/疑问或更复杂指代仍交给语义核验。

    同额不等口径；并列“预算和最高限价均为48万元”要分别取得两个角色。
    数值只消除尾零，不换单位；外币标记也不能被默认人民币吞掉。
    report中“在限价X以内”等只检查被比较金额的角色/数值，不证明比较关系成立；
    来源的上下界仍不是精确金额，不能用来源“预算X以内”支持报告“预算X”。
    """
    labels = r"(?P<labels>(?:" + _MONEY_LABEL_PATTERN + r")(?:\s*(?:、|和|与|及|/)\s*(?:" + _MONEY_LABEL_PATTERN + r"))*)"
    pattern = (labels + r"\s*(?:[（(]\s*(?P<label_unit>万元|元)(?:人民币)?\s*[）)])?"
               r"\s*(?:[（(]如有[）)])?\s*(?:[:：|]\s*)?(?:(?:均)?(?:为|是)\s*)?"
               r"(?P<currency>" + _CURRENCY_PATTERN + r")?\s*[￥¥]?\s*"
               r"(?P<number>[+-]?\d+(?:\.\d+)?)(?![\d.])\s*(?P<unit>亿元|万元|元)?"
               r"\s*(?:[（(]?\s*(?P<suffix>" + _CURRENCY_PATTERN + r")\s*[）)]?)?")
    result = set()
    for match in re.finditer(pattern, text, re.I):
        # 明确“企业/公司(内部/自有)预算”属于企业声明；“采购预算/项目预算”仍是采购口径。
        company = (re.search(r"(?:本公司|本企业|公司|企业)(?:自有|内部|可用)?\s*$", text[:match.start()]) is not None
                   and not re.search(r"采购|项目", match["labels"]))
        if owner == "company" and not company or owner == "procurement" and company:
            continue
        unit = match["unit"] or match["label_unit"]
        currencies = {_CURRENCIES.get(raw.upper(), raw.upper()) for raw in (match["suffix"], match["currency"]) if raw}
        currency = next(iter(currencies)) if len(currencies) == 1 else "ambiguous" if currencies else "CNY"
        # 来源区间/约数/单价不升级为精确总额。报告的单边比较可引用已知阈值，
        # 是否满足/是否只是待核查留给语义核验；角色和数值仍必须有本条引用支持。
        tail = text[match.end():]
        if (re.match(r"(?:[-~～—至到余多]|\.(?=\s*\d)|左右|上下|[/／])", tail.lstrip())
                or not report and re.match(r"以上|以下|以内", tail.lstrip())):
            unit = None
        for label in re.findall(_MONEY_LABEL_PATTERN, match["labels"]):
            result.add((MONEY_LABELS[label], Decimal(match["number"]), unit, currency))
    return result


def _category(path):
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
    result = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
    for span in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(span[i:i + 2] for i in range(len(span) - 1))
    return result


class EvidenceIndex:
    """索引只有本次 manifest 的观察；相似项目永远不能通过检索进入范围。"""
    def __init__(self, manifest):
        if not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
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
        if (not isinstance(self.scope_id, str) or not HEX.fullmatch(self.scope_id)
                or any(not _string(manifest.get(field), 128) for field in ("workspace_id", "actor_id"))
                or not isinstance(observations, list) or not 1 <= len(observations) <= 50
                or not isinstance(profile, dict) or not isinstance(profile.get("payload"), dict)
                or type(profile.get("revision")) is not int or profile["revision"] < 1
                or profile.get("workspace_id") != manifest.get("workspace_id")
                or set(profile["payload"]) != PROFILE_FIELDS
                or any(value is not None and not _string(value, 4000, empty=True) for value in profile["payload"].values())
                or type(manifest.get("catalog_snapshot")) is not int or manifest["catalog_snapshot"] < 0
                or not isinstance(manifest.get("config"), dict)
                or manifest.get("kind") not in ("analysis", "question", "reassessment")):
            raise EvidenceError("invalid_manifest")
        question = manifest.get("question")
        if question is not None and not _string(question, 2000):
            raise EvidenceError("invalid_question")
        if manifest["kind"] == "question" and question is None:
            raise EvidenceError("invalid_question")
        previous = manifest.get("previous_report")
        if previous is not None and (not isinstance(previous, dict) or not isinstance(previous.get("scope"), dict)
                or previous["scope"].get("notice_id") != self.scope_id):
            raise EvidenceError("previous_report_scope_mismatch")
        self.profile = {key: redact(value) if value is not None else None for key, value in profile["payload"].items()}
        self.entries, self.by_id, self.money_claims, seen_observations = [], {}, [], set()
        for position, observation in enumerate(observations):
            if not isinstance(observation, dict) or any(not isinstance(observation.get(key), str)
                    or not HEX.fullmatch(observation[key]) for key in ("notice_id", "observation_id", "raw_sha256")):
                raise EvidenceError("invalid_observation")
            if position == 0 and observation["notice_id"] != self.scope_id:
                raise EvidenceError("manifest_scope_mismatch")
            if observation["observation_id"] in seen_observations:
                raise EvidenceError("duplicate_observation")
            seen_observations.add(observation["observation_id"])
            for root in ("facts", "evidence_fields", "material_reference_evidence"):
                self._walk(observation.get(root, {}), root, observation)
        self.observation_ids = [x["observation_id"] for x in observations]
        self.scope = {"notice_id": self.scope_id, "observation_ids": self.observation_ids,
                      "profile_revision": profile["revision"], "catalog_snapshot": manifest["catalog_snapshot"]}

    def _walk(self, value, path, observation, depth=0, money_role=None):
        if depth > 12:
            raise EvidenceError("evidence_too_deep")
        if isinstance(value, dict):
            # 只读取固定契约位置，不能因路径含budget便把冲突评估/其他金额升级成预算。
            structured = None
            if re.fullmatch(r"evidence_fields\.money\.\d+", path):
                raw_role = value.get("role")
                money_role = (raw_role if isinstance(raw_role, str) and raw_role in MONEY_ROLES | {"package_budget", "unit_price", "deposit"}
                              else "unknown" if "role" in value else None)
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
                # 去掉私人联系部分后切成原文连续片段，不用重写文本伪造“原文引用”。
                for offset, part in _safe_parts(text):
                    for start in range(0, len(part), 2400):
                        snippet = part[start:start + 2400]
                        entry = {"notice_id": observation["notice_id"], "observation_id": observation["observation_id"],
                                 "raw_sha256": observation["raw_sha256"], "locator": locator,
                                 "field": path, "category": _category(path), "text": snippet,
                                 "offset": offset + start}
                        # 标签是原始规范化证据的一部分；模型能辨认“48万元”所属字段。
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
                # 表头与表行共同建立的结构化金额必须引用整组，不能只引一个空表头。
                self.money_claims.append((structured, frozenset(e["evidence_id"] for e in self.entries[first_entry:])))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                self._walk(item, path + "." + str(i), observation, depth + 1, money_role)

    def structured_money_support(self, ids):
        """只返回引用组完整的结构化金额，同时供角色和原单位校验使用。"""
        ids = set(ids)
        return {claim for claim, required in self.money_claims if required.issubset(ids)}

    def money_support(self, ids):
        """金额证据必须属于当前引用集合；企业资金、其他finding和已读未引用均不能补位。"""
        ids = set(ids)
        result = self.structured_money_support(ids)
        for eid in ids:
            entry = self.by_id[eid]
            # 分包预算/单价不能靠原段中的“预算”字样变成项目总预算。
            if entry.get("money_role") not in MONEY_ROLES | {None}:
                continue
            result.update(claim for claim in _money_mentions(entry["text"], owner="procurement") if claim[2] is not None)
            label = entry.get("label", "").strip().rstrip(":：")
            if label in MONEY_LABELS or re.fullmatch(r"(?:" + _MONEY_LABEL_PATTERN + r")[（(](?:万元|元)[）)]", label):
                result.update(claim for claim in _money_mentions(label + "：" + entry["text"]) if claim[2] is not None)
        return result

    def search(self, query, category=None, limit=6):
        """兼容原列表接口；带匹配模式/覆盖信息的Agent工具使用search_result。"""
        return self.search_result(query, category, limit)["evidence"]

    @property
    def category_available_count(self):
        return {category: sum(entry["category"] == category for entry in self.entries) for category in sorted(CATEGORIES)}

    def search_result(self, query, category=None, limit=6):
        """关键词零命中时有标记地回退同分类原片段，不能据此宣称采购没有要求。

        query=''仅在指定分类时表示浏览。回退仍受limit限制，返回原始证据对象，
        不改写原文或生成新引用；调用方与普通命中一样记录read_ids。
        """
        if (not _string(query, 200, empty=True) or not query.strip() and category is None
                or category is not None and (not isinstance(category, str) or category not in CATEGORIES)
                or type(limit) is not int or not 1 <= limit <= 8):
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
        return {"evidence": deepcopy(selected[:limit]), "match_mode": mode, "category": category,
                "keyword_match_count": len(ranked), "category_available_count": self.category_available_count,
                "selected_category_available_count": len(candidates) if category is not None else None,
                "truncated": len(selected) > limit,
                "retrieval_note": "零关键词命中不代表要求不存在。分类回退/浏览是原始候选片段，仍须逐条判断；可用空query浏览指定分类。"}

    def read(self, ids):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 8 or any(not isinstance(x, str) for x in ids)
                or len(set(ids)) != len(ids) or any(x not in self.by_id for x in ids)):
            raise EvidenceError("evidence_not_in_manifest")
        return [deepcopy(self.by_id[x]) for x in ids]

    def coverage(self, read_ids):
        return {"mode": "normalized_snippets", "full_tender_read": False, "observation_ids": self.observation_ids,
                "evidence_ids": sorted(set(read_ids)), "available_evidence_count": len(self.entries),
                "material_status": [str(x.get("material_status", "unknown"))[:200] for x in self.manifest["observations"]]}


def _unsupported_completeness(text):
    """明确否定不因出现关键词而被误伤，复杂含义仍须语义核验。

    否定作用域限定当前分句和紧邻词组，不能用前一句免责声明豁免后面的实际承诺。
    """
    claims = re.compile(r"(?:已经|已|全部|完整)(?:完整|全部)?(?:阅读|读完|审阅|取得|读).{0,10}(?:全文|标书|招标文件|采购文件|PDF全文)"
                        r"|(?:全文|全部标书)(?:已读|已阅读|已审阅)"
                        r"|(?:全部|所有)(?:资格条件|资格|条件)(?:均|都)?(?:已)?满足"
                        r"|(?:已|已经)满足(?:全部|所有)(?:资格|条件)|中标概率", re.I)
    before_negative = re.compile(r"(?:不|未|没有)(?:会|能|可|应)?(?:承诺|保证|包含|提供|计算|评估|预测|估算|宣称|声称|认定|确认|判断|给出|输出|涉及)(?:任何|具体|精确)?$"
                                 r"|(?:并非|不是|不代表|不意味着|不等于|不能据此认为|不能认定|未能确认|尚无法确认)$")
    after_negative = re.compile(r"^(?:暂不|不|未|尚未|无法|不能)(?:予以|予)?(?:提供|计算|评估|预测|估算|给出|确认)")
    for clause in re.split(r"[，,。；;！!？?\n]", text):
        for match in claims.finditer(clause):
            before, after = clause[:match.start()].rstrip(), clause[match.end():].lstrip()
            direct_unread = re.search(r"(?:尚未|还未|未曾|从未|未)$", before)
            if direct_unread and not re.search(r"(?:否认|并非|不是|不承认|不能说)$", before[:direct_unread.start()]):
                continue
            if not before_negative.search(before) and not after_negative.search(after):
                return True
    return False


def _numbers(text):
    """仅规范数值本身的前导/尾随零；不把元数据或没有引用的数字加入事实池。"""
    # 只消除明确日期和时分窗口中的分隔符；空格排版不能让下一段变成负数。
    # 不全局删除负号：负金额/百分比仍是不同数值，未知数字也不能因此放行。
    text = re.sub(r"(?<!\d)(\d{4})\s*-\s*(0?[1-9]|1[0-2])\s*-\s*(0?[1-9]|[12]\d|3[01])(?!\d)",
                  r"\1 \2 \3", text)
    clock = r"(?:[01]?\d|2[0-3])[:：][0-5]\d(?:[:：][0-5]\d)?"
    text = re.sub(r"(?<!\d)(" + clock + r")\s*-\s*(" + clock + r")(?!\d)", r"\1 \2", text)
    return {Decimal(value) for value in re.findall(r"(?<!\d)[+-]?\d+(?:\.\d+)?", text)}


def _number_units(text):
    """等值尾零不能偷换金额/百分比/时长单位；日期角色与语义仍须模型复核。

    中文日期及有月锚点的日期范围不当成时长；省略右端年月不代表若干天。
    这里不做万元→元换算，只允许日/天和全角百分号
    的同义写法；数值相同、单位不同仍拒绝，避免Decimal放宽成金额口径放水。
    """
    month, day = r"(?:0?[1-9]|1[0-2])", r"(?:0?[1-9]|[12]\d|3[01])"
    start = r"(?:\d{4}\s*年\s*)?" + month + r"\s*月\s*" + day + r"\s*[日号]?"
    end = r"(?:\d{4}\s*年\s*)?(?:" + month + r"\s*月\s*)?" + day + r"\s*[日号]"
    # 先消除整个日期范围，避免右端“至13日”被当作13天；没有月份锚点的日数仍保留。
    # “13日内交付”不是日期范围右端，不可借前面的日期吞掉实际时长条件。
    without_dates = re.sub(r"(?<!\d)" + start + r"\s*(?:至|到|[-－—~～])\s*" + end + r"(?!\s*(?:内|以内|之内))", "", text)
    without_dates = re.sub(r"(?:\d{4}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*[日号]", "", without_dates)
    return {(Decimal(number), {"日": "天", "％": "%"}.get(unit, unit))
            for number, unit in re.findall(r"(?<!\d)([+-]?\d+(?:\.\d+)?)\s*(亿元|万元|元|小时|分钟|天|日|周|%|％)", without_dates)}


def _without_cited_ids(text, cited_ids):
    """仅在数值断言视图中去除完整已引ID；不改报告，不豁免未知ID或真实金额/时长。"""
    unknown = False
    def replace(match):
        nonlocal unknown
        if match.group() not in cited_ids:
            unknown = True
            return match.group()
        # 即使恰巧是64位全数字ID，紧跟数值单位时仍按事实检查，不能借引用身份免检。
        if re.match(r"\s*(?:亿元|万元|元|小时|分钟|天|日|周|%|％)", text[match.end():]):
            return match.group()
        return "[已引用证据]"
    clean = re.sub(r"(?<![A-Za-z0-9_])[0-9a-fA-F]{64}(?![A-Za-z0-9_])", replace, text)
    return clean, unknown


def validate_report(report, index, read_ids):
    """机械校验不等于语义证明；错误只返回码，避免不可信文本进入修订指令。"""
    errors = []
    required = {"summary", "findings", "questions", "answer"}
    if not isinstance(report, dict) or set(report) != required:
        return ["report_schema"]
    if (not _string(report["summary"], 1800) or not isinstance(report["findings"], list)
            or not 0 <= len(report["findings"]) <= 30 or not isinstance(report["questions"], list)
            or len(report["questions"]) > 12 or any(not _string(x, 600) for x in report["questions"])
            or report["answer"] is not None and not _string(report["answer"], 3000)):
        return ["report_schema"]
    if index.manifest["kind"] == "question" and report["answer"] is None:
        errors.append("question_answer_missing")
    if CONTACT.search(canonical(report)):
        errors.append("report_contact_information")
    if _unsupported_completeness(canonical(report)):
        errors.append("unsupported_completeness")
    if re.search(r"notice_id|observation_id|raw_sha256|\brevision\b|profile_revision|catalog_snapshot|category_available_count", report["summary"], re.I):
        errors.append("summary_contains_metadata")
    supported_numbers, supported_units, supported_company_money, cited_ids = set(), set(), set(), set()
    for i, finding in enumerate(report["findings"]):
        prefix = "finding_" + str(i) + "_"
        if (not isinstance(finding, dict) or set(finding) != {"category", "requirement", "status", "reason", "evidence_ids", "profile_fields", "unknown_reason"}
                or not isinstance(finding["category"], str) or finding["category"] not in CATEGORIES
                or not isinstance(finding["status"], str) or finding["status"] not in STATUSES
                or not _string(finding["requirement"], 1200) or not _string(finding["reason"], 1600)
                or not isinstance(finding["evidence_ids"], list) or len(finding["evidence_ids"]) > 12
                or any(not isinstance(x, str) for x in finding["evidence_ids"])
                or len(set(finding["evidence_ids"])) != len(finding["evidence_ids"])
                or not isinstance(finding["profile_fields"], list) or len(finding["profile_fields"]) > 9
                or any(not isinstance(x, str) or x not in PROFILE_FIELDS for x in finding["profile_fields"])
                or finding["unknown_reason"] is not None and not _string(finding["unknown_reason"], 800)):
            errors.append(prefix + "schema")
            continue
        ids = finding["evidence_ids"]
        if any(x not in index.by_id or x not in read_ids for x in ids):
            errors.append(prefix + "citation_scope_or_unread")
            continue
        # 模型只提出有来源的采购事项；覆盖缺口由服务器limitations承担，避免混成要求。
        if not ids:
            errors.append(prefix + "missing_evidence")
        if finding["status"] == "unknown" and not finding["unknown_reason"]:
            errors.append(prefix + "missing_unknown_reason")
        if finding["status"] in ("met", "unmet"):
            fields = finding["profile_fields"]
            if not fields or any(not index.profile[field] for field in fields):
                errors.append(prefix + "missing_profile")
            if finding["category"] == "qualification":
                # 当前档案只有管理员声明，proof_status=not_provided，不能升级为资质已证实。
                errors.append(prefix + "qualification_not_verified")
        evidence_text = " ".join(index.by_id[x]["text"] for x in ids)
        cited_ids.update(ids)
        profile_text = " ".join(index.profile[x] or "" for x in finding["profile_fields"])
        finding_text = " ".join(finding[field] or "" for field in ("requirement", "reason", "unknown_reason"))
        # 要求、理由和unknown说明都不能借公司资金或同额限价证明采购预算。
        missing_roles = {claim[0] for claim in _money_mentions(finding_text, owner="procurement", report=True) - index.money_support(ids)}
        errors.extend(prefix + "unsupported_money_role_" + role for role in sorted(missing_roles))
        company_money = _money_mentions(profile_text)
        supported_company_money.update(company_money)
        missing_company = {claim[0] for claim in _money_mentions(finding_text, owner="company", report=True) - company_money}
        errors.extend(prefix + "unsupported_company_money_role_" + role for role in sorted(missing_company))
        structured = index.structured_money_support(ids)
        number_pool = _numbers(evidence_text + " " + profile_text)
        number_pool.update(claim[1] for claim in structured)
        supported_numbers.update(number_pool)
        unit_pool = _number_units(evidence_text + " " + profile_text)
        # 数值在表行、单位在表头时，完整原引用组提供组合依据；缺一条便不能借用。
        unit_pool.update((claim[1], claim[2]) for claim in structured)
        supported_units.update(unit_pool)
        mentioned = _numbers(finding_text)
        if not mentioned.issubset(number_pool):
            errors.append(prefix + "unsupported_number")
        if not _number_units(finding_text).issubset(unit_pool):
            errors.append(prefix + "unsupported_numeric_unit")
    # 摘要/追问回答也不能悄悄增加数字事实；含义与日期角色仍交由独立语义节点核验。
    for field in ("summary", "answer"):
        text = report[field] or ""
        missing_roles = {claim[0] for claim in _money_mentions(text, owner="procurement", report=True) - index.money_support(cited_ids)}
        errors.extend(field + "_unsupported_money_role_" + role for role in sorted(missing_roles))
        missing_company = {claim[0] for claim in _money_mentions(text, owner="company", report=True) - supported_company_money}
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
    result = deepcopy(proposal)
    result["scope"] = deepcopy(index.scope)
    result["coverage"] = index.coverage(read_ids)
    result["limitations"] = ["本次研究输入为规范化证据片段，未阅读完整招标文件；这不表示系统全局未获取或未归档原件。",
                              "企业档案为用户确认声明，未独立验证资质证明。"] + list(limitations or [])
    result["references"] = [deepcopy(index.by_id[x]) for x in sorted(set(read_ids))]
    return result


def baseline_report(manifest):
    """无模型对照：忠实列出片段和未知，不凭关键词宣布企业满足条件。"""
    index = EvidenceIndex(manifest)
    selected = index.entries[:24]
    findings = [{"category": e["category"], "requirement": e["text"][:1200], "status": "unknown",
                 "reason": "规则基线仅列出可得原文，尚未完成语义匹配或证明核验。",
                 "evidence_ids": [e["evidence_id"]], "profile_fields": [],
                 "unknown_reason": "需核对要求与企业证明，当前不判定满足或不满足。"} for e in selected]
    proposal = {"summary": "规则基线：资料待核查，不构成参与资格结论。", "findings": findings,
                "questions": ["需取得完整采购文件并核查企业相应证明。"],
                "answer": "当前规则基线不能有据回答该追问，请核查所列原文与资料缺口。" if manifest.get("question") else None}
    return finalize_report(proposal, index, [e["evidence_id"] for e in selected])

"""v3 格式的业务证据字段：金额、获取文件窗口、获取条件、材料、技术/资格/交付要求。

从公告正文里按明确的标签和段落角色提取，每一项都保留原文和位置。
例如"预算金额：48万元"会得到一条 role=budget 的金额，带上这句原文。

这里只提取"原文说了什么"，不判断哪家公司是否具备资格。
只处理已经抓到的正文，不访问任何链接；看到公告不等于拿到了招标文件。
"""
from decimal import Decimal
import re


DATE = (r"\d{4}\s*[-年/]\s*\d{1,2}\s*[-月/]\s*\d{1,2}日?"
        r"(?:\s*[T ]?\s*\d{1,2}[:时点]\d{1,2}(?:[:分]\d{1,2}秒?)?分?)?")
NUMBER = r"(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?"
MONEY_LABEL = r"预算金额|项目预算|采购预算|预算|最高限价|最高投标限价|招标文件售价|采购文件售价|售价|文件费|投标保证金|履约保证金"
MONEY_RE = re.compile(r"(?P<label>" + MONEY_LABEL + r")\s*(?:[（(]\s*(?P<label_unit>万元|元)(?:人民币)?\s*[）)])?(?:[（(]如有[）)])?\s*[:：|]?\s*")
PACKAGE_RE = re.compile(r"(?:采购)?(?:包|标段)\s*([A-Z0-9一二三四五六七八九十]+)|([A-Z0-9一二三四五六七八九十]+)\s*(?:标)?包")


def evidence(text, locator, label="正文"):
    """生成一条证据：{label, text, locator}。"""
    return {"label": label, "text": text, "locator": locator}


def _dates(text, parse_date):
    """找出文本里的所有日期，按顺序逐个解析。解析失败的位置填 None，后面的日期不会往前挪。"""
    result = []
    for match in re.finditer(DATE, text):
        raw = re.sub(r"\s+", " ", match.group()).replace("点", "时")
        raw = re.sub(r"\s*([-年/月日])\s*", r"\1", raw)
        # 在日期和时间之间补个空格，只是为了方便解析，证据里保留的仍是原文。
        raw = re.sub(r"(日)(?=\d)", r"\1 ", raw)
        raw = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})(?=\d{2}:)", r"\1 ", raw)
        value = parse_date(raw + ("（北京时间）" if "北京时间" in text else ""))
        result.append(value)
    return result


def body_entries(segments, labels, parse_date):
    """从正文段落里补取带标签的字段值（比如"投标截止时间：……"）。

    只取标签明确的值。章节里单独出现的"时间""名称"要看它属于哪一节，不能跨节借用。
    """
    entries, section = [], None
    known_labels = sorted({label for names in labels.values() for label in names}, key=len, reverse=True)
    for segment in segments:
        text, locator = segment["text"].strip(), segment["locator"]
        clean = re.sub(r"^\s*(?:[一二三四五六七八九十]+[、．.]|\d+[、．.)）])\s*", "", text)
        if re.search(r"获取(?:招标|采购|竞争性磋商|磋商|谈判)文件", clean):
            section = "acquisition"
        elif re.search(r"^(?:提交投标文件截止时间|响应文件提交|响应文件的递交)", clean):
            section = "response"
        elif re.search(r"^采购人信息", clean):
            section = "buyer"
        elif re.match(r"^(?:采购代理机构|代理机构信息|项目联系方式)", clean) or re.match(r"^[一二三四五六七八九十]+[、．.]", text):
            section = None
        for label in known_labels:
            match = re.match(re.escape(label) + r"\s*[:：|]\s*(.+)", clean, re.S)
            if match:
                value = match.group(1).strip()
                # 同一段里后面出现的另一个字段，不算当前字段的值。
                if label not in labels["budget"]:
                    value = re.split(r"[；;。]\s*(?:" + "|".join(map(re.escape, known_labels)) + r")\s*[:：]", value)[0]
                entries.append(evidence(value, locator, label))
                break
        else:
            if section == "buyer" and re.match(r"名称\s*[:：]", clean):
                entries.append(evidence(re.split(r"[:：]", clean, maxsplit=1)[1].strip(), locator, "采购人"))
            elif section == "response" and (re.match(r"(?:截止时间\s*[:：]\s*)?\d{4}", clean)):
                dates = _dates(clean, parse_date)
                if len(dates) == 1:
                    # 截止时间那段只取日期部分，后面的递交地址之类的内容不要。
                    token = re.search(DATE, clean).group().replace("点", "时")
                    token = re.sub(r"日(?=\d)", "日 ", token)
                    entries.append(evidence(token + ("（北京时间）" if "北京时间" in clean else ""), locator, "响应文件提交截止时间"))
    return entries


def _role(label):
    """根据标签判断金额角色：预算、最高限价、分包预算、单价、文件售价、保证金。"""
    if "限价" in label:
        return "ceiling"
    if "保证金" in label:
        return "deposit"
    if "售价" in label or "文件费" in label:
        return "file_fee"
    return "budget"


def _claim(label, value, proof, parse_money, *, package=None, label_unit=None):
    """解析一条金额：只取标签后面紧跟的那个数。是范围、约数或单位不明的，记为未解析。"""
    money = None
    foreign = re.search(r"美元|美金|欧元|港元|港币|日元|日圆|英镑|\b(?:USD|EUR|HKD|JPY|GBP)\b", value, re.I)
    match = re.match(r"\s*(?:人民币\s*)?[￥¥]?\s*(" + NUMBER + r")\s*(万元|元)?", value)
    if match:
        number, unit = match.groups()
        tail = value[match.end():]
        # "10-12万元""10余万元"不能被截成"10元"；只解析出半个数也算失败。
        uncertain = re.match(r"\s*(?:[-~～—至到余多.\d]|[,，]\d|左右|上下|以上|以下|起|以内|不等|[（(](?:暂定|估算|约))", tail)
        # 标签写着"（万元）"，但数值后面是"亿元"或"亿美元"时，不能按万元算。
        wrong_unit = unit is None and re.match(r"\s*(?:亿|万亿|千|百|十|USD|EUR|HKD|JPY|GBP)", tail, re.I)
        if not uncertain and not foreign and not wrong_unit:
            unit = unit or label_unit
            if unit is None and re.match(r"\s*[（(](?:元|万元)(?:[/／]|[）)])", tail):
                unit = "万元" if "万元" in tail[:8] else "元"
            money = parse_money(number + (unit or ""), label)
    if money is None and _role(label) == "deposit" and not foreign:
        # 保证金常写成"大写金额（¥数字）"，只接受唯一一个明确的人民币金额；百分比不当成金额。
        amounts = re.findall(r"[（(]\s*[￥¥]\s*(" + NUMBER + r")\s*[）)]", value)
        if len(amounts) == 1 and "人民币" in value:
            money = parse_money(amounts[0] + "元", label)
    # "元/人次"这样的单价单位必须紧跟在这个数字后面，不能把后一句的单位安到前一句的数字上。
    denominator = re.match(r"\s*(?:[/／]|[（(]元\s*[/／])\s*([^，。；;）)\s]+)", value[match.end():]) if match else None
    unit_basis = denominator.group(1) if denominator else None
    role = "unit_price" if unit_basis else ("package_budget" if package and _role(label) == "budget" else _role(label))
    return {"role": role, "status": "parsed" if money else "unparsed",
            "amount": money["amount"] if money else None, "currency": "CNY",
            "source_unit": money["source_unit"] if money else None,
            "package": package, "unit_basis": unit_basis, "evidence": proof}


def _package(text):
    """从文本里找出分包编号（如“包1”“A包”），找不到返回 None。"""
    matches = list(PACKAGE_RE.finditer(text))
    return next((part for part in matches[-1].groups() if part), None) if matches else None


def extract(content, entries, parse_date, parse_money, notice_type):
    """提取全部 v3 证据字段，输出固定结构。

    缺失的不填 0；有冲突的不挑一个值了事，而是标出冲突；附件是否已取得以采集服务的记录为准。
    """
    segments = content.get("segments", [])
    money, windows, conditions = [], [], []
    category, technical, qualifications, delivery, authority = [], [], [], [], []
    section, table_header, pending_money = None, None, None
    for item in content.get("metadata", []):
        label, text = item["label"].strip().rstrip(":："), item["text"].strip()
        proof = [evidence(text, item["locator"], label)]
        if label in ("品目", "采购品目", "类别") and text:
            category.extend(proof)
        match = MONEY_RE.fullmatch(label)
        if match and text:
            money.append(_claim(match["label"], text, proof, parse_money, label_unit=match["label_unit"]))
    for item in segments:
        text, locator = item["text"].strip(), item["locator"]
        if not text:
            continue
        proof = [evidence(text, locator)]
        clean = re.sub(r"^[一二三四五六七八九十]+[、．.]\s*", "", text)
        if re.search(r"获取(?:招标|采购|竞争性磋商|磋商|谈判)文件", clean):
            section = "acquisition"
        elif re.match(r"^[一二三四五六七八九十]+[、．.]", text):
            section = None
        if (section == "acquisition" and re.search(r"时间|\d{4}", text)):
            dates = _dates(text, parse_date)
            # 获取文件的时间窗口必须正好是一个起止日期对；出现第三个日期（延期、分批）就不确定了，不能只取前两个。
            ambiguous = re.search(r"延期|延长|顺延|改期|调整|更正|第[一二三四五六七八九十\d]+批|分批|分期", text)
            if len(dates) == 2 and all(date is not None for date in dates) and not ambiguous and re.search(r"至|到|[—~～]", text):
                windows.append({"value": {"start": dates[0], "end": dates[1]}, "evidence": proof})
            elif re.match(r"时间\s*[:：]", text) or len(dates) >= 2:
                windows.append({"value": None, "evidence": proof})
        access_context = section == "acquisition" or bool(re.search(r"(?:下载|获取|领取|报名).{0,80}(?:文件|招标)|(?:文件|招标).{0,80}(?:下载|获取|领取|报名)", text))
        for kind, pattern in (("registration", r"注册|登记"), ("login", r"登录|登陆"),
                              ("application", r"申请|报名|领取"), ("payment", r"缴费|交费|支付|汇款|售后不退"),
                              ("ca_certificate", r"CA证书|CA数字|数字证书|数字身份认证锁")):
            if access_context and re.search(pattern, text, re.I):
                # 原句里的"无需""必须""可以"都原样保留，不会因为句子里有某个关键词就当成强制要求。
                conditions.append({"kind": kind, "status": "mentioned", "evidence": proof})
        if re.search(r"软件|信息系统|信息化|智能体|人工智能|平台开发|系统开发|系统集成|数据库|接口|云胶片", text):
            technical.extend(proof)
        if re.search(r"资质|资格要求|许可证|中小企业|联合体|分包", text):
            qualifications.extend(proof)
        if re.search(r"履约期限|服务期限|合同履行期限|交付|验收|驻场|试运行|服务地点", text):
            delivery.extend(proof)
        if re.search(r"(?:预算|金额).{0,60}以.{0,60}为准|以.{0,60}(?:正文|采购需求|表格).{0,60}为准", text):
            authority.extend(proof)
        if pending_money:
            line = re.fullmatch(r"(.+?)[:：]\s*([￥¥]?[\d,]+(?:\.\d+)?\s*(?:万元|元))\s*[。；;]?", text)
            if line:
                label, header_proof = pending_money
                money.append(_claim(label, line[2], header_proof + proof, parse_money, package=_package(line[1])))
                continue
            pending_money = None
        heading = MONEY_RE.fullmatch(text)
        if heading:
            pending_money = (heading["label"], proof)
            continue
        # 需求里"按人次报价"的单价不能当成项目总预算或总限价。
        if re.search(r"单价|报价|按|每", text):
            for unit in re.finditer(r"(" + NUMBER + r")\s*元\s*[/／]\s*([^，。；;）)\s”\"]+)", text):
                money.append(_claim("单价", unit.group(), proof, parse_money))
        cells = [cell.strip() for cell in text.split("|")]
        headers = [(i, MONEY_RE.fullmatch(cell)) for i, cell in enumerate(cells)] if len(cells) > 1 else []
        monetary_headers = [(i, match) for i, match in headers if match]
        if monetary_headers:
            table_header = (cells, monetary_headers, proof)
            # 两列的表格行"预算金额 | 100万元"是一个字段和它的值，不是下一行的表头。
            if len(cells) != 2 or monetary_headers[0][0] != 0 or not re.match(r"[￥¥\d]", cells[1]):
                continue
            table_header = None
        elif table_header:
            header_cells, headers, header_proof = table_header
            if len(cells) == len(header_cells) and len(cells) > 1:
                package = next((cells[i] for i, cell in enumerate(header_cells) if re.fullmatch(r"包号|包组|标段(?:编号)?|包件号", cell)), None)
                for i, match in headers:
                    money.append(_claim(match["label"], cells[i], header_proof + proof, parse_money,
                                        package=package, label_unit=match["label_unit"]))
                continue
            table_header = None
        matches = list(MONEY_RE.finditer(text))
        for index, match in enumerate(matches):
            remainder = text[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(text)]
            # "预算金额以正文为准"这类说明不是一个新金额，单独保存，用来判断哪个金额说了算。
            if not remainder.strip(" ，。；;：:、") or re.match(r"以|为准|详见|见", remainder):
                continue
            prefix = re.split(r"[；;。]", text[:match.start()])[-1]
            money.append(_claim(match["label"], remainder, proof, parse_money,
                                package=_package(prefix), label_unit=match["label_unit"]))
    # 同一金额在摘要表和正文里各出现一次时，两条证据都保留；比较是否相同时，48万元和480000元算一样。
    budget = [claim for claim in money if claim["role"] == "budget"]
    parsed = {Decimal(claim["amount"]) for claim in budget if claim["status"] == "parsed"}
    reasons = []
    if len(parsed) > 1:
        reasons.append("different_budget_amounts")
    if any(claim["package"] for claim in money):
        reasons.append("package_amounts_present")
    if any(claim["unit_basis"] for claim in money):
        reasons.append("unit_pricing_present")
    if any(claim["status"] == "unparsed" for claim in budget):
        reasons.append("unparsed_budget_evidence")
    if authority:
        reasons.append("source_precedence_statement")
    if notice_type == "correction" and budget:
        reasons.append("correction_context")
    for claim in money:
        if claim["role"] == "file_fee" and claim["status"] == "parsed" and Decimal(claim["amount"]) > 0:
            conditions.append({"kind": "payment", "status": "mentioned", "evidence": claim["evidence"]})
    state = ("conflicting" if len(parsed) > 1 else "context_required" if any(
        reason in reasons for reason in ("package_amounts_present", "unit_pricing_present", "correction_context", "source_precedence_statement")) else
        "unparsed" if "unparsed_budget_evidence" in reasons else "known" if parsed else "missing")
    window_values = [window["value"] for window in windows]
    window_state = "known" if window_values and all(value == window_values[0] for value in window_values) else "conflicting" if windows else "missing"
    if windows and (notice_type == "correction" or any(value is None or value["start"]["local"] > value["end"]["local"] for value in window_values)):
        window_state = "unparsed"
    refs = []
    attachments = content.get("attachments", [])
    if not isinstance(attachments, (list, tuple)) or len(attachments) > 1000:
        raise ValueError("invalid_attachments")
    for attachment in attachments:
        # 附件是否已取得，只看采集服务有没有真的存下文件，不看链接的扩展名。
        if not isinstance(attachment, dict):
            raise ValueError("invalid_attachment")
        reference = {key: attachment.get(key, "") for key in ("url", "name", "locator")}
        if any(not isinstance(value, str) or len(value) > 4096 for value in reference.values()):
            raise ValueError("invalid_attachment_reference")
        fetched = attachment.get("fetch_status") == "fetched"
        if fetched and (not isinstance(attachment.get("capture_id"), str) or not attachment["capture_id"]
                or not isinstance(attachment.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", attachment["sha256"])):
            raise ValueError("fetched_attachment_without_capture")
        reference.update(status=attachment.get("fetch_status", attachment.get("status", "not_fetched")),
                         capture_id=attachment.get("capture_id") if fetched else None,
                         raw_sha256=attachment.get("sha256") if fetched else None)
        if not isinstance(reference["status"], str) or len(reference["status"]) > 128:
            raise ValueError("invalid_attachment_status")
        # 解析器给的状态不算数，只有导出时核实过的 fetch_status 才能说明附件已取得。
        if reference["status"] == "fetched" and not fetched:
            reference["status"] = "not_fetched"
        refs.append(reference)
    acquired = sum(ref["status"] == "fetched" for ref in refs)
    material_status = "obtained" if refs and acquired == len(refs) else "partially_obtained" if acquired else "not_obtained"
    return {"money": money, "budget_assessment": {"status": state, "reasons": reasons,
                "evidence": [item for claim in budget for item in claim["evidence"]] + authority},
            "acquisition_window": {"status": window_state, "value": window_values[0] if window_state == "known" else None,
                "evidence": [item for window in windows for item in window["evidence"]]},
            "access_conditions": conditions, "material_availability": {"status": material_status, "references": refs},
            "category_evidence": category, "technical_evidence": technical,
            "qualification_evidence": qualifications, "delivery_evidence": delivery}

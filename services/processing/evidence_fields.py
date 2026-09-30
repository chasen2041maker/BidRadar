"""v3 的公开业务证据：按明确标签/段落角色提取，不判断团队是否具备资格。

日期窗口、金额口径和材料取得状态独立于公告身份；每项保留原文及解析器定位。
本模块只处理已取得的正文，不访问链接，不把公开公告等同于已取得采购文件。
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
    return {"label": label, "text": text, "locator": locator}


def _dates(text, parse_date):
    result = []
    for match in re.finditer(DATE, text):
        raw = re.sub(r"\s+", " ", match.group()).replace("点", "时")
        raw = re.sub(r"\s*([-年/月日])\s*", r"\1", raw)
        # 日期和时间之间补分隔符只改解释输入；证据始终保留原文。
        raw = re.sub(r"(日)(?=\d)", r"\1 ", raw)
        raw = re.sub(r"(\d{4}-\d{1,2}-\d{1,2})(?=\d{2}:)", r"\1 ", raw)
        value = parse_date(raw + ("（北京时间）" if "北京时间" in text else ""))
        if value is not None:
            result.append(value)
    return result


def body_entries(segments, labels, parse_date):
    """只补有明确角色的正文值；章节中的“时间/名称”需要相应上文，不能跨节借用。"""
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
                # 同段后续明确字段不属于当前字段值；预算冲突由完整金额证据另行判断。
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
                    # 只裁剪明确截止段的一处日期；例如递交地址不能成为时间的一部分。
                    token = re.search(DATE, clean).group().replace("点", "时")
                    token = re.sub(r"日(?=\d)", "日 ", token)
                    entries.append(evidence(token + ("（北京时间）" if "北京时间" in clean else ""), locator, "响应文件提交截止时间"))
    return entries


def _role(label):
    if "限价" in label:
        return "ceiling"
    if "保证金" in label:
        return "deposit"
    if "售价" in label or "文件费" in label:
        return "file_fee"
    return "budget"


def _claim(label, value, proof, parse_money, *, package=None, label_unit=None):
    """仅解析标签后的数值前缀；范围、约数和单位不明均保留为未解析。"""
    money = None
    foreign = re.search(r"美元|美金|欧元|港元|港币|日元|日圆|英镑|\b(?:USD|EUR|HKD|JPY|GBP)\b", value, re.I)
    match = re.match(r"\s*(?:人民币\s*)?[￥¥]?\s*(" + NUMBER + r")\s*(万元|元)?", value)
    if match:
        number, unit = match.groups()
        tail = value[match.end():]
        # 10-12万元/10余万元不能被截成10元；小数/逗号残片也不能部分成功。
        uncertain = re.match(r"\s*(?:[-~～—至到余多.\d]|[,，]\d|左右|上下|以上|以下|起|以内|不等|[（(](?:暂定|估算|约))", tail)
        # 标签上的“万元”不能把未支持的“亿元/亿美元”数值前缀变成10万元。
        wrong_unit = unit is None and re.match(r"\s*(?:亿|万亿|千|百|十|USD|EUR|HKD|JPY|GBP)", tail, re.I)
        if not uncertain and not foreign and not wrong_unit:
            unit = unit or label_unit
            if unit is None and re.match(r"\s*[（(](?:元|万元)(?:[/／]|[）)])", tail):
                unit = "万元" if "万元" in tail[:8] else "元"
            money = parse_money(number + (unit or ""), label)
    if money is None and _role(label) == "deposit" and not foreign:
        # 大写人民币保证金常附括号阿拉伯数；只接受唯一显式人民币符号，不解析百分比为总额。
        amounts = re.findall(r"[（(]\s*[￥¥]\s*(" + NUMBER + r")\s*[）)]", value)
        if len(amounts) == 1 and "人民币" in value:
            money = parse_money(amounts[0] + "元", label)
    # 分母必须紧跟此次数值，不能把后一句的5元/人次挂到前一句预算0元上。
    denominator = re.match(r"\s*(?:[/／]|[（(]元\s*[/／])\s*([^，。；;）)\s]+)", value[match.end():]) if match else None
    unit_basis = denominator.group(1) if denominator else None
    role = "unit_price" if unit_basis else ("package_budget" if package and _role(label) == "budget" else _role(label))
    return {"role": role, "status": "parsed" if money else "unparsed",
            "amount": money["amount"] if money else None, "currency": "CNY",
            "source_unit": money["source_unit"] if money else None,
            "package": package, "unit_basis": unit_basis, "evidence": proof}


def _package(text):
    matches = list(PACKAGE_RE.finditer(text))
    return next((part for part in matches[-1].groups() if part), None) if matches else None


def extract(content, entries, parse_date, parse_money, notice_type):
    """输出固定v3结构；缺失不填0，冲突不选一个值覆盖，材料状态来自获取层。"""
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
            if len(dates) >= 2 and re.search(r"至|到|[—~～]", text):
                windows.append({"value": {"start": dates[0], "end": dates[1]}, "evidence": proof})
            elif re.match(r"时间\s*[:：]", text):
                windows.append({"value": None, "evidence": proof})
        access_context = section == "acquisition" or bool(re.search(r"(?:下载|获取|领取|报名).{0,80}(?:文件|招标)|(?:文件|招标).{0,80}(?:下载|获取|领取|报名)", text))
        for kind, pattern in (("registration", r"注册|登记"), ("login", r"登录|登陆"),
                              ("application", r"申请|报名|领取"), ("payment", r"缴费|交费|支付|汇款|售后不退"),
                              ("ca_certificate", r"CA证书|CA数字|数字证书|数字身份认证锁")):
            if access_context and re.search(pattern, text, re.I):
                # “无需/必须/可以”保留在引文，不将含一个关键词的句子升级成强制要求。
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
        # 按检查人次/次数报价在需求段出现，不能把单价抄成项目总预算或总限价。
        if re.search(r"单价|报价|按|每", text):
            for unit in re.finditer(r"(" + NUMBER + r")\s*元\s*[/／]\s*([^，。；;）)\s”\"]+)", text):
                money.append(_claim("单价", unit.group(), proof, parse_money))
        cells = [cell.strip() for cell in text.split("|")]
        headers = [(i, MONEY_RE.fullmatch(cell)) for i, cell in enumerate(cells)] if len(cells) > 1 else []
        monetary_headers = [(i, match) for i, match in headers if match]
        if monetary_headers:
            table_header = (cells, monetary_headers, proof)
            # 两列表格“预算金额 | 100万元”是一项字段，而非下一行的表头。
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
            # 叙述“预算金额以正文为准”不是新增数值；保留于authority供冲突判断。
            if not remainder.strip(" ，。；;：:、") or re.match(r"以|为准|详见|见", remainder):
                continue
            prefix = re.split(r"[；;。]", text[:match.start()])[-1]
            money.append(_claim(match["label"], remainder, proof, parse_money,
                                package=_package(prefix), label_unit=match["label_unit"]))
    # 原文重复出现在摘要和正文时证据不丢，判断金额等价时不受小数尾零/万元表示影响。
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
        # 下载成功必须来自已落盘附件证据；本层绝不根据链接扩展名推断取得成功。
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
        # parser原始status不是下载证据；只有export核实的fetch_status可宣布已取得。
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

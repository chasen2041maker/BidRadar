"""商机目录：保存规范化后的公开公告，提供搜索、详情和变化流。

几个核心概念：
    - 公告（notice）：一条采购公告，ID 由"来源 + 来源里的记录键"算出，同一公告 ID 不变。
    - 观察（observation）：某次抓取得到的公告内容。同一公告会有多个观察，每个都不可修改。
    - 当前版本：抓取时间最新的那个观察。晚到的旧观察不会把当前版本"倒退"回去。
    - 快照（snapshot）：观察的接收序号上限。搜索翻页、研究取材都固定在一个快照上，
      期间导入的新数据不会让结果前后不一致。
    - 关联（relationships）：更正公告、结果公告和原采购公告之间的关系，需要有依据才算确认。

只接收规范化格式的数据，不去读原始抓取库。
这是本地开发用的单机后端，不处理企业私有资料，也没有多租户权限。

阅读顺序：import_bundle → query → detail / bundle → relationships。
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3

VERSION = "procurement-facts-v3"
# 公告类型：采购、更正、结果、终止、意向、未知。
KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}
# 规范化事实字段（v1），v2 加了原发布时间和原合同引用。
FIELDS = {"project_number", "buyer", "published_at", "budget", "response_deadline", "lot_identifier"}
V2_FIELDS = FIELDS | {"original_published_at", "original_contract_reference"}
# 来源 → 发布方。只有同一发布方的公告之间才会建立更正、结果关联。
# 这里只列已经核实过的来源，不按名字前缀（比如 tj_）推断。
SOURCE_FAMILIES = {"cn_ccgp": "cn_ccgp", "cn_hainan": "cn_hainan", "tj_procurement_negotiation": "tj_finance_procurement",
                   "tj_procurement_consultation": "tj_finance_procurement",
                   "tj_procurement_correction": "tj_finance_procurement"}
# 每个格式版本对应的规范化器版本，以及允许的来源。
SCHEMAS = {1: ("procurement-facts-v1", {"cn_ccgp", "tj_procurement_negotiation"}),
           2: ("procurement-facts-v2", set(SOURCE_FAMILIES) - {"cn_hainan"}),
           3: (VERSION, set(SOURCE_FAMILIES))}
# 选"当前版本"的排序：先比抓取时间；同一时刻抓到的，选规范化器版本高的；
# 最后才比接收序号。这样晚到的 v1 数据不会把已有的 v2 结果降级。
# 版本高低按这张表查，不按字符串比较。
NORMALIZER_RANK = {"procurement-facts-v1": 1, "procurement-facts-v2": 2, "procurement-facts-v3": 3}
OBSERVATION_ORDER = "observed_at DESC, normalizer_rank(payload) DESC, seq DESC"


def _json(value):
    """固定格式的 JSON 序列化。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    """对象的 SHA-256，用来校验观察 ID 和公告 ID。"""
    return sha256(_json(value).encode()).hexdigest()


def _time(value):
    """解析带时区的 ISO 8601 时间，统一转成 UTC。"""
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except ValueError:
        raise ValueError("invalid_timestamp") from None


def _string(value, limit=4096, empty=False):
    """检查是不超长的字符串；empty=False 时不能全是空白。"""
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_string")


def _evidence(value):
    """检查证据列表：每条都是 {label, text, locator}。"""
    if not isinstance(value, list) or len(value) > 10000:
        raise ValueError("invalid_evidence")
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
            raise ValueError("invalid_evidence")
        _string(entry["label"], 100)
        _string(entry["locator"], 512)
        _string(entry["text"], 200000)


def _date_value(value):
    """检查日期事实：精度（日、分、秒）和格式一致；只有原文写明北京时间才带 +08:00。"""
    if (not isinstance(value, dict) or set(value) != {"local", "precision", "timezone", "timezone_basis"}
            or value["precision"] not in ("date", "minute", "second") or value["timezone"] not in (None, "+08:00")):
        raise ValueError("invalid_date_fact")
    _string(value["local"], 32)
    patterns = {"date": r"\d{4}-\d{2}-\d{2}", "minute": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
                "second": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"}
    if not re.fullmatch(patterns[value["precision"]], value["local"]):
        raise ValueError("date_precision_mismatch")
    datetime.fromisoformat(value["local"])
    if value["timezone_basis"] != ("原文明确北京时间" if value["timezone"] else None):
        raise ValueError("date_timezone_basis_mismatch")


def _validate_evidence_fields(value):
    """检查 v3 格式里的 evidence_fields（金额、获取窗口、资格、交付等证据）。

    目录自己做一遍完整检查，不依赖 processing 的代码。
    """
    keys = {"money", "budget_assessment", "acquisition_window", "access_conditions", "material_availability",
            "category_evidence", "technical_evidence", "qualification_evidence", "delivery_evidence"}
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("invalid_evidence_fields")
    for name in ("category_evidence", "technical_evidence", "qualification_evidence", "delivery_evidence"):
        _evidence(value[name])
    money = value["money"]
    if not isinstance(money, list) or len(money) > 10000:
        raise ValueError("invalid_money_claims")
    for claim in money:
        if (not isinstance(claim, dict) or set(claim) != {"role", "status", "amount", "currency", "source_unit", "package", "unit_basis", "evidence"}
                or claim["role"] not in ("budget", "ceiling", "unit_price", "package_budget", "file_fee", "deposit")
                or claim["status"] not in ("parsed", "unparsed") or claim["currency"] != "CNY"):
            raise ValueError("invalid_money_claim")
        _evidence(claim["evidence"])
        if not claim["evidence"]:
            raise ValueError("money_without_evidence")
        if claim["status"] == "parsed":
            _string(claim["amount"], 40)
            if (not re.fullmatch(r"\d+(?:\.\d{1,6})?", claim["amount"]) or Decimal(claim["amount"]) > 10**15
                    or claim["source_unit"] not in ("元", "万元")):
                raise ValueError("invalid_money_value")
        elif claim["amount"] is not None or claim["source_unit"] is not None:
            raise ValueError("unparsed_money_has_value")
        for name in ("package", "unit_basis"):
            if claim[name] is not None:
                _string(claim[name], 2000)
        if (claim["role"] == "unit_price") != (claim["unit_basis"] is not None):
            raise ValueError("invalid_unit_price_basis")
        if claim["role"] == "package_budget" and claim["package"] is None:
            raise ValueError("package_budget_without_package")
    assessment = value["budget_assessment"]
    if (not isinstance(assessment, dict) or set(assessment) != {"status", "reasons", "evidence"}
            or assessment["status"] not in ("known", "missing", "conflicting", "unparsed", "context_required")
            or not isinstance(assessment["reasons"], list) or len(assessment["reasons"]) > 6
            or any(reason not in ("different_budget_amounts", "package_amounts_present", "unit_pricing_present",
                "unparsed_budget_evidence", "source_precedence_statement", "correction_context") for reason in assessment["reasons"])):
        raise ValueError("invalid_budget_assessment")
    _evidence(assessment["evidence"])
    window = value["acquisition_window"]
    if (not isinstance(window, dict) or set(window) != {"status", "value", "evidence"}
            or window["status"] not in ("known", "missing", "conflicting", "unparsed")
            or (window["status"] == "known") != (window["value"] is not None)):
        raise ValueError("invalid_acquisition_window")
    _evidence(window["evidence"])
    if (window["status"] == "missing") != (not window["evidence"]):
        raise ValueError("invalid_acquisition_evidence")
    if window["status"] == "known":
        dates = window["value"]
        if not isinstance(dates, dict) or set(dates) != {"start", "end"}:
            raise ValueError("invalid_acquisition_dates")
        for date in dates.values():
            _date_value(date)
        if dates["start"]["local"] > dates["end"]["local"]:
            raise ValueError("reversed_acquisition_dates")
    conditions = value["access_conditions"]
    if not isinstance(conditions, list) or len(conditions) > 10000:
        raise ValueError("invalid_access_conditions")
    for condition in conditions:
        if (not isinstance(condition, dict) or set(condition) != {"kind", "status", "evidence"}
                or condition["kind"] not in ("registration", "login", "application", "payment", "ca_certificate")
                or condition["status"] != "mentioned"):
            raise ValueError("invalid_access_condition")
        _evidence(condition["evidence"])
        if not condition["evidence"]:
            raise ValueError("condition_without_evidence")
    materials = value["material_availability"]
    if (not isinstance(materials, dict) or set(materials) != {"status", "references"}
            or materials["status"] not in ("not_obtained", "partially_obtained", "obtained")
            or not isinstance(materials["references"], list) or len(materials["references"]) > 1000):
        raise ValueError("invalid_material_availability")
    for reference in materials["references"]:
        if not isinstance(reference, dict) or set(reference) != {"url", "name", "locator", "status", "capture_id", "raw_sha256"}:
            raise ValueError("invalid_material_reference")
        for name in ("url", "name", "locator", "status"):
            _string(reference[name], empty=True)
        if reference["status"] == "fetched":
            _string(reference["capture_id"], 128)
            if not isinstance(reference["raw_sha256"], str) or not re.fullmatch("[0-9a-f]{64}", reference["raw_sha256"]):
                raise ValueError("fetched_attachment_without_hash")
        elif reference["capture_id"] is not None or reference["raw_sha256"] is not None:
            raise ValueError("unfetched_attachment_has_capture")
    acquired = sum(ref["status"] == "fetched" for ref in materials["references"])
    expected = ("obtained" if acquired and acquired == len(materials["references"]) else
                "partially_obtained" if acquired else "not_obtained")
    if materials["status"] != expected:
        raise ValueError("material_status_without_acquisition")


def _validate(observation, bundle):
    """导入前检查一条观察：字段齐全、类型正确、ID 和内容哈希对得上、事实格式合法。"""
    if not isinstance(observation, dict):
        raise ValueError("invalid_observation")
    required = {"observation_id", "notice_id", "source_id", "simulation", "source_record_key", "identity_kind",
                "source_url", "capture_id", "raw_sha256", "parser_version", "normalizer_version", "observed_at",
                "title", "notice_type", "facts", "references", "material_status", "attribution", "run_id"}
    if bundle["schema_version"] >= 2:
        required.add("material_reference_evidence")
    if bundle["schema_version"] == 3:
        required.add("evidence_fields")
    if set(observation) != required:
        raise ValueError("unsupported_observation_fields")
    value = {k: v for k, v in observation.items() if k != "observation_id"}
    if observation["observation_id"] != _hash(value):
        raise ValueError("observation_hash_mismatch")
    for name in ("observation_id", "notice_id", "raw_sha256"):
        if not isinstance(observation[name], str) or not re.fullmatch("[0-9a-f]{64}", observation[name]):
            raise ValueError("invalid_hash")
    for name in ("source_id", "run_id", "simulation", "normalizer_version"):
        if observation[name] != bundle[name] or type(observation[name]) is not type(bundle[name]):
            raise ValueError("observation_bundle_mismatch")
    if observation["notice_id"] != _hash([observation["source_id"], observation["simulation"], observation["source_record_key"]]):
        raise ValueError("notice_identity_mismatch")
    if observation["identity_kind"] not in ("source_url", "content_snapshot") or observation["notice_type"] not in KINDS:
        raise ValueError("invalid_observation_kind")
    for name in ("source_record_key", "source_url", "capture_id", "parser_version", "material_status"):
        _string(observation[name])
    _string(observation["title"], 2000, empty=True)
    _time(observation["observed_at"])
    facts = observation["facts"]
    expected_fields = V2_FIELDS if bundle["schema_version"] >= 2 else FIELDS
    if not isinstance(facts, dict) or set(facts) != expected_fields:
        raise ValueError("invalid_facts")
    for name, fact in facts.items():
        if not isinstance(fact, dict) or set(fact) != {"status", "value", "evidence"}:
            raise ValueError("invalid_fact")
        state, value, evidence = fact["status"], fact["value"], fact["evidence"]
        if state not in ("known", "missing", "unparsed", "conflicting") or (state == "known") != (value is not None):
            raise ValueError("invalid_fact_state")
        if not isinstance(evidence, list) or len(evidence) > 10000 or (state == "missing") != (len(evidence) == 0):
            raise ValueError("invalid_fact_evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
                raise ValueError("invalid_fact_evidence")
            for item in entry.values():
                _string(item, 200000)
        if state != "known":
            continue
        if name in ("published_at", "response_deadline", "original_published_at"):
            if (not isinstance(value, dict) or set(value) != {"local", "precision", "timezone", "timezone_basis"}
                    or value["precision"] not in ("date", "minute", "second") or value["timezone"] not in (None, "+08:00")):
                raise ValueError("invalid_date_fact")
            _string(value["local"], 32)
            date_patterns = {"date": r"\d{4}-\d{2}-\d{2}", "minute": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}",
                             "second": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"}
            if not re.fullmatch(date_patterns[value["precision"]], value["local"]):
                raise ValueError("date_precision_mismatch")
            datetime.fromisoformat(value["local"])
            if value["timezone_basis"] != ("原文明确北京时间" if value["timezone"] else None):
                raise ValueError("date_timezone_basis_mismatch")
        elif name == "budget":
            if (not isinstance(value, dict) or set(value) != {"amount", "currency", "source_unit", "scope"}
                    or not isinstance(value["amount"], str) or not re.fullmatch(r"\d+(?:\.\d+)?", value["amount"])
                    or value["currency"] != "CNY" or value["source_unit"] not in ("元", "万元")
                    or value["scope"] != "unspecified"):
                raise ValueError("invalid_budget_fact")
        else:
            _string(value, 200000)
    if bundle["schema_version"] == 3:
        _validate_evidence_fields(observation["evidence_fields"])
        if (observation["evidence_fields"]["budget_assessment"]["status"] in ("conflicting", "context_required", "unparsed")
                and facts["budget"]["status"] == "known"):
            raise ValueError("unsafe_budget_projection")
    if bundle["schema_version"] >= 2:
        evidence = observation["material_reference_evidence"]
        if not isinstance(evidence, list) or len(evidence) > 10000:
            raise ValueError("invalid_material_reference_evidence")
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"label", "text", "locator"}:
                raise ValueError("invalid_material_reference_evidence")
            for item in entry.values():
                _string(item, 200000)
    refs = observation["references"]
    if not isinstance(refs, list) or len(refs) > 1000:
        raise ValueError("invalid_references")
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {"url", "text", "locator"}:
            raise ValueError("invalid_reference")
        for item in ref.values():
            _string(item)


def currentness(observation, as_of):
    """按 as_of 时刻判断公告的时间状态。

    只看截止时间是否已过，不代表还能不能参与。原文没写时区的截止时间不做判断，记为 unknown。
    更正、结果、终止、意向这几类公告不是报名公告，直接标出来。
    """
    kind = observation["notice_type"]
    if kind in ("award", "termination", "intention", "correction"):
        return {"status": "not_opening_notice", "reason": kind, "as_of": as_of}
    fact = observation["facts"]["response_deadline"]
    if fact["status"] == "known":
        date = fact["value"]
        if date["precision"] != "date" and date["timezone"]:
            end = _time(date["local"] + date["timezone"])
            return {"status": "deadline_passed" if end <= _time(as_of) else "deadline_not_reached",
                    "reason": "explicit_deadline_only_not_eligibility", "as_of": as_of}
    return {"status": "unknown", "reason": "deadline_or_timezone_unconfirmed", "as_of": as_of}


def _matches_keyword(observation, keyword):
    """关键词是否出现在标题、项目编号、采购人或技术要求原文里。只做字符串匹配，不做任何判断。"""
    texts = [observation["title"]]
    for name in ("project_number", "buyer"):
        fact = observation["facts"][name]
        if fact["status"] == "known":
            texts.append(fact["value"])
    texts.extend(entry["text"] for entry in observation.get("evidence_fields", {}).get("technical_evidence", []))
    return any(keyword.casefold() in text.casefold() for text in texts)


class Catalog:
    """商机目录。root 是数据目录；read_only=True 时只读打开（HTTP 服务用）。"""

    def __init__(self, root, *, read_only=False):
        root = Path(root).absolute()
        if root.is_symlink():
            raise ValueError("store_symlink")
        # 只读模式下目录必须已经存在。路径配错时直接报错，而不是悄悄建一个空库、返回"零结果"。
        if not read_only:
            root.mkdir(parents=True, exist_ok=True)
        marker = root / ".bidradar-catalog-v1"
        if read_only and (not marker.is_file() or not (root / "catalog.sqlite3").is_file()):
            raise ValueError("catalog_not_initialized")
        if not marker.exists() and any(root.iterdir()):
            raise ValueError("catalog_directory_not_empty")
        if not read_only:
            marker.touch(exist_ok=True)
        if (root / "catalog.sqlite3").is_symlink():
            raise ValueError("store_symlink")
        self.db = (sqlite3.connect((root / "catalog.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=5)
                   if read_only else sqlite3.connect(root / "catalog.sqlite3", timeout=5))
        self.db.row_factory = sqlite3.Row
        # 注册一个 SQL 函数，用于在排序时取规范化器版本的高低。
        self.db.create_function("normalizer_rank", 1,
            lambda payload: NORMALIZER_RANK[json.loads(payload)["normalizer_version"]], deterministic=True)
        if read_only:
            try:
                self.key = self.db.execute("SELECT value FROM settings WHERE key='cursor_key'").fetchone()[0].encode()
            except Exception:
                self.db.close()
                raise
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        # seq 是目录收到数据的顺序，不代表公告的新旧；判断当前版本看的是抓取时间。
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations(
                seq INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, notice_id TEXT NOT NULL,
                observed_at TEXT NOT NULL, simulation INTEGER NOT NULL, kind TEXT NOT NULL,
                title TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS notice_time ON observations(notice_id,observed_at,seq);
        """)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES('cursor_key',?)", (secrets.token_hex(32),))
        self.key = self.db.execute("SELECT value FROM settings WHERE key='cursor_key'").fetchone()[0].encode()

    def close(self):
        """关闭数据库连接。"""
        self.db.close()

    def import_bundle(self, bundle):
        """导入一批规范化观察。已有的观察会跳过，返回新增和重复的数量。

        先全部检查，再在一个事务里写入：要么全部导入，要么一条都不导入。
        """
        if (not isinstance(bundle, dict) or type(bundle.get("schema_version")) is not int
                or bundle["schema_version"] not in SCHEMAS
                or bundle.get("kind") != "normalized_observations"
                or bundle.get("normalizer_version") != SCHEMAS[bundle["schema_version"]][0]
                or not isinstance(bundle.get("source_id"), str)
                or bundle.get("source_id") not in SCHEMAS[bundle["schema_version"]][1]
                or type(bundle.get("simulation")) is not bool or bundle.get("coverage") != "bounded_sample"
                or bundle.get("run_status") not in ("succeeded", "partial", "failed", "blocked", "cancelled")):
            raise ValueError("unsupported_normalized_bundle")
        _string(bundle.get("run_id"), 128)
        observations, failures = bundle.get("observations"), bundle.get("failures")
        if not isinstance(observations, list) or len(observations) > 100 or not isinstance(failures, list) or len(failures) > 100:
            raise ValueError("invalid_bundle_items")
        payload = _json(bundle)
        if len(payload.encode()) > 32 * 1024 * 1024:
            raise ValueError("bundle_too_large")
        for observation in observations:
            _validate(observation, bundle)
        bundle_id = _hash(bundle)
        added = 0
        with self.db:
            for observation in observations:
                cursor = self.db.execute("INSERT OR IGNORE INTO observations(id,notice_id,observed_at,simulation,kind,title,payload) "
                                        "VALUES(?,?,?,?,?,?,?)", (observation["observation_id"], observation["notice_id"],
                    _time(observation["observed_at"]).isoformat(), observation["simulation"], observation["notice_type"],
                    observation["title"], _json(observation)))
                added += cursor.rowcount
            self.db.execute("INSERT OR IGNORE INTO imports VALUES(?,?,?)", (bundle_id, bundle["run_id"], payload))
        return {"bundle_id": bundle_id, "added": added, "duplicates": len(observations) - added,
                "run_status": bundle["run_status"], "failures": len(failures)}

    def _cursor(self, value):
        """生成翻页游标：内容加 HMAC 签名，客户端改不了。"""
        raw = _json(value).encode()
        return base64.urlsafe_b64encode(raw).decode() + "." + hmac.new(self.key, raw, sha256).hexdigest()

    def _read_cursor(self, cursor):
        """解析并验证翻页游标。"""
        try:
            if not isinstance(cursor, str) or len(cursor) > 5000:
                raise ValueError()
            encoded, signature = cursor.split(".")
            raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
            if not hmac.compare_digest(signature, hmac.new(self.key, raw, sha256).hexdigest()):
                raise ValueError()
            return json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            raise ValueError("invalid_cursor") from None

    def _current(self, snapshot):
        """在快照范围内，取每条公告的当前版本。"""
        return self.db.execute(f"""SELECT * FROM (
            SELECT *,row_number() OVER(PARTITION BY notice_id ORDER BY {OBSERVATION_ORDER}) AS rank
            FROM observations WHERE seq<=?) WHERE rank=1 ORDER BY observed_at DESC,notice_id ASC""", (snapshot,)).fetchall()

    def query(self, *, keyword="", kind=None, page_size=20, simulation=False, as_of=None, cursor=None):
        """搜索公告。第一页时固定一个快照，后续翻页都用同一个快照，结果不会因为新导入而错乱。

        同时返回最近几次导入的情况。导入失败时目录可能是空的，要让用户知道"空"是因为抓取失败，而不是没有项目。
        """
        _string(keyword, 200, empty=True)
        if kind is not None and kind not in KINDS:
            raise ValueError("invalid_kind_filter")
        if type(page_size) is not int or not 1 <= page_size <= 100 or type(simulation) is not bool:
            raise ValueError("invalid_query")
        filters = {"keyword": keyword, "kind": kind, "page_size": page_size, "simulation": simulation}
        if cursor:
            state = self._read_cursor(cursor)
            if state["filters"] != filters or (as_of is not None and _time(as_of).isoformat() != state["as_of"]):
                raise ValueError("cursor_query_mismatch")
        else:
            self.db.execute("BEGIN")
            try:
                snapshot = self.db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()[0]
                imports_snapshot = self.db.execute("SELECT coalesce(max(rowid),0) FROM imports").fetchone()[0]
            finally:
                self.db.rollback()
            state = {"snapshot": snapshot, "imports_snapshot": imports_snapshot,
                     "offset": 0, "filters": filters,
                     "as_of": _time(as_of).isoformat() if as_of else datetime.now(timezone.utc).isoformat()}
        rows = [json.loads(row["payload"]) for row in self._current(state["snapshot"])
                if bool(row["simulation"]) == simulation and (kind is None or row["kind"] == kind)]
        rows = [row for row in rows if _matches_keyword(row, keyword)]
        offset = state["offset"]
        page = rows[offset:offset + page_size]
        next_cursor = self._cursor({**state, "offset": offset + page_size}) if offset + page_size < len(rows) else None
        imports = [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM imports WHERE rowid<=? ORDER BY rowid DESC", (state["imports_snapshot"],))]
        summaries = [{"run_id": b["run_id"], "source_id": b["source_id"], "run_status": b["run_status"],
                      "observations": len(b["observations"]), "failures": len(b["failures"])}
                     for b in imports if b["simulation"] == simulation][:10]
        return {"items": [{**row, "currentness": currentness(row, state["as_of"])} for row in page],
                "total": len(rows), "next_cursor": next_cursor, "as_of": state["as_of"],
                "snapshot": state["snapshot"], "simulation": simulation, "recent_runs": summaries}

    def detail(self, notice_id, *, as_of=None):
        """公告详情：当前版本、全部历史版本、时间状态、关联公告。"""
        if not isinstance(notice_id, str) or not re.fullmatch("[0-9a-f]{64}", notice_id):
            raise ValueError("invalid_notice_id")
        # 三次读取放在同一个读事务里，期间有新导入也看不到，保证三者一致。
        self.db.execute("BEGIN")
        try:
            snapshot = self.db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()[0]
            history = [json.loads(r[0]) for r in self.db.execute(
                f"SELECT payload FROM observations WHERE notice_id=? ORDER BY {OBSERVATION_ORDER}", (notice_id,))]
            if not history:
                raise ValueError("notice_not_found")
            current = history[0]
            candidates = [json.loads(row["payload"]) for row in self._current(snapshot)]
        finally:
            self.db.rollback()
        now = _time(as_of).isoformat() if as_of else datetime.now(timezone.utc).isoformat()
        return {"current": current, "history": history, "currentness": currentness(current, now),
                "relationships": relationships(current, candidates), "snapshot": snapshot}

    def observation(self, notice_id, observation_id):
        """读取一个固定的观察。用于研究按版本取材：有了新版本，旧报告的输入也不会被替换。"""
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
               for value in (notice_id, observation_id)):
            raise ValueError("invalid_observation_id")
        row = self.db.execute("SELECT payload FROM observations WHERE notice_id=? AND id=?", (notice_id, observation_id)).fetchone()
        if row is None:
            raise ValueError("observation_not_found")
        return json.loads(row[0])

    def changes(self, after=0, limit=100):
        """按接收序号返回 after 之后的新观察，给 tracking 服务消费。

        seq 只是接收顺序，不代表公告本身的新旧。观察从不删除，所以消费方落后多少都能补上。
        注意：SQLite 的写入是串行的，seq 一定连续提交；换成 PostgreSQL 后要重新确认这一点。
        """
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_query")
        self.db.execute("BEGIN")
        try:
            high = self.db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()[0]
            if after > high:
                raise ValueError("cursor_ahead")
            rows = self.db.execute("SELECT seq,id,notice_id,observed_at FROM observations WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?",
                                   (after, high, limit)).fetchall()
            items = [{"seq": r["seq"], "event_id": "catalog:" + r["id"], "notice_id": r["notice_id"],
                      "observation_id": r["id"], "observed_at": r["observed_at"]} for r in rows]
            return {"schema_version": 1, "items": items, "next_after": rows[-1]["seq"] if rows else after, "high_watermark": high}
        finally:
            self.db.rollback()

    def bundle(self, notice_id, snapshot=None):
        """研究用的公告包：主公告的当前版本，加上有确切依据关联到它的更正、结果公告。

        只有关系状态为 evidenced（有原文链接作证）的公告才放进 observations；
        候选、有歧义、有冲突的关系只列在 relationships 里供参考，不混进研究材料。
        即使确认了更正关系，也不会自动用更正里的日期覆盖原公告的截止时间。
        """
        if not isinstance(notice_id, str) or not re.fullmatch(r"[0-9a-f]{64}", notice_id):
            raise ValueError("invalid_notice_id")
        self.db.execute("BEGIN")
        try:
            high = self.db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()[0]
            if snapshot is None:
                snapshot = high
            if type(snapshot) is not int or not 0 <= snapshot <= high:
                raise ValueError("invalid_snapshot")
            candidates = [json.loads(r["payload"]) for r in self._current(snapshot)]
            current = next((o for o in candidates if o["notice_id"] == notice_id), None)
            if current is None:
                raise ValueError("notice_not_found")
            incoming, related = [], []
            for item in candidates:
                if item["notice_id"] == notice_id or item["simulation"] != current["simulation"]:
                    continue
                for relation in relationships(item, candidates):
                    if (relation.get("target_notice_id") == notice_id
                            or relation.get("target_observation_id") == current["observation_id"]):
                        record = {**relation, "source_notice_id": item["notice_id"], "source_observation_id": item["observation_id"]}
                        incoming.append(record)
                        if relation.get("status") == "evidenced" and item not in related:
                            related.append(item)
            related.sort(key=lambda o: (o["observed_at"], o["notice_id"]))
            return {"notice_id": notice_id, "snapshot": snapshot, "current": current,
                    "observations": [current, *related], "relationships": incoming + relationships(current, candidates)}
        finally:
            self.db.rollback()


def relationships(current, candidates):
    """找出一条更正或结果公告对应的原采购公告。

    判断依据，按可靠程度从高到低：
        1. 原文里有指向原公告的链接，并且目录里能找到 → evidenced（确认）
        2. 同一发布方、同一采购人、同一项目编号 → candidate（候选，需要人工确认）
        3. 都找不到 → unresolved
    包号对不上时标为 conflicting；匹配到多个时标为 ambiguous。
    只建立关系，不合并两条公告，也不改写原公告的内容。
    """
    if current["notice_type"] not in ("correction", "award"):
        return []
    contract_reference = current["facts"].get("original_contract_reference", {}).get("evidence", [])
    if contract_reference:
        # 引用的是"原合同"而不是"原采购公告"，不能当成采购更正来关联，哪怕项目编号相同。
        return [{"status": "unresolved", "basis": "original_contract_outside_scope",
                 "source_observation_id": current["observation_id"], "evidence": contract_reference}]
    def value(item, name):
        fact = item["facts"][name]
        return fact["value"] if fact["status"] == "known" else None
    def different_lot(other):
        return (value(current, "lot_identifier") and value(other, "lot_identifier")
                and value(current, "lot_identifier") != value(other, "lot_identifier"))
    family = SOURCE_FAMILIES.get(current["source_id"])
    target_kinds = ("procurement", "correction") if current["notice_type"] == "award" else ("procurement",)
    pool = [c for c in candidates if family is not None and SOURCE_FAMILIES.get(c["source_id"]) == family
            and c["simulation"] == current["simulation"] and c["notice_type"] in target_kinds]
    explicit = [(other, ref) for other in pool for ref in current["references"]
                if other["identity_kind"] == "source_url" and other["source_record_key"] == ref["url"]]
    if explicit:
        conflicts = [(other, ref) for other, ref in explicit if different_lot(other)]
        if conflicts:
            # 链接对上了但包号不一致：标为冲突，两边的原文都保留，不建立关联。
            return [{"status": "conflicting", "basis": "original_link_lot_mismatch",
                     "source_observation_id": current["observation_id"], "target_observation_id": other["observation_id"],
                     "evidence": [ref] + current["facts"]["lot_identifier"]["evidence"]
                     + other["facts"]["lot_identifier"]["evidence"]} for other, ref in conflicts]
        matches = {other["notice_id"]: (other, ref) for other, ref in explicit}
        return [{"target_notice_id": other["notice_id"], "source_observation_id": current["observation_id"],
                 "target_observation_id": other["observation_id"], "status": "evidenced" if len(matches) == 1 else "ambiguous",
                 "basis": "explicit_original_link", "evidence": [ref]} for other, ref in matches.values()]
    if current["references"]:
        return [{"status": "unresolved", "basis": "original_link_not_in_catalog", "evidence": current["references"]}]
    buyer, number = value(current, "buyer"), value(current, "project_number")
    if not buyer or not number:
        return [{"status": "unresolved", "basis": "missing_buyer_or_project_number", "evidence": []}]
    possible = [other for other in pool if value(other, "buyer") == buyer and value(other, "project_number") == number
                and not different_lot(other)]
    if not possible:
        return [{"status": "unresolved", "basis": "no_matching_original", "evidence": []}]
    return [{"target_notice_id": other["notice_id"], "source_observation_id": current["observation_id"],
             "target_observation_id": other["observation_id"], "status": "candidate" if len(possible) == 1 else "ambiguous",
             "target_notice_type": other["notice_type"], "source_notice_type": current["notice_type"],
             "basis": ("same_source_buyer_project_number" if other["source_id"] == current["source_id"]
                       else "same_publisher_buyer_project_number"), "evidence": current["facts"]["project_number"]["evidence"]
             + current["facts"]["buyer"]["evidence"]} for other in possible]

"""目录拥有自己的SQLite，只接收规范观察契约，绝不跨库读取获取原件。

阅读顺序：import_bundle事务 → query固定快照 → detail的更正证据关系。
同一公告的观察不可改写；旧观察晚到也不能回退当前内容；原公告与更正分开。
这是可信单用户本地开发后端，不能作为企业私有资料/多租户授权接口。
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3

VERSION = "procurement-facts-v2"
KINDS = {"procurement", "correction", "award", "termination", "intention", "unknown"}
FIELDS = {"project_number", "buyer", "published_at", "budget", "response_deadline", "lot_identifier"}
V2_FIELDS = FIELDS | {"original_published_at", "original_contract_reference"}
# 发布方映射属于目录关系契约；只列已核实资源，不能按tj_前缀放宽关联范围。
SOURCE_FAMILIES = {"cn_ccgp": "cn_ccgp", "tj_procurement_negotiation": "tj_finance_procurement",
                   "tj_procurement_consultation": "tj_finance_procurement",
                   "tj_procurement_correction": "tj_finance_procurement"}
SCHEMAS = {1: ("procurement-facts-v1", {"cn_ccgp", "tj_procurement_negotiation"}),
           2: (VERSION, set(SOURCE_FAMILIES))}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return sha256(_json(value).encode()).hexdigest()


def _time(value):
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
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError("invalid_string")


def _validate(observation, bundle):
    """目录边界重新检查类型与内容哈希；生产身份认证不由这个本地哈希提供。"""
    if not isinstance(observation, dict):
        raise ValueError("invalid_observation")
    required = {"observation_id", "notice_id", "source_id", "simulation", "source_record_key", "identity_kind",
                "source_url", "capture_id", "raw_sha256", "parser_version", "normalizer_version", "observed_at",
                "title", "notice_type", "facts", "references", "material_status", "attribution", "run_id"}
    if bundle["schema_version"] == 2:
        required.add("material_reference_evidence")
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
    expected_fields = V2_FIELDS if bundle["schema_version"] == 2 else FIELDS
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
    if bundle["schema_version"] == 2:
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
    """查询时计算时间状态：获取成功不等于可参与；未写时区的截止不作过期推断。"""
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


class Catalog:
    def __init__(self, root):
        root = Path(root).absolute()
        if root.is_symlink():
            raise ValueError("store_symlink")
        root.mkdir(parents=True, exist_ok=True)
        marker = root / ".bidradar-catalog-v1"
        if not marker.exists() and any(root.iterdir()):
            raise ValueError("catalog_directory_not_empty")
        marker.touch(exist_ok=True)
        if (root / "catalog.sqlite3").is_symlink():
            raise ValueError("store_symlink")
        self.db = sqlite3.connect(root / "catalog.sqlite3", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        # seq只表示目录接收顺序；当前版本仍先按实际获取时间选，防晚到旧数据回退。
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
        self.db.close()

    def import_bundle(self, bundle):
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
        # 全部类型验证在事务前完成；唯一键与事务保证重复/崩溃不留下半包新观察。
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
        raw = _json(value).encode()
        return base64.urlsafe_b64encode(raw).decode() + "." + hmac.new(self.key, raw, sha256).hexdigest()

    def _read_cursor(self, cursor):
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
        return self.db.execute("""SELECT * FROM (
            SELECT *,row_number() OVER(PARTITION BY notice_id ORDER BY observed_at DESC,seq DESC) AS rank
            FROM observations WHERE seq<=?) WHERE rank=1 ORDER BY observed_at DESC,notice_id ASC""", (snapshot,)).fetchall()

    def query(self, *, keyword="", kind=None, page_size=20, simulation=False, as_of=None, cursor=None):
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
        # 按固定接收序号截断后再选当时最新；分页期间新导入/旧观察不改变此快照。
        rows = [json.loads(row["payload"]) for row in self._current(state["snapshot"])
                if bool(row["simulation"]) == simulation and (kind is None or row["kind"] == kind)
                and keyword.casefold() in row["title"].casefold()]
        offset = state["offset"]
        page = rows[offset:offset + page_size]
        next_cursor = self._cursor({**state, "offset": offset + page_size}) if offset + page_size < len(rows) else None
        # 即使本次失败没有任何观察，也显示最近获取状态，不能让空目录冒充成功零结果。
        imports = [json.loads(r[0]) for r in self.db.execute(
            "SELECT payload FROM imports WHERE rowid<=? ORDER BY rowid DESC", (state["imports_snapshot"],))]
        summaries = [{"run_id": b["run_id"], "source_id": b["source_id"], "run_status": b["run_status"],
                      "observations": len(b["observations"]), "failures": len(b["failures"])}
                     for b in imports if b["simulation"] == simulation][:10]
        return {"items": [{**row, "currentness": currentness(row, state["as_of"])} for row in page],
                "total": len(rows), "next_cursor": next_cursor, "as_of": state["as_of"],
                "snapshot": state["snapshot"], "simulation": simulation, "recent_runs": summaries}

    def detail(self, notice_id, *, as_of=None):
        if not isinstance(notice_id, str) or not re.fullmatch("[0-9a-f]{64}", notice_id):
            raise ValueError("invalid_notice_id")
        # 一个读事务固定详情/历史/关联所用版本，防并发导入在三次读取之间切换依据。
        self.db.execute("BEGIN")
        try:
            snapshot = self.db.execute("SELECT coalesce(max(seq),0) FROM observations").fetchone()[0]
            history = [json.loads(r[0]) for r in self.db.execute(
                "SELECT payload FROM observations WHERE notice_id=? ORDER BY observed_at DESC,seq DESC", (notice_id,))]
            if not history:
                raise ValueError("notice_not_found")
            current = history[0]
            candidates = [json.loads(row["payload"]) for row in self._current(snapshot)]
        finally:
            self.db.rollback()
        now = _time(as_of).isoformat() if as_of else datetime.now(timezone.utc).isoformat()
        return {"current": current, "history": history, "currentness": currentness(current, now),
                "relationships": relationships(current, candidates), "snapshot": snapshot}


def relationships(current, candidates):
    """关系每次绑定当前两端观察，不将旧关联自动套用到新正文；只有更正作为起点。"""
    if current["notice_type"] != "correction":
        return []
    contract_reference = current["facts"].get("original_contract_reference", {}).get("evidence", [])
    if contract_reference:
        # “原合同”不是“原采购公告”。即使同项目号也不建立错误的采购更正关系。
        # v1没有此字段，按旧行为读取；v2始终保留它的原文和定位。
        return [{"status": "unresolved", "basis": "original_contract_outside_scope",
                 "source_observation_id": current["observation_id"], "evidence": contract_reference}]
    def value(item, name):
        fact = item["facts"][name]
        return fact["value"] if fact["status"] == "known" else None
    def different_lot(other):
        return (value(current, "lot_identifier") and value(other, "lot_identifier")
                and value(current, "lot_identifier") != value(other, "lot_identifier"))
    family = SOURCE_FAMILIES.get(current["source_id"])
    pool = [c for c in candidates if family is not None and SOURCE_FAMILIES.get(c["source_id"]) == family
            and c["simulation"] == current["simulation"] and c["notice_type"] == "procurement"]
    explicit = [(other, ref) for other in pool for ref in current["references"]
                if other["identity_kind"] == "source_url" and other["source_record_key"] == ref["url"]]
    if explicit:
        conflicts = [(other, ref) for other, ref in explicit if different_lot(other)]
        if conflicts:
            # 原文链接不能推翻已知包号冲突；保留两边版本供核对，不建立可用关联。
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
             "basis": ("same_source_buyer_project_number" if other["source_id"] == current["source_id"]
                       else "same_publisher_buyer_project_number"), "evidence": current["facts"]["project_number"]["evidence"]
             + current["facts"]["buyer"]["evidence"]} for other in possible]

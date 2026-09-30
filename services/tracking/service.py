"""R2业务边界：当前授权 → 固定输入 → 本库事务 → 可恢复跨服务命令。

access/catalog/research为注入的有界服务客户端。远程授权与本库提交没有分布式
事务；每个后续动作重新核查，不能声称能撤回已经发出的外部调用。
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re

from .diff import RULE_VERSION, SCOPES, canonical, compare_bundles, fingerprint, input_fingerprint
from .store import TrackingError, identifier, integer, new_id, now, text, utc

TERMINAL = frozenset(("succeeded", "partial", "failed", "cancelled", "rejected", "waiting_input"))
RUN_STATES = frozenset(("queued", "running", "waiting_input", "retry_wait", "succeeded", "partial", "failed", "cancelled"))
DELEGATION_FIELDS = {"watch_id", "watch_version", "rule_version", "change_id", "change_fingerprint", "command_key", "input_fingerprint", "membership_version"}


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except TrackingError:
        raise
    except Exception as error:
        # 跨服务异常只能给最小分类；不反射供应商消息、URL、请求或私有正文。
        status = getattr(error, "status", 503)
        if status in (403, 404):
            raise TrackingError("not_found" if status == 404 else "forbidden", status) from None
        if status in (409, 429):
            raise TrackingError("dependency_conflict" if status == 409 else "quota_exceeded", status) from None
        raise TrackingError("dependency_unavailable", 503) from None


def _bundle(value, notice_id):
    try:
        if (not isinstance(value, dict) or value["notice_id"] != notice_id or type(value["snapshot"]) is not int
                or value["snapshot"] < 0 or not isinstance(value["observations"], list)
                or not 1 <= len(value["observations"]) <= 100 or not isinstance(value["relationships"], list)
                or value["current"] != value["observations"][0]):
            raise ValueError()
        seen = set()
        for observation in value["observations"]:
            identifier(observation["notice_id"])
            identifier(observation["observation_id"])
            if observation["notice_id"] in seen:
                raise ValueError()
            seen.add(observation["notice_id"])
        if value["current"]["notice_id"] != notice_id or len(canonical(value).encode()) > 8 * 1024 * 1024:
            raise ValueError()
    except (KeyError, TypeError, ValueError, TrackingError):
        raise TrackingError("invalid_catalog_response", 503) from None
    return value


def _expiry(value):
    if value is None:
        return utc(now() + 7 * 86400)
    try:
        text(value, 40)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0 or not now() < parsed.timestamp() <= now() + 7 * 86400:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, TypeError):
        raise TrackingError("invalid_delegation_expiry") from None


class TrackingService:
    def __init__(self, store, access, catalog, research):
        self.store, self.access, self.catalog, self.research = store, access, catalog, research
        self.db = store.db

    def _authorize(self, actor_id, workspace_id, action="read"):
        text(actor_id); text(workspace_id)
        value = _call(self.access.authorize, actor_id, workspace_id, action)
        if (not isinstance(value, dict) or value.get("role") not in ("admin", "member", "viewer")
                or type(value.get("membership_version")) is not int or value["membership_version"] < 1
                or type(value.get("current_profile_revision")) is not int or value["current_profile_revision"] < 0):
            raise TrackingError("invalid_access_response", 503)
        if action in ("track", "analyze") and value["role"] == "viewer":
            raise TrackingError("forbidden", 403)
        return value

    def decision(self, actor_id, workspace_id, notice_id):
        self._authorize(actor_id, workspace_id); identifier(notice_id)
        with self.store.tx(write=False):
            rows = self.db.execute("SELECT payload FROM decision_history WHERE workspace_id=? AND notice_id=? ORDER BY version DESC LIMIT 201", (workspace_id, notice_id)).fetchall()
            if len(rows) > 200:
                raise TrackingError("history_limit", 413)
            history = [json.loads(row[0]) for row in rows]
        self._authorize(actor_id, workspace_id)
        return {"schema_version": 1, "current": history[0] if history else None, "history": history}

    def set_decision(self, actor_id, workspace_id, notice_id, state, reason, expected_version, key):
        self._authorize(actor_id, workspace_id, "track")
        identifier(notice_id); integer(expected_version); text(reason, 4000, empty=True)
        if state not in ("needs_review", "follow_up", "dismissed"):
            raise TrackingError("invalid_decision")
        request = [actor_id, notice_id, state, reason, expected_version]
        with self.store.tx(write=False):
            _, cached = self.store.cached(workspace_id, "decision", key, request)
        if cached is not None:
            return cached
        bundle = _bundle(_call(self.catalog.bundle, notice_id), notice_id)
        self._authorize(actor_id, workspace_id, "track")
        with self.store.tx():
            digest, cached = self.store.cached(workspace_id, "decision", key, request)
            if cached is not None:
                return cached
            old = self.db.execute("SELECT version FROM decisions WHERE workspace_id=? AND notice_id=?", (workspace_id, notice_id)).fetchone()
            if (old[0] if old else 0) != expected_version:
                raise TrackingError("decision_version_conflict", 409)
            value = {"schema_version": 1, "workspace_id": workspace_id, "notice_id": notice_id, "version": expected_version + 1,
                     "state": state, "reason": reason, "actor_id": actor_id, "recorded_at": utc(),
                     "observation_id": bundle["current"]["observation_id"]}
            raw = canonical(value)
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?) ON CONFLICT(workspace_id,notice_id) DO UPDATE SET version=excluded.version,payload=excluded.payload",
                            (workspace_id, notice_id, value["version"], raw))
            self.db.execute("INSERT INTO decision_history VALUES(?,?,?,?)", (workspace_id, notice_id, value["version"], raw))
            return self.store.remember(workspace_id, "decision", key, digest, value)

    def watch(self, actor_id, workspace_id, notice_id):
        self._authorize(actor_id, workspace_id); identifier(notice_id)
        row = self.db.execute("SELECT * FROM watches WHERE workspace_id=? AND notice_id=?", (workspace_id, notice_id)).fetchone()
        return {"schema_version": 1, "watch": self._watch_value(row) if row else None}

    @staticmethod
    def _watch_value(row):
        value = json.loads(row["payload"])
        return {**value, "catalog_seq": row["catalog_seq"], "profile_revision": row["profile_revision"]}

    def set_watch(self, actor_id, workspace_id, notice_id, active, expected_version, key, *, auto_reassess=False, expires_at=None, scope=None):
        auth = self._authorize(actor_id, workspace_id, "track")
        identifier(notice_id); integer(expected_version)
        if type(active) is not bool or type(auto_reassess) is not bool or (auto_reassess and not active):
            raise TrackingError("invalid_watch")
        scope = sorted(SCOPES) if scope is None else scope
        if not isinstance(scope, list) or not scope or len(set(scope)) != len(scope) or any(not isinstance(s, str) or s not in SCOPES for s in scope):
            raise TrackingError("invalid_watch_scope")
        if not auto_reassess and expires_at is not None:
            raise TrackingError("unexpected_expiry")
        request = [actor_id, notice_id, active, auto_reassess, expected_version, expires_at, sorted(scope)]
        with self.store.tx(write=False):
            _, cached = self.store.cached(workspace_id, "watch", key, request)
        if cached is not None:
            return cached
        # 停止操作不依赖目录在线；新建/重新启用才从明确baseline开始。
        old = self.db.execute("SELECT * FROM watches WHERE workspace_id=? AND notice_id=?", (workspace_id, notice_id)).fetchone()
        bundle = _bundle(_call(self.catalog.bundle, notice_id), notice_id) if old is None or active and not old["active"] else json.loads(old["bundle"])
        auth = self._authorize(actor_id, workspace_id, "analyze" if auto_reassess else "track")
        expiry = _expiry(expires_at) if auto_reassess else None
        if auto_reassess and auth["current_profile_revision"] < 1:
            raise TrackingError("profile_required", 409)
        with self.store.tx():
            digest, cached = self.store.cached(workspace_id, "watch", key, request)
            if cached is not None:
                return cached
            current = self.db.execute("SELECT * FROM watches WHERE workspace_id=? AND notice_id=?", (workspace_id, notice_id)).fetchone()
            if (current["version"] if current else 0) != expected_version:
                raise TrackingError("watch_version_conflict", 409)
            watch_id = current["id"] if current else new_id()
            value = {"schema_version": 1, "id": watch_id, "workspace_id": workspace_id, "notice_id": notice_id,
                     "version": expected_version + 1, "active": active, "auto_reassess": auto_reassess,
                     "actor_id": actor_id, "membership_version": auth["membership_version"], "expires_at": expiry,
                     "rule_version": RULE_VERSION, "scope": sorted(scope), "recorded_at": utc(),
                     "warnings": ["unstable_content_snapshot_identity"] if bundle["current"].get("identity_kind") == "content_snapshot" else []}
            # 已存在且未重新启用时保留消费基线，配置保存不能跳过尚未处理的变化。
            base = current if current and current["active"] else None
            catalog_seq = base["catalog_seq"] if base else bundle["snapshot"]
            profile_revision = base["profile_revision"] if base else auth["current_profile_revision"]
            if base:
                bundle = json.loads(base["bundle"])
            self.db.execute("INSERT INTO watches VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id,notice_id) DO UPDATE SET "
                            "version=excluded.version,active=excluded.active,payload=excluded.payload,bundle=excluded.bundle,catalog_seq=excluded.catalog_seq,profile_revision=excluded.profile_revision",
                            (watch_id, workspace_id, notice_id, value["version"], int(active), canonical(value), canonical(bundle), catalog_seq, profile_revision))
            self.db.execute("INSERT INTO watch_history VALUES(?,?,?)", (watch_id, value["version"], canonical(value)))
            # 修改规则/关闭会使全部旧委托失效；保留run引用供后续取消对账。
            rows = self.db.execute("SELECT id,run_id FROM reassessments WHERE watch_id=? AND state NOT IN ('succeeded','partial','failed','cancelled','rejected','waiting_input')", (watch_id,)).fetchall()
            for row in rows:
                self.db.execute("UPDATE reassessments SET state='cancelled',reason='watch_changed',version=version+1,lease_epoch=lease_epoch+1,lease_until=0,next_attempt=0 WHERE id=?", (row["id"],))
                self.store.reassessment_event(row["id"], "cancelled", "watch_changed", row["run_id"])
            return self.store.remember(workspace_id, "watch", key, digest, {**value, "catalog_seq": catalog_seq, "profile_revision": profile_revision})

    def _page(self, actor_id, workspace_id, table, after=0, limit=20):
        self._authorize(actor_id, workspace_id); integer(after); integer(limit, 1, 100)
        with self.store.tx(write=False):
            rows = self.db.execute(f"SELECT seq,payload FROM {table} WHERE workspace_id=? AND seq>? ORDER BY seq LIMIT ?", (workspace_id, after, limit)).fetchall()
            high = self.db.execute(f"SELECT coalesce(max(seq),0) FROM {table} WHERE workspace_id=?", (workspace_id,)).fetchone()[0]
            values = [{**json.loads(row["payload"]), "seq": row["seq"]} for row in rows]
            if table == "notifications":
                for value in values:
                    read = self.db.execute("SELECT read_at FROM notification_reads WHERE workspace_id=? AND notification_id=? AND user_id=?", (workspace_id, value["id"], actor_id)).fetchone()
                    value["read_at"] = read[0] if read else None
        self._authorize(actor_id, workspace_id)
        return {"schema_version": 1, "items": values, "next_after": rows[-1]["seq"] if rows else after, "high_watermark": high}

    def changes(self, actor_id, workspace_id, after=0, limit=20):
        return self._page(actor_id, workspace_id, "changes", after, limit)

    def notifications(self, actor_id, workspace_id, after=0, limit=20):
        return self._page(actor_id, workspace_id, "notifications", after, limit)

    def mark_read(self, actor_id, workspace_id, notification_id):
        self._authorize(actor_id, workspace_id); text(notification_id)
        with self.store.tx():
            if not self.db.execute("SELECT 1 FROM notifications WHERE workspace_id=? AND id=?", (workspace_id, notification_id)).fetchone():
                raise TrackingError("not_found", 404)
            self.db.execute("INSERT INTO notification_reads VALUES(?,?,?,?) ON CONFLICT(workspace_id,notification_id,user_id) DO NOTHING", (workspace_id, notification_id, actor_id, utc()))
            return {"schema_version": 1, "id": notification_id, "read_at": self.db.execute("SELECT read_at FROM notification_reads WHERE workspace_id=? AND notification_id=? AND user_id=?", (workspace_id, notification_id, actor_id)).fetchone()[0]}

    def _event(self, owner, event):
        common = {"seq", "event_id"}
        fields = common | ({"notice_id", "observation_id", "observed_at"} if owner == "catalog" else
                            {"event_type", "workspace_id", "actor_id", "target_user_id", "profile_revision", "membership_version", "occurred_at"})
        if not isinstance(event, dict) or set(event) != fields:
            raise TrackingError("invalid_event", 503)
        integer(event["seq"], 1); text(event["event_id"])
        if owner == "catalog":
            identifier(event["notice_id"]); identifier(event["observation_id"])
        else:
            text(event["workspace_id"])
            if event["event_type"] not in ("ProfileConfirmed", "AccessChanged"):
                raise TrackingError("unsupported_event", 503)
            if event["event_type"] == "ProfileConfirmed":
                integer(event["profile_revision"], 1)
            else:
                integer(event["membership_version"], 1); text(event["target_user_id"])

    def consume_event(self, owner, event, *, replay=False):
        """先按每个watch补齐固定snapshot，再同事务接受事件/游标/变化/提醒/复核。

        Inbox已有事件仍可供新watch追赶；watch自己的catalog_seq防止重复或回退。
        全局cursor只连续前进，不能用MAX已见事件掩盖缺口。
        """
        if owner not in ("catalog", "workspace") or type(replay) is not bool:
            raise TrackingError("invalid_event_owner")
        self._event(owner, event)
        seq, digest = event["seq"], fingerprint(event)
        cursor = self.store.cursor(owner)
        if seq > cursor + 1:
            raise TrackingError("event_gap", 409)
        existing = self.db.execute("SELECT * FROM inbox WHERE owner=? AND seq=?", (owner, seq)).fetchone()
        if existing and (existing["digest"] != digest or existing["event_id"] != event["event_id"]):
            raise TrackingError("event_conflict", 409)
        if seq <= cursor and existing is None:
            raise TrackingError("event_gap", 409)
        rows = self.db.execute("SELECT * FROM watches WHERE active=1 ORDER BY id").fetchall()
        plans = []
        for row in rows:
            watch = json.loads(row["payload"])
            if owner == "catalog":
                if row["catalog_seq"] >= seq:
                    continue
                bundle = _bundle(_call(self.catalog.bundle, row["notice_id"], snapshot=seq), row["notice_id"])
                if bundle["snapshot"] != seq:
                    raise TrackingError("catalog_snapshot_mismatch", 503)
                diff = compare_bundles(json.loads(row["bundle"]), bundle)
                revision = row["profile_revision"]
            else:
                if event["workspace_id"] != row["workspace_id"]:
                    continue
                if event["event_type"] == "AccessChanged":
                    if event["target_user_id"] == watch["actor_id"] and event["membership_version"] > watch["membership_version"]:
                        plans.append((row, None, None, None, None))
                    continue
                if event["profile_revision"] <= row["profile_revision"]:
                    continue
                revision, bundle = event["profile_revision"], json.loads(row["bundle"])
                diff = {"schema_version": 1, "rule_version": RULE_VERSION, "kind": "profile_change", "scopes": ["profile"],
                        "entries": [{"scope": "profile", "path": "profile_revision", "before": row["profile_revision"], "after": revision}],
                        "unclassified": [], "before_observation_ids": [x["observation_id"] for x in bundle["observations"]],
                        "after_observation_ids": [x["observation_id"] for x in bundle["observations"]]}
            auth = None
            if diff["kind"] != "no_business_change" and watch["auto_reassess"] and not replay:
                try:
                    auth = self._authorize(watch["actor_id"], watch["workspace_id"], "analyze")
                except TrackingError as exc:
                    if exc.status not in (403, 404):
                        raise
            plans.append((row, bundle, diff, revision, auth))
        with self.store.tx():
            current_cursor = self.store.cursor(owner)
            if seq > current_cursor + 1:
                raise TrackingError("event_gap", 409)
            for old, bundle, diff, revision, auth in plans:
                row = self.store.watch_row(old["workspace_id"], old["id"])
                # 远程读取期间配置或另一个消费者推进则整体重试，不带过期基线提交。
                if (row["version"], row["catalog_seq"], row["profile_revision"]) != (old["version"], old["catalog_seq"], old["profile_revision"]):
                    raise TrackingError("watch_changed_during_consume", 409)
                watch = json.loads(row["payload"])
                if diff is None:
                    self._invalidate_commands(watch["id"], "access_changed")
                    continue
                self.db.execute("UPDATE watches SET bundle=?,catalog_seq=?,profile_revision=? WHERE id=?",
                                (canonical(bundle), seq if owner == "catalog" else row["catalog_seq"], revision, row["id"]))
                if diff["kind"] == "no_business_change":
                    continue
                change = {"schema_version": 1, "id": new_id(), "workspace_id": row["workspace_id"], "watch_id": row["id"],
                          "watch_version": row["version"], "notice_id": row["notice_id"], "owner": owner, "event_id": event["event_id"],
                          "event_seq": seq, "rule_version": RULE_VERSION, "created_at": utc(), "catalog_snapshot": bundle["snapshot"],
                          "profile_revision": revision, "diff": diff, "stale": True, "replay": replay}
                change["fingerprint"] = fingerprint([owner, event["event_id"], diff, revision])
                existing_change = self.db.execute("SELECT 1 FROM changes WHERE watch_id=? AND rule_version=? AND fingerprint=?", (watch["id"], RULE_VERSION, change["fingerprint"])).fetchone()
                if existing_change:
                    continue
                self.db.execute("INSERT INTO changes(id,workspace_id,watch_id,rule_version,fingerprint,payload) VALUES(?,?,?,?,?,?)",
                                (change["id"], row["workspace_id"], row["id"], RULE_VERSION, change["fingerprint"], canonical(change)))
                self.store.notify(watch, change, "change_detected", replay=replay)
                permitted = (auth is not None and auth["membership_version"] == watch["membership_version"]
                             and auth["current_profile_revision"] == revision and watch["expires_at"] is not None
                             and watch["expires_at"] > utc() and bool(set(diff["scopes"]) & set(watch["scope"])))
                if permitted:
                    self._queue_reassessment(watch, change, bundle, revision)
            if existing is None:
                self.db.execute("INSERT INTO inbox VALUES(?,?,?,?,?)", (owner, seq, event["event_id"], digest, int(replay)))
            self.db.execute("INSERT INTO cursors VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET seq=max(seq,excluded.seq)", (owner, seq))
        return {"schema_version": 1, "owner": owner, "after": self.store.cursor(owner)}

    def _invalidate_commands(self, watch_id, reason):
        rows = self.db.execute("SELECT id,run_id FROM reassessments WHERE watch_id=? AND state NOT IN ('succeeded','partial','failed','cancelled','rejected','waiting_input')", (watch_id,)).fetchall()
        for row in rows:
            self.db.execute("UPDATE reassessments SET state='cancelled',reason=?,version=version+1,lease_epoch=lease_epoch+1,lease_until=0,next_attempt=0 WHERE id=?", (reason, row["id"]))
            self.store.reassessment_event(row["id"], "cancelled", reason, row["run_id"])

    def _queue_reassessment(self, watch, change, bundle, profile_revision):
        # 变化历史逐条保留，但新的自动复核接替尚未完成的旧输入，避免旧结果冒充当前。
        self._invalidate_commands(watch["id"], "input_superseded")
        command_id = new_id()
        command_key = "tracking:" + command_id
        ids = [item["observation_id"] for item in bundle["observations"]]
        delegation = {"watch_id": watch["id"], "watch_version": watch["version"], "rule_version": RULE_VERSION,
                      "change_id": change["id"], "change_fingerprint": change["fingerprint"], "command_key": command_key,
                      "input_fingerprint": input_fingerprint(watch["notice_id"], profile_revision, bundle["snapshot"], ids),
                      "membership_version": watch["membership_version"]}
        request = {"schema_version": 1, "actor_id": watch["actor_id"], "workspace_id": watch["workspace_id"], "notice_id": watch["notice_id"],
                   "key": command_key, "mode": "agent", "kind": "reassessment", "profile_revision": profile_revision,
                   "catalog_snapshot": bundle["snapshot"], "delegation": delegation}
        self.db.execute("INSERT INTO reassessments VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (command_id, watch["workspace_id"], watch["id"], change["id"], command_key, canonical(request), "pending", None, 1, 0, 0, 0, 0, None, None))
        self.store.reassessment_event(command_id, "pending")

    def poll(self, owner, *, limit=20, replay=False):
        integer(limit, 1, 100)
        if owner not in ("catalog", "workspace"):
            raise TrackingError("invalid_event_owner")
        after = self.store.cursor(owner)
        if owner == "catalog":
            row = self.db.execute("SELECT min(catalog_seq) FROM watches WHERE active=1").fetchone()
            if row[0] is not None:
                after = min(after, row[0])
        page = _call(self.catalog.changes if owner == "catalog" else self.access.events, after, limit)
        if not isinstance(page, dict) or set(page) != {"schema_version", "items", "next_after", "high_watermark"} or page["schema_version"] != 1:
            raise TrackingError("invalid_event_page", 503)
        items = page["items"]
        if not isinstance(items, list) or len(items) > limit or type(page["high_watermark"]) is not int:
            raise TrackingError("invalid_event_page", 503)
        expected = after
        for event in items:
            self._event(owner, event)
            if event["seq"] != expected + 1:
                raise TrackingError("event_gap", 409)
            expected = event["seq"]
        if page["next_after"] != expected or page["high_watermark"] < expected or not items and page["high_watermark"] > after:
            raise TrackingError("event_gap", 409)
        for event in items:
            self.consume_event(owner, event, replay=replay)
        return {"schema_version": 1, "owner": owner, "processed": len(items), "next_after": expected, "high_watermark": page["high_watermark"]}

    def check_delegation(self, actor_id, workspace_id, delegation):
        auth = self._authorize(actor_id, workspace_id, "analyze")
        if not isinstance(delegation, dict) or set(delegation) != DELEGATION_FIELDS:
            raise TrackingError("invalid_delegation")
        with self.store.tx(write=False):
            row = self.store.watch_row(workspace_id, text(delegation["watch_id"]))
            watch = json.loads(row["payload"])
            command = self.db.execute("SELECT * FROM reassessments WHERE workspace_id=? AND watch_id=? AND command_key=?", (workspace_id, row["id"], text(delegation["command_key"]))).fetchone()
            if command is None:
                raise TrackingError("not_found", 404)
            request = json.loads(command["request"])
            if (delegation != request["delegation"] or request["actor_id"] != actor_id or not row["active"] or not watch["auto_reassess"]
                    or watch["version"] != delegation["watch_version"] or auth["membership_version"] != watch["membership_version"]
                    or auth["current_profile_revision"] != request["profile_revision"] or watch["expires_at"] is None
                    or watch["expires_at"] <= utc() or command["state"] not in ("pending", "accepted")):
                raise TrackingError("delegation_invalid", 409)
        bundle = _bundle(_call(self.catalog.bundle, request["notice_id"], snapshot=request["catalog_snapshot"]), request["notice_id"])
        ids = [item["observation_id"] for item in bundle["observations"]]
        if input_fingerprint(request["notice_id"], request["profile_revision"], request["catalog_snapshot"], ids) != delegation["input_fingerprint"]:
            raise TrackingError("delegation_input_mismatch", 409)
        # 目录读取期间可能有人停watch或撤权；授权不可缓存到后续模型动作。
        latest_auth = self._authorize(actor_id, workspace_id, "analyze")
        with self.store.tx(write=False):
            latest = self.store.watch_row(workspace_id, watch["id"])
            latest_command = self.db.execute("SELECT state FROM reassessments WHERE id=?", (command["id"],)).fetchone()
            if (latest["version"] != watch["version"] or not latest["active"]
                    or latest_command[0] not in ("pending", "accepted")
                    or latest_auth["membership_version"] != watch["membership_version"]
                    or latest_auth["current_profile_revision"] != request["profile_revision"] or watch["expires_at"] <= utc()):
                raise TrackingError("delegation_invalid", 409)
        return {"valid": True, "notice_id": request["notice_id"], "profile_revision": request["profile_revision"],
                "catalog_snapshot": request["catalog_snapshot"], "observation_ids": ids}

    def reassessments(self, actor_id, workspace_id):
        self._authorize(actor_id, workspace_id)
        rows = self.db.execute("SELECT * FROM reassessments WHERE workspace_id=? ORDER BY rowid DESC LIMIT 101", (workspace_id,)).fetchall()
        if len(rows) > 100:
            raise TrackingError("list_limit", 413)
        return {"schema_version": 1, "items": [self._run_value(row) for row in rows]}

    @staticmethod
    def _run_value(row):
        return {key: row[key] for key in ("id", "workspace_id", "watch_id", "change_id", "command_key", "state", "run_id", "version", "reason", "attempts")}

    def claim(self, worker_id, *, lease_seconds=30):
        text(worker_id); integer(lease_seconds, 1, 120)
        with self.store.tx():
            row = self.db.execute("SELECT * FROM reassessments WHERE state IN ('pending','accepted') AND lease_until<=? AND next_attempt<=? ORDER BY rowid LIMIT 1", (now(), now())).fetchone()
            if row is None:
                return None
            epoch = row["lease_epoch"] + 1
            self.db.execute("UPDATE reassessments SET lease_epoch=?,lease_until=? WHERE id=?", (epoch, now() + lease_seconds, row["id"]))
            return {"id": row["id"], "lease_epoch": epoch, "request": json.loads(row["request"]), "state": row["state"], "run_id": row["run_id"]}

    def _leased(self, claim):
        row = self.db.execute("SELECT * FROM reassessments WHERE id=?", (claim["id"],)).fetchone()
        if (row is None or row["lease_epoch"] != claim["lease_epoch"] or row["lease_until"] <= now()
                or row["state"] not in ("pending", "accepted") or json.loads(row["request"]) != claim["request"]):
            raise TrackingError("lease_lost", 409)
        return row

    def finish(self, claim, remote):
        if (not isinstance(remote, dict) or remote.get("state") not in RUN_STATES or not isinstance(remote.get("id"), str)):
            raise TrackingError("invalid_research_response", 503)
        reason = remote.get("reason")
        # owner只返回机器原因码；保留计费未知等停止依据，不转存上游任意错误正文。
        if reason is not None and (not isinstance(reason, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,127}", reason) is None):
            raise TrackingError("invalid_research_response", 503)
        request = claim["request"]
        # 接受/发布前再检查当前委托；远程结果到达不能复活取消或已失权流程。
        self.check_delegation(request["actor_id"], request["workspace_id"], request["delegation"])
        with self.store.tx():
            row = self._leased(claim)
            if (row["run_id"] is not None and row["run_id"] != remote["id"]
                    or remote.get("workspace_id", request["workspace_id"]) != request["workspace_id"]):
                raise TrackingError("research_result_mismatch", 409)
            state = remote["state"] if remote["state"] in TERMINAL else "accepted"
            # waiting_input在research中已经终止自动执行；不能继续轮询，更不能换键收费重试。
            self.db.execute("UPDATE reassessments SET state=?,run_id=?,version=version+1,lease_until=0,next_attempt=?,result=?,reason=? WHERE id=?",
                            (state, remote["id"], 0 if state in TERMINAL else now() + 5,
                             canonical({"id": remote["id"], "state": remote["state"], "reason": reason}), reason, row["id"]))
            self.store.reassessment_event(row["id"], state, reason=reason, run_id=remote["id"])
            if state in ("succeeded", "partial"):
                watch = json.loads(self.store.watch_row(row["workspace_id"], row["watch_id"])["payload"])
                change = json.loads(self.db.execute("SELECT payload FROM changes WHERE id=?", (row["change_id"],)).fetchone()[0])
                self.store.notify(watch, change, "reassessment_completed")
            return self._run_value(self.db.execute("SELECT * FROM reassessments WHERE id=?", (row["id"],)).fetchone())

    def dispatch_one(self, worker_id="tracking-worker"):
        claim = self.claim(worker_id)
        if claim is None:
            return None
        request = claim["request"]
        try:
            self._leased(claim)
            self.check_delegation(request["actor_id"], request["workspace_id"], request["delegation"])
            # 每次都先查询稳定命令，包括上次create响应丢失的情况；仅确认404才首次发送。
            try:
                remote = _call(self.research.command, request["workspace_id"], request["actor_id"], request["key"])
            except TrackingError as exc:
                if exc.status != 404:
                    raise
                self._leased(claim)
                self.check_delegation(request["actor_id"], request["workspace_id"], request["delegation"])
                remote = _call(self.research.create_analysis, request)
            return self.finish(claim, remote)
        except TrackingError as exc:
            with self.store.tx():
                row = self._leased(claim)
                attempts = row["attempts"] + 1
                state = "rejected" if exc.status in (403, 404, 409, 429) else "deferred" if attempts >= 3 else row["state"]
                self.db.execute("UPDATE reassessments SET state=?,reason=?,attempts=?,lease_until=0,next_attempt=?,version=version+1 WHERE id=?",
                                (state, exc.code, attempts, now() + 5 * attempts, row["id"]))
                self.store.reassessment_event(row["id"], state, exc.code, row["run_id"])
                return self._run_value(self.db.execute("SELECT * FROM reassessments WHERE id=?", (row["id"],)).fetchone())

    def cancel_reassessment(self, actor_id, workspace_id, reassessment_id, expected_version, key):
        self._authorize(actor_id, workspace_id, "track"); integer(expected_version, 1); text(reassessment_id)
        with self.store.tx():
            digest, cached = self.store.cached(workspace_id, "cancel", key, [actor_id, reassessment_id, expected_version])
            if cached is not None:
                return cached
            row = self.db.execute("SELECT * FROM reassessments WHERE workspace_id=? AND id=?", (workspace_id, reassessment_id)).fetchone()
            if row is None:
                raise TrackingError("not_found", 404)
            if row["version"] != expected_version or row["state"] in TERMINAL:
                raise TrackingError("reassessment_version_conflict", 409)
            self.db.execute("UPDATE reassessments SET state='cancelled',reason='user_cancelled',version=version+1,lease_epoch=lease_epoch+1,lease_until=0,next_attempt=0 WHERE id=?", (row["id"],))
            self.store.reassessment_event(row["id"], "cancelled", "user_cancelled", row["run_id"])
            return self.store.remember(workspace_id, "cancel", key, digest, self._run_value(self.db.execute("SELECT * FROM reassessments WHERE id=?", (row["id"],)).fetchone()))

    def retry_reassessment(self, actor_id, workspace_id, reassessment_id, expected_version, key):
        """依赖连续失败后需人明确重试；保留原命令key，先对账而不是重建收费任务。"""
        self._authorize(actor_id, workspace_id, "track"); integer(expected_version, 1); text(reassessment_id)
        row = self.db.execute("SELECT * FROM reassessments WHERE workspace_id=? AND id=?", (workspace_id, reassessment_id)).fetchone()
        if row is None:
            raise TrackingError("not_found", 404)
        with self.store.tx():
            digest, cached = self.store.cached(workspace_id, "retry", key, [actor_id, reassessment_id, expected_version])
            if cached is not None:
                return cached
            current = self.db.execute("SELECT * FROM reassessments WHERE workspace_id=? AND id=?", (workspace_id, reassessment_id)).fetchone()
            if current["version"] != expected_version or current["state"] != "deferred":
                raise TrackingError("reassessment_version_conflict", 409)
            self.db.execute("UPDATE reassessments SET state=?,attempts=0,next_attempt=0,reason=NULL,version=version+1 WHERE id=?",
                            ("accepted" if current["run_id"] else "pending", reassessment_id))
            self.store.reassessment_event(reassessment_id, "pending", "explicit_retry", current["run_id"])
            return self.store.remember(workspace_id, "retry", key, digest, self._run_value(self.db.execute("SELECT * FROM reassessments WHERE id=?", (reassessment_id,)).fetchone()))

    def reconcile_cancelled(self, limit=20):
        """取消已持久化后再尽力停止远端；晚到成功只记历史且绝不发布完成提醒。"""
        integer(limit, 1, 100)
        rows = self.db.execute("SELECT * FROM reassessments WHERE state='cancelled' AND reason!='cancel_reconciled' AND next_attempt<=? ORDER BY next_attempt,rowid LIMIT ?", (now(), limit)).fetchall()
        count = 0
        for row in rows:
            request = json.loads(row["request"])
            try:
                remote = _call(self.research.command, row["workspace_id"], request["actor_id"], row["command_key"])
                if remote.get("state") not in RUN_STATES or not isinstance(remote.get("id"), str):
                    raise TrackingError("invalid_research_response", 503)
                if remote["state"] not in TERMINAL:
                    remote = _call(self.research.cancel, remote["id"], row["workspace_id"], request["actor_id"], "cancel:" + row["command_key"])
            except TrackingError as exc:
                if exc.status not in (403, 404):
                    continue
                # 404也可能是取消与尚在途的create交错，不能当作“永远未接受”。
                # 保留取消终态并稍后用原key继续对账；撤权后也不借服务身份读报告。
                with self.store.tx():
                    self.db.execute("UPDATE reassessments SET next_attempt=? WHERE id=? AND state='cancelled'", (now() + 60, row["id"]))
                continue
            with self.store.tx():
                fresh = self.db.execute("SELECT * FROM reassessments WHERE id=?", (row["id"],)).fetchone()
                if fresh["state"] != "cancelled" or fresh["reason"] == "cancel_reconciled":
                    continue
                minimal = {"id": remote.get("id"), "state": remote.get("state")}
                self.db.execute("UPDATE reassessments SET result=?,reason='cancel_reconciled',version=version+1 WHERE id=?", (canonical(minimal), row["id"]))
                self.store.reassessment_event(row["id"], "cancelled", "late_result:" + str(remote.get("state")), remote.get("id"))
                count += 1
        return {"schema_version": 1, "reconciled": count}

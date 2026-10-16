"""catalog 的业务用例：导入规范化公告、搜索、详情、研究取材。

别的模块只能调用这个文件里的公开函数：
    import_observations()  采集结果（规范化后）导入目录。ingestion 只能通过它写目录，不能直接写表
    search()               搜索公告（浏览、搜索都不调模型）
    get_notice()           详情：当前版本、历史版本、时间状态、关联公告
    get_observation()      按版本读取一个固定的观察（研究按版本取材）
    research_bundle()      研究用的公告包：主公告 + 有确切依据关联到它的更正、结果公告
    changes()              按接收序号拉取新观察（tracking 做变化检测用）

公告是公开数据，目录本身不分公司。"谁能看"由调用方先用 workspace.authorize() 检查。
"""

import base64
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from bidradar.infra import clock, outbox
from bidradar.infra.errors import AppError
from bidradar.modules.catalog import models, repo
from bidradar.modules.catalog.models import KINDS, NORMALIZER_RANK, SOURCE_FAMILIES

TOPIC_VERSION_ADDED = "catalog.notice_version_added"
NOTICE_ID_LENGTH = 64


def _check_notice_id(value: object) -> str:
    if not isinstance(value, str) or len(value) != NOTICE_ID_LENGTH or not all(c in "0123456789abcdef" for c in value):
        raise AppError("invalid_notice_id")
    return value


def import_observations(session: Session, bundle: dict[str, Any]) -> dict[str, Any]:
    """导入一批规范化观察。已有的观察跳过，返回新增和重复的数量。

    先全部检查，再在一个事务里写入：要么全部导入，要么一条都不导入。
    每条新观察发一个 catalog.notice_version_added 事件（同一事务），tracking 据此检测变化。
    """
    bundle_id = models.validate_bundle(bundle)
    # 导入串行执行（事务级锁，提交时释放）。为什么：PostgreSQL 的自增序号是在插入时分配的，
    # 两个导入同时进行，序号 6 可能比序号 5 先提交。有人恰好在这时读"序号 ≤ 6"的快照，
    # 就会漏掉 5，而 5 稍后又出现在快照范围里，翻页和变化检测都会出错。导入很少，串行没有性能问题。
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext("catalog.import"))))
    now = clock.now()
    repo.add_import_if_missing(
        session,
        {
            "id": bundle_id,
            "run_id": bundle["run_id"],
            "source_id": bundle["source_id"],
            "simulation": bundle["simulation"],
            "run_status": bundle["run_status"],
            "observations": len(bundle["observations"]),
            "failures": bundle["failures"],
            "imported_at": now,
        },
    )
    added = 0
    for observation in bundle["observations"]:
        repo.add_notice_if_missing(
            session,
            {
                "id": observation["notice_id"],
                "source_id": observation["source_id"],
                "simulation": observation["simulation"],
                "source_record_key": observation["source_record_key"],
                "first_seen_at": now,
            },
        )
        seq = repo.add_version_if_missing(
            session,
            {
                "id": observation["observation_id"],
                "notice_id": observation["notice_id"],
                "import_id": bundle_id,
                "observed_at": repo.observed(observation["observed_at"]),
                "normalizer_rank": NORMALIZER_RANK[observation["normalizer_version"]],
                "simulation": observation["simulation"],
                "kind": observation["notice_type"],
                "source_id": observation["source_id"],
                "title": observation["title"],
                "search_text": models.search_text(observation),
                "payload": observation,
            },
        )
        if seq is not None:
            added += 1
            outbox.publish(
                session,
                TOPIC_VERSION_ADDED,
                {"notice_id": observation["notice_id"], "observation_id": observation["observation_id"], "seq": seq},
                event_key=f"notice_version:{observation['observation_id']}",
            )
    session.commit()
    return {
        "bundle_id": bundle_id,
        "added": added,
        "duplicates": len(bundle["observations"]) - added,
        "run_status": bundle["run_status"],
        "failures": len(bundle["failures"]),
    }


# ---- 搜索 ----
#
# 翻页游标是 base64 编码的 JSON，记着快照号、偏移量和筛选条件。没有签名：
# 目录是公开数据，客户端改了游标顶多看到别的页，看不到不该看的东西；格式不对就报错。


def _encode_cursor(state: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(state, sort_keys=True).encode()).decode()


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        if len(cursor) > 2000:
            raise ValueError()
        state = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if (
            not isinstance(state, dict)
            or set(state) != {"snapshot", "imports_snapshot", "offset", "filters", "as_of"}
            or type(state["snapshot"]) is not int
            or type(state["imports_snapshot"]) is not int
            or type(state["offset"]) is not int
            or state["offset"] < 0
        ):
            raise ValueError()
        return state
    except (ValueError, TypeError, UnicodeError):
        raise AppError("invalid_cursor") from None


def search(
    session: Session,
    *,
    keyword: str = "",
    kind: str | None = None,
    simulation: bool = False,
    page_size: int = 20,
    cursor: str | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """搜索公告。第一页时固定一个快照，后续翻页都用同一个快照，结果不会因为新导入而错乱。

    同时返回最近几次导入的情况：导入失败时目录可能是空的，要让用户知道"空"是因为抓取失败，而不是没有项目。
    """
    if not isinstance(keyword, str) or len(keyword) > 200:
        raise AppError("invalid_query")
    keyword = keyword.strip()
    if kind is not None and kind not in KINDS:
        raise AppError("invalid_kind_filter")
    if type(page_size) is not int or not 1 <= page_size <= 100 or type(simulation) is not bool:
        raise AppError("invalid_query")
    filters = {"keyword": keyword, "kind": kind, "page_size": page_size, "simulation": simulation}
    if cursor:
        state = _decode_cursor(cursor)
        if state["filters"] != filters:
            raise AppError("cursor_query_mismatch")
    else:
        state = {
            "snapshot": repo.max_seq(session),
            "imports_snapshot": repo.max_import_seq(session),
            "offset": 0,
            "filters": filters,
            "as_of": as_of or clock.iso(clock.now()),
        }
    rows, total = repo.search_current(
        session,
        state["snapshot"],
        simulation=simulation,
        kind=kind,
        keyword=keyword,
        offset=state["offset"],
        limit=page_size,
    )
    next_offset = state["offset"] + page_size
    return {
        "items": [{**row.payload, "currentness": models.currentness(row.payload, state["as_of"])} for row in rows],
        "total": total,
        "next_cursor": _encode_cursor({**state, "offset": next_offset}) if next_offset < total else None,
        "as_of": state["as_of"],
        "snapshot": state["snapshot"],
        "simulation": simulation,
        "recent_runs": [
            {
                "run_id": row.run_id,
                "source_id": row.source_id,
                "run_status": row.run_status,
                "observations": row.observations,
                "failures": len(row.failures),
            }
            for row in repo.recent_imports(session, state["imports_snapshot"], simulation)
        ],
    }


# ---- 详情与研究取材 ----


def _relationship_pool(session: Session, snapshot: int, current: dict[str, Any]) -> list[dict[str, Any]]:
    """算关联用的候选公告：只有同一发布方、同为演示或同为真实数据的公告之间才可能有关联。"""
    family = SOURCE_FAMILIES.get(current["source_id"])
    sources = {source for source, f in SOURCE_FAMILIES.items() if f == family} if family else set()
    return [
        row.payload
        for row in repo.current_versions(session, snapshot, simulation=current["simulation"], source_ids=sources)
    ]


def get_notice(session: Session, notice_id: str, *, as_of: str | None = None) -> dict[str, Any]:
    """公告详情：当前版本、全部历史版本、时间状态、关联公告。"""
    _check_notice_id(notice_id)
    snapshot = repo.max_seq(session)
    history = [row.payload for row in repo.history(session, notice_id, snapshot)]
    if not history:
        raise AppError("notice_not_found", 404)
    current = history[0]
    now = as_of or clock.iso(clock.now())
    return {
        "current": current,
        "history": history,
        "currentness": models.currentness(current, now),
        "relationships": models.relationships(current, _relationship_pool(session, snapshot, current)),
        "snapshot": snapshot,
    }


def get_observation(session: Session, notice_id: str, observation_id: str) -> dict[str, Any]:
    """读取一个固定的观察。有了新版本，旧报告的输入也不会被替换。"""
    _check_notice_id(notice_id)
    _check_notice_id(observation_id)
    row = repo.get_version(session, notice_id, observation_id)
    if row is None:
        raise AppError("observation_not_found", 404)
    return row.payload


def get_observations(session: Session, observation_ids: list[str]) -> list[dict[str, Any]]:
    """按观察 ID 读取一组固定的观察（研究任务固定下来的公告版本），顺序和传入的一致。缺任何一个都报错。"""
    rows = {row.id: row.payload for row in repo.versions_by_ids(session, observation_ids)}
    if len(rows) != len(set(observation_ids)):
        raise AppError("observation_not_found", 404)
    return [rows[i] for i in observation_ids]


def research_bundle(session: Session, notice_id: str, snapshot: int | None = None) -> dict[str, Any]:
    """研究用的公告包：主公告的当前版本，加上有确切依据关联到它的更正、结果公告。

    只有关系状态为 evidenced（有原文链接作证）的公告才放进 observations；
    候选、有歧义、有冲突的关系只列在 relationships 里供参考，不混进研究材料。
    即使确认了更正关系，也不会自动用更正里的日期覆盖原公告的截止时间。
    """
    _check_notice_id(notice_id)
    high = repo.max_seq(session)
    snapshot = high if snapshot is None else snapshot
    if type(snapshot) is not int or not 0 <= snapshot <= high:
        raise AppError("invalid_snapshot")
    row = repo.current_version(session, notice_id, snapshot)
    if row is None:
        raise AppError("notice_not_found", 404)
    current = row.payload
    candidates = _relationship_pool(session, snapshot, current)
    incoming, related = [], []
    for item in candidates:
        if item["notice_id"] == notice_id:
            continue
        for relation in models.relationships(item, candidates):
            if (
                relation.get("target_notice_id") == notice_id
                or relation.get("target_observation_id") == current["observation_id"]
            ):
                incoming.append(
                    {**relation, "source_notice_id": item["notice_id"], "source_observation_id": item["observation_id"]}
                )
                if relation.get("status") == "evidenced" and item not in related:
                    related.append(item)
    related.sort(key=lambda o: (o["observed_at"], o["notice_id"]))
    return {
        "notice_id": notice_id,
        "snapshot": snapshot,
        "current": current,
        "observations": [current, *related],
        "relationships": incoming + models.relationships(current, candidates),
    }


def changes(session: Session, after: int = 0, limit: int = 100) -> dict[str, Any]:
    """按接收序号返回 after 之后的新观察。观察从不删除，消费方落后多少都能补上。

    序号连续提交由 import_observations 的串行锁保证，所以"读到 6 就说明 5 已经提交"成立。
    """
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise AppError("invalid_query")
    high = repo.max_seq(session)
    if after > high:
        raise AppError("cursor_ahead")
    rows = repo.versions_after(session, after, high, limit)
    return {
        "items": [
            {
                "seq": row.seq,
                "notice_id": row.notice_id,
                "observation_id": row.id,
                "observed_at": clock.iso(row.observed_at),
            }
            for row in rows
        ],
        "next_after": rows[-1].seq if rows else after,
        "high_watermark": high,
    }

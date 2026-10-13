"""跟踪 worker：定期拉取新事件、处理变化、派送复核命令。

每次 tick 只处理有限数量的事件和一个复核命令。处理进度都存在跟踪服务的数据库里，
进程重启后从上次的位置接着来。
"""

from .service import TrackingService
from .store import TrackingError, TrackingStore


def tick(root, access, catalog, research, *, worker_id="tracking-worker", limit=20, replay=False):
    """执行一轮：先处理档案和权限事件，再处理公告变化，最后派送一个复核命令、同步取消。"""
    store = TrackingStore(root)
    try:
        service = TrackingService(store, access, catalog, research)
        result = {"schema_version": 1}
        # 先处理权限和档案事件，这样撤权能在处理公告变化之前生效。
        # 重建历史（replay）时只记录，不派送任何复核命令。
        for owner in ("workspace", "catalog"):
            result[owner] = service.poll(owner, limit=limit, replay=replay)
        if not replay:
            result["reassessment"] = service.dispatch_one(worker_id)
            result["cancelled"] = service.reconcile_cancelled(limit)
        return result
    finally:
        store.close()


def run_worker(root, access, catalog, research, stop_event, *, interval=5, on_status=None):
    """每隔 interval 秒执行一次 tick，直到 stop_event 被设置。"""
    if type(interval) not in (int, float) or not 1 <= interval <= 60:
        raise ValueError("invalid_worker_interval")
    while not stop_event.is_set():
        try:
            status = tick(root, access, catalog, research)
        except TrackingError as exc:
            status = {"schema_version": 1, "error": exc.code}
        if on_status is not None:
            on_status(status)
        stop_event.wait(interval)

"""有界tracking轮询；一次tick只处理有限事件/复核，断点以tracking数据库为准。"""
from .service import TrackingService
from .store import TrackingError, TrackingStore


def tick(root, access, catalog, research, *, worker_id="tracking-worker", limit=20, replay=False):
    store = TrackingStore(root)
    try:
        service = TrackingService(store, access, catalog, research)
        result = {"schema_version": 1}
        # 先权限/档案再公开变化；历史重建模式连模型命令派送也禁用。
        for owner in ("workspace", "catalog"):
            result[owner] = service.poll(owner, limit=limit, replay=replay)
        if not replay:
            result["reassessment"] = service.dispatch_one(worker_id)
            result["cancelled"] = service.reconcile_cancelled(limit)
        return result
    finally:
        store.close()


def run_worker(root, access, catalog, research, stop_event, *, interval=5, on_status=None):
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

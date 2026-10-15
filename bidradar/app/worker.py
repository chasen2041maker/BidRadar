"""Worker 进程入口：从任务表领取后台任务，交给对应模块的 service 执行。

阶段 3 开始实现（文档解析是第一个后台任务）。任务表用 PostgreSQL 的 SKIP LOCKED 加租约，见蓝图 6.6 节。
"""

from bidradar.app.settings import Settings


def run(settings: Settings) -> None:
    raise SystemExit("Worker 在阶段 3 实现。")

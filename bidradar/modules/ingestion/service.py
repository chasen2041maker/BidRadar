"""ingestion 的业务用例：把一次采集运行的结果规范化，交给 catalog 导入。

采集本身（登记运行 → 抓取 → 归档 → 解析）在 pipeline.py 和 tianjin.py 里，由命令行驱动（cli.py）：
它慢、要联网、要人工核对来源准入文件，不适合放在网页请求里。

这里的公开函数：
    open_store()     打开采集存储（数据库会话 + 原始文件目录）
    export_run()     把一次已结束的运行导出成证据包（raw_evidence_bundle）
    normalize()      证据包 → 规范化观察（normalized_observations）
    publish_run()    导出 + 规范化 + 导入目录，一步完成

守住边界：采集结果只能通过 catalog.service.import_observations 进目录，ingestion 不碰 catalog 的表。
"""

from typing import Any

from sqlalchemy.orm import Session

from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError
from bidradar.modules.catalog import service as catalog
from bidradar.modules.ingestion.archive import Store, StoreError
from bidradar.modules.ingestion.export import export_bundle
from bidradar.modules.ingestion.normalize import normalize_bundle


def open_store(session: Session, blobs: BlobStore) -> Store:
    return Store(session, blobs)


def export_run(session: Session, blobs: BlobStore, run_id: str) -> dict[str, Any]:
    """导出一次已结束运行的证据包。导出时重新读原始文件、校验 SHA-256、用当前解析器再解析一遍。"""
    try:
        return export_bundle(open_store(session, blobs), run_id)
    except StoreError as exc:
        raise AppError(exc.code) from None


def normalize(raw_bundle: dict[str, Any]) -> dict[str, Any]:
    """证据包 → 规范化观察。任何一个文档格式不对，整个包都拒绝。"""
    try:
        return normalize_bundle(raw_bundle)
    except ValueError as exc:
        raise AppError(str(exc)) from None


def publish_run(session: Session, blobs: BlobStore, run_id: str) -> dict[str, Any]:
    """导出、规范化、导入目录。重复执行是安全的：已导入的观察会被跳过。"""
    return catalog.import_observations(session, normalize(export_run(session, blobs, run_id)))

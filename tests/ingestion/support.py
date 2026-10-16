"""让旧版的采集和目录测试直接跑在 v2 代码上的一层薄适配。

旧测试是按"一个数据目录一个 SQLite"写的：Store(目录)、Catalog(目录)、main(["--store", 目录, ...])。
这里把它们接到 v2 的 PostgreSQL 实现上，测试本身几乎不用改：
旧版写过的几百条行为测试，原样拿来证明搬迁没有改变行为。

数据库会话由 conftest.py 的夹具在每个测试开始前放进 SESSION，测试结束时整个回滚。
"""

from pathlib import Path

from bidradar.infra.blobs import BlobStore
from bidradar.infra.errors import AppError
from bidradar.modules.catalog import service as catalog_service
from bidradar.modules.ingestion import archive
from bidradar.modules.ingestion.cli import main as cli_main

SESSION = None


def Store(root):  # noqa: N802 —— 保持旧测试里的名字
    return archive.Store(SESSION, BlobStore(Path(root) / "blobs"))


def main(argv):
    """旧命令行的第一个参数是 --store 目录，v2 里换成 data_dir，数据库用测试会话。"""
    argv = list(argv)
    data_dir = Path(".bidradar-data")
    if argv[:1] == ["--store"]:
        data_dir, argv = Path(argv[1]), argv[2:]
    return cli_main(argv, data_dir=data_dir, session=SESSION)


def _value_error(function):
    """v2 的业务错误是 AppError；旧测试按 ValueError 检查，这里转一下，错误码不变。"""

    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except AppError as error:
            raise ValueError(error.code) from None

    return wrapper


class Catalog:
    """旧版 services/catalog/store.py 的 Catalog 接口，转到 v2 的 catalog.service。"""

    def __init__(self, root=None, *, read_only=False):
        self.session = SESSION

    def close(self):
        pass

    @_value_error
    def import_bundle(self, bundle):
        return catalog_service.import_observations(self.session, bundle)

    @_value_error
    def query(self, **kwargs):
        return catalog_service.search(self.session, **kwargs)

    @_value_error
    def detail(self, notice_id, *, as_of=None):
        return catalog_service.get_notice(self.session, notice_id, as_of=as_of)

    @_value_error
    def observation(self, notice_id, observation_id):
        return catalog_service.get_observation(self.session, notice_id, observation_id)

    @_value_error
    def bundle(self, notice_id, snapshot=None):
        return catalog_service.research_bundle(self.session, notice_id, snapshot)

    @_value_error
    def changes(self, after=0, limit=100):
        return catalog_service.changes(self.session, after, limit)

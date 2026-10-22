"""两次采集运行同时存下同一个网址的不同内容：版本号依次分配，两边都成功。

并发问题在回滚的事务里测不出来，所以这个测试用两个真正提交事务的数据库连接。
它用随机的幂等键和网址，结束时只删自己建的行，不影响别的测试。所有网页内容都是虚构的。
"""

import threading
import uuid

from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session

from bidradar.infra.blobs import BlobStore
from bidradar.modules.ingestion.archive import Store
from bidradar.modules.ingestion.pipeline import public_request_spec
from bidradar.modules.ingestion.tables import CaptureRow, FetchRunRow, FetchTaskRow, UrlVersionRow
from bidradar.modules.ingestion.transport import FetchResult


def test_concurrent_runs_get_consecutive_versions_for_the_same_url(engine: Engine, tmp_path) -> None:
    tag = uuid.uuid4().hex
    url = f"https://www.ccgp.gov.cn/cggg/zygg/gkzb/202610/t20261022_{int(tag[:12], 16)}.htm"
    blobs = BlobStore(tmp_path / "blobs")
    sessions = [Session(engine), Session(engine)]
    run_ids: list[str] = []
    try:
        prepared = []
        for index, session in enumerate(sessions):
            store = Store(session, blobs)
            request = public_request_spec(source_id="cn_ccgp", notice_urls=[url], max_notices=1)
            run_id, _ = store.create_run(request, f"concurrency-{tag}-{index}")
            run_ids.append(run_id)
            token = store.acquire(run_id)
            store.enqueue(run_id, token, [{"kind": "notice", "url": url}])
            prepared.append((store, run_id, token, store.next_task(run_id), index))

        # 两个线程在同一时刻开始存：都会读到"这个网址目前最大是 0 号版本"。
        barrier = threading.Barrier(len(prepared))
        outcomes: dict[int, str] = {}

        def save(store, run_id, token, task, index):
            body = f"<p>虚构公告内容 {tag} 第{index}次</p>".encode()
            response = FetchResult(url, url, 200, {"content-type": "text/html"}, body, "2026-10-22T00:00:00Z", 1)
            barrier.wait()
            try:
                store.record(run_id, token, task, response=response, parsed={"status": "ok"}, parser_version="test")
                outcomes[index] = "ok"
            except Exception as error:  # 把异常类型记下来，断言时一眼看出是哪种失败
                outcomes[index] = type(error).__name__

        threads = [threading.Thread(target=save, args=item) for item in prepared]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not any(thread.is_alive() for thread in threads), "两个运行互相等待，没有结束"
        assert outcomes == {0: "ok", 1: "ok"}
        with Session(engine) as check:
            versions = check.scalars(select(UrlVersionRow.version).where(UrlVersionRow.url == url)).all()
        assert sorted(versions) == [1, 2]
    finally:
        for session in sessions:
            session.rollback()
            session.close()
        with Session(engine) as cleanup, cleanup.begin():
            cleanup.execute(delete(CaptureRow).where(CaptureRow.run_id.in_(run_ids)))
            cleanup.execute(delete(UrlVersionRow).where(UrlVersionRow.url == url))
            cleanup.execute(delete(FetchTaskRow).where(FetchTaskRow.run_id.in_(run_ids)))
            cleanup.execute(delete(FetchRunRow).where(FetchRunRow.id.in_(run_ids)))

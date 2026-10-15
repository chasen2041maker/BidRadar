"""采集和目录测试的夹具：把回滚事务里的会话交给 support.py 的适配层。"""

import pytest
from sqlalchemy.orm import Session

from tests_v2.ingestion import support


@pytest.fixture(autouse=True)
def legacy_session(db_session: Session):
    support.SESSION = db_session
    yield
    support.SESSION = None

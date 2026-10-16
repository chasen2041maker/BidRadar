"""pytest 的公共夹具：测试数据库、跑在回滚事务里的会话、接到测试库的 HTTP 客户端。

造测试数据的函数（建用户、建公司、登录）在 tests/factories.py。

测试连单独的数据库（默认 bidradar_test），不碰开发用的 bidradar 库。
地址可以用环境变量或 .env 里的 BIDRADAR_TEST_DATABASE_URL 修改。

服务测试和接口测试都用 db_session：每个测试跑在一个事务里，结束时整个回滚，测试之间互不影响，
也不用在每个测试后手动清数据。
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from bidradar.app.deps import get_session
from bidradar.app.main import create_app
from bidradar.app.settings import Settings
from bidradar.infra.db import make_engine
from bidradar.modules.identity import models as identity_models

REPO_ROOT = Path(__file__).resolve().parents[1]


class PytestSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIDRADAR_TEST_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://bidradar:bidradar@localhost:5432/bidradar_test"


TEST_DATABASE_URL = PytestSettings().database_url


@pytest.fixture(autouse=True)
def fast_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试里把密码哈希的迭代次数从 60 万降到 1000，不然每建一个用户要等半秒。

    迭代次数存在哈希里，核对密码时用的是哈希里记的次数，所以这样改不影响逻辑。
    """
    monkeypatch.setattr(identity_models, "PASSWORD_ITERATIONS", 1000)


@pytest.fixture(scope="session")
def alembic_config() -> Config:
    """指向测试库的 Alembic 配置，和命令行 alembic 用同一份 alembic.ini 和 migrations/env.py。"""
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.attributes["database_url"] = TEST_DATABASE_URL
    return config


@pytest.fixture(scope="session")
def engine(alembic_config: Config) -> Iterator[Engine]:
    """已经迁移到最新版本的测试库。整轮测试只迁移一次。"""
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect():
            pass
    except OperationalError:
        pytest.fail(
            "连不上测试数据库。先运行 docker compose up -d --wait db，"
            "或者用 BIDRADAR_TEST_DATABASE_URL 指定一个可用的 PostgreSQL。",
            pytrace=False,
        )
    command.upgrade(alembic_config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    """跑在一个事务里的会话，测试结束后整个回滚。

    join_transaction_mode="create_savepoint"：被测代码调用 session.commit() 时，只提交到一个保存点，
    外层事务仍然能在测试结束时回滚。这样 service 里照常写 commit，测试也不会留下数据。
    """
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """调用 API 的客户端。不起真实的服务器，请求直接在进程里交给 FastAPI 应用处理。

    路由拿到的会话换成了 db_session，接口测试写进去的数据同样会在测试结束时回滚。
    """
    app = create_app(Settings(database_url=TEST_DATABASE_URL))
    app.dependency_overrides[get_session] = lambda: db_session
    # 浏览器发 POST 时会自动带上 Origin，测试客户端不会，这里补上，模拟"从本站页面发出的请求"。
    with TestClient(app, headers={"Origin": "http://testserver"}) as client:
        yield client

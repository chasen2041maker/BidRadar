"""健康检查接口：数据库正常时返回 200，连不上时返回 503，且不泄露连接信息。"""

from fastapi.testclient import TestClient

from bidradar.app.main import create_app
from bidradar.app.settings import Settings


def test_health_reports_database_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_is_unreachable() -> None:
    # 本机 1 号端口上没有数据库，连接会被立刻拒绝。用一次真实的连接失败，而不是模拟出来的异常。
    app = create_app(Settings(database_url="postgresql+psycopg://secret_user:secret_pw@127.0.0.1:1/missing"))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unreachable"}
    assert "secret" not in response.text

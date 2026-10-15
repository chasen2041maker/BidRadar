"""应用配置：从环境变量和 .env 文件读取。

每一项都可以用 BIDRADAR_ 开头的环境变量覆盖，比如 BIDRADAR_DATABASE_URL。
环境变量优先于 .env。.env 不进 git，模板见 .env.example。
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIDRADAR_", env_file=".env", extra="ignore")

    # 默认值对应 docker-compose.yml 里的本地开发数据库，这样克隆下来不用先写 .env 就能跑。
    # 这个账号密码只用于本机开发，数据库端口也只绑定在 127.0.0.1 上。
    database_url: str = "postgresql+psycopg://bidradar:bidradar@localhost:5432/bidradar"

    # 允许的 Host 头。挡 DNS 重绑定：恶意网页把自己的域名解析到 127.0.0.1，借浏览器访问本机的服务。
    # testserver 是 FastAPI 测试客户端用的主机名。
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "testserver"]

    # 部署到 HTTPS 时改成 True，Cookie 只走加密连接。本机开发是 http，必须是 False 才能登录。
    cookie_secure: bool = False

    # 上传的招标文件和原始网页按 SHA-256 存在这里。不进 git。
    data_dir: Path = Path(".bidradar-data/v2")

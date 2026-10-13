"""应用配置：从环境变量和 .env 文件读取。

每一项都可以用 BIDRADAR_ 开头的环境变量覆盖，比如 BIDRADAR_DATABASE_URL。
环境变量优先于 .env。.env 不进 git，模板见 .env.example。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BIDRADAR_", env_file=".env", extra="ignore")

    # 默认值对应 docker-compose.yml 里的本地开发数据库，这样克隆下来不用先写 .env 就能跑。
    # 这个账号密码只用于本机开发，数据库端口也只绑定在 127.0.0.1 上。
    database_url: str = "postgresql+psycopg://bidradar:bidradar@localhost:5432/bidradar"

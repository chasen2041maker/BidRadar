"""Alembic 的运行入口：告诉 Alembic 连哪个数据库、拿哪些表定义和数据库比较。

数据库地址和 API 进程读同一份配置（BIDRADAR_DATABASE_URL 或 .env），迁移和应用不会连到两个不同的库。
测试通过 config.attributes["database_url"] 传入测试库的地址。
"""

from logging.config import fileConfig

from alembic import context

from bidradar.app.metadata import ALL_METADATA, SCHEMAS
from bidradar.app.settings import Settings
from bidradar.infra.db import make_engine

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False：测试里调用 Alembic 时，不要把应用已经配好的日志关掉。
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url() -> str:
    return config.attributes.get("database_url") or Settings().database_url


def include_name(name: str | None, type_: str, parent_names: dict) -> bool:
    """只比较本项目的 schema。public 和数据库里的其他 schema 不归我们管，不生成迁移。"""
    if type_ == "schema":
        return name in SCHEMAS
    return True


def configure(**kwargs) -> None:
    context.configure(
        target_metadata=ALL_METADATA,
        # 表分散在各模块的 schema 里，要让 Alembic 逐个 schema 比较，而不是只看 public。
        include_schemas=True,
        include_name=include_name,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """只生成 SQL、不连数据库（alembic upgrade head --sql），方便先看一眼迁移会执行什么。"""
    configure(url=database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

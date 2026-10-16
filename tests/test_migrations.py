"""迁移和表定义：每个模块都有自己的 schema，表定义和迁移保持一致，迁移能退回再升上去。"""

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect

from bidradar.app.metadata import ALL_METADATA, SCHEMAS


def schema_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_schema_names())


def test_every_module_schema_exists_after_upgrade(engine: Engine) -> None:
    """新增了模块却忘了写建 schema 的迁移，这条会失败。"""
    assert set(SCHEMAS) <= schema_names(engine)


def test_each_schema_belongs_to_exactly_one_module() -> None:
    assert len(SCHEMAS) == len(set(SCHEMAS))


def test_tables_stay_in_their_module_schema() -> None:
    """表只能建在自己模块的 schema 里。在 __table_args__ 里把 schema 改成别的模块，这条会失败。"""
    for metadata in ALL_METADATA:
        for table in metadata.tables.values():
            assert table.schema == metadata.schema, f"{table.fullname} 不在 {metadata.schema} schema 里"


def test_table_definitions_match_migrations(engine: Engine, alembic_config: Config) -> None:
    """改了 tables.py 却忘了生成迁移，这条会失败（等同于命令行的 alembic check）。"""
    command.check(alembic_config)


def test_downgrade_to_empty_and_upgrade_again(engine: Engine, alembic_config: Config) -> None:
    """每个迁移的 downgrade 都写对了，数据库才能退回到空库再升回最新版本。"""
    command.downgrade(alembic_config, "base")
    assert not set(SCHEMAS) & schema_names(engine)

    command.upgrade(alembic_config, "head")
    assert set(SCHEMAS) <= schema_names(engine)

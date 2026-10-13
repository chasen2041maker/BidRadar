"""数据库连接：引擎、会话工厂，以及各模块定义表时共用的 MetaData 规则。

整个进程共用一个引擎，引擎里有连接池。每个 HTTP 请求或每个后台任务从会话工厂取一个会话，用完关掉。
这里只管"怎么连数据库"，不认识任何业务表；各模块的表定义在自己的 tables.py 里。
"""

import logging

from sqlalchemy import Engine, MetaData, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

# 数据库连不上时，3 秒内报错，而不是让请求一直挂着。健康检查也靠它及时返回"不可用"。
CONNECT_TIMEOUT_SECONDS = 3

# 约束的命名规则。约束有了固定的名字，Alembic 以后修改或删除约束时才找得到它。
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def make_engine(database_url: str) -> Engine:
    """创建引擎。这一步只记下连接参数，第一次执行 SQL 时才真正连数据库。"""
    return create_engine(
        database_url,
        # 池里的连接可能已经被数据库断开（比如数据库重启过），取出来用之前先探一下，坏了就换一个新的。
        pool_pre_ping=True,
        connect_args={"connect_timeout": CONNECT_TIMEOUT_SECONDS},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建会话工厂。

    expire_on_commit=False：提交以后，对象上的字段仍然能直接读。
    service 提交完事务，可以把对象原样交给 api 返回，不会因为读字段再触发一次查询。
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


def module_metadata(schema: str) -> MetaData:
    """给一个模块定义表用的 MetaData。

    绑定了 schema：用它定义的表自动放进这个模块自己的 schema，不会写进别的模块。
    以后要把某个模块拆成独立服务，把它的 schema 整个搬走就行。
    """
    return MetaData(schema=schema, naming_convention=NAMING_CONVENTION)


def ping(session: Session) -> bool:
    """数据库能执行 SELECT 1 就返回 True。

    连不上时返回 False，不往外抛异常；异常原文写进日志，因为里面可能有数据库地址和用户名，不适合返回给调用方。
    """
    try:
        session.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.warning("数据库连接检查失败", exc_info=True)
        return False
    return True

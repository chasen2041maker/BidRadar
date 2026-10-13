"""FastAPI 的依赖：路由需要的东西从这里取。现在有数据库会话，阶段 1 加上当前用户。

路由只声明"我需要一个会话"，不关心会话从哪来。
测试时用 app.dependency_overrides 换成跑在回滚事务里的会话，路由代码一行都不用改。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session


def get_session(request: Request) -> Iterator[Session]:
    """每个请求一个会话，请求结束时关闭。

    这里不提交事务。一个业务动作什么时候算做完，只有 service 知道，所以由 service 提交。
    没提交的改动在会话关闭时自动回滚。
    """
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


# 路由参数写成 `session: DbSession`，FastAPI 就会调用 get_session 注入会话。
DbSession = Annotated[Session, Depends(get_session)]

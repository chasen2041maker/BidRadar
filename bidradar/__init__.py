"""BidRadar v2：模块化单体。

一份代码，两个进程：API（FastAPI）和 Worker（后台任务）。分层规则见 docs/dev-guide.md 的"v2 代码"一节，
完整设计见 docs/v2-blueprint.md。

从哪里读起：
    app/main.py      API 进程怎么组装起来
    app/health.py    一个最小的完整请求：路由 → 依赖 → 数据库
    infra/db.py      数据库连接
    modules/         各业务模块，内部结构都一样
"""

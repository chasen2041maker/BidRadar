"""业务模块。每个模块内部结构一样，依赖只能从上往下：

    api.py       HTTP 路由：只管请求进、响应出，然后调用 service
    schemas.py   接口的请求和响应结构（Pydantic），只给 api 用
    service.py   业务用例：一个函数一个业务动作，负责提交事务；也是别的模块唯一能调用的入口
    repo.py      数据库读写，只给本模块的 service 用
    tables.py    SQLAlchemy 表定义，全部放在本模块自己的 schema 里
    models.py    领域对象和业务规则，纯 Python，不依赖 Web 和数据库

这些规则写在 pyproject.toml 的 import-linter 契约里，CI 会检查。
"""

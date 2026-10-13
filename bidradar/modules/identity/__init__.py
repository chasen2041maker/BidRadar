"""identity 模块：用户、登录、会话、登录限流。

表放在 PostgreSQL 的 identity schema 里：users、sessions、login_limits。
对外提供：current_user()。别的模块只能调用本模块 service.py 里的函数。
阶段 1 实现。
"""

"""tracking 模块：跟进或放弃的决定、关注、变化、提醒。

表放在 PostgreSQL 的 tracking schema 里：decisions、watches、changes、notifications。
对外提供：set_decision()、watch()。别的模块只能调用本模块 service.py 里的函数。
阶段 5 实现。
"""

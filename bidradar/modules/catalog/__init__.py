"""catalog 模块：公开公告、公告版本、更正和结果公告的关系、搜索。

表放在 PostgreSQL 的 catalog schema 里：notices、notice_versions、notice_links。
对外提供：search()、get_notice()。别的模块只能调用本模块 service.py 里的函数。
阶段 2 实现。
"""

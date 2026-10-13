"""documents 模块：招标文件上传、解析、切片、检索。

表放在 PostgreSQL 的 documents schema 里：documents、chunks。
对外提供：get_chunks()、search_chunks()。别的模块只能调用本模块 service.py 里的函数。
阶段 3 实现。
"""

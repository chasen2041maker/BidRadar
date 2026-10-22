"""ingestion 模块：从公开采购网站采集公告，归档原始网页，规范化后交给 catalog 导入。

表放在 PostgreSQL 的 ingestion schema 里：fetch_runs、fetch_tasks、captures、url_versions、replays（见 tables.py）。
采集由命令行驱动（cli.py），不在网页请求里联网。别的模块只能调用本模块 service.py 里的函数。
"""

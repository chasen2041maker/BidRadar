"""ingestion 模块：采集来源、采集记录、原始页面归档。

表放在 PostgreSQL 的 ingestion schema 里：sources、fetch_runs、captures。
对外提供：启动采集；采集到的公告交给 catalog.service 导入。别的模块只能调用本模块 service.py 里的函数。
阶段 2 实现。
"""

"""research 模块：研究任务、步骤结果、报告、追问、费用账本。

表放在 PostgreSQL 的 research schema 里：runs、run_steps、requirement_sets、requirements、findings、reports、llm_calls。
对外提供：create_run()、get_report()、ask()。别的模块只能调用本模块 service.py 里的函数。
阶段 4 实现。
"""

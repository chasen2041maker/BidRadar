"""研究流水线：解析 → 抽取要求 → 匹配 → 核验 → 汇总，以及追问 Agent。

这里是纯逻辑，不 import modules、app、infra，也不 import FastAPI 和 SQLAlchemy。
读文档、调模型、保存步骤结果这些事，都由 research 模块通过参数传进来。
这样流水线不连数据库也能单测和评测，以后拆成独立服务也不用改。

阶段 4 实现，设计见蓝图第 6 节。
"""

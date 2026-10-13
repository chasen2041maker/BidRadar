"""研究服务（research）：接收研究请求、在后台调用模型完成研究、保存报告。

文件分工：
    http_api.py    内部 HTTP 接口，只给 workspace 和 tracking 两个服务调用
    service.py     业务规则：权限检查、冻结输入、受理任务
    store.py       任务和报告的数据库
    worker.py      后台进程：领取任务、调用 Agent、保存结果
    agent.py       研究 Agent 本身（模型怎么取证、怎么写报告）
    evidence.py    原文片段索引和报告的程序检查
    provider.py    DeepSeek 接口适配
    budget.py      模型费用账本
    evaluation.py  规则基线和评测对照
"""

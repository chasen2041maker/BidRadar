"""流水线的五个步骤，一个文件一步：parse、extract、match、verify、compose。

每个步骤都是纯函数：输入是数据和一个 LLMClient，输出是 schemas.py 里的结构。
步骤之间怎么串、结果怎么存、崩溃后怎么续跑，由 pipeline.py 负责。
"""

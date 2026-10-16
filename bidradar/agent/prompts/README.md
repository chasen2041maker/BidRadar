# 提示词

一个步骤一个文件，文件名带版本号，比如 `extract.v1.md`。代码通过 `prompts.load("extract")` 读取，
用哪个版本写在 `bidradar/agent/prompts/__init__.py` 的 `CURRENT` 里。

- 研究任务创建时，全部提示词的内容会参与计算配置版本（config_version）。旧报告永远知道自己是按哪版规则生成的。
- 改提示词就新建一个版本文件（`extract.v2.md`），改 `CURRENT`，再跑评测对比前后结果，在 PR 里贴出指标。
- 每个提示词控制在 30 行左右。写不下，说明这一步做的事太多，应该再拆，或者把规则移进程序检查（`agent/checks.py`）。
- 提示词里只写"这一步要做什么、输出什么结构"。外部内容（招标文件原文、档案）一律放在 user 消息的 JSON 里，
  不拼进提示词。

| 文件 | 步骤 | 状态 |
| --- | --- | --- |
| extract.v1.md | ② 抽取要求 | 已写 |
| extract_recall.v1.md | ② 漏检复查 | 已写 |
| verify.v1.md | ④ 模型核验 | 已写 |
| summary.v1.md | ⑤ 摘要（默认关闭） | 已写 |
| followup.v1.md | 追问 | 已写 |
| answer_check.v1.md | 追问回答的核验 | 已写 |
| match.v1.md | ③ 匹配 | **练习 2，由项目负责人写** |

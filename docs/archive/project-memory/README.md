# 项目记忆脚本（已归档）

2026-09 到 10-01 期间，项目用这套脚本维护交接文档（PROJECT_STATE、CURRENT_TASK、学习索引）：

- `scripts/check_project_memory.py`：检查交接文档的结构、链接和大小上限。
- `scripts/refresh_project_memory.py`：生成学习索引，检查每次改动是否附带了任务笔记。
- `scripts/publish_project_memory.py`：main 分支更新后，把状态摘要写到一个固定的 GitHub Issue。
- `tests/`：上面三个脚本的测试。
- `project-memory.yml`：对应的 GitHub Actions 工作流。

2026-10-12 文档精简后不再使用（见 [决策 0009](../../decisions/0009-plain-language-docs.md)）。
这些文件放在这里不会被执行，也不会被测试发现。

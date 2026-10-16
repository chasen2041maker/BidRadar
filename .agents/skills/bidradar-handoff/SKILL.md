---
name: bidradar-handoff
description: 在 BidRadar 仓库开始新任务、接手别人的工作或切换会话时使用。先读协作规则和路线图，核对仓库的真实状态，再开始工作；结束时更新路线图和相关文档。
---

# BidRadar 接手工作

## 开始时

1. 读 `AGENTS.md`（协作规则）和 `docs/roadmap.md`（当前进度和下一步）。
2. 用 `git status --short --branch` 和 `git log --oneline -5` 确认当前分支和最新提交。不要依赖之前对话里的记忆。
3. 按任务读相关文档：`docs/architecture.md`、`docs/agent.md`、`docs/agent-design.md`、`docs/data-model.md`、`docs/dev-guide.md`。
   `docs/exercises.md` 里的三个练习留给项目负责人自己做，不要代做。
4. 用一两句话说明这次要做什么、不做什么、怎么算做完，再动手。

## 结束时

1. 跑测试：`uv run pytest` 和 `uv run lint-imports`。改了研究流水线（提示词、维度表、规则）还要跑 `uv run python -m evals.run --step all`。
2. 代码改了，相关的注释和文档一起改。
3. 进度有变化就更新 `docs/roadmap.md`；有重要设计决定就在 `docs/decisions/` 下新建一页。
4. 过程细节（试过什么、踩过什么坑、验证结果）写在 PR 描述里。

## 规则

- 文档和注释用人话写：先说结论，一句话一件事，不用代号。
- 不越过 `AGENTS.md` 里"不能碰的线"：费用、真实数据、权限、不可逆操作。

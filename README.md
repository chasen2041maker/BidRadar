# BidRadar

帮软件公司和 AI 服务团队研究中国公开采购项目：这个项目要求什么、我们能不能做、还缺哪些信息。

> An evidence-based tender research and tracking agent for software and AI service teams.

## 它解决什么问题

一家软件公司想接政府或国企的软件项目，每天要在大量招标公告里找合适的，再逐条核对资格、技术要求、交付期限、预算。BidRadar 把这件事拆成几步：

1. **确认企业档案**：公司能做什么、不做什么、有哪些限制。
2. **发现项目**：从公开采购网站采集公告，整理成可搜索的目录。
3. **研究项目**：AI 逐条列出采购要求，对照企业档案判断"满足 / 不满足 / 未知"，每条结论都附原文引用。
4. **追问**：针对同一个项目继续提问。
5. **人工决定**：跟进还是放弃，由人来定。
6. **跟踪变化**：公告更正或档案修改后，提醒并（经授权后）自动复核。

它不预测中标概率，不自动投标，也不替人联系采购方。

## 现在能做什么

- 四个本地服务已经能跑通整条流程：登录、公司成员、企业档案、商机目录、AI 研究、追问、跟进决定、关注、变化提醒、自动复核。
- AI 研究使用 DeepSeek，每条结论都要通过程序检查和模型语义核验；没通过就发布只列原文的保守版本。
- 数据来自中国政府采购网和海南省公共资源交易平台的有限公开样本（12 条真实公告），企业是两家虚构公司。
- 486 项自动化测试在 Python 3.10 和 3.12 上通过。

还没有做的：正式的用户系统、云端部署、完整招标文件的获取，以及真实用户试用。详见 [路线图](docs/roadmap.md)。

正在进行 v2 改造：新代码写在 `bidradar/`，改成模块化单体（FastAPI + PostgreSQL）。阶段 0 地基已完成，计划见 [v2 改造蓝图](docs/v2-blueprint.md)。

## 快速开始

需要 Python 3.10 或更高版本。旧代码只用标准库，不需要安装依赖。

```bash
# 启动四个服务（不调用真实模型）
python scripts/run_research_workbench.py

# 浏览器打开 http://127.0.0.1:8965/
# 测试账号在 .bidradar-data/r12-workbench/demo-accounts.json
```

其他常用命令：

```bash
python -m unittest discover -s tests -p 'test_*.py'   # 跑全部测试
python scripts/run_data_demo.py                        # 离线演示数据链（采集 → 规范化 → 目录）
python scripts/run_research_workbench.py --enable-model   # 允许调用 DeepSeek（会产生费用，见开发指南）
```

v2 新代码需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Docker：

```bash
docker compose up -d --wait db                  # 启动 PostgreSQL 16
uv run alembic upgrade head                     # 迁移数据库
uv run uvicorn bidradar.app.main:app            # 启动 API，打开 http://127.0.0.1:8000/health
uv run pytest && uv run lint-imports            # 测试和分层规则检查
```

详见 [开发指南](docs/dev-guide.md#v2-新代码bidradar)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [架构说明](docs/architecture.md) | 系统由哪几部分组成，一个请求怎么走，代码在哪 |
| [研究 Agent](docs/agent.md) | AI 研究怎么工作、怎么核验、怎么控制费用，以及实测结果 |
| [数据模型](docs/data-model.md) | 各服务的表和关键概念 |
| [开发指南](docs/dev-guide.md) | 本地运行、测试、格式化、提交约定、写注释和文档的规则 |
| [路线图](docs/roadmap.md) | 当前进度、已知短板、下一步 |
| [决策记录](docs/decisions/) | 重要的设计决定，每个一页 |
| [v2 改造蓝图](docs/v2-blueprint.md) | 迁移到模块化单体 + FastAPI/PostgreSQL 的计划 |
| [历史文档](docs/archive/) | 2026-09 到 10-01 的原始规划和过程记录 |

## 目录结构

```text
bidradar/       v2 新代码（模块化单体）
  app/          组装：FastAPI 应用、配置、依赖注入
  modules/      业务模块：identity、workspace、catalog、ingestion、documents、research、tracking
  agent/        研究流水线（纯逻辑）
  infra/        数据库连接等基础设施
migrations/     数据库迁移（Alembic）
tests_v2/       v2 新代码的测试（pytest）
services/       旧代码，v2 阶段 6 删除
  ingestion/    采集：从公开网站抓取公告，保存原始文件
  processing/   规范化：把原始内容整理成统一格式
  catalog/      商机目录：公告、版本、搜索
  workspace/    工作台：登录、公司、成员、企业档案；也是浏览器入口
  research/     研究：AI Agent、证据核验、费用账本
  tracking/     跟踪：跟进决定、关注、变化、提醒、自动复核
  common/       服务之间通信用的 HTTP 工具
scripts/        启动、演示、评测脚本
tests/          旧代码的测试（unittest）
docs/           文档
third_party/    第三方代码的来源与许可
```

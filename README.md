# BidRadar — 招标商机分析与跟踪 Agent

> 帮希望承接公开采购项目的软件与 AI 服务团队，读懂要求、对照能力、保留证据并跟踪变化。远程团队也是目标用户，不以能否驻场划分用户资格。
>
> An evidence-based tender research and tracking agent for software and AI service teams.

**当前：DESIGN_REVIEW + 工程规范建设。没有业务服务、已接通数据源、运行中的 CMS/缓存或部署成果。文档检查工具不等于产品实现。**

## 新会话先读

[AGENTS.md](AGENTS.md) → [项目状态](PROJECT_STATE.md) → [当前任务](CURRENT_TASK.md)。按[上下文管理](docs/21-context-management.md)再读相关专题，不要求每次把所有历史文档塞入上下文。

**2026-09-12 最新协作要求：**默认由 Codex 执行已授权任务的开发、自测、联调与记录；人工负责业务决策、风险授权和必要验收。此分工替代旧材料中“负责人先写关键代码”的前置要求，仍要求理解关键逻辑、审核真实证据并对结果负责。规范见 [20](docs/20-development-standard.md)。自动合并与自动关闭未获常态授权；本次人工合并授权不豁免独立审查。

## 产品主线

确认企业档案 → 找到相关项目 → 查看原文与材料缺口 → 分析条件和证据 → 同项目追问 → 人工记录跟进/放弃 → 跟踪变化并复核。

不是只写摘要或给中标概率。技术相关不等于全部要求满足；没填资质不等于没有资质；没写驻场不等于允许远程。资料不足、取消、失败、预算暂停和旧报告陈旧都要能解释，不自动投标、报价或联系采购方。

## 首批业务、企业档案与数据源

已确认首批中国公开项目、中文优先、不限城市，产品给软件公司/AI服务团队使用；第一阶段做到“确认档案→真实项目→实际材料→引用与未知项分析→同项目追问→人工决定”。[23](docs/23-first-market-rag.md)区分RAG、目录检索与业务后端。

企业档案按八类组织，首用先问主体、想接什么、能做什么、交付限制，再按项目补案例/资质/排期/商务信息。[24](docs/24-company-profile-minimum.md)说明事实、证明、偏好和硬限制如何分开。

[25](docs/25-china-source-validation.md)登记中国政府采购网的首源验证方向、采购方/地方补证和三组真实网页样本；已复核正文不等于已接通采集或获得完整文件。江苏原公告与更正作版本走查，河南师大/宿迁原截止已过，仅作历史样本。完整来源许可、附件、API和稳定性仍待验证。

## 已确认方向与待验证方案

| 已确认 | 仍待验证/审核 |
| --- | --- |
| 国内软件开发/系统/AI采购外包机会，软件公司客户；少源和项目优先 | 中国政府采购网先验证，官方/地方平台按需补证；正式来源准入仍待完成 |
| 微服务，Redis/K8s 从首条业务链纳入 | ingestion / processing / catalog / workspace / research / tracking 六服务边界 |
| 真实网页/文件清洗、可追溯证据与故障验证 | 真实样本、评测效果、资源容量、具体框架与模型 |
| 阿里云方向，总月预算约 100–200 元 | 机器、地域、网络、续费、备份与模型费用；预算不是购买授权 |
| 企业工作台与平台后台分开职责 | 页面细节、具体接口与身份方案 |
| 复用成熟开源 CMS，中文使用/开发体验优先 | Strapi 5 社区版暂作首选验证对象，未锁定生产选型；中文门槛见 22 |
| 缓存必须实现并验证 | 命中、失效、撤权、故障回源和容量实测 |
| Codex 执行，人工决策，按小任务推进 | 具体业务任务授权和时间表；28 周仍为容量估计 |

不再恢复四源/六源/六大区作为发布门槛。减少来源不是改回单体、取消清洗或降低权限要求，也不先创建六个空服务。

## 后台、CMS 与缓存

平台后台包含公告材料、来源任务、用户权限、平台原创内容、费用与操作记录五类功能。CMS 只管理帮助、常见问题和站内公告，不改写采购原件、不接管企业私有资料或跨服务业务表。详见 [19](docs/19-admin-cms-cache.md)。

CMS 中文优先新增为明确要求：菜单、表单、验证提示、中文输入法、搜索、媒体与发布/撤下均要验证；“有中文语言包”不等于通过。详见 [22 中文 CMS 验收](docs/22-cms-chinese-acceptance.md)。Strapi/Node 是内容支撑组件，不要求改写 Python 核心业务。

数据库保存事实、任务和报告；redis-cache 只保存可重建副本，与 redis-core 事件/配额进程分开。私有缓存命中也要当前授权，刷新报告不重跑模型，撤下或撤权不能等待长 TTL。CMS、缓存、存储和备份仍计入原总预算。

## 工程执行与证据

任务在独立分支/工作树执行，授权内跨模块修改由 Codex 完成并记录。交付区分契约、模拟、真实联调；最终版本通过必要检查与独立审查后，经人工放行通过 PR 进入 main。发布另有授权、验证与恢复措施。

```bash
python3 scripts/refresh_project_memory.py --write
python3 scripts/refresh_project_memory.py --check
python3 scripts/check_project_memory.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

上述命令只检查交接结构、链接、大小和工具测试，不构成 CMS、缓存、真实接口或生产验证。仓库规则是否已生效以 GitHub 实际配置为准，不能凭本文宣称开启了保护。

## 专题导航（按需读取）

| 文档 | 内容 |
| --- | --- |
| [00 审核指南](docs/00-review-guide.md) | 业务审核关口；旧执行分工以 20 的更新为准 |
| [01 项目计划书](docs/01-project-plan.md) | 产品阶段和边界；旧手工实施前置条件已被新分工替代 |
| [02 产品范围](docs/02-product-scope.md) | 用户故事、页面和业务契约 |
| [03 数据基础](docs/03-data-foundation.md) | 数据、清洗和追溯 |
| [04 微服务架构](docs/04-system-architecture.md) | 服务、数据所有权与独立发布 |
| [05 领域模型](docs/05-domain-model.md) | 公告、程序、标段、版本、证据 |
| [06 Agent 与记忆](docs/06-agent-memory-matching.md) | 产品 Agent 的业务记忆，不是研发代理交接 |
| [07 可靠性与安全](docs/07-reliability-security-operations.md) | 事务、消息、权限、恢复 |
| [08 评测验收](docs/08-evaluation-acceptance.md) | 业务用例、基线和阻断测试 |
| [09 排期](docs/09-roadmap-ownership.md) | 阶段与容量；旧本人手工实现要求按 20 替代 |
| [10 能力对照](docs/10-jd-alignment.md) | 能力证据；AI 生成不冒称本人手工实现 |
| [11 决策登记](docs/11-decision-register.md) | 历史需求与提案；最新协作替代项见 20 第 1 节 |
| [12 来源登记](docs/12-source-register.md) | 带日期的外部依据，不自动刷新有效期 |
| [13 来源与脏数据](docs/13-source-matrix-and-dirty-data.md) | 按缺口扩源与清洗案例 |
| [14 服务契约](docs/14-service-event-contracts.md) | 接口、事件、兼容与重放 |
| [15 K8s 与容量](docs/15-kubernetes-capacity-plan.md) | 发布、压力、故障与恢复 |
| [16 全球候选与预算](docs/16-global-sources-cloud-budget.md) | 后续来源池与费用边界 |
| [17 产品蓝图](docs/17-product-delivery-blueprint.md) | 报告、追问、人工决定与跟踪 |
| [18 交接说明](docs/18-review-handoff.md) | 新审核者入口与历史基线 |
| [19 后台/CMS/缓存](docs/19-admin-cms-cache.md) | 后台边界与缓存验收 |
| [20 研发规范](docs/20-development-standard.md) | 最新执行分工、独立审查、验证和恢复 |
| [21 上下文管理](docs/21-context-management.md) | AGENTS、短状态、按需知识与 Skill |
| [22 中文 CMS 验收](docs/22-cms-chinese-acceptance.md) | 中文优先、选型证据与下一小流程 |
| [23 首批市场与RAG](docs/23-first-market-rag.md) | 中国软件公司客户、首闭环和检索/生成分工 |
| [24 企业档案](docs/24-company-profile-minimum.md) | 八类信息、四组首用问题和逐项补证 |
| [25 国内来源](docs/25-china-source-validation.md) | 来源验证顺序、官方样本、材料限制和下一走查 |
| [学习索引](docs/learning/INDEX.md) | 按任务恢复有用记录，不等于用户已经掌握 |

main、任务分支与 PR 的实际 SHA 以 GitHub 为准。文档合并不等于业务开发、采购或上线获批；当前没有真实用户规模、业务测试成绩或生产 SLA。

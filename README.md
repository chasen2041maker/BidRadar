# BidRadar — 招标商机分析与跟踪 Agent

> 帮软件与 AI 服务团队找值得进一步核查的公开项目，读懂要求、保存证据，并跟踪变化。
>
> A multi-source, evidence-based tender intelligence agent for software and AI service teams.

**v0.2｜DESIGN_REVIEW｜2026-09-11。当前只有设计文档，没有业务代码、已接通的数据管道或部署成果。**

## 这次明确调整了什么

负责人已明确要求：采用微服务；Redis 和 Kubernetes 从基础设计及首个可运行阶段纳入；后续增加数据量、并发和故障压力；制定排期；接入多个真实来源，包含网页、文件和真实脏数据，不只消费整洁 API。

因此，v0.1 的“模块化单体起步、Redis/K8s 后置、长期只接一个来源”不再是当前方案。保留历史提交以便比较；具体服务数量、模型、预算和数据源准入仍是待审核提案。**要求做微服务，不代表已经批准六个服务的全部细节，更不代表批准现在开发。**

## 先看这四份

| 入口 | 审核重点 |
| --- | --- |
| [项目计划书](docs/01-project-plan.md) | 产品目的、规模、交付层次和边界 |
| [微服务架构](docs/04-system-architecture.md) | 六个业务服务为什么拆、谁拥有数据、怎样协作 |
| [数据来源与清洗](docs/03-data-foundation.md) | 多源组合、网页/文件、真实脏数据、可用性证据 |
| [排期与个人责任](docs/09-roadmap-ownership.md) | 相对周计划、工时假设、交付物、验收与个人实现 |

[审核指南](docs/00-review-guide.md)提供阅读顺序；[决策登记](docs/11-decision-register.md)区分已确认方向、待审核设计与未完成验证。

## 目标架构，不是当前实现

六个业务微服务提案：**ingestion 采集、processing 解析清洗、catalog 商机目录与检索、workspace 企业档案与权限、research Agent 研究、tracking 跟踪提醒。**每个服务独立镜像、迁移、数据库角色和契约；禁止跨服务直连业务表。

一个 Git 仓库不妨碍独立构建和发布。开发环境可共用一个 PostgreSQL 实例，但按服务分数据库和凭据，不能共享 ORM 实体或跨库事务。Redis 的事件传输与缓存分别部署；K8s 从首次服务部署即验证。具体内容见架构与可靠性文档。

## 数据规模怎样逐步增加

首条业务链接两种异构输入，不等于最终只有两个来源。目标是从 TED、CanadaBuys、香港 GLD、两个政府网页来源、BOAMP 等候选中逐项验证，形成 **4 个稳定来源的阶段版本，再到 6 个经审核来源的扩展版本**。英国来源保留为待复核补充。

来源数量与“当前商机覆盖”分开：历史授标/取消资料可以用于关联和清洗回归，但不能冒充仍在招标的项目。API、CSV、HTML、XML、PDF 是输入形式，不是分别计数的五个来源。详见 [来源矩阵与脏数据台账](docs/13-source-matrix-and-dirty-data.md)。

## 计划中的业务闭环

发现公开公告 → 留存原件 → 清洗与质量隔离 → 关联程序/标段/版本 → 检索并匹配企业能力 → 带证据的人工初筛 → 收藏与记忆 → 变化复核和应用内提醒。

系统不保证参与资格或中标，不自动投标、报价、联系采购方，不把未知当满足或不满足。

## 文档目录

| 文档 | 用途 |
| --- | --- |
| [00 审核指南](docs/00-review-guide.md) | 设计审核关口 |
| [01 项目计划书](docs/01-project-plan.md) | 产品目标和交付定义 |
| [02 产品范围](docs/02-product-scope.md) | 用户故事、页面、非目标 |
| [03 数据基础](docs/03-data-foundation.md) | 获取、清洗、质量、版本与使用边界 |
| [04 微服务架构](docs/04-system-architecture.md) | 服务边界、数据所有权、同步与异步通信 |
| [05 领域模型](docs/05-domain-model.md) | 公告/程序/标段/证据/状态 |
| [06 Agent 与记忆](docs/06-agent-memory-matching.md) | 受约束编排、工具、长期记忆 |
| [07 可靠性与安全](docs/07-reliability-security-operations.md) | Outbox/Inbox、恢复、缓存、权限 |
| [08 评测验收](docs/08-evaluation-acceptance.md) | 基线、真实/合成样本、阻断测试 |
| [09 排期与个人责任](docs/09-roadmap-ownership.md) | 28 周容量提案与分阶段交付 |
| [10 JD 能力对照](docs/10-jd-alignment.md) | 多公司能力样本与实际证据 |
| [11 决策登记](docs/11-decision-register.md) | 最新需求、替代决策、风险与批准记录 |
| [12 来源登记](docs/12-source-register.md) | 官方出处、核验日期及受阻边界 |
| [13 来源与脏数据](docs/13-source-matrix-and-dirty-data.md) | 候选源逐项准入、具体质量案例 |
| [14 事件与服务契约](docs/14-service-event-contracts.md) | 事件信封、数据权限、版本和重放 |
| [15 K8s 与容量计划](docs/15-kubernetes-capacity-plan.md) | 初始部署、扩容、压测、故障与费用 |

## 协作和状态

负责人独立负责全链路，并亲手实现关键逻辑；AI 协助设计、解释、审查和文档，不以生成代码量代替掌握。协作者先读 [AGENTS.md](AGENTS.md)。

当前只做文档审核，没有启动爬虫、调用业务模型、创建密钥或云资源。数据源网页可读不等于采集成功或使用许可已完成；计划中的记录数、并发、周数均不是实测成绩。具体实施由负责人另行明确授权。

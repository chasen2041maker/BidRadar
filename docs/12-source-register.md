# 12｜来源登记与核验边界

编制/读取日期：2026-09-11。本文记录本次确实读取到的内容和受阻项，不把过去聊天当作当前官方证据。

外部事实与设计提案分开。文档中的内部字段、状态、架构、样本数量和阈值是本项目设计，不是从这些来源照抄的产品要求。只做必要摘要，不复制整份 JD 或第三方公告。

## 官方数据与技术资料

<a id="s1"></a>
### S1｜TED Search API

- 原页：https://docs.ted.europa.eu/api/latest/search.html
- 本次状态：已读取正文。
- 支持：官方公开已发布采购公告查询，文档写明无需认证，操作为 `POST /v3/notices/search`。
- 不支持：本机实测成功、实际字段完整、所有附件可取、全市场覆盖、任意再分发权限。
- 项目用途：候选数据源与 G1 验证起点；本次未调用该 POST 接口。

<a id="s2"></a>
### S2｜TED Legal notice

- 原页：https://ted.europa.eu/en/legal-notice
- 本次状态：打开时显示 JavaScript/访问验证，未取得完整正文。
- 支持边界：仅确认尝试访问该官方入口；不据此复述其全部现行条款。
- 待办：在实际环境中完整阅读并登记公告复用、个人信息、外部材料与展示/分发限制。过去讨论说过可复用，不等于此次完整复核已经完成。

<a id="s3"></a>
### S3｜TED Developers' corner 与 API 总览

- 原页：https://ted.europa.eu/en/simap/developers-corner-for-reusers
- 本次状态：开发者复用页面遭访问验证，未取得正文。
- 补充官方入口：https://docs.ted.europa.eu/api/latest/index.html
- 补充状态：API 总览已读取；具体下载方式、格式字段与分页仍需按实际接口契约验证。
- 项目用途：不强行照搬历史 URL 模板，不声称当前已打通 XML/HTML/PDF 获取。

<a id="s4"></a>
### S4｜eForms Schema Documentation

- 原页：https://docs.ted.europa.eu/eforms/latest/schema/index.html
- 本次状态：已读取官方模式文档入口。
- 项目用途：后续将真实公告模式版本与解析器绑定，完成原生标识、字段和关联语义的核验。
- 限制：没有逐字段完成映射；没有把 latest 解析为项目已锁定版本。本计划的字段名均为内部设计。

<a id="s5"></a>
### S5｜LangGraph Persistence

- 原页：https://docs.langchain.com/oss/python/langgraph/persistence
- 本次状态：已读取正文。
- 支持：checkpointer 面向线程图状态，store 面向跨线程信息；内存保存器的状态不跨进程重启保留。
- 项目用途：区分图进度与长期业务记忆，不由框架状态推导业务事务已完成。
- 限制：本次未安装、运行或验证任何保存器；旧 durable-execution 链接本次重定向到此页，不作为单独新证据。

<a id="s6"></a>
### S6｜PostgreSQL SELECT / SKIP LOCKED

- 原页：https://www.postgresql.org/docs/current/sql-select.html
- 本次状态：已读取，页面当前展示 PostgreSQL 18 文档，不代表项目选定该版本。
- 支持：SKIP LOCKED 可跳过被锁行；文档指出其不一致视图不适合一般用途，可用于队列式多消费者场景。
- 项目用途：短事务领取任务的候选机制。
- 限制：不保证自动恢复、幂等或外部副作用恰好一次，这些是应用自行设计并测试的责任。

<a id="s7"></a>
### S7｜PostgreSQL Row Security Policies

- 原页：https://www.postgresql.org/docs/current/ddl-rowsecurity.html
- 本次状态：已读取正文。
- 支持：RLS 的策略和执行角色影响行访问；某些角色存在绕过边界。
- 项目用途：提醒验证实际应用角色、连接池与 Worker 权限路径。
- 限制：没有配置或测试本项目 RLS，不能声称多租户隔离完成。

<a id="s8"></a>
### S8｜Kubernetes Jobs

- 原页：https://kubernetes.io/docs/concepts/workloads/controllers/job/
- 本次状态：已读取相关故障说明。
- 支持：文档说明即使某些单次/单并行配置，同一程序仍可能启动两次。
- 项目用途：把应用幂等与发布记录设计为独立于编排器的责任。
- 限制：不是本项目已部署 Kubernetes 的证据，不据此选择任何具体集群版本。

<a id="s9"></a>
### S9｜OWASP SSRF Prevention Cheat Sheet

- 原页：https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- 本次状态：已读取官方防护指南。
- 项目用途：受控外链采集的允许列表、IP/DNS、重定向与网络边界设计依据。
- 限制：列出设计不等于防护实现或渗透测试通过；必须用受控测试验证连接目的地与重定向。

## 雇主原始 JD

<a id="s10"></a>
### S10｜Apertera — Generative AI Engineer

- 原页：https://apertera.applytojob.com/apply/ZLostY5yaB/Generative-AI-Engineer
- 本次状态：雇主原页正文可读取；未确认名额持续有效，页面未提供明确发布日期。
- 必要摘要：上海，相关行业经验 2 年以上优先；Python、Context/RAG/Harness、云与可复现实验，同时涉及训练和推理。
- 用途：能力参照，不认定为杭州初级应用岗，不推测薪资。

<a id="s11"></a>
### S11｜EPAM — AI Engineer

- 原页：https://careers.epam.com/en/vacancy/ai-engineer-bltxjv3f5r67tmhbonh_en
- 本次状态：雇主原页正文可读取；未确认持续招聘状态，未见明确发布日期。
- 必要摘要：仅限乌克兰；Python 3 年以上、B2+ 英语；LangGraph、数据工程、向量库、RAG 评测与部署监控。
- 用途：能力参照，不视为可从中国加入的远程岗位。

## 历史线索：不作为当前在招证据

<a id="h1"></a>
### H1｜Pixlr 岗位整理

来源为此前用户提供的岗位研究整理，不是本次读取的雇主原页。本次未取得可确认的原始 JD 链接，不猜测补链，不上传原附件或简历。对 TS/Node、Agent 状态与队列等要求仅标历史线索，实际投递前重新核验。

<a id="h2"></a>
### H2｜State Street 岗位整理

来源为此前用户提供的岗位研究整理，不是本次读取的雇主原页。本次未取得可确认的原始 JD 链接，不将历史的地点、年限和技术清单标为当前事实。数据库、权限与云交付只作待核验能力线索。

## 核验清单

| 项目 | 结果 |
| --- | --- |
| TED 官方查询文档 | 已读取 |
| TED 真实 API 拉取、分页与附件 | 未执行 |
| TED 完整法律声明复核 | 当前工具读取受阻，待完成 |
| 真实样本字段/语种/关联质量 | 未测 |
| LangGraph/PostgreSQL/K8s/OWASP 相关资料 | 已读取必要部分，未实现 |
| 两份雇主原始 JD | 已读取，仅作能力样本 |
| 历史杭州岗位线索 | 未重新核验，不作已确认机会 |
| 真实使用者价值、模型费用、性能指标 | 未验证 |

后续来源状态变化必须更新本表与对应决策，不用旧结论覆盖新限制。

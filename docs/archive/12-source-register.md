# 12｜来源、核验状态与事实边界 v0.2

**核验日期：2026-09-11。**区分本轮重读、沿用 v0.1 记录、页面可见、访问受阻和真实运行。没有执行数据接入、批量下载、模型、负载或部署。

架构、内部字段、服务数量、工时、数据量与性能阈值均为本项目设计，不是来源事实。仅保留必要摘要，不复制整份 JD 或公告。

## 原有数据与技术来源

<a id="s1"></a>
### S1 TED Search API

https://docs.ted.europa.eu/api/latest/search.html

本轮重读。说明已发布公告查询、无需认证、`POST /v3/notices/search` 及 XML 批量获取能力。未实际调用此 POST，未证明字段完整、附件全可取或任意再分发。

<a id="s2"></a>
### S2 TED Legal notice

https://ted.europa.eu/en/legal-notice

本轮再次访问出现 JavaScript/机器人验证，没有完整法律正文。因此保留采集、存储、模型处理、展示/分发的完整复核任务；不拿历史聊天中的肯定表述代替授权证据。

<a id="s3"></a>
### S3 TED 开发者入口

https://ted.europa.eu/en/simap/developers-corner-for-reusers

https://docs.ted.europa.eu/api/latest/index.html

沿用 v0.1：开发者网页读取曾受阻，API 总览可读。本輪具体能力优先以 S1 支持；不据旧模板生成未验证下载路径。

<a id="s4"></a>
### S4 eForms Schema

https://docs.ted.europa.eu/eforms/latest/schema/index.html

沿用 v0.1 的官方文档入口记录，本轮未逐字段重读。用于后续真实模式版本映射；latest 不代表项目已锁定版本。

<a id="s5"></a>
### S5 LangGraph Persistence

https://docs.langchain.com/oss/python/langgraph/persistence

沿用 v0.1 阅读记录：线程 checkpoint 与跨线程 store 分工。本项目尚未安装/运行保存器；业务事务不因图状态保存而完成。

<a id="s6"></a>
### S6 PostgreSQL SKIP LOCKED

https://www.postgresql.org/docs/current/sql-select.html

沿用 v0.1 阅读记录。队列式领取可跳过锁行，不是通用一致性读取或自动幂等。本轮未运行 SQL；版本待技术验证。

<a id="s7"></a>
### S7 PostgreSQL RLS

https://www.postgresql.org/docs/current/ddl-rowsecurity.html

沿用 v0.1 阅读记录。角色及策略影响行访问，存在绕过边界；本项目未配置/验证 RLS，不声称隔离已完成。

<a id="s8"></a>
### S8 Kubernetes Jobs

https://kubernetes.io/docs/concepts/workloads/controllers/job/

沿用 v0.1 阅读记录。同一程序可能重复启动，应用需自行控制重复副作用；不是已部署的证据。

<a id="s9"></a>
### S9 OWASP SSRF Prevention

https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html

沿用 v0.1 官方指南记录。用于域/IP/DNS/重定向和网络边界设计；没有实施或渗透测试结果。

## 雇主原始材料

<a id="s10"></a>
### S10 Apertera — Generative AI Engineer

https://apertera.applytojob.com/apply/ZLostY5yaB/Generative-AI-Engineer

本轮重读雇主原页：上海；相关经验 2 年以上优先；Python、Context/RAG/Harness、云和实验记录，同时强调训练/推理。未见明确发布日期，不保证名额持续有效；非杭州初级纯应用岗，不推测薪酬。

<a id="s11"></a>
### S11 EPAM — AI Engineer

https://careers.epam.com/en/vacancy/ai-engineer-bltxjv3f5r67tmhbonh_en

本轮重读雇主原页：候选人需在乌克兰，Python 3 年以上、B2+ 英语；Agent、数据工程、向量库、RAG 评测和部署监控。仅能力样本，不认定从中国可远程加入。

<a id="h1"></a>
### H1 Pixlr 历史岗位整理

来自此前用户提供的研究整理，本轮未取得雇主原页，不猜原始链接。TS/Node、Agent 状态和队列等仅作历史能力线索；不把其地点或在招状态标当前事实。

<a id="h2"></a>
### H2 State Street 历史岗位整理

同为此前用户材料，未重新读取原 JD；数据库、权限、云交付仅作待核验能力线索。不上传用户原始附件或简历。

## v0.2 新增来源

<a id="s13"></a>
### S13 CanadaBuys tender notices

https://open.canada.ca/data/en/dataset/6abd20d4-7a1c-4b38-baa2-9525d0bb2fd2?wbdisable=true

官方目录本轮可读，显示 2026-09-10 修改；列有新/开放/按财年历史 CSV 及 XML 数据字典，明确该数据集文件只覆盖联邦政府组织，不等于网站所有层级机会。可见双语字段说明和历史格式迁移说明。未下载解析 CSV，具体授权/字段质量及最新性需逐项验证。

<a id="s14"></a>
### S14 香港 GLD 公开招标 XML 目录

https://data.gov.hk/en-data/dataset/hk-gld-gldetb-gldetb-tendernotice/resource/16d4f5cd-45e1-496a-bcf4-f424d35e2ad1

本轮读取官方资源说明，列出 XML 与公开招标编号、主题、部门、关闭时间等。资源入口为 `https://pcms2.gld.gov.hk/iportal/TenderNotice.xml`。本轮未调用或解析 XML，也未登录 e-Tender Box；公开列表不等于全部投标附件免登录。

<a id="s15"></a>
### S15 香港医务卫生局公开采购网页

https://www.healthbureau.gov.hk/en/tender/index.html

本轮读取 HTML：同页包含不同年份、需求征询、招标、关闭说明和结果，存在 PDF/Word 链接或图标、标题与附件的层级关系。观察到同一信息块含不同性质截止时间。未批量获取附件、未核准其再分发；页面的医疗物资不属软件业务，软件相关记录与非相关负例必须区分。

<a id="s16"></a>
### S16 香港规划署 Tender Notice

https://www.pland.gov.hk/pland_en/consultancies_and_tender/tender_notice/index.html

本轮读取 HTML 表格；当前所见主要为授标及取消记录，含软件/系统服务。`Open` 在 Tender Procedure 列，即使已授标/取消仍出现。适合作为真实语义清洗检查，不把它说成已找到可投项目。没有批量采集或许可完成证据。

<a id="s17"></a>
### S17 BOAMP 数据/API 入口

https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/api/

https://boamp-datadila.opendatasoft.com/explore/dataset/boamp/information/

本轮页面可确认 BOAMP 与数据集标识 `boamp`，但含动态模板占位符，未完整取得 schema、记录和许可。只能列候选入口，不能写成 API 已打通或数据集为空。法语分析仍需独立验证。

<a id="s18"></a>
### S18 英国 Contracts Finder / Find a Tender 补充入口

https://www.contractsfinder.service.gov.uk/apidocumentation

https://www.contractsfinder.service.gov.uk/apidocumentation/V2

官方文档本轮可读，区分发布接口与 OCDS 检索/CSV 获取，V2 有兼容迁移说明。主页 `https://www.contractsfinder.service.gov.uk/` 与 `https://www.find-tender.service.gov.uk/` 本轮均遇 403；未核验现行制度下的完整新公告覆盖，也未实际请求数据。仅列补充/历史验证，不假定完整实时英国来源。

<a id="s19"></a>
### S19 Redis Key eviction

https://redis.io/docs/latest/develop/reference/eviction/

本轮官方页可读，说明内存/淘汰策略。事件和缓存分实例是本项目据故障语义作出的设计，不是 Redis 自动提供的业务保证。

<a id="s20"></a>
### S20 Redis XAUTOCLAIM

https://redis.io/docs/latest/commands/xautoclaim/

本轮官方页可读，命令涉及消费组消息所有权转移。不能据此推断 exactly-once、业务任务自动恢复或已丢数据可找回；这些由持久事件日志/Inbox/任务协议设计并测试。

<a id="s21"></a>
### S21 Kubernetes HPA

https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/

本轮官方页可读。HPA 依赖指标与资源配置；队列扩容所需自定义指标适配器及配额控制需另外实现。本项目未启用 HPA，不引用页面版本作已冻结环境。

<a id="s22"></a>
### S22 Kubernetes NetworkPolicy

https://kubernetes.io/docs/concepts/services-networking/network-policies/

本轮官方页可读。策略执行需要支持它的网络实现；本项目将真实 allow/deny 连接测试纳入验收，不把 YAML 存在当隔离生效。

## 核验总表

| 项目 | 状态 |
| --- | --- |
| 新版架构与排期 | 设计提案，未实施 |
| TED/CanadaBuys/GLD 入口或资源说明 | 已读取，不代表连接成功 |
| 两个真实 HTML 页面 | 人工网页阅读，未系统采集/统计 |
| BOAMP 完整 schema/记录 | 未取得 |
| 英国实际数据与现行覆盖 | 未验证，部分页面受阻 |
| 全部来源及附件的完整使用边界 | 未完成准入 |
| 自然脏数据样本集/质量指标 | 未建立，仅记录页面观察与待测类别 |
| 六微服务/Redis/K8s/模型/压测 | 均未运行 |
| 当前招聘事实 | 两原页可读；其它历史线索未复核 |

后续每源保存核验时间和结果，不让旧肯定结论覆盖新限制。源许可与代码许可证分开决策。

# 33｜R1/R2：研究 Agent 与持续跟踪

2026-10-01｜AGENT-001。负责人授权完成R1/R2，并明确AI是核心技术。基线`fc68ef51353623c9dd638eb2219dadea99d79042`（R1-A，PR #12未合并）。本契约将随实现核验修订，不把设计写成验收成绩。

## 目标、模型与边界

企业档案和明确选择 → 持久研究任务 → 模型选择受控工具/检索证据/有限补证 → 条件与引用核验 → 保存报告 → 同项目追问 → 人工决定 → 明确跟踪及自动复核委托 → 公告/档案变化 → 影响说明、复核和应用内唯一提醒。

采用单一研究主编排器和确定性校验节点。AI技术交付必须能展示真实工具选择轨迹、上下文/输入版本、引用、未知项、费用和对照评测；不靠角色数量、固定脚本或空服务展示Agent。模型原始思维不作为产品输出或审计证据，只保留工具调用和可解释结果。

用户指定DeepSeek V4 Flash，本轮模型测试预算20–30元，执行取20元人民币上限；凭据只在仓库外本机文件。2026-10-01核查[官方价格/模型页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)及[升级说明](https://api-docs.deepseek.com/zh-cn/news/news260910/)：请求保留`deepseek-v4-flash`，官方现路由至V4.1 Flash，记录实际响应模型；关闭thinking，峰时无缓存输入2元/百万、缓存命中0.04元/百万、输出8元/百万做保守估算，保留价格版本。预算按所有研究/追问/复核/失败调用累计预留并核对；供应商超时/用量缺失记未知，不能当零。应用限额不是供应商账单硬封顶保证。

本轮所有真实入口共享仓库外`deepseek-r12-20261001.sqlite3`预算账本；业务库重建不能刷新20元授权。公司上限10元、单任务1元均为本轮开发保护值，不是正式套餐。marker发现账本丢失即拒绝新调用。全部消息含恢复、工具结果、追问和历史报告统一做联系方式最小化，保存脱敏版本标识；原件不修改、不进供应商请求。权限代数在受理时冻结，移除再加入不能使旧任务或旧委托复活。

只用两家虚构企业和既有获准公开采购样本的必要片段；真实企业、完整敏感证明、任意URL采集和生产部署不在本轮。现阶段服务各自SQLite/回环HTTP，保持D-TENANCY-01正式PostgreSQL/RLS方案。Redis/K8s和R3部署不冒充本轮验收；获准历史变化回放明确标识。

## 服务与入口

catalog、workspace、research、tracking各自拥有数据库，仅通过有界HTTP通信；可共享纯传输工具，不共享业务ORM或跨库事务。每个服务的内部API精确Host、独立Bearer令牌、loopback绑定、JSON/大小/超时校验；模型不接触服务令牌或任意路由。共用传输层正常body读取总截止10秒；早拒绝先发送错误JSON并半关闭写端，再有限丢弃最多64KiB/250ms尾部，歧义报文不进入解析或业务。此边界避免普通迟到小请求体造成连接竞态，不承诺超界发送者能读完响应。workspace继续作为浏览器入口，验证Cookie/Origin/CSRF并从当前会话派生actor_id，浏览器不得自报主体。

内部接口统一前缀`/internal/v1`（catalog保留`/v1`）。内部调用含`workspace_id/actor_id`仍须向workspace重验当前授权；服务令牌只识别受信本地调用者，不替代业务权限。用户明确提交的持久任务可在刷新/退出浏览器后继续，取消任务须显式操作；成员撤权/降级在下一私有读取、工具动作和模型调用前拒绝。已发出外部请求不承诺瞬时撤回。

| 拥有者 | 新接口/输入 | 输出与边界 |
| --- | --- | --- |
| workspace | POST authorize `{actor_id,workspace_id,action}`，action=read/analyze/track/admin | 当前成员角色、membership_version、current_profile_revision；无权限拒绝，依赖不可用则关闭动作 |
| workspace | POST context `{actor_id,workspace_id,profile_revision,selection_id}`；selection_id可null | 指定不可变档案、当前版本、选定选择（若给ID须selected）；不自动换新档案 |
| workspace | POST events `{schema_version,after,limit}` | 持久顺序事件引用：ProfileConfirmed/AccessChanged；不传私有正文，仅tracking可拉取 |
| catalog | GET /v1/observations/{notice_id}/{observation_id} | 固定不可变观察，不存在404，不静默返回latest |
| catalog | GET /v1/bundles/{notice_id}?snapshot=N | 截至接收序号的主观察和有依据的incoming更正/结果，候选/歧义单独保留；不按标题合并 |
| catalog | GET /v1/changes?after=N&limit=M | 按提交接收序号分页的观察引用、高水位与next_after；seq不是来源业务版本 |
| research | POST analyses | 先保存任务/输入/幂等再202+run_id；后台Worker执行 |
| research | 内部POST runs/list、runs/get、runs/cancel；analyses的kind=question | 当前授权、明确取消、同项目问答固定父报告/输入版本，刷新不建新任务 |
| tracking | decisions、watches、notifications、delegations/check | 决定与watch分开，所有私有接口当前授权；只有有效明确auto_reassess委托才能发起自动模型任务 |

内部请求携带schema_version=1，未知字段/类型拒绝；各响应按所属资源契约返回，不承诺每个响应都有schema_version。浏览器入口是`/api/workspaces/{wid}/research|tracking/...`：研究列表/详情用GET，analyses、questions和runs/{id}/cancel用POST；网关映射为内部POST并注入身份。任务时间为服务端UTC ISO8601；预算整数micro-CNY（1元=1,000,000），未知实际费用为null并保留预留额。分页limit1–100；研究按created_at降序、id降序，以before任务ID为游标，事件按seq升序以after为游标。错误`{error:{code,message}}`，400输入/401服务身份/403权限/404无权对象或不存在/409版本幂等冲突/429额度/503依赖故障；网关把依赖服务401转503，避免误注销有效浏览器会话。旧浏览器/目录v1接口保持兼容，新功能未配置时明确不可用。

## 研究输入、任务与费用

每次研究限一个选定公告范围；多项选择在页面逐项目明确提交，不能空选分析全部。人工分析请求含`actor_id,workspace_id,selection_id,notice_id,key,mode`，mode=agent或baseline。追问另含`parent_run_id,question,key`，正文1–2000字，保留有限上下文，旧回答不能当原始证据。跟踪命令还含delegation固定watch/rule/change/input版本，研究服务接受及每次后续动作都重查委托。

冻结manifest含schema_version、workspace_id、actor_id、scope_notice_id、profile（正式完整版本对象）、observations（公共规范观察列表，主项第一）、catalog_snapshot、question（可null）、previous_report（可null）、kind（analysis/question/reassessment）、config（规则/提示词/模型/检索版本）。后续材料只通过明确新分析/复核进入，不原地修改报告。

任务状态queued/running/waiting_input/retry_wait/succeeded/partial/failed/cancelled；成功指研究流程，不指投标条件满足。状态历史、输入、checkpoint、报告、幂等结果归research。领取用有期限lease和单调epoch；旧Worker不得保存结果。取消持久化；不在数据库锁内执行HTTP/模型。

模型调用前同事务检查任务/公司/本轮上限并预留费用、保存attempt；每次工具/模型/发布前检查取消、当前权限、委托和租约。响应后记录供应商模型/请求ID/usage/价格版本。未知计费保留预留且停止自动继续；崩溃恢复不盲重放已可能发出的模型调用。无副作用的本地步骤可从checkpoint继续，外部调用不承诺恰好一次。

首轮有界参数作为开发配置：最多8次模型调用（包括核验/修订）、16次工具动作、单调用60秒、从首次领取起300秒执行窗口、输出4096tokens；执行窗口在动作边界检查，排队不计入，恢复不重置started_at；不能强行中断已发出的请求，不承诺端到端硬截止。真实评测后根据证据调整。每请求保守输入估计和输出上限预留，实际usage超估计仍按真实值入账并暂停后续动作；不伪造供应商硬上限。网络错误不自动换模型或重试收费调用。

## Agent、证据与报告

模型通过chat messages和函数工具协议真实决定检索/读取/完成。工具仅为冻结输入的`search_evidence`、`read_evidence`、`get_profile_snapshot`、`finish_report`（仅提案）；参数本地schema验证，不允许URL/SQL/Shell/文件路径。工具结果按tool_call_id返回，缺材料是结果不是下一条网页指令。

Agent纯入口为`run_agent(manifest, complete, *, guard, checkpoint, resume=None, max_steps=8)`；complete接收messages/tools并返回供应商适配后的message/usage/model，费用由调用包装器持久化；guard在每动作前重验权限/取消/lease；checkpoint保存有限执行状态和工具轨迹。返回report、trace、state和quality，进程/HTTP由服务层负责。测试注入脚本provider与真实provider严格标识。

证据ID由来源观察/字段位置/片段计算，带notice_id/observation_id/raw_sha256/locator/text；不从别的项目或新版本补足；新索引保留原label和结构化money_role，因此新任务的证据ID可能变化，旧报告保持原引用。现有规范数据只有选定证据片段，不是完整标书；报告须写实际覆盖与未获取材料。检索基线用结构分类/中文关键词与字符片段匹配，记录检索版本；是否引入向量依实际效果评测，不先堆组件。

报告包含scope、summary、findings（category/requirement/status/reason/evidence_ids/profile_fields/unknown_reason）、questions、coverage、limitations与追问answer。五状态met/unmet/unknown/conflicting/not_applicable；每项已知要求必须有当前输入引用，企业未填资质不能判断满足/不满足，管理员确认不是资质认证。程序校验证据存在/范围/数字及字段，额外语义核验检查引用能否支持结论；最多两次修订，且总模型8次/工具16次上限不增加；未通过降级partial并显示未决项，不把可点击引用等同事实正确。要求、理由和未知说明中的明确采购金额断言按角色、数值、原单位与币种绑定本项引用，企业自身预算另由本项企业字段支持：预算、最高限价和文件费同额也不能互为证据，表头/表行共同证明时须完整引用。摘要/回答仅对本报告实际引用的完整ID豁免数值扫描，原文不改，未引或伪造ID仍拒绝。这个机械守卫不是完整自然语言或法律判断器，复杂条件仍须语义核验及独立样本审查。

报告分三层：findings为0–30项有依据的采购要求，建议优先3–6个关键项；所有类别（含materials）必须引用非空已读证据。questions展示待核查缺口，覆盖缺口只由服务器coverage/limitations说明；不把疑问或企业声明伪装成来源要求。没有finding直接partial/not_verified，不对空数组花费语义核验。动态工具schema只枚举本次已读引用ID，恢复时严格重建并比较，不自动猜补错误ID。零已读证据仍能使用检索工具，只是此时finish不能提交非空finding。

资格要求采用抽取式契约：每项只能引用一条已读、可完整摘录的资格entry，requirement与整条原文只允许空白规范化差异，统一上限2400字。类别由来源同时约束，不能换成other/technical逃避；过长或隐私裁剪形成的片段标记qualification_extractable=false，不冒充完整条款。规则基线展示片段时另列限制。程序不为模型补ID或改写候选，旧报告不迁移。当前资格证明未核验，qualification+unknown的reason固定为“企业资格资料尚未核验，本条要求及适用分支需结合原文和企业证明逐项核对。”，unknown_reason固定为“缺少已核验的企业资格资料，当前不能确认本条的适用情况。”；模型必须提交正确文本，程序只校验、不补写。代价是不能自动给出已核完的材料清单，详细分支由原文承载。技术/交付等自由理由及摘要仍须语义核验，完整摘录不等于整份报告不会出错。

追问answer对外仍为string/null；新候选须按本报告finding顺序组合1–3条不同的完整reason文本，仅允许规范空白，非追问为null。程序检查匹配，不改写候选、不补新句、不复用旧报告理由；语义节点按派生answer_finding_indices检查所选理由是否支持结论并回应子问题。除上述未核验资格固定说明外，理由仍由AI生成和核验，复用不保证理由本身正确。基线追问也复用自身保守理由；无条目保持null和partial，不设置模板白名单。

questions对外仍为string[]，产品含义为待核查缺口，不要求疑问句；最多12项、每项600字，仅完整复用当前finding的非空unknown_reason，允许空白规范化及选择顺序，不得截句、补句、拼接或重复。unknown_reason上限800字，过长时不可截短放入questions；其本身仍须逐项证据核验。程序派生questions_finding_indices供语义核验，无checkpoint新字段；基线也从自身缺口去重，没有条目就用空列表，没有通用模板豁免。此约束不限制用户自由追问。

当前配置为`bounded-research-agent-v14` / `research-question-gaps-v13` / `frozen-evidence-v13-question-gaps`。provider使用`deepseek-chat-v2-temperature-zero`，temperature=0及provider版本写入冻结配置和计费请求标识；降低随机性不保证确定性或正确性。旧任务保留其冻结配置；版本变化后待执行任务等待人工处理，不静默用新规则续跑。两次修订均需至少留出提出新报告与核验的两次模型额度；格式纠正不等于语义通过，错误前提不能借开放问句绕过检查。

## R2 委托、变化与提醒

人工decision状态needs_review/follow_up/dismissed，带reason、expected_version、key，历史追加。watch独立active和auto_reassess（默认false），包含actor、公司、notice_id、rule_version、期望版本及明确复核范围；自动委托默认且最长7天，成员授权版本变化后须重新授权。停跟踪不删除历史也不自动改变人工决定。

本轮持续跟踪处理已进入catalog的新观察和workspace正式档案事件；四服务运行器不会主动采集源站，源站变化尚未导入时无法发现。因此本阶段不承诺网站无人值守持续更新。

tracking保存每个owner的连续接收游标和Inbox，按固定snapshot消费每个新观察，不只轮询latest以免A→B→A丢中间变化。新更正/结果通过incoming有依据关联映射到watch；关系不明给待核查，不能自动改写原公告。只比较结构化字段相同不足以声称原文只是排版变化：raw变化但缺完整正文fingerprint时记unclassified_content_change，保守提示/复核。

每个watch/规则/变化fingerprint唯一change；提醒另按kind去重，因此同一变化可各有一条change_detected和reassessment_completed，后者也可能对应partial，不代表条件满足。实质变化标记相关报告陈旧，auto_reassess为真且当前权限/委托有效时保存reassessment命令；额度由research在模型外发前检查并预留。接受后按command key查research终态，丢失响应不能换键重复创建。正常派送/对账只领取pending/accepted；deferred须人工显式重试，waiting_input为待处理终态，不自动恢复。不能靠事件收到就称研究完成。取消或改规则使旧delegation失效；迟到结果仅留历史，不复活watch。重建模式只重建投影默认不发布历史提醒。

## 验收与集中阅读

开发测试与真实AI评测分层：工具schema/注入/错引用/缺材料/资质未知；两公司/三角色/撤权；幂等/并发取消/lease过期/未知计费/重启；事件重复/断点/乱序/更正incoming/原文未分类变化；无委托不调模型、停watch阻断下一动作、历史重建不提醒。浏览器完成研究→报告→追问→决定→跟踪/变更提醒。

同样冻结资料与问题比较规则基线和Agent，记录引用支持、未知误判、关键条件漏项、耗时及真实费用；人工标签与模型草拟分开，合成/真实/历史回放分开。至少覆盖真实公开样本及不同失败类，不能用测试数量替代AI效果。本人仍待阅读/实践，最终精选3个AI核心入口给统一导读；未执行项明确缺口，独立审查绑定最终SHA。

## 本地运行与复验

在仓库根目录使用Python3.10+，仅标准库。`python scripts/run_research_workbench.py`启动四个独立进程，默认8965–8968；Ctrl+C停止本次进程，重启保留各服务数据。浏览器打开`http://127.0.0.1:8965/`，虚构账号在忽略目录`.bidradar-data/r12-workbench/demo-accounts.json`，不提交或公开该文件。首次只有明确标记的虚构样本；可用`--bundle <已获准的normalized.json>`导入既有规范包，本命令不自动采集网站。

本轮已获模型与费用授权，只有`--enable-model`才读取用户目录`.bidradar/credentials/deepseek-api-key.txt`并调用固定DeepSeek官方端点。默认运行不调用模型；点击需要模型的任务会停在待处理状态，不冒称已分析。预算账本固定在用户目录`.bidradar/budgets/deepseek-r12-20261001.sqlite3`，不要删除账本或marker来重置额度。`--root`仅切换业务库；`--port`从指定端口连续使用四个端口。

真实验收CLI：`python scripts/evaluate_research_case.py --notice-id <目录公告ID> --case-key <唯一用例名> --execute --wait-seconds 45`。不传`--execute`只显示计划；同一用例名重复运行复用原命令，故意复验新代码才换用例名。`--company 1`使用第二家虚构企业；`--parent-run-id <已完成任务ID> --question <问题>`测试同项目追问。完整冻结输入/报告留本机忽略目录`evaluations`，终端仅输出状态与预算；退出0只证明CLI成功，不证明报告质量。

离线回归用根AGENTS的全量unittest命令；重点入口`test_research_agent.py`、`test_research_runtime.py`和`test_tracking_http_replay.py`。后两者运行真实回环HTTP、临时分库和脚本模型，不花真实费用；实际供应商结果、浏览器及最终SHA证据另列AGENT-001笔记。恢复边界是本地进程故障与应用幂等，不是生产断电恢复承诺。

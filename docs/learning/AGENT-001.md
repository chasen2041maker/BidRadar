# AGENT-001｜研究 Agent 与持续跟踪

摘要：2026-10-01获授权完成R1/R2，以真实模型工具循环、证据质量与变化复核为核心；DeepSeek V4 Flash测试预算按20元执行，实施中。

## 本次改变

新目标覆盖R1后半段及R2；基线fc68ef51353623c9dd638eb2219dadea99d79042，独立分支codex/r1-r2-agent。模型使用/测试预算由负责人本轮明确，凭据仓库外保存。范围、接口和验收见[33](../33-agent-research-tracking.md)。

基础接口已增加服务身份、档案固定版本/授权代数、所属服务事件流和目录研究包。研究预算账本与可重建业务库分开，预留后才外发，未知费用继续占额。它们是实施中的依赖，不代表 Agent/R2 已验收。

四个本地服务、持久任务、研究工具循环、人工决定和持续跟踪界面已连接，正在真实验收。首次真实模型返回deepseek-flash，但反复检索触及16次工具上限，按设计降级为partial；这证明调用链可用，不证明AI质量通过，后续修复保留这条失败记录。

## 阅读顺序

阶段结束统一阅读这三个入口，当前实现仍在验证中：

1. `services/research/agent.py::run_agent`：为什么模型选择工具，而权限、次数、核验和发布权由程序掌握。先看SYSTEM与TOOLS，再顺状态机读。
2. `services/research/evidence.py::EvidenceIndex / validate_report / finalize_report`：从固定观察生成引用身份，理解“存在引用”和“引用支持结论”的区别；缺证据时为什么必须unknown。
3. `services/tracking/service.py::consume_event / _queue_reassessment / check_delegation`：变化怎样变成新研究任务，为什么关注、自动复核授权和人工决定分开。

需要深入恢复时再读`research/worker.py::tick`→`budget.py::complete`→`store.py::claim/checkpoint/finish`。不要求先通读全部文件；最终审查还会核对注释和行为。

## 数据流与失败边界

固定档案/公告输入→持久任务与预算→模型选择只读工具→验证引用与未知→私有报告→显式跟踪委托→变化/复核/唯一提醒。工具、模型、恢复和发布均检查当前权限，外部内容不得扩权。

## 验证与未验证

实施中。尚无本轮真实模型质量、费用、R2或最终审查成绩；基线395项仅证明R1-A。使用两家虚构企业及获准公开样本，SQLite不冒充正式PostgreSQL/RLS。

2026-10-01，本工作树 Windows/Python3.13.12/SQLite3.51.1，基于613f4ca工作树差异：`python -m unittest discover -s tests -p test_catalog_http.py -q` 21项、`test_workbench_http.py` 13项、`test_workspace_store.py` 34项、`test_research_budget.py` 5项均退出0。均本地虚构输入，包含真实回环HTTP；未调用模型。最终提交和全量验证另记。

中途整合d775b4d后全量`python -m unittest discover -s tests -p test_*.py -q` 466项、13.827秒、退出0；此时新运行脚本尚未纳入。独审发现的脱敏键/电话格式P1在8786118关闭，provider11项复验通过。恢复断点、真实事件名称和待人工终态的P2正在修复，未称最终通过。

工程复验（2026-10-01 01:40+08，完整`bb5458357983c15a679169861cfa61c124b22dd0`，Python3.13.12）由未参与实现的review_source_001独立执行runtime15、tracking35、workbench HTTP14、agent21、budget6，共91项退出0；JS语法和增量差异检查通过。原五项P2关闭：已入账响应恢复、ProfileConfirmed事件名、waiting_input终态、报告当前性、内部401不误注销。另核对同时间分页、档案变化+目录离线、撤权与真实注销。此结论只覆盖该SHA工程边界，不代替后续AI质量与最终审查。

真实样本首轮（b7c6a22，2026-10-01，8965–8968本机四独立进程，Python3.13.12）：`python scripts/evaluate_research_case.py --notice-id b8b2c90fb20e1302c4bfc0d065f21fb8b29b9a90602a0a11610eba4f08908ac0 --case-key gansu-ai-a-v1 --execute --wait-seconds 45`退出0。5次供应商调用，任务7.847秒，费用峰价估算0.028956元，无未知费用；Agent结果partial/not_verified，工具16次未形成已核验分析。完整证据位于本工作树忽略目录`.bidradar-data/r12-workbench/evaluations/gansu-ai-a-v1.json`，不发布凭据/账号文件。命令成功不是质量成功。

第二轮同命令仅case-key改`gansu-ai-a-v2`（运行代码cd30a45/8f6f70e），5次模型调用、13次工具、0.088042元；仍partial：否定免责声明被关键词规则误伤、无引用缺口误列要求、交付检索漏掉已存在原句。第三轮`gansu-ai-a-v3`（`eac79de18f80eb11de425d982b28f61db89b97bc`），5次模型/11次工具、0.115375元；分类回退已找回交付片段，但摘要添加机器标识等数字被拒，未通过AI验收。被拒候选另暴露“可能冲突”误用conflicting、把采购分类暗变为行业资质门槛，正在修复。两轮命令退出0，完整失败文件仍在同一evaluations目录，未改写成通过。至第三轮累计15次调用/0.232373元峰价估算/0次费用未知，单次和累计均读取持久账本。

第四轮甲公司`gansu-ai-a-v4`与乙公司`gansu-ai-b-v1`（40b6402591442a5b7a30ceabc6311cd63303cf8f）：均仍partial，分别6次/0.124127元、5次/0.097984元。甲公司已正确区分开发期限、驻场时长与资格未知并进入语义核验，但核验提示未说明schema允许的材料覆盖条目，产生规则不一致；乙公司把64位引用ID漏写一位，严格检查拒绝，未自动猜测纠正。继续修复核验上下文与精确引用修订提示。累计26次、0.454484元、费用未知0次；甲公司本轮采用新档案第2版，配对规则基线与Agent使用同一版本，不拿它与旧轮做纯模型收益统计。

浏览器本地四进程验收（2026-10-01 01:53+08，40b6402前已运行的eac79de后端，当前UI）：虚构甲公司仅开启watch、不授权自动模型；通过真实档案表单提交并确认第2版，生成一条档案变化与唯一提醒，可标记本人已读；无复核任务、账本调用数未增加。ResearchStore只读比对确认旧v3报告正文未变且仍固定档案第1版。截图`.bidradar-data/verification/tracking-no-auto.png`及`agent-v3-partial.png`；自动复核另待验证，不以本项冒称完整R2通过。

## 我的参与

负责人明确AI核心方向、DeepSeek V4 Flash和20–30元本轮预算；执行取20元。本人代码阅读/修改/运行/能解释仍待反馈。之前练习不代做。

## 值得保留的决定与坑

变化观察ID不等于业务变化；仅规范字段相同不能推导原文只改排版。跟踪须发现后续更正incoming关系，不只轮询原公告。真实Agent与脚本provider分开，收费超时不能记零或盲目重试。

小练习留给负责人：指出“合同签订后60日内完成系统开发”能否证明需要连续驻场60天，沿引用找到依据，并解释缺材料时报告该用哪种状态。此题尚未由负责人回答，不预记掌握。

亲手修改练习：在自己的练习分支自拟一条不同的虚构采购要求和一条企业限制，给`test_research_agent.py`增加一个状态边界用例；先写预期及理由，再运行测试。这个新用例由负责人选择和编写，本轮不代做。

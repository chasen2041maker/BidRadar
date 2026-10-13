# 05｜领域模型与服务数据归属 v0.2

**Proposed｜2026-09-11。逻辑模型不是已冻结 SQL，不包含未经样本验证的源字段映射。**

## 1. 核心概念

公告是一次发布；采购程序是有依据关联的过程；标段可能有独立金额、日期与条件；文档版本是不可变内容及解析结果；商机是面向企业的工作视图；分析运行是特定输入的执行。

更正可能以另一份公告引用旧公告；转载、语言和格式不等于不同商机。局部 LOT-0001 只能在来源/程序作用域解释。缺可靠程序信息时保留 unlinked，不伪造完整关系；可以程序级初筛，但不能假称标段级分析。

## 2. 实体归属

| 所有者 | 实体 | 不变量 |
| --- | --- | --- |
| ingestion | source_registry、sync_run/item、raw_asset | 原件不可变，观察时间与发布时刻分开 |
| processing | parse_run、document_version、evidence_span、normalized_observation、quality_issue | 解析版本与原始版本分开；隔离可追查 |
| catalog | notice_identity、notice_revision、notice_relation、procedure、lot、scope、canonical_revision、public_search_projection | 源身份分命名空间；合并可审查/撤销；历史不覆盖 |
| workspace | workspace、membership、profile_revision/assertion、preference_revision | 确认事实有确认者/时间/有效性；偏好不是资质 |
| research | analysis_run/attempt、snapshot_manifest、requirement、match_finding、finding_evidence、checkpoint、report | 所有判断绑定输入、证据和配置版本 |
| tracking | watch、change_cursor、reassessment、notification/read_state | 一个变化和规则版本不重复生成内部提醒 |
| 每个服务各自 | inbox、outbox、event_log、local_job/attempt、idempotency_record | 不能有跨服务共享的万能任务表 |

跨服务 ID 为契约引用，不能创建跨库外键或用 SQL JOIN 读取别的服务。表关系只在 owning service 内通过事务保证；远端关系用版本 API、投影、事件和对账保证可解释的一致性。

## 3. 原件、解析和目录的关系

一个 raw_asset 可产生不同解析版本；每个 document_version 对应固定原件、解析器和规则。一个规范观察可以指向多个局部标段，但尚不决定其全局身份。

catalog 依据明确原生 ID/跨引用将观察纳入公告族和程序。相似标题/采购方/金额仅为候选关联，不能自动证明相同。跨来源重复保留 provenance；合并撤销生成新关联版本，并使受影响的报告/提醒需重新核查。

processing 的 evidence_id + document_version + locator 被 research 引用；catalog 只保留必要的证据引用/索引投影。processing 删除或撤回材料后，派生消费者按事件与读取校验撤销可访问性。

## 4. 重要字段契约

金额保存原文、Decimal、币种、类型（估值/预算/授标等）、税/期间口径和作用域；缺失不填零，跨币种不默认换算。

日期保存原文、类型（提交/参与/提问/其他/未知）、来源时区、精度和可确定 UTC。只有日期不能补造时分秒，未知时区不能默认为 UTC。source published、first observed、fetched、parsed、analyzed 分开。

事实状态至少区分 parsed、missing、parse_error、inaccessible、conflicting、not_applicable。处理错误不是官方项目状态。

网页列中的 Open 必须带字段/章节语义：它可能是采购方式，不是开放状态。状态断言也必须有证据位置。

## 5. 匹配发现

每项包含目标程序/标段、requirement、要求类别、企业档案版本、状态、解释、支持和反证、未决项、输入版本与配置。

| 状态 | 含义 |
| --- | --- |
| met | 在本轮证据范围内有依据支持该条件满足 |
| unmet | 要求明确且已确认企业信息明确不符合 |
| unknown | 需求或企业资料不足，无法判断 |
| conflicting | 证据冲突未解决 |
| not_applicable | 有依据说明不适用 |

所有已识别要求为 met 也不代表已识别全部要求，更不等于法律资格认证。硬条件、偏好、内容相关性不同维度，不能一个总分相互抵消。

## 6. 分析快照

snapshot_manifest 至少引用：workspace/主体、profile_revision、preference_revision、catalog_revision/关联版本、documents/evidence、解析/索引版本、规则/提示词/模型配置、权限检查时间及可得版本。

跨库快照不是数据库全局 snapshot transaction。调用 owning service 读取不可变版本，记录 manifest complete/partial；取不到依赖则等待或 partial，不能悄悄换成另一个版本。分析当前性为单独校验，不修改历史报告。

权限版本不代替实时鉴权。私有读/下载/模型启动前确认当前允许范围；权限服务不可用时拒绝或延后。撤权与在途请求的竞态按可靠性文档处理，不承诺跨系统瞬时原子撤回。

## 7. 分开保存的状态

公告业务状态：可进一步核查的阶段、结束、取消、授标、未知；来源证据决定。

处理状态：queued/fetched/parsed/partial/quarantined/inaccessible；某文件失败不等于项目结束。

跟进状态：new/reviewing/watching/dismissed/archived，是用户工作状态。

任务状态：queued/running/waiting_input/retry_wait/succeeded/partial/failed/cancelled；succeeded 表示流程完成，不表示所有业务条件满足。

pipeline 状态：raw_saved、normalized、catalogued、indexed、analyzed、notified 由各拥有者分别报告。不能以 broker ACK 填整个业务 done。

## 8. 唯一性和并发

源制品身份经样本确认后建立约束；raw hash 与原生版本不混同。客户端命令按 owner_service+workspace+idempotency_key 去重，同键异请求返回冲突。

Inbox 按 consumer+event_id；本地 job 按业务输入指纹；报告发布按 run/阶段/输入版本；提醒按 watch+rule_version+change fingerprint+类型。DB 约束是最终内部发布防线，不依赖 Redis 锁永久存在。

档案更新 expected_version；任务租约 attempt_epoch；catalog_revision 不因旧事件晚到回退。跨源主版本不存在天然总序，按明确关系与冲突策略决定，不只比较发布时间。

## 9. 权限和删除

公共公告可共享；企业能力、偏好、笔记、匹配、私有文档、图状态和缓存按 workspace 隔离。跨服务调用验证服务身份及用户/后台委托范围，不能信任事件或 HTTP 中任意填的 tenant_id。

RLS 是所属服务数据库的附加防线，不代替调用鉴权；验证实际应用角色和 Worker。多来源报告按最严格可见性处理。[S7](12-source-register.md#s7)

删除不是静默删一行：在线索引/缓存/图状态/可访问报告需失效，事件带删除引用而非敏感正文；备份过期和法定/必要审计元数据边界单独说明。事件重放也必须尊重删除，不能把已撤回数据重建为可见。

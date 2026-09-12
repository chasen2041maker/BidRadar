# 14｜服务 API、事件与恢复契约 v0.2

**Proposed｜2026-09-11。以下为内部协议草案，不是已实现接口、官方数据字段或冻结 JSON Schema。**

## 1. 必须先明确的通信规则

查询走拥有者 API；耗时命令本地接受后异步；跨服务事实变化走事件。无共享业务数据库或跨服务事务，不依靠同一仓库 import 另一个服务 ORM 来替代通信。

API 成功接受、事件传输成功、下游持久接收、业务执行完成、用户读到结果是不同事实。每一步都有可查询的状态；不要给它们共用一个 success。

## 2. 拥有者 API 草案

| 拥有者 | 示例用例 | 核心约束 |
| --- | --- | --- |
| ingestion | 查询来源/同步状态；请求批准附件获取；授权 raw 引用读取 | 请求是登记 ID 而非任意 URL；权限/大小/频率由服务检查 |
| processing | 读取 document_version、规范制品、evidence；查看质量问题；请求重解析 | 不覆盖旧版本；原件访问受控；不存在证据返回明确错误 |
| catalog | GET opportunities、版本/标段 bundle、检索、比较修订 | 分页、as_of、indexed_revision；不假装全市场或强一致索引 |
| workspace | 当前授权；读取确认档案版本；expected_version 更新；管理偏好 | 服务端主体与工作空间解析；旧值冲突不静默覆盖 |
| research | POST analyses；查询/取消 run；读取报告；SSE 观察 | 幂等键、202/run_id；显式取消；报告私有访问当前授权 |
| tracking | PUT/DELETE watches；查询变化、复核、应用内提醒/已读 | watch/rule 版本，取消的旧命令不得继续授权 |

真实路径、错误 schema、分页、OpenAPI 和兼容窗口在实现前冻结。客户端重复命令同键同请求返回原结果，同键异请求返回冲突。用户/服务的认证不能由 URL 中的 workspace_id 代替。

## 3. 事件和命令目录

| 类型 | 生产者 | 订阅者/目标 | 持久语义 |
| --- | --- | --- | --- |
| RawAssetCaptured | ingestion | processing | 原件和元数据已提交，不等于已解析 |
| DocumentNormalized | processing | catalog | 指定解析版本已发布，可能 partial，缺口明确 |
| DocumentQuarantined | processing | ingestion 的源质量投影 | 当前解析进入隔离；不改变采购业务状态；processing 仍是权威 |
| CatalogRevisionPublished | catalog | tracking、research 各自消费组 | 新业务投影/关联版本已提交；需读 revision 查看具体影响 |
| ProfileRevisionConfirmed | workspace | research、tracking 各自消费组 | 用户确认了新企业/偏好版本，事件不携带私有全文 |
| AccessChanged | workspace | research、tracking；私有文档启用后再扩消费者 | 撤权/变更提示；当前鉴权 API 仍是访问防线 |
| AnalysisRequested（命令） | tracking | research | 一个后台复核请求，需验证委托/版本/预算后接受或拒绝 |
| AnalysisCompleted | research | tracking | 指定请求形成 succeeded/partial 的可查询报告 |
| AnalysisRejected | research | tracking | 未接受命令的可解释终态，如 watch 已取消或授权不足 |
| AnalysisFailed | research | tracking | 已接受任务失败/取消/超预算，原因分类明确；不让跟踪永远卡住 |

research 也可以接受用户直接创建的任务；带 watch 的事件才关联跟踪工作流。消费者只订阅自己需要的类型，不能向所有服务广播完整企业档案。

## 4. 事件信封草案

```json
{
  "event_id": "example-event-unique-id",
  "event_type": "CatalogRevisionPublished",
  "schema_version": 1,
  "producer": "catalog",
  "topic": "catalog.revisions.v1",
  "producer_seq": 42,
  "aggregate_type": "procurement_unit",
  "aggregate_id": "example-unit-id",
  "aggregate_version": 7,
  "occurred_at": "2026-09-11T00:00:00Z",
  "correlation_id": "example-flow-id",
  "causation_id": "example-parent-event-id",
  "classification": "public",
  "workspace_id": null,
  "payload_ref": {
    "owner": "catalog",
    "resource_id": "example-catalog-revision-id",
    "version": 7,
    "sha256": "example-content-hash"
  }
}
```

全部值是示例。producer_seq 在该 producer/topic 的持久有序日志中分配，不是 Redis stream ID，也不是跨服务全局序；aggregate_version 只在该业务聚合内解释。实现时需选择**提交有序且能解释跳号的日志位置**，不能把并发事务提前分配的 SQL sequence 最大值当所有更早事务已提交。

payload_ref 是允许类型的业务引用，不是可供模型任意 GET 的网址；不放短时下载凭据、密钥、联系人全文或企业资料。大型制品经拥有者鉴权后读取。private 事件含必要 workspace 标识和受限引用，不能只因有 workspace_id 就认为已授权。

内部 schema_version 与来源文档版本、解析器版本、业务 revision 分开。occurred_at 是事件产生时间，不冒充官方公告生效时间。

## 5. 发布和接收事务

生产者：本地业务写入 + Outbox 同一事务 → relay 取得已提交记录 → 将记录登记/发布到持久有序 event_log 与 Streams → 记录传输结果。event_id 不随重试更换；同一业务变化不会为每次传输生成新事件。

event_log 的分配、outbox 标记与 relay 并发必须保证可重放位置稳定。可用按 topic 串行追加和持久游标实现第一版，避免高并发下把未提交事件永久跨过；具体方案需契约实验确认。

消费者：检查 schema/可信生产者/允许类型 → 本地事务写 Inbox 与业务变更或 local_job → 提交 → XACK。处理崩溃、ACK 丢失时同事件再次到达，由 Inbox 判断已接受。长时模型/解析由本地 job 的租约执行，不靠长时间占用 Stream pending 项替代任务状态。

同一服务多个实例使用同消费组分担；不同业务消费者各自组，否则会出现 catalog 的消息被 tracking 抢走而 research 永远收不到的问题。消费组不是权限机制，Redis 凭据和 key/channel 访问范围另设。

## 6. 消息丢失后的恢复，不只重发未发送 Outbox

Redis 即使已确认 XADD，也不能让本项目删掉所有数据库恢复依据。设想 relay 标 sent 后 Redis 丢数据：只扫描 unsent 没用。

恢复协议提案：

1. 生产者保留按 topic 的持久 event_log，暴露经过身份验证的重放/高水位查询；不开放数据库直读。
2. 消费者保存连续接受水位、间隙和 Inbox。过滤不关心的事件也需明确推进该 topic 的接收位置；业务 job 完成水位与事件接受水位分开。
3. 收到序号缺口、消费组重建或周期对账发现落后时，向拥有者请求缺失区间。不能直接把 MAX(seen_seq) 作为无缺口证明。
4. 重发保留原 event_id 与业务版本，Inbox 消除重复；旧业务版本不能回退当前指针。
5. 事件日志保留期覆盖批准的最大离线窗口，清理要考虑消费者位置。超出窗口则通过版本化快照重建，再从确定切换位置接实时事件。
6. 发生间隙无法补齐时明确告警/降级，不用“同步完成”掩盖未知。

保留期和最大滞后需预算确认。XAUTOCLAIM 只接管 Redis 中仍存在的待处理消息，不能恢复已丢失的历史。[S20](12-source-register.md#s20)

## 7. 历史重建、删改与通知

重放有三种模式：缺口补投、历史投影重建、压力实验。历史重建在受控新投影/消费命名空间执行，默认不向使用者发送旧提醒；记录快照边界与实时切换水位，不能遗漏重建期间的新消息。

tombstone/权限撤回/许可撤回同样参与投影恢复，已删除私有数据不能在重放后再次可见。重放日志只保留必要引用/审计信息，不把敏感正文永久写入不可删除消息体。

正常重复投递也不得重复提醒。tracking 用 watch+rule_revision+change_fingerprint+kind 的数据库约束控制内部发布。

## 8. 失败命令与跨服务工作流

tracking 保存 reassessment pending + AnalysisRequested Outbox；research 接收后保存命令 idempotency 与分析任务，或记录拒绝并发 AnalysisRejected。耗时失败生成 AnalysisFailed，成功/部分成功生成 AnalysisCompleted。

tracking 根据命令 ID、watch/rule/input 版本更新自己的工作流。过期结果只能记历史，不能重新打开已经取消的 watch。等待超时先查询 research 的已接受命令/任务状态并对账，不立刻生成新的无关任务。

不使用全局数据库回滚。输入已经采集就保持事实；失败的复核可以重试/取消/标 partial；相应提醒明确“资料有更新，分析待完成”。错误类别和权限拒绝不含私有正文。

## 9. 服务身份、委托与当前授权

网关验证入口身份，各 owning service 仍验证调用者身份和目标 audience。后台调用使用受限服务身份及工作空间/watch 的委托引用，不能让客户端或 LLM 构造 workspace_id 获取权限。

private 资料在读取、导出、模型启动前检查 workspace 当前权限；AccessChanged 用于清理投影缓存，不能替代该检查。鉴权不可用时 fail closed/defer。已经发出的外部调用不能保证瞬时撤回，需记录竞态并减少上下文暴露。

服务网络可达不等于授权；K8s ServiceAccount 也不是自动的 HTTP 鉴权协议。具体认证产品和凭据轮换在部署决策中确认。

## 10. 兼容性与测试

契约文件版本化，新增可选字段优先；消费者对未知 enum 明确隔离/降级而非猜测。破坏性变化使用新 schema/topic 或有记录的双读迁移；发布测试至少验证当前与上一受支持版本。数据库迁移由各服务独立执行。

必测：同事件重复、不同消费者均收到、消费者提交后 ACK 丢失、Redis 清空已 sent 消息、序号缺口、并发提交乱序、过期任务、schema 不兼容、权限撤回、历史重建无重复提醒、丢失终态事件后的工作流对账。

目前只有协议，不含可运行 JSON Schema、broker 或实现。验收需要由负责人实际实现和运行。

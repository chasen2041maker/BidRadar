# 数据模型

每个服务有自己的 SQLite 数据库，只读写自己的表。服务之间需要数据时走 HTTP 接口，从不直接读别人的库。

## 关键概念

| 概念 | 含义 | ID 怎么来 |
| --- | --- | --- |
| 公告 notice | 一条采购公告 | `sha256([来源, 是否演示, 来源里的记录键])`，同一条公告 ID 不变 |
| 观察 observation | 某次抓取得到的公告内容。同一公告可以有多个，每个都不可修改 | 观察内容（不含 ID）的 SHA-256，内容一变 ID 就变 |
| 当前版本 | 抓取时间最新的观察。晚到的旧观察不会让当前版本倒退 | — |
| 快照 snapshot | 目录接收序号的上限。翻页和研究取材都固定在一个快照上 | 整数 |
| 证据片段 evidence | 研究时可以引用的一段原文 | 片段内容和位置的 SHA-256 |
| 档案版本 profile revision | 公司正式档案的第 N 版，确认后不再修改 | 每家公司从 1 递增 |
| 研究任务 run | 一次研究，冻结了输入（manifest） | 随机 32 位十六进制 |
| 成员版本 membership version | 成员关系每变一次加一 | 递增整数 |

几条通用规则：

- **只插不改**：观察、档案版本、报告、各种历史记录都只插入，不修改也不删除（SQLite 触发器保证）。
- **幂等键**：所有修改类请求都带一个 key，`commands` 表记下"键 → 请求指纹 → 结果"。同键同内容返回原结果，同键不同内容报冲突。
- **乐观锁**：会被并发修改的记录都有 `version` 列，修改时要带上期望的版本号。
- **时间**：库里存整数秒（UTC）；对外统一是 ISO 8601 字符串，例如 `2026-10-01T03:56:00Z`。
- **金额**：库和接口里都是十进制字符串，单位元，不用浮点数。

下面按服务列出主要的表。`payload` 一类的列存的是 JSON，结构见对应代码。

## ingestion（采集）

数据目录：`.bidradar-data/`（`services/ingestion/archive.py`）

| 表 | 存什么 |
| --- | --- |
| runs | 一次采集运行：请求参数、幂等键、状态、租约 |
| tasks | 运行的抓取队列：列表页、公告页、附件 |
| captures | 每次抓取的结果：状态码、响应头、原始文件 SHA-256、当时的解析结果 |
| versions | 同一 URL 的内容版本 |
| replays | 用新解析器重新解析的结果，不覆盖当初的结论 |

原始文件放在 `blobs/` 目录下，按 SHA-256 命名，写入后不再修改。

## catalog（商机目录）

`services/catalog/store.py`

| 表 | 存什么 |
| --- | --- |
| observations | 每个观察一行：接收序号 seq、公告 ID、抓取时间、类型、标题、完整内容 payload |
| imports | 每次导入的证据包（含失败记录，用来区分"抓取失败"和"没有项目"） |
| settings | 翻页游标的签名密钥 |

观察的 payload 主要字段：

- `facts`：项目编号、采购人、发布时间、预算、截止时间、包号等。每个字段是 `{status, value, evidence}`，status 为 known / missing / unparsed / conflicting。
- `evidence_fields`（v3）：金额（区分预算、最高限价、单价、分包预算、文件售价、保证金）、获取文件的时间窗口、获取条件（登录、CA、付费等）、材料是否已取得、技术/资格/交付要求原文。
- `references`：原文里指向其他公告的链接，用来建立更正、结果和原公告的关联。

## workspace（工作台）

`services/workspace/store.py`

| 表 | 存什么 |
| --- | --- |
| users | 用户名、显示名、密码哈希（PBKDF2，60 万次迭代） |
| sessions | 会话令牌和 CSRF 令牌的哈希、过期时间（8 小时） |
| login_limits | 登录失败计数（15 分钟内最多 5 次） |
| workspaces | 公司，以及当前正式档案的版本号 |
| memberships | 谁在哪家公司、什么角色、是否有效、版本号 |
| membership_events | 成员变更的审计记录 |
| profile_proposals | 档案修改提议 |
| profile_revisions | 正式档案版本 |
| selections / selection_history | 用户选出的待研究公告，以及选择的状态变化 |
| commands | 幂等键记录 |
| owner_events | 发给 tracking 的事件：ProfileConfirmed、AccessChanged |

企业档案有九个字段：公司名称、城市、想接的项目类型、能力、交付限制、案例、资质、人力排期、商务限制。空着表示"还没填"，不表示"没有"。

## research（研究）

`services/research/store.py` 和 `budget.py`

| 表 | 存什么 |
| --- | --- |
| runs | 研究任务：请求、冻结的 manifest、状态、租约和 epoch、进度 checkpoint、重试次数 |
| reports | 报告、工具调用轨迹、质量信息 |
| events | 任务状态变化历史 |
| cancel_commands | 取消命令的幂等记录 |

任务状态：`queued` → `running` → `succeeded` / `partial` / `failed` / `cancelled` / `waiting_input`，依赖服务暂时不可用时进入 `retry_wait`。注意 succeeded 指"研究流程完成"，不指"企业满足条件"。

费用账本是另一个数据库，放在 `~/.bidradar/budgets/`：

| 表 | 存什么 |
| --- | --- |
| authorization | 本轮授权编号和三层上限，以及是否暂停 |
| attempts | 每次模型调用：预留额、实际费用、状态（pending / settled / unknown / not_dispatched）、响应、用量 |

## tracking（跟踪）

`services/tracking/store.py`

| 表 | 存什么 |
| --- | --- |
| decisions / decision_history | 对每条公告的人工决定（待评估、跟进、放弃）及历史 |
| watches / watch_history | 关注设置：是否开启、是否自动复核、到期时间、关注哪些维度、比较基线 |
| cursors / inbox | 已经处理到哪个事件、处理过哪些事件 |
| changes | 检测到的变化：哪个关注、哪些维度变了、前后对比 |
| reassessments / reassessment_history | 自动复核命令和状态历史 |
| notifications / notification_reads | 站内提醒，以及每个人的已读记录 |
| commands | 幂等键记录 |

变化的维度：截止时间、金额、资格、交付、材料、公告状态、档案、其他。同一个变化、同一种提醒只会有一条。

## 跨服务引用

服务之间只通过 ID 引用，不建外键：

- research 的任务里记着 `workspace_id`、`actor_id`、`notice_id`、观察 ID 列表、档案版本号、目录快照号。
- tracking 的关注里记着 `notice_id`、目录序号、档案版本号；复核命令里记着 research 的任务 ID。
- 要用对方的数据时，带着这些 ID 调用对方的接口，由对方检查权限。

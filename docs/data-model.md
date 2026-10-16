# 数据模型

一个 PostgreSQL 16 数据库，每个模块一个 schema，只读写自己的 schema。模块之间不建外键：跨模块的 ID（比如 `workspace_id`）只存值，由对方的 service 保证有效。这样以后拆服务时，可以把一个 schema 整个搬走。

表定义在各模块的 `tables.py`，迁移在 `migrations/versions/`（一个阶段一个）。

## 关键概念

| 概念 | 含义 | ID 怎么来 |
| --- | --- | --- |
| 公告 notice | 一条采购公告 | `sha256([来源, 是否演示, 来源里的记录键])`，同一条公告 ID 不变 |
| 观察 observation（公告版本） | 某次抓取得到的公告内容。同一公告可以有多个，每个都不可修改 | 观察内容的 SHA-256，内容一变 ID 就变 |
| 快照 snapshot | 目录的版本号上限（`notice_versions.seq`）。研究取材、变化检测都固定在一个快照上 | 递增整数 |
| 档案版本 profile version | 公司正式档案的第 N 版，确认后不再修改 | 每家公司从 1 递增 |
| 片段 chunk | 招标文件切出来的一段原文，带页码和章节路径 | 文件 SHA-256 + 序号 + 内容的 SHA-256，重新解析也不变 |
| 研究任务 run | 一次研究、追问或自动复核，创建时固定输入 | 随机 32 位十六进制 |
| 调用键 call_key | 一次模型调用在账本里的主键 | `任务:步骤:要求:第几次`，续跑时靠它找回已付费的响应 |

几条通用规则：

- **只插不改**：公告版本、档案版本、报告、各种 history 表都只插入。
- **幂等键**：修改类请求都带一个 `key`，`infra.idempotency_keys` 记下"键 → 请求指纹 → 结果"。同键同内容返回原结果，同键不同内容报 `idempotency_conflict`。
- **乐观锁**：会被并发修改的记录都有 `version` 列，修改时带上期望的版本号，对不上返回 409。
- **时间**：`timestamptz`，接口里统一是 ISO 8601，例如 `2026-10-01T03:56:00Z`。
- **金额**：公告里的金额是十进制字符串，单位元。模型费用是整数，单位百万分之一元（micro-CNY），不用浮点数。

## infra（公共设施）

| 表 | 存什么 |
| --- | --- |
| idempotency_keys | 幂等键：作用域（通常是公司）、操作、键、请求指纹、结果引用 |
| jobs | 后台任务：类型、参数、去重键、状态、尝试次数、租约（`lease_owner`、`lease_until`） |
| outbox | 待投递的事件：唯一的 `event_key`、主题、内容、`published_at`（空表示还没投递）、失败次数 |

文件不进数据库：上传的招标文件和采集的原件按 SHA-256 存在 `<data_dir>/blobs/xx/<sha256>.bin`（`infra/blobs.py`）。

## identity（身份）

| 表 | 存什么 |
| --- | --- |
| users | 用户名、显示名、PBKDF2 密码哈希 |
| sessions | 会话令牌和 CSRF 令牌的**哈希**（原文只在 Cookie 里）、过期时间（8 小时） |
| login_limits | 按用户名计的失败次数和时间窗，挡暴力猜密码 |

## workspace（公司与档案）

| 表 | 存什么 |
| --- | --- |
| workspaces | 公司、当前档案版本号 |
| memberships | 谁在哪家公司、角色（admin / member / viewer）、是否有效、版本号 |
| membership_events | 每次成员变化的前后值，只插入 |
| profile_proposals | 档案修改建议：基于第几版、九个字段的内容、谁提的 |
| profile_versions | 管理员确认后的正式档案版本，只插入 |

档案是九个文本字段：公司名称、城市、想接的项目、能力、交付限制、案例、资质声明、人员排期、商务限制。没填是 `null`，表示"还不知道"，不表示"没有"。
字段名和研究的维度表（`agent/dimensions.py`）一一对应，研究时两边对不上会直接报错。

## catalog（商机目录）

| 表 | 存什么 |
| --- | --- |
| notices | 每条公告一行：来源、是否演示、来源里的记录键、第一次看到的时间 |
| notice_versions | 每个观察一行：递增的 `seq`（就是快照号）、公告 ID、抓取时间、类型、标题、搜索文本、完整内容 `payload` |
| imports | 每次导入：来源、运行状态、成功和失败的条数（用来区分"抓取失败"和"没有项目"） |

导入用一把串行锁，保证 `seq` 严格按提交顺序递增：tracking 靠"基线快照号比新版本旧"来判断哪些关注要重新比较。

`payload` 主要字段：

- `facts`：项目编号、采购人、发布时间、预算、截止时间、包号等。每个字段是 `{status, value, evidence}`，status 为 known / missing / unparsed / conflicting。
- `evidence_fields`：金额（区分预算、最高限价、单价、分包预算、文件售价、保证金）、获取文件的时间窗口、获取条件（登录、CA、付费等）、技术 / 资格 / 交付要求原文。
- `references`：原文里指向其他公告的链接，用来建立更正、结果和原公告的关联。

研究用的"公告包"（`catalog.service.research_bundle`）= 这条公告在某个快照下的当前版本 + 有依据关联的更正和结果公告。

## ingestion（采集）

| 表 | 存什么 |
| --- | --- |
| fetch_runs | 一次采集运行：请求参数、幂等键、状态、租约 |
| fetch_tasks | 运行的抓取队列：列表页、公告页、附件 |
| captures | 每次抓取的结果：状态码、响应头、原件 SHA-256、当时的解析结果 |
| url_versions | 同一 URL 的内容版本 |
| replays | 用新解析器重新解析的结果，不覆盖当初的结论 |

## documents（招标文件）

| 表 | 存什么 |
| --- | --- |
| documents | 上传的文件：所属公司、关联公告、文件名、类型、大小、SHA-256、解析状态（queued / parsing / parsed / failed）、页数、片段数 |
| chunks | 片段：序号、页码、章节路径（`["第三章 采购需求", "二、交付要求"]`）、类型（text / heading / table_row）、原文 |

同一家公司重复上传同样内容的文件，只存一份（`workspace_id + sha256` 唯一）。上传的文件只有本公司能看到。

## research（研究）

| 表 | 存什么 |
| --- | --- |
| runs | 研究任务：类型（analysis / question / reassessment）、模式（agent / baseline）、发起人和当时的成员版本、**固定的输入**（档案版本、目录快照、公告版本 ID、文件 ID）、配置版本、预估费用、状态和原因 |
| run_steps | 每一步的结果：步骤名（`extract:<批次>`、`match:<要求>:<第几次>`……）、输入指纹、输出。续跑时直接复用 |
| reports | 报告或追问回答的 JSON，只插入。结构就是 `agent/schemas.py` 的 `Report` / `FollowupAnswer` |
| llm_calls | 模型调用账本：调用键、预留额、实际费用、状态（pending / settled / unknown / released）、响应原文、价格版本 |
| requirement_sets | 抽取结果的缓存：同一批片段、同一版抽取规则只抽一次。公开公告的结果各公司共用；上传文件的结果只给本公司用 |

任务状态：queued → running → succeeded / partial / failed / cancelled / waiting_input。
`waiting_input` 表示额度用完，或者有一次调用结果不确定（可能已经计费），停下来等人处理，不自动重试。

已花费用 = 已结算的实际费用 + 还在进行中或结果不确定的预留额。三层额度（全部公司、每家公司、每个任务）在预留时检查，一把全局锁保证两个 Worker 不会同时花掉最后一笔额度。

## tracking（跟踪）

| 表 | 存什么 |
| --- | --- |
| decisions、decision_history | 人工决定（需要核查 / 跟进 / 放弃）和原因，当前一行 + 全部历史 |
| watches、watch_history | 关注：是否开启、是否授权自动复核、委托到期时间和范围、发起人和当时的成员版本、**基线**（上次比较时的公告包和快照号） |
| changes | 检测到的变化：差异内容、规则版本、指纹（同一变化只记一次）、来源（公告或档案） |
| notifications、notification_reads | 站内提醒（按 `dedupe_key` 去重）和每个人自己的已读状态 |
| reassessments | 自动复核：对应哪个关注和变化、状态（pending / dispatched / finished / cancelled / rejected）、建出的研究任务 |

## 事件

模块之间的异步通知都走 `infra.outbox`，和业务数据在同一个事务里写入：

| 主题 | 谁发 | 谁处理 | 用来做什么 |
| --- | --- | --- | --- |
| `catalog.notice_version_added` | catalog 导入 | tracking | 比较关注的公告有没有变化 |
| `workspace.profile_confirmed` | workspace 确认档案 | tracking | 档案变了，提醒并按委托复核 |
| `workspace.access_changed` | workspace 改成员 | tracking | 发起人权限变了，委托作废 |
| `research.run_finished` | research 任务结束 | tracking | 记下自动复核的结果，发提醒 |

投递事件的消费者是 [练习 3](exercises.md#练习-3outbox-消费者)。

# BidRadar v2 改造蓝图

> 一句话：把 BidRadar 改成一个人能看懂、能讲清、能长期维护的后端项目。做法是模块化单体，技术栈用 FastAPI + PostgreSQL，研究部分改成"抽取 → 匹配 → 核验"的流水线。
>
> 先把整体规划定下来，再按阶段落地。本文件是规划；落地后的实际做法以 [架构说明](architecture.md)、[开发指南](dev-guide.md) 和代码为准。

**进度（2026-10-15）：阶段 0–6 已完成，旧代码已删除。** 留给负责人的三个练习见 [练习](exercises.md)，进度见 [路线图](roadmap.md)。

**之前的进度（2026-10-13）：**

- 第 9 节的文档改造已完成：旧文档归档，新文档见 [docs/](./)。
- 现有代码的注释已全部改写成人话，`run_agent` 已拆成按阶段的方法，代码已统一格式化（行为不变）。
- 已确定：模块化单体先行、FastAPI + PostgreSQL、研究用流水线而不是 LangGraph（[决策 0008](decisions/0008-v2-modular-monolith.md)）。
- 第 13 节除第 4 项（前端）外都已拍板（2026-10-13）。
- 阶段 0 地基已完成：`bidradar/` 骨架、`GET /health`、PostgreSQL、Alembic、import-linter 契约、CI。进度见 [路线图](roadmap.md)。

**怎么读：**

- 第 1–2 节讲为什么改、改成什么样，大约 10 分钟。
- 第 3–6 节是核心设计：分层、模块、数据、Agent。
- 第 7–9 节讲评测、工程约定、文档。
- 第 10–13 节是路线、分工和待你拍板的事。

---

## 1. 为什么要改

现在的项目有不少值得保留的设计（第 12 节会列出来），但有四个结构性问题。

### 1.1 分层不清

- **一个类管了所有事。** `services/workspace/store.py` 有 711 行，`WorkspaceStore` 一个类同时负责登录会话、密码校验、成员权限、档案确认、选择记录和全部 SQL。想改一条业务规则，得在 SQL 中间去找。
- **数据结构看不出来。** 业务数据大多以 JSON 字符串存在 `payload` 一类的列里，比如 `decisions.payload`、`runs.manifest`。代码里到处传 `dict`，一个对象有哪些字段，只能去读校验函数。
- **自己写了框架的活。** 每个服务都用手写的 HTTP 层，`services/common/local_http.py` 有 269 行，专门处理请求体读取、超时和连接关闭。这些本该交给框架。
- **风格不统一。** 只有 `workspace/domain/access_policy.py` 是独立的"领域层"，其他模块都没有。

### 1.2 Agent 架构把太多事塞进一次调用

- **状态机缠在一起。** `services/research/agent.py` 里的 `run_agent` 是一个约 235 行的函数，用闭包加一个大字典当状态机。调模型、分发工具、本地校验、语义核验、要求修订、保存断点，全部缠在一起。
- **一次调用要干六件事。** 同一次模型调用要读材料、找要求、和企业比较、写理由、写摘要、回答追问。任务太多，模型就容易出错。
- **提示词靠打补丁变长。** 每出一个错就加一条规则，SYSTEM 提示词里已有几十条，很多是为某个样本的某个错误专门加的，比如"60 日开发期限不等于驻场""服务不能改写成开发"。在两条公告上越修越准，换一批公告又会出新错。
- **一个文件干五件事。** `evidence.py` 有 612 行，同时负责建索引、检索、解析金额、校验报告、生成基线报告。
- **出错只能整份重来。** 哪怕只有一条结论有问题，也只能整份报告重写（最多两次），不能只修那一条。

### 1.3 架构规模和项目阶段不匹配

四个服务各自有 SQLite、回环 HTTP 和服务令牌。对一个人维护、还没有用户的项目来说，大量精力花在服务之间的通信和一致性上，而不是业务本身。

### 1.4 文档读不懂

- 34 篇专题加上学习笔记，大约 44 万字节。
- 句子高度压缩，充满"X 不等于 Y"和"甲A13/乙B9"这样的代号。
- 状态文档已经过期：写着 PR 未合并，实际早已合并。

---

## 2. 改成什么样

### 2.1 关键选择

| 项 | 选择 | 理由 |
| --- | --- | --- |
| 服务形态 | 模块化单体，分 API 和 Worker 两个进程 | 先把边界想清楚、代码看懂。边界清楚了，以后拆服务只是体力活 |
| Web 框架 | FastAPI + Pydantic v2 | 路由、参数校验、接口文档都现成，不用再手写 HTTP |
| 数据库 | PostgreSQL 16+、SQLAlchemy 2.0、Alembic | 事务、行锁、JSONB、行级安全（RLS）、`SKIP LOCKED` 队列都能用上 |
| 后台任务 | PostgreSQL 任务表，用 `SKIP LOCKED` 加租约 | 少一个组件。建任务和写业务数据在同一个事务里提交 |
| 模块通信 | 同步调用对方 `service` 的公开函数；异步走 outbox 事件表 | 拆服务时，把函数调用换成 HTTP、把 outbox 接到消息队列即可 |
| Agent | 显式流水线：解析 → 抽取 → 匹配 → 核验 → 汇总；只有追问用工具循环 | 每一步小、可单测、可单独评测，出错只重做那一步 |
| 大模型调用 | 自己封装 `LLMClient` 接口，DeepSeek 作为一个实现；保留预算账本 | 不绑框架，换模型只换一个类 |
| 前端 | 暂时保留现有原生 JS，改成调用新接口 | 这次重点在后端 |
| 工具链 | Python 3.12、uv、ruff、pytest、import-linter、GitHub Actions | 主流，面试好讲 |

### 2.2 整体结构

```text
                 浏览器（web/）
                      │  Cookie 会话
      ┌───────────────▼────────────────────────┐
      │  API 进程（FastAPI）                     │
      │  identity · workspace · catalog         │
      │  documents · research · tracking        │
      └───────┬──────────────────┬─────────────┘
              │ 读写业务表        │ 同一事务写入 jobs / outbox
      ┌───────▼──────────────────▼─────────────┐
      │  PostgreSQL（每个模块一个 schema）        │
      └───────▲──────────────────▲─────────────┘
              │ 领取任务          │ 消费事件
      ┌───────┴──────────────────┴─────────────┐
      │  Worker 进程                             │
      │  采集 · 文档解析 · 研究流水线 · 变化检测    │
      └───────┬────────────────────────────────┘
              │ HTTPS
        DeepSeek API / 公开采购网站
```

### 2.3 关于"先模块化，以后再拆微服务"

是的，就是这个顺序。模块化单体要守住三条规矩，以后拆起来才顺：

1. **数据各管各的。** 每个模块只读写自己的 schema。拆的时候把整个 schema 搬到新数据库就行。
2. **只走公开入口。** 模块之间只调用对方 `service.py` 里的公开函数。拆的时候写一个同名接口的 HTTP 客户端换上去。
3. **异步用事件。** 异步的事统一写 outbox 事件表。拆的时候让 outbox 投递到 Redis Streams 或 RabbitMQ。

最先拆的应该是 Worker 那一侧，也就是采集和研究流水线。它们慢、依赖外部网站和大模型，需要单独限流、扩容。

---

## 3. 分层规则

### 3.1 目录结构

```text
bidradar/
  app/
    main.py          # 组装 FastAPI 应用：挂路由、中间件、异常处理
    worker.py        # Worker 进程入口：领取任务、分发给各模块
    deps.py          # 依赖注入：数据库会话、当前用户、各模块 service
    settings.py      # 配置（pydantic-settings）
  modules/
    identity/        # 用户、登录、会话
    workspace/       # 公司、成员、角色、企业档案
    catalog/         # 公开商机、公告版本、更正与结果关系
    ingestion/       # 采集器（CCGP、海南），结果交给 catalog
    documents/       # 招标文件上传、解析、切片、检索（新增）
    research/        # 研究任务、报告、追问、费用账本
    tracking/        # 跟进决定、关注、变化、提醒
  agent/             # 研究流水线：纯逻辑，不依赖 Web 和数据库
    pipeline.py      # 编排五个步骤
    steps/           # parse / extract / match / verify / compose
    followup.py      # 追问 Agent（工具循环）
    checks.py        # 通用检查：原文摘录、数字、引用
    llm.py           # LLMClient 接口 + DeepSeek 实现
    schemas.py       # Requirement、Finding、Report 等数据结构
    prompts/         # 提示词文件，一个步骤一个，带版本号
  infra/             # 数据库会话、任务队列、outbox、日志、时钟
evals/               # 评测用例和评测脚本
tests/
bidradar/app/static/ # 前端（沿用现有页面，由 API 同源提供）
migrations/          # Alembic 迁移
```

迁移期间，新代码的测试先放在 `tests_v2/`，因为 `tests/` 里还是旧代码的 unittest 测试。阶段 6 删掉旧代码后改名为 `tests/`（2026-10-15 已完成）。

### 3.2 每个模块内部长一个样

```text
modules/research/
  api.py        # 路由：只管 HTTP 进出，然后调用 service
  service.py    # 用例：一个函数对应一个业务动作，负责事务边界
  models.py     # 领域对象和业务规则，纯 Python，可以直接单测
  repo.py       # 数据库读写，只给本模块用
  tables.py     # SQLAlchemy 表定义（本模块的 schema）
  schemas.py    # 接口的请求、响应结构（Pydantic）
  events.py     # 本模块发出和订阅的事件
```

### 3.3 依赖方向

- `api → service → models / repo`，不能反过来。
- `models.py` 不 import 任何框架，包括 FastAPI 和 SQLAlchemy。
- 跨模块只能 import 对方的 `service`（或订阅事件），不能碰对方的 `repo` 和 `tables`。
- `agent/` 不 import `modules/`。它需要的东西（读文档、调模型、存步骤结果）都通过参数传进来。

这些规则写成 import-linter 的契约，放进 CI 检查。规则靠机器守，不靠自觉。

### 3.4 一个请求怎么走

以"对某个公告发起研究"为例：

```text
POST /api/workspaces/{wid}/research-runs
  → research/api.py          解析请求体，拿到当前用户
  → research/service.py      create_run()
      → workspace.service.authorize(user, wid, "analyze")    权限
      → workspace.service.get_profile(wid, version)          固定档案版本
      → repo.insert_run(...) + jobs.enqueue("research", run_id)
        （同一个事务提交，避免"任务建了但没排队"）
  ← 202 Accepted + run_id

Worker 领取 research 任务
  → research/service.py      execute_run(run_id)
      → agent.pipeline.run(...)   五个步骤，每步结果落库
      → repo.save_report(...) + outbox.publish("ResearchCompleted")
```

---

## 4. 模块划分

| 模块 | 管什么 | 主要表 | 对外提供 | 来自旧代码 |
| --- | --- | --- | --- | --- |
| identity | 用户、登录、会话、登录限流 | users, sessions, login_limits | `current_user()` | `workspace/store.py` 的登录部分 |
| workspace | 公司、成员、三种角色、企业档案版本 | workspaces, memberships, membership_events, profile_proposals, profile_versions | `authorize()`、`get_profile()` | `workspace/store.py` 和 `access_policy.py` |
| catalog | 公开公告、版本、更正和结果关系、搜索 | notices, notice_versions, notice_links | `search()`、`get_notice()` | `catalog/*` |
| ingestion | 采集来源、采集记录、原始页面归档 | sources, fetch_runs, captures | 采集结果交给 catalog 导入 | `ingestion/*`、`processing/normalize.py` |
| documents | 招标文件上传、解析、切片、检索（新增） | documents, chunks | `get_chunks()`、`search_chunks()` | `processing/evidence_fields.py` 的一部分，另加 PDF/Word 解析 |
| research | 研究任务、步骤结果、报告、追问、费用账本 | runs, run_steps, requirement_sets, requirements, findings, reports, llm_calls | `create_run()`、`get_report()`、`ask()` | `research/*` |
| tracking | 跟进/放弃决定、关注、变化、提醒 | decisions, watches, changes, notifications | `set_decision()`、`watch()` | `tracking/*` |

**一处简化：** 去掉现在的"选择批次（selection）"状态机。用户在公告详情页点"分析"时，页面先列出范围和预计费用，确认后直接创建研究任务。"列表不会悄悄调模型"的原则照样保留，只是少了一套状态和两张表（selections、selection_history）。

**数据入口的变化：** 招标文件的资格、评分、技术参数大多只在正式文件里，公告里没有。所以新增 documents 模块，让用户上传自己合法获取的招标文件（PDF/Word），作为研究的主要材料。公开采集继续负责"发现有哪些项目"。

---

## 5. 数据设计

### 5.1 原则

- **按模块分 schema。** 每个模块一个 PostgreSQL schema：identity、workspace、catalog、ingestion、documents、research、tracking、infra。
- **模块之间不建外键。** 跨模块的 ID 只存值，由 service 保证有效。这样每个 schema 都能整体搬到别的数据库。
- **私有数据必须带公司 ID。** 私有表都有 `workspace_id`，仓储函数必须传它。阶段 1 再加 RLS 作为第二道防线：每个事务开始时执行 `SET LOCAL app.workspace_id = ...`。
- **会查询的字段用正经列。** 需要查询或过滤的字段建成正式列。结构多变的内容（报告正文、原始解析结果）用 JSONB，结构由 Pydantic 模型定义。
- **不可变数据只插不改。** 公告版本、档案版本、报告只插入、不更新。新材料产生新版本，旧报告保持原样。
- **并发和重复提交有保护。** 并发修改用 `version` 列做乐观锁；重复提交用幂等键。

### 5.2 核心表（示意）

```text
documents.documents   id, workspace_id(NULL=公开), notice_id(NULL), filename, sha256,
                      mime, status, parser_version, created_at
documents.chunks      id, document_id, seq, page, section_path, kind, text, text_hash

research.runs              id, workspace_id, actor_id, notice_id, document_ids, profile_version,
                           kind(analysis/question/reassessment), status, config_version,
                           idempotency_key, created_at, started_at, finished_at
research.run_steps         run_id, step, input_hash, status, output(JSONB), finished_at
research.requirement_sets  id, document_id, extractor_version, workspace_id(NULL=公开)
research.requirements      id, set_id, category, quote, chunk_ids, money_role, conditions(JSONB)
research.findings          id, run_id, requirement_id, status, reason, profile_fields,
                           verified, verify_notes
research.reports           run_id, summary, payload(JSONB), created_at
research.llm_calls         id, run_id, workspace_id, purpose, reserved, charged, state,
                           usage(JSONB), model, provider_request_id, created_at, settled_at

infra.jobs     id, kind, payload, status, attempts, lease_owner, lease_until, run_after
infra.outbox   id, topic, payload, created_at, published_at
```

**为什么要单独的 `requirement_sets`：** 同一份公开招标文件、同一版抽取规则，只需要抽取一次要求，所有公司共用。既省钱，也方便单独评测抽取质量。用户私有上传的文件则带上 `workspace_id`，只给本公司用。

中文检索先用简单关键词匹配（ILIKE）。数据量上来后，再评估 pg_trgm、zhparser 分词或向量检索。

---

## 6. Agent：研究流水线

### 6.1 思路

原来是一次大调用干六件事，现在拆成五个小步骤，每步只干一件事。能用程序判断的就不交给模型；必须用模型的，每次只给它一个小问题。

### 6.2 五个步骤

| 步骤 | 输入 | 输出 | 用模型吗 | 怎么检查 |
| --- | --- | --- | --- | --- |
| ① 解析 | 招标文件或公告正文 | 带页码、章节的切片（chunks） | 不用 | 单元测试 |
| ② 抽取要求 | 切片，按章节分批 | 要求列表：类别、原文摘录、引用切片、金额角色、适用条件 | 用 | 摘录必须能在所引切片里原样找到。同一文件同一版本只抽一次，各公司共用 |
| ③ 匹配 | 一条要求，加上同一维度的档案字段 | 状态、理由、用到的档案字段 | 部分用：确定性规则先判，剩下的才问模型 | 规则表加结构化输出校验 |
| ④ 核验 | 一条结论，加上它自己的引用和档案字段 | 支持或不支持，以及问题说明 | 程序检查加一次小调用 | 不通过只重做这一条，最多两次；仍不通过就降为"未知"，并标注"未通过核验" |
| ⑤ 汇总 | 通过核验的结论 | 报告：摘要、要求对照表、待核查清单 | 摘要可以用模型，其余由程序拼 | 摘要里的数字必须来自引用原文 |

### 6.3 走一遍例子

招标文件写着"合同签订后 60 日内完成系统开发"；企业档案的交付限制写着"不接受超过 30 天的连续驻场"。

1. 抽取得到一条要求：`类别=交付，摘录="合同签订后60日内完成系统开发"，引用=第 3 页第 2 段`。程序确认摘录确实出现在第 3 页第 2 段。
2. 匹配时，交付期限只会和"排期、团队规模"这类字段比较；驻场限制是另一个维度，不会拿来比。模型给出"未知：档案里没有排期和人力计划"。
3. 核验时只看这一条的引用和这几个档案字段，确认理由有原文支撑。
4. 汇总时，这条进入"待核查清单"：请补充 60 天内的开发排期。

现在提示词里"60 日开发期限不等于驻场"这条补丁规则，在新设计里由第 2 步的"按维度挑选档案字段"自然解决，不需要专门写进提示词。

### 6.4 通用机制替代补丁规则

| 现在的补丁规则 | 新流水线里由谁负责 |
| --- | --- |
| 资格条款必须逐字复制 | ② 所有类别的摘录统一做"原文子串"校验 |
| 资格未核验时必须用固定句子 | ③ 规则：资格类且档案没有证明，直接判"未知"，说明由代码生成，不问模型 |
| 不能借别的结论的证据 | ④ 每条结论单独核验，只给它自己的引用 |
| 预算和最高限价同额也不能互证 | ② 抽取时就标好金额角色；④ 数字检查按角色比对 |
| 交付期限和驻场是两回事 | ③ 每类要求只配对同一维度的档案字段 |
| 待核查清单只能复制未知理由 | ⑤ 由程序从"未知"结论生成，不让模型写 |

### 6.5 追问 Agent

追问是真正需要模型自己决定"再查什么"的地方，所以只在这里用工具循环。

- **工具：** `search_chunks`、`read_chunk`、`get_finding`、`get_profile`。
- **回答要求：** 必须带切片引用，然后复用第 4 步的核验。
- **上限：** 最多 6 次模型调用、12 次工具调用。
- **安全：** 工具参数由程序校验。模型拿不到 URL、SQL 或文件路径，只能读当前研究范围内的切片。

### 6.6 编排、恢复和费用

- **编排是普通 Python 类，不用框架。** 每一步的结果写进 `research.run_steps`。Worker 崩溃重启后，已完成的步骤直接跳过。
- **调模型前先预留费用。** 所有模型调用都经过 `BudgetedLLM`：先预留费用，再调用，再按实际用量结算。结果不确定（比如网络中断）时保留预留额，任务停下等人处理，不自动重试，避免重复扣费。这部分沿用现有设计。
- **每次调模型前都检查一遍。** 步骤之间、每次调模型之前，都检查取消状态、当前权限和租约。

代码大致是这个样子：

```python
class ResearchPipeline:
    def __init__(self, llm: LLMClient, docs: DocumentReader, steps: StepStore, guard: Guard):
        ...

    def run(self, run: ResearchRun) -> Report:
        chunks = self.step("parse", lambda: self.docs.chunks(run.document_ids))
        reqs = self.step("extract", lambda: extract_requirements(chunks, self.llm))
        findings = [self.step(f"match:{r.id}", partial(match, r, run.profile, self.llm)) for r in reqs]
        checked = [self.step(f"verify:{f.id}", partial(verify, f, chunks, run.profile, self.llm)) for f in findings]
        return compose_report(checked)

    def step(self, name, fn):
        if (done := self.steps.get(name)) is not None:
            return done            # 恢复时直接复用已完成步骤
        self.guard.check(name)     # 取消、权限、租约
        result = fn()
        self.steps.save(name, result)
        return result
```

### 6.7 提示词管理

- **文件形式：** 提示词放在 `agent/prompts/`，文件名带版本，如 `extract.v1.md`。
- **版本冻结：** 任务创建时记录当时用的版本，旧报告永远知道自己是按哪版规则生成的。
- **保持短：** 每个提示词控制在 30 行左右。超过了，说明这一步干的事太多，应该再拆。
- **改了必评测：** 改提示词必须跑评测（见第 7 节）。

---

## 7. 评测

没有评测，就只能靠感觉修提示词，越修越像打补丁。

- **用例格式：** `evals/cases/*.yaml`，每个用例包含一份文档、一份企业档案和标准答案（应抽出的要求，以及每条应得的状态）。
- **指标：**
  - 抽取：召回率、精确率。
  - 匹配：状态准确率。
  - 最严重的错误：把"未知"判成"满足"或"不满足"，单独统计。
  - 引用：有效率。
  - 成本：每份报告的费用和耗时。
- **起步：**
  - 先把现有 2 条公告 × 2 家虚构公司改成 4 个用例。
  - 目标扩到 30 个，覆盖不同来源和类型：软件开发、运维服务、AI 算力、设备采购等。
  - 其中至少 10 个由你亲自标注标准答案。
- **规矩：**
  - 改提示词或规则，必须跑全量评测，并在 PR 里贴出前后对比。
  - 留三分之一作为保留集，只在每个阶段结束时跑一次，防止调规则时"背答案"。
- **命令：** `uv run python -m evals.run --step extract`

---

## 8. 工程约定

- **测试分三层：**
  - 纯逻辑单测：覆盖 `models.py` 和 agent 各步骤，模型用 FakeLLM 代替。
  - 服务测试：连真实 PostgreSQL，每个测试跑在一个事务里，结束后回滚。
  - 接口测试：用 httpx 直接调 FastAPI 应用。
- **CI：** ruff、import-linter、pytest（带 PostgreSQL 服务容器）、迁移检查。
- **配置：** 用 pydantic-settings 加 `.env`，`.env` 不提交，密钥仍然放在仓库外。
- **日志：** 结构化日志，每条带 `request_id` 或 `run_id`，方便串起一次请求的全过程。
- **同步还是异步：** API 先用同步 SQLAlchemy（FastAPI 会把同步接口放进线程池），简单、好调试。真遇到并发瓶颈再考虑 async。
- **本地运行：**

  ```bash
  docker compose up -d db
  uv run alembic upgrade head
  uv run bidradar api       # 启动 API
  uv run bidradar worker    # 启动 Worker
  ```

- **文件解析库：** 优先用 MIT 许可的 pdfplumber 和 python-docx。PyMuPDF 是 AGPL 许可，公开仓库要慎用。扫描件的 OCR 放到以后。

---

## 9. 文档改造

### 9.1 新的文档集合（不超过 8 个文件）

| 文件 | 内容 |
| --- | --- |
| `README.md` | 是什么、怎么跑、能演示什么 |
| `docs/architecture.md` | 本蓝图落地后的架构说明 |
| `docs/agent.md` | 研究流水线和评测 |
| `docs/data-model.md` | 表结构和数据规则 |
| `docs/dev-guide.md` | 分层规则、测试、提交约定 |
| `docs/decisions/NNN-*.md` | 架构决策记录（ADR），一个决定一页：背景、决定、代价 |
| `docs/roadmap.md` | 阶段计划和当前进度 |

其他配套调整：

- 旧文档整体移到 `docs/archive/`，保留历史，不再维护。
- `AGENTS.md` 精简到 2KB 以内，`CLAUDE.md` 继续只引用它。
- 项目记忆刷新脚本和对应的 CI 是为了维护旧的压缩交接文档而存在的，一起归档。

### 9.2 写作规则

1. 先说结论，再说原因。
2. 一句话只讲一件事。
3. 优先用肯定句。必须说明的边界集中写在"限制"小节里，不要每句话后面都跟一个"不等于……"。
4. 不用代号。术语第一次出现时，用一句话解释。
5. 代码注释写"为什么这样做"，不复述代码在做什么。

### 9.3 改写示例

> 原文：R1-A覆盖本地登录、公司成员三角色、渐进企业档案建议/确认/历史、公共商机目录与证据、私有固定版本选择。
>
> 改写：工作台现在能做这些事：登录；按管理员、成员、只读三种角色管理公司成员；成员提出档案修改建议，由管理员确认；浏览公开商机并查看原文证据。

> 原文：未知调用保持waiting_input，不自动重发。
>
> 改写：如果一次模型调用的结果不确定（比如网络中途断了），任务会停下来等人处理，而不是自动重试，这样不会重复扣费。

---

## 10. 分阶段路线

**迁移方式：** 新代码写在 `bidradar/` 目录，旧的 `services/` 原样保留、照常能跑，到阶段 6 再删。每个阶段结束时项目都能跑、能测，并且能讲出一个完整的技术故事。

| 阶段 | 做什么 | 完成标准 | 能练到的后端能力 | 规模 |
| --- | --- | --- | --- | --- |
| 0 地基 | uv 项目、FastAPI 骨架、docker-compose 起 PostgreSQL、Alembic、CI、import-linter 契约 | `GET /health` 能通，CI 全绿 | 项目结构、依赖注入、数据库迁移 | 小 |
| 1 身份与公司 | identity + workspace：登录、三种角色、档案版本，加 RLS | 两家公司互相看不到数据的测试通过；旧工作台的登录和档案功能能在新接口上用 | 会话安全、乐观锁、幂等、事务、RLS | 中 |
| 2 商机与采集 | catalog + ingestion：迁移 CCGP 和海南的采集适配器（基本是纯函数，大多能照搬） | 现有 12 条真实公告能导入、能搜索 | 数据管道、去重、版本化 | 中 |
| 3 文档与切片 | documents：上传 PDF/Word、解析、切片、检索 | 上传一份真实招标文件，能看到带页码的切片 | 文件存储、解析、后台任务 | 中 |
| 4 研究流水线 | research：五个步骤、任务队列、预算账本、评测 v0 | 4 个评测用例跑通并出指标；中途杀掉 Worker 能续跑，且不重复扣费 | 大模型结构化输出、队列和租约、成本控制、评测 | 大 |
| 5 追问与跟踪 | 追问 Agent + tracking（决定、关注、变化、提醒）+ outbox | 档案或公告变化后提醒只出现一次；授权后自动复核 | 事件驱动、outbox、去重、最终一致 | 大 |
| 6 收尾 | 前端改接新接口，删除 `services/`，文档定稿，评测扩到 30 个 | 删掉旧代码后，全部测试和评测仍然通过 | 重构收尾、清理技术债 | 中 |
| 之后按需 | 拆服务（Worker 侧优先）、Redis 缓存/限流、K8s、阿里云部署 | 有明确理由时再做 | 分布式、部署、运维 | — |

---

## 11. 怎么分工，经验才算你的

目标是"全部由你负责"，所以不能又变成 AI 一口气写完、你事后再读。建议每个阶段这样分：

1. **我搭骨架、写示例。** 搭好骨架，写一个完整示例：一个用例从接口、到表、到测试全打通，并讲清楚每层为什么这样放。
2. **你照着写同类功能。** 其余同类功能由你照着示例写，我负责审查。
3. **硬骨头由你主写。** 每个阶段挑一块：阶段 1 的 RLS、阶段 4 的匹配步骤和评测指标、阶段 5 的 outbox 消费者。
4. **讲得清才进下一阶段。** 阶段结束时，你能脱稿讲清一条请求从浏览器到数据库的完整路径。

---

## 12. 保留什么，去掉什么

**保留（这些是现在做得好的地方）：**

- 证据优先，五种状态，"未知"不当成满足。
- 冻结输入：新材料生成新报告，不覆盖旧报告。
- 任务先落库再执行，用幂等键和租约防止重复和并发冲突。
- 调模型前先预留费用；费用不确定时不当零，也不自动重试。
- 每个动作前都检查当前权限。
- 两家虚构公司的测试数据、12 条真实公告样本、采集适配器和解析代码。

**去掉或简化：**

- 四个服务之间的回环 HTTP 和服务令牌。
- 手写的 HTTP 层。
- 选择批次（selection）状态机。
- 大提示词里的补丁规则。
- 项目记忆自动刷新脚本和压缩式交接文档。
- 每份文档里反复出现的"边界免责"句子。

---

## 13. 需要你拍板的事

1. ~~模块化单体先行，以后再拆服务~~ —— 已确定（2026-10-12）。
2. ~~招标文件上传作为主要分析入口，公开采集只负责发现项目~~ —— 已确定（2026-10-13，[决策 0010](decisions/0010-upload-first-direct-analysis.md)）。
3. ~~去掉选择批次，改为点"分析"时确认范围和费用~~ —— 已确定（2026-10-13，同上）。
4. ~~前端暂时保留原生 JS~~ —— 阶段 6 按默认做法继续用原生 JS（2026-10-15）：没有构建步骤，CSP 只允许本站脚本。以后页面明显变复杂再考虑换框架。
5. ~~第 11 节的分工方式~~ —— 已确定（2026-10-13）。
6. ~~旧文档归档到 `docs/archive/`~~ —— 已完成（2026-10-12）。

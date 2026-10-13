# 架构说明

这份文档描述**现在的代码**是怎么组织的。计划中的新架构见 [v2 改造蓝图](v2-blueprint.md)。

## 一句话

两条命令行数据链负责把公开公告整理进目录；四个本地 HTTP 服务负责登录、研究和跟踪。每个服务有自己的数据库，彼此只通过 HTTP 通信。

## 整体结构

```text
                         浏览器
                           │  Cookie 会话 + CSRF
                           ▼
              ┌────────────────────────────┐
              │ workspace  :8965           │  登录、公司、成员、企业档案、选择
              │ （浏览器唯一入口）            │  转发研究、跟踪请求
              └──┬─────────┬──────────┬────┘
        只读查询 │         │ 研究请求  │ 跟踪请求
                 ▼         ▼          ▼
   ┌──────────────┐ ┌─────────────┐ ┌──────────────┐
   │ catalog :8966│ │research :8967│ │tracking :8968│
   │ 公告目录      │◄┤ 任务 + Worker │◄┤ 决定、关注    │
   │              │ │ AI Agent     │ │ 变化、提醒    │
   └──────▲───────┘ └──────┬──────┘ │ + Worker     │
          │                │        └──────────────┘
          │ 命令行导入       │ HTTPS
          │                ▼
   ingestion → processing  DeepSeek API
   （采集）     （规范化）
```

- 每个方框是一个独立进程，各有一个 SQLite 数据库，放在 `.bidradar-data/r12-workbench/<服务名>/`。
- 服务之间用 `services/common/local_http.py` 的 HTTP 客户端和服务器通信，只监听 127.0.0.1。
- 服务之间靠令牌识别调用方。catalog 的只读接口用一个共享令牌；workspace、research、tracking 之间每对"调用方 → 被调用方"各有一个令牌，被调用方据此决定允许调用哪些接口。令牌只说明"是哪个服务在调用"，用户权限仍要单独检查。
- research 和 tracking 进程里各跑一个后台 Worker 线程。

`scripts/run_research_workbench.py` 一次启动这四个进程。

## 各部分做什么

### 数据链：ingestion → processing → catalog

这三步都是命令行工具，通过 JSON 文件衔接，互相不读对方的数据库。

| 步骤 | 代码 | 输入 | 输出 |
| --- | --- | --- | --- |
| 采集 | `services/ingestion/` | 来源准入文件、栏目或公告 URL | 原始文件（按 SHA-256 存储）和抓取记录；导出为证据包 JSON |
| 规范化 | `services/processing/` | 证据包 JSON | 规范化观察 JSON：项目编号、采购人、预算、截止时间等字段，每个都带原文证据 |
| 导入目录 | `services/catalog/` | 规范化观察 JSON | 目录数据库里的公告和版本 |

采集时每个请求都要过五道关：来源准入、robots.txt、公网 DNS 检查、固定 IP 连接、请求次数和字节上限。默认不联网，必须显式加 `--allow-network`。

`scripts/run_public_chain.py` 把这三步串成一条命令；`scripts/run_data_demo.py` 用虚构数据离线跑一遍。

### workspace：浏览器入口

代码：`services/workspace/`

- **浏览器接口**（`http_api.py`）：登录、退出、公司列表、成员管理、档案提议和确认、商机搜索、选择。研究和跟踪的请求由 `research_routes.py` 转发给对应服务。
- **内部接口**：给 research 和 tracking 查权限（`authorize`）、取档案和选择（`context`）、拉事件（`events`）。
- **前端**：`web/` 下的原生 JavaScript 页面，没有用框架。
- **存储**：`store.py`，用户、会话、成员、档案版本、选择、命令记录、事件。

浏览器永远不能自己声明身份：发起人和公司 ID 都由 workspace 从会话里取出后再转发。

### catalog：商机目录

代码：`services/catalog/`

保存规范化后的公开公告。同一条公告可以有多个"观察"（每次抓取一个），按抓取时间选当前版本，晚到的旧数据不会把当前版本倒退回去。

对外提供只读接口：搜索、详情、按版本读取、研究用的公告包（含有依据关联的更正和结果公告）、变化流（给 tracking 用）。

### research：AI 研究

代码：`services/research/`

1. workspace 转来一个研究请求，`service.py` 检查权限，取出固定版本的企业档案和公告，冻结成 manifest，存为排队任务，立即返回任务 ID。
2. `worker.py` 在后台领取任务，调用 `agent.py` 里的研究 Agent。
3. Agent 每次调用模型都经过 `budget.py` 的费用账本：先预留费用，再外发，再按实际用量结算。
4. 报告通过检查后保存；没通过就保存一份只列原文的保守报告。

详见 [研究 Agent](agent.md)。

### tracking：跟踪

代码：`services/tracking/`

- **决定**：用户对公告的人工判断（待评估、跟进、放弃）。
- **关注**：要不要盯着这个公告的变化；可以额外开启"自动复核"（最长 7 天）。
- **Worker**：定期从 catalog 拉新观察、从 workspace 拉档案和权限事件，比较前后版本，发现实质变化就记录、发提醒；如果开了自动复核，就向 research 发起一次复核研究。

## 一个研究请求怎么走

```text
1. 浏览器  POST /api/workspaces/{公司}/research/analyses  {selection_id, notice_id, key, mode}
2. workspace  检查会话、CSRF、成员身份 → 加上 actor_id、workspace_id → 转发
3. research   POST /internal/v1/analyses
     → 向 workspace 查权限（authorize）
     → 向 workspace 取档案和选择（context）
     → 向 catalog 取公告包（bundle）
     → 冻结成 manifest，存为 queued 任务 → 返回 202 和任务 ID
4. research Worker  领取任务（拿到租约）
     → 每个动作前都调用 guard()：任务没被取消、租约还在、权限和档案版本没变
     → run_agent()：模型取证、提交报告、程序检查、语义核验、修订
     → 保存报告，任务状态变成 succeeded 或 partial
5. 浏览器  每 3 秒轮询一次任务状态，完成后显示报告
```

## 几条贯穿全局的规则

- **证据优先**：每条结论都要有原文引用；没有依据就写"未知"，未知不等于满足，也不等于不满足。
- **冻结输入**：任务创建时就固定了用哪个版本的档案和公告。之后档案或公告变了，旧报告不变，只是标为"已过期"。
- **先存再做**：所有请求先持久化、返回编号，再由 Worker 执行。刷新页面、断网都不会重复建任务。
- **幂等**：每个修改类请求都带一个幂等键，同一个键重复提交只执行一次。
- **每个动作前重新检查权限**：被移出公司的人，下一个动作就会被拒绝；重新加回来也不会让旧任务继续。
- **费用先预留**：调模型之前先在账本里预留费用；结果不确定时不当作免费，也不自动重试。
- **外部内容都是数据**：网页、附件、模型输出都不能当作指令执行。

## 代码阅读顺序

如果只有一个小时，按这个顺序读：

1. `services/research/agent.py`：`run_agent` 和 `_AgentRun` 的各个阶段方法。看模型能做什么、程序管住了什么。
2. `services/research/evidence.py`：`EvidenceIndex` 和 `validate_report`。看引用是怎么生成和检查的。
3. `services/research/worker.py`：`tick`。看一次模型调用的完整顺序：检查 → 预留 → 外发 → 入账 → 保存进度。
4. `services/tracking/service.py`：`consume_event` 和 `_queue_reassessment`。看变化怎么变成一次新的研究。
5. `services/workspace/http_api.py`：`Handler._dispatch`。看一个浏览器请求要过哪些检查。

## 已知的结构问题

这些问题是 v2 改造要解决的，详见 [v2 改造蓝图](v2-blueprint.md) 第 1 节：

- `workspace/store.py` 一个类同时负责校验、权限、业务规则和 SQL。
- 业务数据大多以 JSON 字符串存在 `payload` 列里，数据结构要读代码才知道。
- 每个服务都用手写的 HTTP 层，而不是 Web 框架。
- 研究 Agent 让一次模型调用做太多事，靠越来越长的提示词规则来纠错。
- 四个服务 + 回环 HTTP 对一个人维护的项目来说偏重。

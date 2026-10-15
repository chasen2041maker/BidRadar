# 留给项目负责人的三块硬骨头

v2 的其余代码已经写完。这三块是路线图里写明"你主写"的部分，代码里只留了接口、测试和 TODO。
每一块都按同一个格式写：要做什么、做完的标准、从哪里开始、提示、常见的坑。

| 练习 | 阶段 | 位置 | 验收测试 |
| --- | --- | --- | --- |
| 1. 行级安全（RLS） | 1 | `app/deps.py` 的 `workspace_id_param`、新迁移 | `tests_v2/workspace/test_rls.py` |
| 2. 匹配步骤和评测指标 | 4 | `agent/steps/match.py`、`evals/metrics.py` | `tests_v2/agent/test_match.py`、`tests_v2/evals/test_metrics.py` |
| 3. outbox 消费者 | 5 | `infra/outbox.py` 的 `OutboxConsumer` | `tests_v2/infra/test_outbox_consumer.py` |

做之前先读一遍对应的验收测试：测试就是需求。做完把测试文件开头的 `pytestmark = pytest.mark.skip(...)` 删掉，全部通过即完成。

---

## 练习 1：行级安全（RLS）

### 要做什么

让数据库自己保证"一个请求只能看到当前公司的数据"。现在的隔离靠每个 repo 函数都带 `workspace_id` 条件，
这是第一道防线。RLS 是第二道：就算哪天有人写了一句漏掉条件的 SQL，数据库也只返回当前公司的行。

### 做完的标准

1. `tests_v2/workspace/test_rls.py` 去掉 skip 后全部通过。
2. 其余测试照样全部通过（RLS 不能把正常功能挡住）。
3. 写一页决策记录 `docs/decisions/0011-row-level-security.md`：为什么做、怎么做、代价。

### 从哪里开始

1. 新建一个迁移 `0003`……（编号接在当前最新的后面），在里面：
   - 建一个数据库角色 `bidradar_app`，只给它业务表的读写权限，**不让它成为表的所有者**。
   - 给 workspace schema 里带 `workspace_id` 的表执行 `ALTER TABLE ... ENABLE ROW LEVEL SECURITY`，
     再建策略：`USING (workspace_id = current_setting('app.workspace_id', true))`。
2. 在 `bidradar/app/deps.py` 的 `workspace_id_param` 里接入：每个事务开始时执行
   `SELECT set_config('app.workspace_id', :wid, true)`（第三个参数 true 等于 `SET LOCAL`，事务结束自动失效）。
3. 先让 workspace 模块的表跑通，再推广到 documents、research、tracking 的私有表。

### 提示

- 表的所有者默认不受 RLS 约束。要么让应用用非所有者角色连库，要么给表加 `FORCE ROW LEVEL SECURITY`。想一想两种做法各自的代价。
- `current_setting('app.workspace_id', true)` 的第二个参数 true 表示"没设置时返回 NULL 而不是报错"。
  `workspace_id = NULL` 的结果是 NULL，行会被过滤掉，这正是"忘了设置就什么都看不到"想要的效果。
- 有些查询本来就要跨公司：登录时查"这个用户在哪些公司"（`memberships` 按 `user_id` 查）、Worker 领取任务。
  这些地方怎么办？可选做法：给这类查询单独建一个不受 RLS 约束的角色；或者让策略允许"按 user_id 查自己的成员关系"。

### 常见的坑

- **service 中途提交了事务。** 比如 `change_member` 提交以后还要再查一次用户信息。`SET LOCAL` 在提交时就失效了，
  之后的查询不再带公司范围。可以用 SQLAlchemy 的 `after_begin` 事件，在每个事务开始时自动设置。
- **连接池复用连接。** 如果用了会话级的 `SET`（不带 LOCAL），这个连接被下一个请求拿去时还带着上一家公司的设置。用 `SET LOCAL` 或 `set_config(..., true)`。
- **测试夹具用的是保存点。** `db_session` 夹具让 service 的 `commit()` 只提交到保存点，外层事务一直不结束，
  `SET LOCAL` 在整个测试里都有效。真实请求里不是这样，记得用一个真实的、会提交的场景再验证一遍。

### 能学到

PostgreSQL 的角色和权限、RLS 策略、事务级设置、连接池的状态泄漏。面试时可以讲"两道防线"，以及第二道防线挡住过什么。

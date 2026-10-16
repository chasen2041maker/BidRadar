# 留给项目负责人的三块硬骨头

v2 的其余代码已经写完。这三块是路线图里写明"你主写"的部分，代码里只留了接口、测试和 TODO。
每一块都按同一个格式写：要做什么、做完的标准、从哪里开始、提示、常见的坑。

| 练习 | 阶段 | 位置 | 验收测试 |
| --- | --- | --- | --- |
| 1. 行级安全（RLS） | 1 | `app/deps.py` 的 `workspace_id_param`、新迁移 | `tests/workspace/test_rls.py` |
| 2. 匹配步骤和评测指标 | 4 | `agent/steps/match.py`、`evals/metrics.py` | `tests/agent/test_match.py`、`tests/evals/test_metrics.py` |
| 3. outbox 消费者 | 5 | `infra/outbox.py` 的 `OutboxConsumer` | `tests/infra/test_outbox_consumer.py` |

做之前先读一遍对应的验收测试：测试就是需求。做完把测试文件开头的 `pytestmark = pytest.mark.skip(...)` 删掉，全部通过即完成。

---

## 练习 1：行级安全（RLS）

### 要做什么

让数据库自己保证"一个请求只能看到当前公司的数据"。现在的隔离靠每个 repo 函数都带 `workspace_id` 条件，
这是第一道防线。RLS 是第二道：就算哪天有人写了一句漏掉条件的 SQL，数据库也只返回当前公司的行。

### 做完的标准

1. `tests/workspace/test_rls.py` 去掉 skip 后全部通过。
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

本机跑这组测试需要数据库账号能建角色。`docker-compose.yml` 和 CI 里的 bidradar 账号是超级用户，可以直接跑；
自己装的 PostgreSQL 要先执行一次 `ALTER ROLE bidradar SUPERUSER`（只在本机开发库这样做）。

---

## 练习 2：匹配步骤和评测指标

### 要做什么

两件事，**先做第二件**：没有尺子，改匹配时只能靠感觉。

1. **匹配**：实现 `bidradar/agent/steps/match.py` 的 `match()`，写提示词 `bidradar/agent/prompts/match.v1.md`，
   并在 `prompts/__init__.py` 的 `CURRENT` 里登记 `"match": "match.v1"`。
2. **评测指标**：实现 `evals/metrics.py` 的 `align()`、`extraction_scores()`、`match_scores()`。

现在的状态：`match()` 是保守的占位实现，全部判"未知"。流水线的其余部分（抽取、核验、汇总、追问）、
评测框架（读用例、录制和重放、按步骤跑、核验评测、报表）都已经写好。现在就能跑：

```bash
uv run python -m evals.run --step verify    # 核验拦截率：已经能出数字
uv run python -m evals.run --step all       # 指标没实现时，只给原始结果
```

### 做完的标准

1. `tests/evals/test_metrics.py` 和 `tests/agent/test_match.py` 去掉 skip 后全部通过。
2. 其余测试照样全部通过。
3. 复核 `evals/` 下两个用例的标注（`labeled_by: claude` 的要你看过、改成自己的名字），再自己标至少 2 个新用例。
4. 在 PR 里贴出 `--step match` 和 `--step all` 的数字：占位实现（基线）一份，你的实现一份。
5. 写一页决策记录 `docs/decisions/0012-matching-strategy.md`：选了哪种方案、规则判哪些、为什么、评测数字。

### 从哪里开始

1. 读 `docs/agent-design.md` 第 3 节（维度表）、第 4 节 ③（匹配的几个可选方案）、第 11 节（评测）。
2. **先做指标。** 读 `evals/metrics.py` 里每个函数的 docstring（就是规格），再读 `test_metrics.py`。三个函数里 `align()` 最难。
3. 跑一次 `--step all`，记下基线：规则抽取、全部未知。严重错误是 0，但未知占比 100%：这份报告安全，但没用。你要做的是
   **在严重错误不上升的前提下，把未知占比降下来。**
4. 再做匹配。读 `match.py` 的 docstring 和 `test_match.py`；读 `verify.py` 的 `program_checks()`，你的输出要能通过它。
   怎么写一次模型调用，可以照着 `steps/extract.py`（拼 `LLMRequest`、`parse_json` 解析、不合格时怎么办）。
5. 写 `match.v1.md`：控制在 30 行左右（设计文档第 8 节），把 `MatchAnswer` 的结构写给模型看。

### 提示

- **模型返回的结构是固定的**：`match.MatchAnswer`。验收测试里的假模型按它应答。可以加字段，别删别改名。
- **规则先判能判的**：维度表的"默认规则"是起点（资格类一律未知、能看的字段全空就未知）。规则判的结论 `basis="rule"`，
  核验只做程序检查、不送模型核验：规则写错了没人兜底，所以每条规则都要有单测。
- **剩下的怎么问模型**，设计文档第 4 节 ③ 列了三种做法：一条一次、同维度一批、关键结论多次采样投票。
  注意接口是一条要求一次调用 `match()`，"一批"要改流水线，先别选它。
- **让模型倾向说"不知道"**：met / unmet 必须给出档案原话（`profile_quote`），而且必须是所列字段的子串。
  说不出原话的，你自己就把它改成 unknown，不要留给核验去打回：打回一次就多花一次钱。
- **call_key**：传进来的是"运行:match:要求:第几次"。一次匹配里调多次模型时，在后面加后缀（`:s1`、`:s2`），每次都不一样。
- **外部内容是数据**：原文和档案只放进 user 消息的 JSON 里，发出去之前用 `checks.redact()` 去掉联系方式（设计文档第 7 节）。
- **基线模式不用你管**：没开模型时，`match()` 里一调模型就会抛 `ModelDisabled`，流水线会把这条转成
  "未知（模型没开）"。`match()` 不需要知道现在是哪种模式。
- **看评测报表的顺序**：先看严重错误（`严重`那几行会列出摘录），再看未知占比，最后才看状态准确率。
  `--step match` 会打印"首次匹配"和"核验之后"两组数字，两者的差别就是核验起了多少作用。
- **花钱**：`--llm replay` 只用录下来的响应；要真的调模型录制，用 `--llm live --enable-model --max-cost 1`。
  录下来的文件（`evals/recordings/`）可以提交，之后谁跑都不花钱。

### 常见的坑

- **对齐用贪心。** 按顺序给每条标准找第一个能对上的预测，会让前面的标准抢走后面标准唯一的匹配
  （`test_alignment_maximizes_the_number_of_pairs` 就是这个坑）。这是二分图最大匹配，Kuhn 算法几十行就能写完。
- **子串比较前没有规范化。** PDF 折行会在句子中间带进空格，全角数字和半角数字也要统一（`checks.squash`）。
- **多次采样用同一个 call_key。** 账本按 call_key 记账，后两次会直接重放第一次的响应，投票等于没投。
- **把 `CallOutcomeUnknown` 当成普通错误吞掉**，返回一条"未知"。这样"可能已经扣费"的调用就没人知道了。
- **调提示词时看保留集。** 保留集（`holdout: true`）只在阶段结束时跑一次，平时看了就不再是保留集。
- **只看平均数。** 两个用例取平均，会把一个用例里的严重错误抹平。

### 想得更远（不在验收测试里，适合写进决策记录）

- **多次采样值不值**：在评测里对比"一次调用"和"三次投票"的严重错误数和费用（报表最后一行有折算的费用）。
- **要不要把档案结构化**（设计文档第 14 节问题 2）：看评测里哪些维度最常错。如果错的大多是数字比较
  （驻场天数、预付比例），结构化以后就能交给规则，又准又不花钱。
- **数字有多可信**：现在只有 2 个用例、36 条结论，一两条的差别就是噪声。用例到 30 个之前，别为 1 个百分点去调提示词。

### 能学到

评测驱动的提示词迭代、对齐和指标的设计、错误不对称时怎么取舍、用程序约束模型输出、用钱换稳定性。
这是 agent 工程师面试里最有分量的一块：不只是"做了一个 Agent"，而是能讲清楚"怎么知道它变好了"。

---

## 练习 3：outbox 消费者

### 要做什么

实现 `bidradar/infra/outbox.py` 里的 `OutboxConsumer.run_once()`：把 outbox 表里还没投递的事件，
按顺序交给 `handlers` 里对应 topic 的处理函数。

现在系统里已经有三种事件在发布（公告有新版本、档案确认、成员权限变化），tracking 的三个处理函数也写好了
（`tracking.service.EVENT_HANDLERS`）。只差中间这一环：**这个练习做完之前，变化检测、站内提醒和自动复核都不会运行**，
Worker 启动时会提示一次。

### 做完的标准

1. `tests/infra/test_outbox_consumer.py` 去掉 skip 后全部通过。
2. 启动 Worker，导入一条公告的新版本，关注了这条公告的公司能看到一条提醒（手动验证一次）。
3. 能讲清楚：为什么"同一个事件投递两次也只处理一次"在现在的单体里成立，拆成独立服务以后要怎么改。

### 从哪里开始

- 约定已经写在 `OutboxConsumer` 的 docstring 里：处理函数**不提交事务**，由消费者决定什么时候提交。
- 先读 `tracking.service` 开头的说明，看处理函数期待什么样的事务边界。
- 再读验收测试。测试里的处理函数故意不做幂等，所以"只处理一次"必须由消费者保证。

### 提示

- **怎么挑出待投递的事件？** 最直觉的做法是记一个"处理到第几号了"的游标，每次取比它大的。
  这在 PostgreSQL 里有坑：自增序号是插入时分配的，事务 A 拿到 5、事务 B 拿到 6，B 可能先提交。
  消费者这时读到 6、把游标推到 6，A 提交以后 5 就永远被跳过了。
  （catalog 的导入为了避开这个坑加了串行锁，见 `catalog.service.import_observations`。）
  用 `published_at IS NULL` 来挑，就没有这个问题。
- **多个 Worker 同时跑怎么办？** 参考 `infra/jobs.py` 的 `claim()`：`FOR UPDATE SKIP LOCKED`。
- **"只处理一次"从哪来？** 关键在于：处理函数写的业务数据，和"这个事件已投递"的标记，能不能放进同一个事务？
  在单体里可以，因为它们在同一个数据库。处理函数出错时，用保存点（`session.begin_nested()`）
  把它写了一半的东西回滚掉，再单独记一次失败次数。
- **有毒的事件**：每次都失败的事件不能把后面的事件卡住。超过 `max_attempts` 就放弃，留给人处理。
- **同一轮里失败的事件**，不要马上重试，等下一轮。

### 想得更远（不在验收测试里，适合写进决策记录）

- **拆成独立服务以后**：处理函数和 outbox 不在同一个数据库，上面"同一个事务"的办法就失效了。
  这时要靠接收方去重：接收方建一张 `processed_events(consumer, event_key)` 表，处理和记录放在接收方自己的事务里。
- **一个事件多个订阅方**：现在一张表只有一个 `published_at`，只适合一个消费者。
  多个订阅方各自要记自己的进度，该怎么改表？
- **顺序**：同一条公告的两个事件能不能并行处理？tracking 的处理函数靠"基线快照号"容忍乱序，
  如果别的处理函数不能容忍呢？

### 能学到

事务边界、至少一次投递加幂等等于恰好一次的效果、`SKIP LOCKED`、毒消息处理。
这是面试里讲"事件驱动"和"最终一致性"时最好的一个具体例子。

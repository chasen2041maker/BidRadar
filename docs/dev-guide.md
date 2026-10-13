# 开发指南

仓库里现在有两套代码：旧代码在 `services/`，v2 新代码在 `bidradar/`。下面几节讲旧代码；新代码见 [v2 新代码](#v2-新代码bidradar)。旧代码到 v2 阶段 6 删除。

## 环境

- Python 3.10 或更高版本。旧代码运行和测试只用标准库，不需要 `pip install`。
- Node.js（可选）：只用来检查前端语法 `node --check services/workspace/web/app.js`。
- 格式化工具（可选）：`ruff`（Python）和 `prettier`（前端），只在改代码后格式化时用到。

## 运行

```bash
# 完整环境：四个服务，浏览器打开 http://127.0.0.1:8965/
python scripts/run_research_workbench.py

# 导入一个规范化公告包后再启动
python scripts/run_research_workbench.py --bundle 路径/normalized.json

# 允许调用 DeepSeek（会产生费用）
python scripts/run_research_workbench.py --enable-model
```

端口从 `--port`（默认 8965）开始连续占用四个：workspace、catalog、research、tracking。Ctrl+C 停止，数据保留。

第一次启动时会创建两家虚构公司和四个测试账号，随机密码保存在 `.bidradar-data/r12-workbench/demo-accounts.json`：

| 用户名 | 公司 | 角色 |
| --- | --- | --- |
| demo.admin | 山岚软件（虚构） | 管理员 |
| demo.member | 山岚软件（虚构） | 协作成员 |
| demo.viewer | 山岚软件（虚构） | 只读成员 |
| other.admin | 海隅智能（虚构） | 管理员 |

数据链相关：

```bash
python scripts/run_data_demo.py          # 用虚构数据离线跑一遍采集 → 规范化 → 目录
python scripts/run_public_chain.py --help   # 对真实网站跑一次（需要准入文件和 --allow-network）
python -m services.ingestion --help       # 采集命令行
python -m services.catalog --help         # 目录命令行
```

真实网站采集需要一份人工核对过的来源准入文件，格式和注意事项见 [历史文档 31](archive/31-public-source-layer.md)。

## 数据和密钥放在哪

| 内容 | 位置 | 进 git 吗 |
| --- | --- | --- |
| 各服务的数据库、测试账号、评测结果 | `.bidradar-data/` | 不进（已在 .gitignore） |
| DeepSeek 密钥 | `~/.bidradar/credentials/deepseek-api-key.txt` | 不进（在仓库外） |
| 模型费用账本 | `~/.bidradar/budgets/deepseek-r12-20261001.sqlite3` | 不进（在仓库外） |

不要删费用账本来"重置额度"：账本旁边有一个标记文件，账本丢了服务会拒绝启动。

## 测试

```bash
# 全部测试（约 30 秒）
python -m unittest discover -s tests -p 'test_*.py'

# 只跑某一个文件
python -m unittest tests.test_research_agent -v
```

测试全部使用虚构数据和本机临时目录，不联网，不调真实模型。

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_research_agent.py` | Agent 的协议、工具、检查、修订、恢复（模型用脚本代替） |
| `test_research_runtime.py` | 四个服务真实跑起来，Worker 执行研究任务的全过程 |
| `test_research_budget.py` | 费用账本：并发预留、结算、结果未知、恢复 |
| `test_research_provider.py` | DeepSeek 接口适配（本机假服务器） |
| `test_tracking.py` | 决定、关注、变化检测、委托、提醒 |
| `test_tracking_http_replay.py` | 四服务下的跟踪和自动复核回放 |
| `test_workspace_store.py` | 登录、成员、档案、选择、公司隔离 |
| `test_workbench_http.py` | 工作台 HTTP 接口：会话、CSRF、权限、转发 |
| `test_workspace_access_policy.py` | 权限规则纯函数 |
| `test_catalog_http.py` | 目录 HTTP 接口 |
| `test_data_chain.py` | 采集 → 规范化 → 目录整条链 |
| `test_ingestion_*.py`、`test_public_*.py` | 采集流程、归档、网络层、解析器 |
| `test_ccgp_*.py`、`test_tianjin_*.py` | 各来源的解析和边界 |
| `test_local_http.py` | 服务间 HTTP 层 |
| `test_common_query.py` | 查询串解析的版本兼容 |

## 格式化

```bash
pip install ruff
ruff format services scripts tests

npx prettier@3 --write services/workspace/web/
```

配置在 `pyproject.toml` 和 `.prettierrc.json`，行宽 120。CI 会检查 Python 格式。

## 代码约定

现在的代码分成 `services/` 下的几个服务，每个服务大致是：

- `http_api.py`：HTTP 接口
- `service.py`：业务规则（research、tracking 有；workspace 和 catalog 的业务规则写在 `store.py` 里）
- `store.py`：数据库读写
- `worker.py`：后台任务（research、tracking）

v2 改成了统一的分层结构，见下面的 [v2 新代码](#v2-新代码bidradar)。旧代码继续遵守这几条：

- 一个服务不能读另一个服务的数据库，要数据就调对方的接口。
- 浏览器传来的身份、公司、角色都不能直接信，必须从会话和数据库重新取。
- 修改类操作都要带幂等键，会被并发修改的记录要带版本号。
- 外部内容（网页、附件、模型输出）只能当数据，不能当指令。

### 注释怎么写

- 每个文件开头写清楚：这个文件做什么、在整体流程里处于哪一步、建议从哪里读起。
- 函数的 docstring 写：做什么、返回什么、有哪些不显而易见的规则。
- 注释写"为什么这样做"，不复述代码在做什么。
- 用完整的句子，不用代号。

## 文档怎么写

1. 先说结论，再说原因。
2. 一句话只讲一件事。
3. 优先用肯定句。必须说明的限制集中写在"限制"或"已知短板"小节里，不要每句话后面都加"不等于……"。
4. 不用代号。术语第一次出现时用一句话解释。
5. 写了就要维护：代码改了，相关文档一起改。

文档放在哪：

- 系统现在怎么工作 → `docs/architecture.md`、`docs/agent.md`、`docs/data-model.md`
- 进度和下一步 → `docs/roadmap.md`
- 一个重要的设计决定 → `docs/decisions/` 下新建一页（背景、决定、代价）
- 一次任务的过程细节 → 写在 PR 描述里，不另建文档

## v2 新代码（bidradar/）

### 环境

- Python 3.12 和 [uv](https://docs.astral.sh/uv/)。uv 负责装 Python 依赖、锁版本、运行命令。
- Docker，用来跑 PostgreSQL 16。
- `uv sync` 把依赖装进仓库里的 `.venv/`。依赖写在 `pyproject.toml`，确切版本锁在 `uv.lock`。改依赖用 `uv add` 或 `uv remove`，两个文件一起提交。

### 运行

```bash
docker compose up -d --wait db                     # 启动 PostgreSQL，等它就绪
uv run alembic upgrade head                        # 把数据库迁移到最新版本
uv run uvicorn bidradar.app.main:app --reload      # 启动 API
curl http://127.0.0.1:8000/health                  # {"status":"ok","database":"ok"}
```

接口文档在 http://127.0.0.1:8000/docs ，由 FastAPI 自动生成。

- 配置项在 `bidradar/app/settings.py`。每一项都能用 `BIDRADAR_` 开头的环境变量或 `.env` 文件覆盖，模板见 `.env.example`。
- 默认值对应 `docker-compose.yml` 里的本地数据库，不写 `.env` 也能跑。
- 数据库第一次启动时会建两个库：`bidradar` 给开发用，`bidradar_test` 给 pytest 用。
- 不用 Docker 也可以：自己装 PostgreSQL 16，建好这两个库，再用 `BIDRADAR_DATABASE_URL` 和 `BIDRADAR_TEST_DATABASE_URL` 指过去。

### 测试和检查

```bash
uv run pytest                                      # 新代码的测试，连 bidradar_test 库
uv run lint-imports                                # 检查分层规则
uv run alembic check                               # 检查表定义和迁移是否一致
uv run ruff format bidradar migrations tests_v2    # 格式化
```

新代码的测试放在 `tests_v2/`，用 pytest。旧测试留在 `tests/`，用 unittest。阶段 6 删掉旧代码后，`tests_v2/` 改名为 `tests/`。

测试分三层，公共夹具在 `tests_v2/conftest.py`：

| 层 | 测什么 | 怎么写 |
| --- | --- | --- |
| 纯逻辑 | `models.py`、`agent/` 的各步骤 | 直接调函数，不连数据库；大模型用假实现代替 |
| 服务 | `service.py` 加 `repo.py`，连真实 PostgreSQL | 用 `db_session` 夹具。每个测试跑在一个事务里，结束后回滚 |
| 接口 | 从 HTTP 请求到数据库的完整路径 | 用 `client` 夹具。请求直接在进程里交给 FastAPI，会话同样会回滚 |

### 每一层放什么，为什么这样分

| 层 | 位置 | 放什么 | 为什么单独一层 |
| --- | --- | --- | --- |
| app | `bidradar/app/` | `main.py` 组装应用、挂路由；`deps.py` 给路由注入数据库会话，阶段 1 加上当前用户；`settings.py` 读配置；`metadata.py` 列出全部表定义 | 只有这一层知道一共有哪些模块。换配置、加模块都只改这里 |
| api | `modules/*/api.py` | 路由：解析请求，调用 service，把结果转成响应 | 状态码、Cookie 这些 HTTP 细节不会混进业务规则 |
| schemas | `modules/*/schemas.py` | 接口的请求和响应结构（Pydantic） | 接口长什么样和表长什么样分开定义，改表不会悄悄改掉接口 |
| service | `modules/*/service.py` | 业务用例：一个函数一个业务动作，由它提交事务 | API 和 Worker 都调用它。它也是别的模块唯一能调用的入口，以后拆服务时换成同名接口的 HTTP 客户端就行 |
| repo | `modules/*/repo.py` | 数据库读写 | 业务规则里看不到 SQL，SQL 里看不到业务规则 |
| tables | `modules/*/tables.py` | SQLAlchemy 表定义，绑定在本模块自己的 schema 上 | 每个模块只写自己的 schema，拆服务时把 schema 整个搬走 |
| models | `modules/*/models.py` | 领域对象和业务规则，纯 Python | 规则最集中的地方，不连数据库就能单测 |
| agent | `bidradar/agent/` | 研究流水线和追问 Agent，纯逻辑 | 需要的东西都从参数传进来，所以能单独评测，以后也能单独拆走 |
| infra | `bidradar/infra/` | 数据库连接，以后还有任务队列、outbox、日志、时钟 | 最底层，谁都能用，它不认识业务 |

一个请求怎么走，看 `bidradar/app/health.py`：路由（app）→ 依赖注入会话（`deps.py`）→ 执行 SQL（`infra/db.py`）→ 数据库。阶段 1 起，业务请求会多经过 api → service → repo 这几层。

### 依赖规则

这些规则写成了 import-linter 契约，放在 `pyproject.toml` 里，CI 每次都检查。违反时 `uv run lint-imports` 会指出是哪一行 import 越界了。

1. 模块内只能从上往下 import：api → schemas → service → repo → tables → models。
2. api 只调用 service，不直接 import repo 和 tables。
3. 跨模块只能 import 对方的 service，不能碰对方的其他文件。
4. `models.py` 不 import FastAPI、SQLAlchemy 这类 Web 和数据库库。
5. `agent/` 不 import app、modules、infra，也不 import Web 和数据库库。
6. `infra/` 不 import app、modules、agent。

### 数据库迁移

- 改了 `tables.py` 之后生成迁移：`uv run alembic revision --autogenerate --rev-id 0002 -m "create identity tables"`。编号接着上一个往下排。
- 生成的迁移一定要读一遍再提交。比如改列名，自动生成的是"删一列、加一列"，会丢数据，要手动改成重命名。
- 迁移合并到 main 以后就不再修改。要改，就再写一个新迁移。
- 迁移里不 import 应用代码，表名和 schema 名直接写在迁移里。应用代码会继续改，迁移必须保持原样。
- 模块之间不建外键。跨模块的 ID（比如 `workspace_id`）只存值，由 service 保证它有效。这样每个 schema 都能整体搬走。
- 新增模块时，在 `bidradar/app/metadata.py` 加一行，再写一个建 schema 的迁移。漏了哪一步，pytest 都会失败。

## 提交和合并

- 在独立分支上开发，通过 PR 合并到 main。
- 提交信息用英文祈使句开头，例如 `fix: ...`、`docs(research): ...`、`refactor: ...`，正文说明为什么改。
- 合并前确认 CI 通过。
- 不提交 `.bidradar-data/` 里的任何内容、密钥、真实企业资料或个人联系方式。

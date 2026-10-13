# 开发指南

## 环境

- Python 3.10 或更高版本。运行和测试只用标准库，不需要 `pip install`。
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

v2 会改成统一的四层结构，见 [v2 改造蓝图](v2-blueprint.md) 第 3 节。在那之前：

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

## 提交和合并

- 在独立分支上开发，通过 PR 合并到 main。
- 提交信息用英文祈使句开头，例如 `fix: ...`、`docs(research): ...`、`refactor: ...`，正文说明为什么改。
- 合并前确认 CI 通过。
- 不提交 `.bidradar-data/` 里的任何内容、密钥、真实企业资料或个人联系方式。

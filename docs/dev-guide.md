# 开发指南

怎么把项目跑起来、怎么测试、代码按什么规矩写。系统怎么工作见 [架构说明](architecture.md)。

## 环境

- Python 3.12 和 [uv](https://docs.astral.sh/uv/)。uv 负责装依赖、锁版本、运行命令。
- Docker，用来跑 PostgreSQL 16。不用 Docker 也可以，见下面"不用 Docker"。
- Node.js（可选）：只用来检查前端语法和格式（`node --check`、`prettier`）。

`uv sync` 把依赖装进仓库里的 `.venv/`。依赖写在 `pyproject.toml`，确切版本锁在 `uv.lock`。改依赖用 `uv add` 或 `uv remove`，两个文件一起提交。

## 跑起来

```bash
docker compose up -d --wait db                 # PostgreSQL 16，第一次启动会建 bidradar 和 bidradar_test 两个库
uv run alembic upgrade head                    # 迁移到最新版本
uv run python -m bidradar seed-demo            # 两家虚构公司、四个演示账号、初始档案
uv run python -m bidradar ingest demo          # 用虚构数据离线跑一遍采集（不联网）
uv run python -m bidradar ingest publish <上一步输出的 run_id>   # 规范化并导入商机目录

uv run python -m bidradar api                  # 终端 1：API 和工作台，http://127.0.0.1:8000/
uv run python -m bidradar worker               # 终端 2：后台任务（解析文件、执行研究）
```

演示账号的随机密码在 `.bidradar-data/v2/demo-accounts.json`：

| 用户名 | 公司 | 角色 |
| --- | --- | --- |
| demo.admin | 山岚软件（虚构） | 管理员 |
| demo.member | 山岚软件（虚构） | 协作成员 |
| demo.viewer | 山岚软件（虚构） | 只读成员 |
| other.admin | 海隅智能（虚构） | 管理员 |

登录后在商机目录把数据范围切到"虚构演示样本"，打开公告，上传 `tests/fixtures/tender-fictional.pdf`（一份虚构招标文件），预估费用后确认发起。
模型默认关闭，研究走规则基线，不花钱。

接口文档在 http://127.0.0.1:8000/docs ，由 FastAPI 自动生成。

### 配置

配置项在 `bidradar/app/settings.py`，每一项都能用 `BIDRADAR_` 开头的环境变量或 `.env` 文件覆盖，模板见 `.env.example`。默认值对应 `docker-compose.yml` 里的本地数据库，不写 `.env` 也能跑。

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `BIDRADAR_DATABASE_URL` | 本机 bidradar 库 | |
| `BIDRADAR_DATA_DIR` | `.bidradar-data/v2` | 上传的文件、采集的原件（按 SHA-256 存）、演示账号 |
| `BIDRADAR_MODEL_ENABLED` | `false` | 开了才会调 DeepSeek，会花钱。**没有项目负责人同意不开** |
| `BIDRADAR_DEEPSEEK_API_KEY_FILE` | `~/.bidradar/credentials/deepseek-api-key.txt` | 密钥放在仓库外的文件里，不写进 `.env` |
| `BIDRADAR_BUDGET_*_CAP_MICRO` | 总 20 元、每家公司 10 元、每个任务 2 元 | 单位是百万分之一元。改上限也要负责人同意 |

### 采集真实网站

采集真实网站要同时满足三件事：命令里显式加 `--allow-network`、一份人工核对过的准入文件、一台能直接连外网的机器（采集器不走代理，见下面"限制"）。

```bash
# CCGP 中央公开招标栏目第 1 页，最多 10 条公告，只抓标题里有"软件"或"平台"的
uv run python -m bidradar ingest collect-public --source ccgp --category zygg/gkzb \
  --title-term 软件 --title-term 平台 --max-notices 10 \
  --key ccgp-gkzb-20261022 --allow-network --policy .bidradar-data/public-policy.json
uv run python -m bidradar ingest show <run_id>       # 看每一页抓到了什么、解析得怎么样
uv run python -m bidradar ingest publish <run_id>    # 规范化并导入商机目录
```

`--category` 只接受核对过的栏目，写错会报"未支持的来源或栏目"：

| 来源 | 栏目 |
| --- | --- |
| `--source ccgp` | `zygg/…` 是中央、`dfgg/…` 是地方，后半段是 `gkzb` 公开招标、`jzxcs` 竞争性磋商、`jzxtpgg` 竞争性谈判、`gzgg` 更正、`zbgg` 中标、`cjgg` 成交，例如 `dfgg/jzxcs` |
| `--source hainan` | `cggg`、`zfcgqtgg`、`cgzbgg`（2026-09-30 按官网核对的三个政府采购栏目） |

**准入文件**是你核对过网站规则后写下的记录，不是网站给的许可。放在 `.bidradar-data/` 下面，不进 git。格式如下：

```json
{
  "schema_version": 2,
  "approved": false,
  "purpose": "本机开发：抽样核对 CCGP 和海南的栏目解析器还能不能用",
  "reviewed_at": "2026-10-22T10:00:00+08:00",
  "expires_at": "2026-11-22T10:00:00+08:00",
  "evidence": ["2026-10-22 核对了 robots.txt：……", "2026-10-22 核对了网站使用说明：……"],
  "attachments_allowed": false,
  "robots_missing": "deny",
  "allowed": [
    {"host": "www.ccgp.gov.cn", "path_prefix": "/cggg/zygg/", "kinds": ["listing", "notice"]},
    {"host": "www.ccgp.gov.cn", "path_prefix": "/cggg/dfgg/", "kinds": ["listing", "notice"]},
    {"host": "ggzy.hainan.gov.cn", "path_prefix": "/ggzy/ggzy/cggg/", "kinds": ["listing", "notice"]}
  ]
}
```

- 核对完 robots.txt 和网站的使用说明，把核对结果写进 `evidence`，再把 `approved` 改成 `true`。不是 `true` 时采集器报 `policy_invalid`。
- 过了 `expires_at` 采集器就拒绝（`policy_expired_or_not_yet_valid`），重新核对以后再延期。
- `path_prefix` 以 `/` 结尾才按前缀匹配，否则只放行这一个路径。
- 网站没有 robots.txt（返回 404）时，默认的 `"robots_missing": "deny"` 会让运行以 `robots_unavailable` 停下。核对过网站确实没有 robots.txt，可以改成 `"allow_404"`。只有 404 这样处理，403、5xx 和网络错误仍然按"禁止"算。
- 附件默认不抓。要抓就把 `attachments_allowed` 改成 `true`，并为附件路径另加一条 `kinds` 为 `["attachment"]` 的规则。
- 更细的规则（robots 返回 404 怎么处理、DNS 模式）见 [历史文档 31](archive/31-public-source-layer.md)。

**怎么看解析器还能不能用**：`ingest show` 输出里每一页都有 `parsed.status`。

- `ok` 或 `partial`：解析正常，`partial` 的原因列在 `issues` 里。
- `parse_error`，原因是 `unexpected_listing_template` 或 `unexpected_notice_template`：网站改版了，要更新 `bidradar/modules/ingestion/sources/` 里的解析器。原件已经存下，改完先用 `ingest replay <capture_id>` 看新的解析结果，再 `ingest publish <run_id>`（导出时会用新解析器重新解析原件），都不用再联网。
- `blocked`：遇到了验证码或限流，整次运行已经停下。等一段时间再说，不要换着法子重试。

**限制**

- 采集器不读系统代理，用解析出的 IP 直接连网站，所以在只能走 HTTPS 代理的环境里（比如 Claude 的云端会话）连不上这些网站。
- 本机的代理软件把 DNS 换成假地址时（比如 fake-IP 模式），加 `--dns-mode google-doh`。
- 天津开放接口的条款核对有效到 2026-10-29（按 UTC 日期）。从 10-30 起，`collect-tianjin` 会报 `source_terms_review_required`。重新核对条款以后，改 `bidradar/modules/ingestion/tianjin.py` 里 `fetch_page` 的日期。天津平台目前仍拒绝开发者令牌。

### 不用 Docker

自己装 PostgreSQL 16，建好 `bidradar` 和 `bidradar_test` 两个库，再用 `BIDRADAR_DATABASE_URL` 和 `BIDRADAR_TEST_DATABASE_URL` 指过去。
练习 1（RLS）的测试要能建数据库角色，本机开发库的账号需要 `ALTER ROLE bidradar SUPERUSER`。

## 数据和密钥放在哪

| 内容 | 位置 | 进 git 吗 |
| --- | --- | --- |
| 业务数据、费用账本（`research.llm_calls`） | PostgreSQL | — |
| 上传的文件、采集的原件、演示账号 | `.bidradar-data/v2/` | 不进（已在 .gitignore） |
| DeepSeek 密钥 | `~/.bidradar/credentials/deepseek-api-key.txt` | 不进（在仓库外） |
| 评测用例和录下的模型响应 | `evals/` | 进（全部是虚构内容） |

不要删费用账本里的记录来"重置额度"：结果不确定的调用按已花计，删了就可能被重复计费。

## 测试和检查

```bash
uv run pytest                                  # 全部测试，连 bidradar_test 库，约 30 秒
uv run pytest tests/agent                      # 只跑研究流水线（不连数据库，不到 1 秒）
uv run lint-imports                            # 分层规则
uv run alembic check                           # 表定义和迁移是否一致
uv run python -m evals.run --step all          # 离线评测（规则基线，不调模型）
uv run ruff format bidradar migrations tests evals
npx prettier@3.8.1 --write bidradar/app/static
```

测试全部用虚构数据，不联网，不调真实模型（模型用 `FakeLLM` 按脚本应答）。分三层，公共夹具在 `tests/conftest.py`，造数据的函数在 `tests/factories.py`：

| 层 | 测什么 | 怎么写 |
| --- | --- | --- |
| 纯逻辑 | `models.py`、`agent/` 的各步骤 | 直接调函数，不连数据库 |
| 服务 | `service.py` 加 `repo.py`，连真实 PostgreSQL | 用 `db_session` 夹具。每个测试跑在一个事务里，结束后回滚；service 里的 `commit()` 只提交到保存点 |
| 接口 | 从 HTTP 请求到数据库的完整路径 | 用 `client` 夹具。请求直接在进程里交给 FastAPI，会话同样会回滚 |

三个练习的验收测试默认跳过（文件开头的 `pytestmark = pytest.mark.skip(...)`），见 [练习](exercises.md)。

## 代码结构

```text
bidradar/
  app/        组装：main.py（应用、路由、安全响应头）、deps.py（会话、当前用户、CSRF）、worker.py、cli.py、static/（前端）
  modules/    七个业务模块，每个一个 PostgreSQL schema
    identity  workspace  catalog  ingestion  documents  research  tracking
  agent/      研究流水线和追问 Agent，纯逻辑，不碰数据库
  infra/      数据库连接、任务队列、outbox、幂等键、文件存储、时钟
migrations/   Alembic 迁移，一个阶段一个
evals/        评测：用例、录制和重放、按步骤跑
tests/        和 bidradar/ 同样按模块分目录
```

### 每一层放什么，为什么这样分

| 层 | 位置 | 放什么 | 为什么单独一层 |
| --- | --- | --- | --- |
| app | `bidradar/app/` | 组装应用、挂路由；给路由注入会话和当前用户；读配置；Worker 的任务注册 | 只有这一层知道一共有哪些模块。换配置、加模块都只改这里 |
| api | `modules/*/api.py` | 路由：解析请求，调用 service，把结果转成响应 | 状态码、Cookie 这些 HTTP 细节不会混进业务规则 |
| schemas | `modules/*/schemas.py` | 接口的请求和响应结构（Pydantic） | 接口长什么样和表长什么样分开定义，改表不会悄悄改掉接口 |
| service | `modules/*/service.py` | 业务用例：一个函数一个业务动作，先检查权限，最后提交事务 | API 和 Worker 都调用它。它也是别的模块唯一能调用的入口，以后拆服务时换成同名接口的 HTTP 客户端就行 |
| repo | `modules/*/repo.py` | 数据库读写，只读写、不提交 | 业务规则里看不到 SQL，SQL 里看不到业务规则 |
| tables | `modules/*/tables.py` | SQLAlchemy 表定义，绑定在本模块自己的 schema 上 | 每个模块只写自己的 schema，拆服务时把 schema 整个搬走 |
| models | `modules/*/models.py` | 领域对象和业务规则，纯 Python | 规则最集中的地方，不连数据库就能单测 |
| agent | `bidradar/agent/` | 研究流水线和追问 Agent | 需要的东西（模型、步骤存储、守卫、账本）都从参数传进来，所以能单测、能离线评测 |
| infra | `bidradar/infra/` | 数据库、任务队列、outbox、幂等键、文件存储、时钟 | 最底层，谁都能用，它不认识业务 |

**加一个功能**都是同一套顺序：`tables.py` 建表并生成迁移 → `models.py` 写规则和纯逻辑单测 → `repo.py` → `service.py` → `schemas.py` + `api.py` → 服务测试和接口测试 → `uv run pytest && uv run lint-imports && uv run alembic check`。

### 依赖规则

这些规则写成了 import-linter 契约，放在 `pyproject.toml` 里，CI 每次都检查。违反时 `uv run lint-imports` 会指出是哪一行 import 越界了。

1. 模块内只能从上往下 import：api → schemas → service → repo → tables → models。
2. api 只调用 service，不直接 import repo 和 tables。
3. 跨模块只能 import 对方的 service，不能碰对方的其他文件。
4. `models.py` 不 import FastAPI、SQLAlchemy 这类 Web 和数据库库。
5. `agent/` 不 import app、modules、infra，也不 import Web 和数据库库。
6. `infra/` 不 import app、modules、agent。

### 几条业务上的规矩

- 浏览器传来的身份、公司、角色都不能直接信：当前用户从会话取，权限每个动作前用 `workspace.authorize()` 重新查。
- 修改类请求都带幂等键（`key`）；会被并发修改的记录带版本号（`expected_version`），对不上就返回 409。
- 异步的事写 outbox（和业务数据同一个事务）；后台任务写 jobs（同样同一个事务）。不在事务提交前调用外部服务。
- 外部内容（网页、附件、模型输出）只当数据：显示时用 `textContent`，发给模型时放进 user 消息的 JSON 里。
- 未知不当成满足，也不当成不满足。

### 数据库迁移

- 改了 `tables.py` 之后生成迁移：`uv run alembic revision --autogenerate --rev-id 0007 -m "..."`。编号接着上一个往下排。
- 生成的迁移一定要读一遍再提交。比如改列名，自动生成的是"删一列、加一列"，会丢数据，要手动改成重命名。
- 迁移合并到 main 以后就不再修改。要改，就再写一个新迁移。
- 迁移里不 import 应用代码，表名和 schema 名直接写在迁移里。
- 模块之间不建外键。跨模块的 ID（比如 `workspace_id`）只存值，由 service 保证它有效。这样每个 schema 都能整体搬走。
- 新增模块时，在 `bidradar/app/metadata.py` 加一行，再写一个建 schema 的迁移。漏了哪一步，pytest 都会失败。

### 改研究流水线

- 提示词放 `bidradar/agent/prompts/`，文件名带版本号。改提示词就新建一个版本文件，改 `prompts/__init__.py` 的 `CURRENT`。
- 提示词、维度表、规则的指纹会算进每个任务的 `config_version`：旧报告永远知道自己是按哪版规则生成的。
- 改了提示词、维度表或规则，跑 `uv run python -m evals.run --step all`，在 PR 里贴出改前改后的数字。详见 [Agent 设计](agent-design.md) 第 11 节。

## 注释和文档怎么写

- 每个文件开头写清楚：这个文件做什么、在整体流程里处于哪一步、建议从哪里读起。
- 函数的 docstring 写：做什么、返回什么、有哪些不显而易见的规则。
- 注释写"为什么这样做"，不复述代码在做什么。
- 先说结论，再说原因；一句话只讲一件事；优先用肯定句；不用代号。
- 代码改了，相关文档一起改。

文档放在哪：

- 系统现在怎么工作 → `docs/architecture.md`、`docs/agent.md`、`docs/data-model.md`
- 研究流水线的详细设计 → `docs/agent-design.md`
- 进度和下一步 → `docs/roadmap.md`
- 一个重要的设计决定 → `docs/decisions/` 下新建一页（背景、决定、代价）
- 一次任务的过程细节 → 写在 PR 描述里，不另建文档

## 提交和合并

- 在独立分支上开发，通过 PR 合并到 main。
- 提交信息用英文祈使句开头，例如 `fix: ...`、`docs(research): ...`、`refactor: ...`，正文说明为什么改。
- 合并前确认 CI 通过。
- 不提交 `.bidradar-data/` 里的任何内容、密钥、真实企业资料或个人联系方式。

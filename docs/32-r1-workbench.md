# 32｜R1-A 本地商机核查工作台

2026-09-30。负责人授权继续一大段、完成后统一阅读核心代码。基线 `7b267b53a119761f3c835bdde302b84c6da3f904` 为 DATA-001 最终已审版本，PR #11 尚未合并。本任务独立分支，不沿用合并授权。

## 目标与边界

可操作流程：登录 → 公司空间 → 建议并确认企业档案 → 搜索商机、核查证据与材料缺口 → 保存固定版本选择 → 明确确认或取消。对应 R1 前半段，不声称完成深度分析、报告、追问或跟进决定。

企业输入全部为虚构测试数据。公共采购数据复用 DATA-001 已获准本地证据，不新增采集。catalog HTTP 服务拥有公共目录库，workspace 拥有私有开发库，只通过固定 HTTP 契约通信，不跨库/共享 ORM。标准库、SQLite、回环 HTTP 是开发适配器，不是 PostgreSQL/RLS、正式身份系统或生产恢复 SLA 验收；D-TENANCY-01 保持不变，真实企业资料须先通过相关隔离/保留/恢复门槛。

## 接口与安全契约

- catalog 仅监听 127.0.0.1、校验含端口的精确 Host 和 Bearer 服务令牌。只读 `GET /v1/notices` 与 `GET /v1/notices/{notice_id}`，沿用 query/detail JSON。未知/重复参数拒绝，故障不返回空列表，无任意 URL、采集、导入或原件下载 HTTP 接口。
- workspace 提供中文静态界面及 `/api`，仅预置测试账号，无公开注册或代发邀请。随机会话、8 小时期限、哈希落库、注销失效，密码独立盐慢哈希；登录失败有限次数持久限制。每次业务操作和幂等重放检查当前会话/成员/动作，不信任客户端角色与资源归属。
- 主 Cookie 为 HttpOnly/SameSite=Strict/Path=/，CSRF Cookie 为 SameSite=Strict/Path=/，后者供页面刷新后恢复请求头；写请求精确 Origin+JSON+会话绑定 CSRF，登录精确 Origin。无通配 CORS、私有响应 no-store、精确 Host、防重绑定、请求/响应大小及超时边界。HTTP 只限回环开发，本机恶意进程不在隔离能力范围。凭据不进入 URL、日志、源码。
- 管理员/协作成员可建议档案及准备/确认/取消普通选择；只读成员只读。管理员确认档案、调整已有测试成员角色/撤权，最后一个管理员不能被移除/降级。选择公司内共享，不仅创建者可操作；这些写动作保留操作者历史。无权对象不泄漏内容。

成员关系携带递增 version（初始1）；修改必须传 expected_version，旧页面返回409，不能把已撤权状态写回为可访问。成员变化同事务追加操作者、旧/新状态、版本和时间。HTTP/普通账号不得使用CLI bootstrap入口。选择校验所有目录请求共享15秒总限额，单请求最多5秒；已成功的创建命令重放先检查当前授权与请求身份，不依赖目录此刻在线。

## 档案与选择契约

档案 payload 固定九键：company_name、city、project_types、capabilities、delivery_constraints、cases、qualifications、staffing、commercial_constraints。名称非空，其他为有界字符串或 null，空白转 null；名称/城市属于同一类，共八类。不强迫首次全填，不把偏好当硬限制。正式确认仅指用户声明；证明未提供、读取未尝试、有效性未核查，不能据文字推断文件已到达或资格满足。

建议保留 base_revision，管理员确认比较 expected_revision 和当前版本；0 表示尚无正式版本，新版本为递增整数。不可覆盖历史，记录作者、确认者、服务端 UTC 时间。旧建议不能覆盖新版本；幂等键按公司/动作/规范内容绑定，同键同内容返回原结果，异内容 409，重放前仍检查当前授权。

选择冻结 1–20 个唯一 notice_id + observation_id、catalog 返回的标题/来源/simulation，以及正式 profile_revision。浏览器只提交固定 ID，不能伪造可信标题。创建经 catalog 核验进入 awaiting_decision；明确确认转 selected、取消转 cancelled，版本从 1 递增。selected 只表示人工选择后续研究对象，不是投标决定或研究任务。空选择/默认全部/重复公告/未知版本拒绝。

目录核验后，私有写事务内再次检查绑定会话、当前成员、档案和选择版本。catalog 变更返回 409，故障 503，均保留已有选择。跨服务没有原子事务：保存核验时点及固定观察，不承诺私库提交瞬间目录仍绝对最新；后续研究必须再次核查。已确认选择不静默换版本；原始输入/历史继续可核对。

workspace API：POST `/api/login`、`/api/logout`；GET `/api/session`；公司范围 `/api/workspaces/{id}` 下的 profile、members、notices、selections。建议/确认、成员修改、选择确认/取消使用明确 POST 子路径。档案与选择命令携带版本和幂等key；成员修改只携带expected_version，响应未知时重读成员状态核对；登录/注销不使用业务幂等键。错误为 `{error:{code,message}}`，不返回异常栈或私有正文。契约为本地 v1，旧 CLI 不变；日期/金额保持 DATA-001 语义，UTC 时间不改写采购原文日期。

## 验收

真实浏览器和两个独立 HTTP 服务联调；两公司互拒、只读禁写、撤权后旧会话与幂等拒绝、最后管理员保护、并发确认、固定选择版本、catalog 失败、重启持久恢复、Host/Origin/CSRF负例、不可信文本不执行。所有查看/刷新/搜索及选择的模型调用为零，research 和费用功能本阶段未实现。

实际命令/版本/结果及 1–3 个核心阅读入口见 [R1-A 笔记](learning/R1A-001.md)。最终成绩绑定提交和独审；本人保持待阅读/待实践。

## 本地运行与恢复

在本任务工作树根目录、Python 3.10+ 执行：

```bash
python scripts/run_workbench.py
```

启动器生成两家虚构公司、管理员/协作/只读四个账号及随机密码，账号文件为 `.bidradar-data/r1-workbench/demo-accounts.json`，不进仓库。默认页面 `http://127.0.0.1:8765`、内部目录端口8766。浏览器先用 demo.admin，再选择“虚构演示样本”即可完整走查。目录默认“真实公开样本”，未导入时如实为空，不拿虚构记录填充真实结果。

真实公开样本需已有获准的规范包，通过重复 `--bundle /path/to/normalized.json` 导入，不访问来源网站。初次与重启均保留已有账号、档案和选择；`--prepare-only`只准备数据不运行服务。冲突端口用 `--port` 和 `--catalog-port` 指定不同空闲端口，启动器不停止其他进程。

Ctrl+C只停止本次子进程，再运行同一命令恢复；相同规范包重复导入不新增观察。原公共获取证据另在DATA-001本机目录，本服务不提供任意原件下载。开发库故障保留目录、停止服务后由负责人决定处理，不能无提示删库重建。进程中断回滚测试不是备份还原/断电/RPO/RTO的成绩；真实企业接入仍受26门槛约束。

```bash
python -B -W error::ResourceWarning -m unittest discover -s tests -p 'test_*.py' -v
node --check services/workspace/web/app.js
python scripts/refresh_project_memory.py --check
python scripts/check_project_memory.py
git diff --check
```

无第三方Python运行依赖。Node仅用于JS语法检查，运行页面无需Node；HTTP服务与JS静态资源的发布物就是版本库文件。未构建生产镜像，未部署Redis/K8s，不将本地标准库服务器称生产Web服务器。

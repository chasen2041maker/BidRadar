# 29｜SOURCE-002：首源数据获取阶段契约

2026-09-30｜本阶段交付一个完整、可命令运行的CCGP公共资料获取链。用户要求阶段实现、验证和独立审查后统一学习最终版本，替代每个小片暂停；不扩大为付费、账号领取、公司私有材料、模型或部署。

## 范围与完成条件

受控查询/有限分页或明确公告入口 → 原始字节及响应元数据归档 → 列表/正文与附件清单解析 → 稳定公告标识和内容版本 → 持久运行记录、重试/恢复/取消 → 本地JSON结果及离线重放。附件只获取准入范围内允许的公开文件，未知外链/登录/CA/付费保留缺口。固定页面模板外的HTML不得作为已验证公告。

使用标准库CLI及ingestion独有的本地SQLite元数据和内容寻址文件，作为本阶段开发运行后端；不是PostgreSQL/RLS生产验收，不存公司资料，不允许其他服务跨库。数据库、原文和结果位于本地忽略目录。未经来源使用范围批准，真实网络默认关闭；模拟端到端与真实网站验收分开。

## 来源准入与当前证据

09-30少量官方页面只读核查：search.ccgp.gov.cn/bxsearch返回访问频繁提示，中央栏目HTTP403；CN-01正文经浏览工具可读。两域robots未取得，机器读取/保存/再分发条款未确认。当前真实自动发现与准入受阻，不得把搜索摘要、手动URL或虚构HTML计作真实列表验收。证据链接沿用25与28；官方接口通知 https://www.ccgp.gov.cn/zcfg/mof/202403/t20240306_21605100.htm 面向地方财政信息部门申请账号，非匿名接口。

运行需显式本地准入记录：用途、核验时间/依据、批准的主机/路径、附件许可及到期时间；运行时仍查robots和当前访问响应，无法取得规则则拒绝。域名/路径白名单、DNS公网地址、逐跳重定向及下载上限每次检查。403/429/验证码停止，不重试绕过；网络超时和暂时5xx有限重试。TLS验证保持开启，不接受任意地址或浏览器传来的授权字段。

## 输入输出与状态

CLI提供获取、按run_id恢复/取消、查看运行/结果、按capture_id离线重放。关键词/发布日期沿用SOURCE-001；页数/条数为正整数且有限，日期为YYYY-MM-DD自然发布日期而非截止时间。公告以来源+规范URL标识，内容SHA256标识字节版本；未知发布时间/金额/资格不填零或推断。

运行先持久登记并输出run_id，再进行获取。幂等键限定本地存储与请求内容：同键同内容返回同一运行，同键异内容冲突；同URL同字节复用内容版本，但每次获取记录独立。原件不可覆盖，变更新增版本；重新解析保留原输入/解析器版本，不修改原字节。时间全部ISO8601 UTC秒精度，HTTP日期/来源发布文本另存不混用。

运行状态区分queued/running/succeeded/partial/blocked/failed/cancelled；解析状态沿用ok/empty/partial/blocked/parse_error。run完成不等于全国覆盖或全部附件取得。HTTP状态、故障代码、已取得候选/材料、未取得原因均保留。并发运行用数据库租约排他；取消持久化，下一动作前检查，过期执行者不能提交新结果。内置版本v1，不发布HTTP兼容承诺。

## 必须验证

正常查询、空页、多个分页、重复链接、正文和附件、未知字段、同URL内容变化、幂等冲突、中断恢复、取消/并发租约、超时/5xx有限重试、403/429/验证码停止、跨域/私网/重定向/超大文件拒绝、归档校验和离线重放。全部使用虚构样本和隔离目录；真实核验只在有效来源准入下少量执行，不把未执行写通过。

阶段交付精选1–3个核心阅读入口和一条完整数据流，先给实际运行方法与结果，再解释关键设计与失败案例。本人仍待阅读/待实践，旧练习不代做；不因外部站点受限而伪造真实验收。

## 本地运行与验收

Python 3.10+，只用标准库，无需pip、账号、Redis或云资源。先进入当前SOURCE-002分支所在仓库根目录；Windows的`python`与Linux的`python3`按本机解释器选择。

```bash
python -m services.ingestion --help
python -m services.ingestion demo
python -m services.ingestion verify
```

demo完全不联网，运行固定虚构列表两页、同一公告及一个附件签名样本，输出两行JSON：run_registered（先有run_id）及run_report。正常演示4条获取记录、3份唯一字节文件，simulation为true；同一演示键重跑返回同run_id，传输不重做。演示PDF只是签名样本，不是完整PDF内容验收。

默认存储`.bidradar-data/`已忽略，不能提交原件/响应/联系人。`--store`须放在子命令之前，指定空目录或已有专属账本；可信本地用户管理目录，不能作为多用户网络服务直接暴露。SQLite与blobs须一并保留，不手动改原件；verify只核验账本引用的原件存在/哈希，不是备份恢复测试。

以下占位编号替换成自己输出的run_id/capture_id：

```bash
python -m services.ingestion show RUN_ID
python -m services.ingestion replay CAPTURE_ID
python -m services.ingestion cancel RUN_ID
python -m services.ingestion resume RUN_ID
```

show输出当前状态；replay校验字节后新增解析记录，不联网、不覆盖旧解析。取消只能作用queued/running，终态保持不变；取消不会中断已在途的HTTP，但内部每次请求前、等待/DNS后及提交前重验持久租约，覆盖robots/重试/重定向，拒绝后续请求与迟到结果。Ctrl+C尽量释放运行为queued；进程被强杀后等待120秒租约过期再resume。租约过期执行者不能提交，极慢请求可能需要恢复；不承诺每次HTTP恰好一次。

成功/部分/受阻/失败/取消均为终态，resume直接返回已有结果。需要重新获取时使用新幂等键，不能靠换键绕过站点限制；须先解决实际原因并复核准入。

## v1字段与限制

| 输入/输出 | 类型、空值与语义 |
| --- | --- |
| keyword / notice_urls | 1–200字符的非空关键词，或1–20个明确CCGP公告URL，二选一；关键词首尾去空白，明确URL去片段/去重；未用项为null/空数组 |
| pages / max_notices / max_attachments | 整数，分别1–5/1–20/1–10；默认1/10/5；后两者是整个run上限，不是每页/每公告上限 |
| start_date / end_date | 各自为null或YYYY-MM-DD，同时设置时起始不晚于结束；仅关键词搜索可用，沿用来源发布日期语义，不是截止时间；明确URL不接受搜索日期/pages非1 |
| attachments | 布尔，默认false；true仍须policy允许文件类型/路径；不代表所有链接可下载 |
| key | 1–128字符、非全空白字符串；同存储内唯一，大小写敏感；同键异请求报idempotency_conflict，不修改旧run |
| run_id / capture_id / replay id | 随机32位十六进制标识；run_id在联网前输出；不存在报run_not_found/capture_not_found |
| captures / pending | 按本地提交顺序返回获取记录数组，以及本run待处理任务数量；本地v1报告不另分页，规模由run上限约束 |
| notice_id / version_id / sha256 | 来源+规范公告URL的稳定标识、来源URL+字节哈希版本标识、原始字节SHA256；同URL同字节复用版本号，不同字节递增 |
| fetched_at / created_at / updated_at | 取得/记录时间，UTC ISO8601秒；来源published_text、HTTP Last-Modified单独保留，不推测时区、截止时间或金额 |
| http_status / byte_count / sha256 / parsed | 未取得完整响应时可以为null；错误码独立，不能把失败解释为0条结果 |
| parser_version / decoded_input_sha256 | 解析器版本及解码后UTF-8指纹；与原始字节SHA分开，重解析保存新记录 |

CLI stdout为UTF-8 JSON行（--help/参数用法错误除外），本地接口schema_version=1；未发布HTTP、租户API或对外兼容窗口。终态退出码：succeeded=0、partial=2、blocked=3、failed/运行错误=4、cancelled=5、queued/running=6；Ctrl+C=130。argparse用法错误也退出2，但不是run_report；调用方看event/status，不能只凭退出2判断“部分成功”。show同样返回所查询状态对应退出码。replay/verify正常为0。

partial表示至少部分材料已取得但有解析/附件/条数上限缺口；succeeded只表示本次范围内无已知缺口，空页也须来源明确0条，不表示全国覆盖或内容证明有效。附件URL仅登记约定扩展名；下载仅校验签名、拦截伪装HTML，不展开压缩包、不运行内容、不OCR。

HTML上限2MiB/页，附件5MiB/个；每次CLI执行预算50次HTTP（含robots/重定向/失败）、180秒、64MiB累计字节；单次网络10秒、最多2次暂时故障尝试、重定向最多3跳，默认最小请求间隔2秒。robots要求更慢时遵守；超过本地等待预算直接停止，不减少站点要求。恢复是新的执行预算但保留同run范围和历史，终态不恢复。

## 真实准入流程（目前受阻）

来源规则需核实用途、读取/保存条件和有效期；policy只记录已完成的人工核验，字段写true不会产生网站许可。不给当前来源预填批准。下方故意不可执行的模板仅解释格式：

```json
{
  "schema_version": 1,
  "approved": false,
  "purpose": "待确认的使用目的",
  "reviewed_at": null,
  "expires_at": null,
  "evidence": [],
  "attachments_allowed": false,
  "allowed": [
    {"host": "search.ccgp.gov.cn", "path_prefix": "/bxsearch", "kinds": ["listing"]},
    {"host": "www.ccgp.gov.cn", "path_prefix": "/cggg/", "kinds": ["notice"]}
  ]
}
```

核验完成后才由负责人维护本地policy（不要提交内部证据），approved=true、真实purpose/evidence、带时区reviewed_at/expires_at且当前在有效期内；附件如获准须另外明确路径及attachment类别。目录前缀以/结尾，否则精确匹配；允许主机仅CCGP三个登记域，禁止任意域名、凭据、端口和可疑路径。

有效policy下的入口为 `python -m services.ingestion collect --keyword 软件 --key 自己的新运行键 --policy 本地准入文件.json`，或用`--notice-url`替换keyword；可附加前述范围参数。**当前不要用虚构许可运行真实采集**。不传policy可验默认门控：运行先持久登记，继而blocked/policy_required，退出3，网络零请求。每次执行重新取得robots，执行内按origin缓存；robots未知/站点403/429/挑战页均停止，不借浏览器cookie、代理或登录绕过。

## 验证命令与后续边界

```bash
python -m compileall -q services/ingestion
python scripts/refresh_project_memory.py --write
python scripts/refresh_project_memory.py --check
python scripts/check_project_memory.py
python -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

标准库直接运行，无独立打包构建或已配置静态类型/lint工具；compileall只验证语法，契约/故障/集成由测试验证。提交后再执行`python scripts/refresh_project_memory.py --check --base a7c0df934fd312f8719dae86e1bbda646957f6af`及`git diff --check a7c0df934fd312f8719dae86e1bbda646957f6af HEAD`，最终SHA/命令/环境/时间/退出码/独立审查绑定本任务PR。

真实站点补测责任：负责人/来源方确认许可和规则，Codex在访问恢复后少量验证真实列表、分页、正文、附件和归档重放，再决定正式启用。生产持久化、私有鉴权、备份恢复、Worker/Redis/K8s、模型与前端在后续授权阶段各自验收。本阶段可学的最终逻辑见[三个核心入口](learning/SOURCE-002.md)，不因代码完成而预填这些外部验收。

# 30｜DATA-001：第一阶段数据证据与目录契约

2026-09-30。基线7c637f987b1b3de2d79bc706108f0649369e8bb8，延续已合并SOURCE-002。整阶段必须有获准真实输入到可核对目录的证据，不以模拟替代。Redis/K8s及云部署后置，不进入模型或真实企业资料。

## 分层与本地接口

ingestion独占原件/获取账本，输出版本化JSON证据包；processing无状态地规范字段；catalog接收规范观察，独占目录/历史库。三个CLI以JSON文件/stdin交接，不跨库读取，不共享业务ORM。标准库/Python3.10+，无HTTP服务、Docker构建或对外端口；不是生产鉴权接口。

v1证据包最多100条记录（天津最多5页×20行），最多32MiB。raw_evidence_bundle必有source_id、simulation布尔、run_id、run_status、documents、failures。每个文档有获取标识、source_url、source_record_key、identity_kind、observed_at、原字节sha256、parser_version和content。导出拒绝queued/running，验证原件哈希并用当前解析版本重解析；失败也导出明确记录，不把失败空包当零结果。

processing输出normalized_observations v1，冻结normalizer_version、事实与证据、观察哈希。catalog整包验证后事务写入；同观察重复导入不增加历史，未知契约版本拒绝。CLI错误退出4；获取状态沿用0成功、2部分、3阻塞、4失败、5取消、6未结束。导出和处理退出0表示契约处理成功，不把其中的failed/blocked来源状态抹掉。

## 天津首源：已选定，真实契约待账号验收

负责人09-30明确采用天津官方免费接口。核对：[政府采购竞争性谈判公告接口](https://open.data.tj.gov.cn/sjjk/addbe9f2805346c28d4b639311e6e68a.htm)、[网站声明](https://open.data.tj.gov.cn/xgxx/wzsm/index.htm)、[常见问题](https://open.data.tj.gov.cn/hdjl/cjwt/index.htm)、[2022手册](https://open.data.tj.gov.cn/docs/2022-11/d72a65c5684b4f92aea777b947ee944d.pdf)。文档HTTP200读取证据保存于本机忽略目录research；不提交含联系人信息的网页预览。

接口标为无条件开放；网站声明现阶段免费、非排他使用，无条件开放数据可依法使用/传播/分享，成果注明“天津市信息资源统一开放平台”；网站保留应用成果发布审核权，不保证完整/准确/及时。当前用途仅本地开发验证，不据此授权发布产品或转发原数据到公开仓库。

固定API资源：`https://open.data.tj.gov.cn/api/invoke/addbe9f2805346c28d4b639311e6e68a`。官方GET/POST、JSON/UTF-8；实现用GET。`page`是每页条数（官方上限5000，本地限20），`pageNum`是页码，`authToken`是令牌。没有已确认的关键词/排序参数，当前取有限页，在目录按标题筛选；不宣称最新或全量覆盖。默认网络关闭，显式--allow-network才读取凭据，默认位于用户主目录`.bidradar/credentials/tianjin-token.txt`，不得提交。

2022手册称用户中心注册开发者可取token；09-30实际登录后的页面还要求开发者名称、身份、单位/公司、专业、研究领域、邮箱、验证码，由负责人自行如实填写。仅登录不等于已取得API资格。是否立即签发/需审核，以实际结果为准。

准入复核窗口为2026-09-30至2026-10-30（结束不含），过期先重查来源条件。不读取浏览器Cookie/代理，不跳转，不自动重试拒绝。固定域名、公网DNS检查、固定IP/TLS校验、每次请求前取消检查、1秒间隔、单页4MiB/单次5请求、180秒启动后预算；在途请求超时20秒。令牌不进入任务、URL日志、原件元数据；响应回显令牌则拒绝保存。

公开预览只有columnNames/list/totalCount，行被截断，不能作为完整响应。适配器只接受code=200且等宽完整字符串/null行，预览标记或未知格式拒绝并保留无凭据原件，待真实契约升级；HTTP成功不是业务成功。API没有已确认的公告唯一ID/附件URL：目前每条完整记录用内容哈希标为content_snapshot，仅为快照身份，不虚构原公告链接、跨变更稳定ID或材料全文。稳定ID、可用材料/更正及业务时效是尚需真实验收的缺口。

## 事实、身份与时间

CCGP用规范来源URL生成公告身份；天津用来源资源中的内容快照键。source_id和simulation共同进入notice_id，演示不能与真实公告混合。观察固定capture_id、原件哈希、parser/normalizer版本及内容；新获取/重解析另存观察，历史不覆盖。

项目编号、采购方、包号、发布日期、预算、响应截止分别保存value（可null）、status（known/missing/unparsed/conflicting）和原文/locator。没有或冲突不填零，不丢不可解析的第二条证据。预算用十进制字符串/CNY，保留元/万元；最高限价不当预算，金额scope=unspecified，不擅自当项目总额或第一个包的总额。多包表格未有明确映射时留原始行、字段空缺，不求和猜测。

自然日期不补零点；无时区的时刻只存local/timezone=null；原文明确北京时间才用+08:00。获取时间统一UTC。公告类型来自明确标题后缀，未知保留unknown。更正中的日期/预算只留证据不选新/旧值。查询时按as_of判断明确截止是否经过，未到截止也不代表资格满足；中标/终止/意向/更正不是开放招标公告。

## 查询、历史与关系

`catalog query`支持标题关键词、类型、1–100页大小；默认只查真实资料，模拟用--simulation。排序为获取时间降序+notice_id，当前观察按获取时间优先，同时间按接收顺序；晚到旧数据不能回退。游标绑定目录本机、固定接收快照、筛选/as_of，分页期间新导入不改变旧页；改筛选或篡改游标拒绝。recent_runs显示最近导入运行的成功/失败，空目录不能掩盖来源失败。

`catalog detail`返回当前观察、全部历史及更正关系。原公告和更正保留各自身份；明确原公告链接命中一个目标为evidenced，多个为ambiguous；同来源/采购方/项目号仅生成candidate，多目标为ambiguous；链接目标缺失时unresolved，不退回标题猜测。包号已知且不同则不关联。关系绑定两端观察版本，实时计算不把旧关系自动套到新正文，也不改写原公告。

## 操作与验收

本地演示：`python scripts/run_data_demo.py`。三个真实子进程/独立存储+虚构传输，输出目录/金额/幂等摘要，证据保存在`.bidradar-data/demo-chain/`。不是网站接通。

账号配置好后的有限验证：`python -m services.ingestion --store .bidradar-data/tianjin collect-tianjin --key tj-acceptance-01 --pages 2 --page-size 2 --allow-network`。只在用户确认开发者资格/本机凭据就绪后执行。先取得真实响应核实契约、字段、分页/顺序和时效，不盲目扩大采样。

跨CLI：ingestion export RUN_ID → services.processing → catalog import；本地文件均放忽略目录。恢复/取消/重放/verify沿用ingestion入口。Windows管道编码由演示脚本统一UTF-8；手工导出须显式UTF-8，不用PowerShell默认重定向猜编码。

代码验收涵盖类型/金额/日期/冲突、多包保守留缺、凭据泄漏负例、拒绝/预览/零结果、重复/中断/取消/重放、原件损坏、事务/乱序/稳定分页、关系歧义。独立审查绑定最终head。真实接口、稳定公告身份、实际材料与更正、故障/恢复和原件重放未验证前，整个阶段保持未完成。负责人待阅读/待实践，完成后统一学习三个核心文件。

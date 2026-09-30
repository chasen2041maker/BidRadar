# 30｜DATA-001：第一阶段数据证据与目录契约

2026-09-30。基线7c637f987b1b3de2d79bc706108f0649369e8bb8，延续已合并SOURCE-002。整阶段必须有获准真实输入到可核对目录的证据，不以模拟替代。Redis/K8s及云部署后置，不进入模型或真实企业资料。

## 分层与本地接口

ingestion独占原件/获取账本，输出版本化JSON证据包；processing无状态地规范字段；catalog接收规范观察，独占目录/历史库。三个CLI以JSON文件/stdin交接，不跨库读取，不共享业务ORM。标准库/Python3.10+，无HTTP服务、Docker构建或对外端口；不是生产鉴权接口。

v1证据包最多100条记录（天津最多5页×20行），最多32MiB。raw_evidence_bundle必有source_id、simulation布尔、run_id、run_status、documents、failures。每个文档有获取标识、source_url、source_record_key、identity_kind、observed_at、原字节sha256、parser_version和content。导出拒绝queued/running，验证原件哈希并用当前解析版本重解析；失败也导出明确记录，不把失败空包当零结果。

processing当前输出normalized_observations v2，冻结normalizer_version、事实与证据、观察哈希；catalog同时接收明确配套的v1/v2，兼容细则见下文。整包验证后事务写入；同观察重复导入不增加历史，未知契约版本拒绝。CLI错误退出4；获取状态沿用0成功、2部分、3阻塞、4失败、5取消、6未结束。导出和处理退出0表示契约处理成功，不把其中的failed/blocked来源状态抹掉。

## 天津首源：已选定，真实数据响应仍受阻

负责人09-30明确采用天津官方免费接口。核对：[政府采购竞争性谈判公告接口](https://open.data.tj.gov.cn/sjjk/addbe9f2805346c28d4b639311e6e68a.htm)、[网站声明](https://open.data.tj.gov.cn/xgxx/wzsm/index.htm)、[常见问题](https://open.data.tj.gov.cn/hdjl/cjwt/index.htm)、[2022手册](https://open.data.tj.gov.cn/docs/2022-11/d72a65c5684b4f92aea777b947ee944d.pdf)。文档HTTP200读取证据保存于本机忽略目录research；不提交含联系人信息的网页预览。

接口标为无条件开放；网站声明现阶段免费、非排他使用，无条件开放数据可依法使用/传播/分享，成果注明“天津市信息资源统一开放平台”；网站保留应用成果发布审核权，不保证完整/准确/及时。当前用途仅本地开发验证，不据此授权发布产品或转发原数据到公开仓库。

默认谈判API资源：`https://open.data.tj.gov.cn/api/invoke/addbe9f2805346c28d4b639311e6e68a`，另两个固定资源见下文。官方GET/POST、JSON/UTF-8；实现用GET。`page`是每页条数（官方上限5000，本地限20），`pageNum`是页码，`authToken`是令牌。没有已确认的关键词/排序参数，当前取有限页，在目录按标题筛选；不宣称最新或全量覆盖。默认网络关闭，显式--allow-network才读取凭据，默认位于用户主目录`.bidradar/credentials/tianjin-token.txt`，不得提交。

09-30负责人已自行完成账号与开发者注册，并授权使用本机仓库外令牌。注册成功弹窗、重新打开的开发者页面及本机凭据一致；页面显示每日20次、有效期至2026-12-29。该额度按账号共享，不等于每个运行有20次；本地单次5请求限制也不等于已实现账号全局日额度账本，其他程序/官网测试的消耗未知。

真实验证出现平台状态不一致：开发者页确认已注册，官方在线测试先提示需要注册开发者；负责人重新登录后在线测试提示null。重新登录前后两次客户端请求，以及20:56应负责人要求用正式CLI执行的一次请求，均HTTP200、业务code=500，msg为“令牌不合法【失效或被禁用】”。拒绝立即停止后续页，不自动重试；不能据此推断审核未过、注册失败或自行换凭据。需平台确认令牌状态，接口有效前不扩大真实采样。

本机系统DNS把官方域名解析为198.18.1.97（非公网），原公网检查正确拦截。新增显式`--dns-mode google-doh`，默认仍system；只向固定Google HTTPS解析服务查询官方公开域名，不发送令牌，不改系统网络设置，不接受任意解析器/目标，不自动降级。核对Question/Status/截断/重复键、同名A记录、正TTL及全部地址为公网；当前不支持CNAME，遇到即停止。每页重新解析、不缓存IP，固定数字IP连接且TLS校验原域名。依据为[Google官方DoH JSON契约](https://developers.google.com/speed/public-dns/docs/doh/json)。

DNS模式写入任务请求：旧请求缺省为system，恢复时沿用原模式；更换模式须新幂等键，终态仍不可恢复。DoH最多64KiB、10秒、不重试/跳转；单次最多5次DNS HTTPS和5次数据API，二者共用运行180秒启动预算，数据请求前仍检查取消/期限。parser升级tj-open-data-v2，将已实测的code500/明确令牌拒绝文本标为api_credential_rejected，其他业务错误保留api_business_error；旧捕获与旧解析结果不覆盖，重放返回新解析版本。

准入复核窗口为2026-09-30至2026-10-30（结束不含），过期先重查来源条件。不读取浏览器Cookie/代理，不跳转，不自动重试拒绝。固定域名、公网DNS检查、固定IP/TLS校验、每次请求前取消检查、1秒间隔、单页4MiB/单次5请求、180秒启动后预算；在途请求超时20秒。令牌不进入任务、URL日志、原件元数据；响应在字节和JSON解码后的键/字符串中回显令牌则拒绝保存；重复JSON键/非法JSON在安全检查时拒绝归档；HTTP启动前再次检查时间预算。

公开预览只有columnNames/list/totalCount，行被截断，不能作为完整响应。适配器只接受code=200且等宽完整字符串/null行，预览标记或未知格式拒绝并保留无凭据原件，待真实契约升级；HTTP成功不是业务成功。API没有已确认的公告唯一ID；磋商/更正虽有公开附件字段定义，实际URL/下载准入仍未知。目前每条完整记录用内容哈希标为content_snapshot，仅为快照身份，不虚构原公告链接、跨变更稳定ID或材料全文。稳定ID、可用材料/更正及业务时效是尚需真实验收的缺口。

## 事实、身份与时间

### 天津多资源增量契约（公开定义已核实，成功响应仍待验）

CLI新增`--resource negotiation|consultation|correction`，默认negotiation兼容旧请求。只接受固定白名单：negotiation→tj_procurement_negotiation/addbe9f2805346c28d4b639311e6e68a；consultation→tj_procurement_consultation/cafe018abce346a7a34682a33fb6c4c2；correction→tj_procurement_correction/67393436a7cd44f5a896b3e98153f7a7。三者均为市财政局无条件开放资源；API参数相同，仍是每次一个资源、独立运行/分页/预算，不自动遍历所有资源或绕过拒绝。资源身份进入幂等请求、URL和归档版本，恢复沿用原资源，传输与运行资源不匹配时拒绝。

原始证据包继续v1，接口响应仍按现有严格暂定解析器，不因为多资源接入就声称已核实真实成功结构。内容快照的source_id仍按具体资源区分，不把同项目号的原公告、更正、合同合并为一个身份。公开[更正定义](https://open.data.tj.gov.cn/sjjk/67393436a7cd44f5a896b3e98153f7a7.htm)有“采购项目编号/首次公告日期/原合同公告链接/更正附件链接”，[磋商定义](https://open.data.tj.gov.cn/sjjk/cafe018abce346a7a34682a33fb6c4c2.htm)有“项目编号/包号/其他附件文件下载链接”；这些只说明字段角色，不保证实际有值或实际可下载。

processing改出normalized_observations **schema_version=2 / procurement-facts-v2**：保留v1所有字段，facts追加original_published_at（首次公告日期；与published_at同类型/空值规则）和original_contract_reference（原合同公告链接的原始文本事实，不执行、不当原采购公告URL）；每条观察追加material_reference_evidence数组，元素为label/text/locator，保留两种天津附件字段原文，未解析成下载授权。material_status为reference_only表示存在待核实字段，attachment_reference_missing表示已声明该字段但本条无引用；原未提供字段的资源保持not_provided_by_api。

类型判定结合固定资源分类：标题未知时可采用该资源的采购/更正分类；更正资源与标题明确其他类型冲突时保留unknown，不生成采购关联。更正中的预算/截止等仍不猜新旧作用域，首次公告日期单独处理。catalog兼容读取/导入v1原两来源（schema1+normalizer-v1）和v2（schema2+normalizer-v2），拒绝混搭版本/字段或未知来源；观察哈希固定新算法版本，旧观察不改写。当前本地迁移窗口同时支持v1/v2，未设淘汰日期；SQL表结构无需改动。

跨资源关系只对明确列出的三个天津来源视为同一发布方，不按字符串前缀或项目号自动合并；与CCGP、模拟/真实仍隔离。同采购方+项目号最多candidate，多原文为ambiguous，已知包号冲突排除。有原合同引用时停止建立原采购公告候选并返回unresolved/original_contract_outside_scope，即使字段只是未解析文本也不悄悄回落猜关联；合同公告接入不在本增量内。真实原件、附件地址准入与稳定公告号仍须实测，不把引用字段当已下载材料。

CCGP用规范来源URL生成公告身份；天津用来源资源中的内容快照键。source_id和simulation共同进入notice_id，演示不能与真实公告混合。观察固定capture_id、原件哈希、parser/normalizer版本及内容；新获取/重解析另存观察，历史不覆盖。

项目编号、采购方、包号、发布日期、预算、响应截止分别保存value（可null）、status（known/missing/unparsed/conflicting）和原文/locator。没有或冲突不填零，不丢不可解析的第二条证据。预算用十进制字符串/CNY，保留元/万元；最高限价不当预算，金额scope=unspecified，不擅自当项目总额或第一个包的总额。多包表格未有明确映射时留原始行、字段空缺，不求和猜测。

自然日期不补零点；无时区的时刻只存local/timezone=null；原文明确北京时间才用+08:00。获取时间统一UTC。公告类型优先看明确标题后缀，天津固定资源按上文补分类；仍无法判定则unknown。更正中的published_at/响应截止/预算只留证据不选新旧值，首次公告日期单存original_published_at。查询时按as_of判断明确截止是否经过，未到截止也不代表资格满足；中标/终止/意向/更正不是开放招标公告。

## 查询、历史与关系

`catalog query`支持标题关键词、类型、1–100页大小；默认只查真实资料，模拟用--simulation。排序为获取时间降序+notice_id，当前观察按获取时间优先，同时间按接收顺序；晚到旧数据不能回退。游标绑定目录本机、固定接收快照、筛选/as_of，分页期间新导入不改变旧页；改筛选或篡改游标拒绝。recent_runs显示最近导入运行的成功/失败，空目录不能掩盖来源失败。

`catalog detail`返回当前观察、全部历史及更正关系。原公告和更正保留各自身份；明确原公告链接命中一个目标为evidenced，多个为ambiguous；同来源或明确同发布方资源/采购方/项目号仅生成candidate，多目标为ambiguous；链接目标缺失时unresolved，不退回标题猜测。包号已知且不同则不关联；显式链接与包号冲突返回conflicting依据。关系绑定两端观察版本，实时计算不把旧关系自动套到新正文，也不改写原公告。

## 操作与验收

本地演示：`python scripts/run_data_demo.py`。三个真实子进程/独立存储+虚构传输，输出目录/金额/幂等摘要，证据保存在`.bidradar-data/demo-chain/`。不是网站接通。

账号配置好后的有限验证：`python -m services.ingestion --store .bidradar-data/tianjin collect-tianjin --key tj-acceptance-01 --pages 2 --page-size 2 --allow-network`。只在用户确认开发者资格/本机凭据就绪后执行。先取得真实响应核实契约、字段、分页/顺序和时效，不盲目扩大采样。

跨CLI：ingestion export RUN_ID → services.processing → catalog import；本地文件均放忽略目录。恢复/取消/重放/verify沿用ingestion入口。Windows管道编码由演示脚本统一UTF-8；手工导出须显式UTF-8，不用PowerShell默认重定向猜编码。

代码验收涵盖类型/金额/日期/冲突、多包保守留缺、凭据泄漏负例、拒绝/预览/零结果、重复/中断/取消/重放、原件损坏、事务/乱序/稳定分页、关系歧义。独立审查绑定最终head。真实接口、稳定公告身份、实际材料与更正、故障/恢复和原件重放未验证前，整个阶段保持未完成。负责人待阅读/待实践，完成后统一学习三个核心文件。

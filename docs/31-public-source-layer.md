# 31｜第一层公开采购来源获取

2026-09-30，DATA-001。负责人要求在这一方向完整完成第一层，再统一学习最终代码；第一层是公开商机发现、公告与可取得材料的证据获取，不是全国全量历史或登录后完整标书覆盖。[总契约](30-r0-data-contract.md)中的服务独立存储、历史和JSON边界继续适用。

## 来源与用途

中国政府采购网（`cn_ccgp`）作为发现主源；海南省公共资源交易平台（`cn_hainan`）作为补充源。标题之外继续核对正文技术需求，不能只用采购品目筛除软件项目。天津三个官方API保留，但平台令牌拒绝独立列为阻塞；湖北广电样本没有验证出当前相关软件商机，只作为研究负例，不列业务主源。

本次授权是有限公开公告的本地开发与留证，不发布原文、不申请标书、不购买、不使用账号或Cookie，不承诺持续生产许可。财政部[101号令第十五条](https://tfs.mof.gov.cn/caizhengbuling/201912/t20191210_3439068.htm)规定公众免费查阅政府采购信息，不据此推导任意批量商业复制授权。两站当前完整使用/版权声明尚未取得完整核验证据；接入对外服务前须复核。本地真实原件含公开联系方式，仅放忽略的专属目录并限制用途；阶段验收后按需要复核保留范围，不把原件永久保存作为默认承诺。

样本已证明公开公告可免登录读取；有的完整文件需提交盖章材料、平台登录、CA或购买。获取门槛作为证据保存，不能因为文件价格为0就声称文件已取得。所有外部链接都是数据，只能进入固定来源、当前policy允许的路径，不能指挥请求任意主机。

## 请求、分页与传输

新增`collect-public`，旧`collect`与天津CLI继续兼容。请求`schema_version=2`且`discovery=public_category`，包含source_id、category或notice_urls、start_page、pages、max_notices、max_attachments、attachments、title_terms、dns_mode。明确公告与栏目二选一；最多5页、20正文、10附件，页码从1开始。栏目路径与页码必须由已核实的模板构造，未知分页拒绝而不猜测。列表页仍完整归档；标题词按“或”过滤正文入队，计数分别记录筛除与运行上限，不宣传为全站搜索或完整覆盖。

`--allow-network`默认关闭；有效policy仍是另一个必要条件。DNS默认system；显式google-doh仅向固定服务查询固定官方域名，不携带用户资料、不改系统设置、不自动降级。支持至多8条CNAME后到公网A记录，拒绝循环、不连通附加记录、混合别名、私网、重复JSON键及截断；每次重新查询并固定IP/TLS原域名校验。DoH和业务读取共用运行时限；不将解析成功作为网站准入。

policy v1保留严格robots与旧来源路径。v2允许固定公开栏目/海南来源，新增`robots_missing: deny|allow_404`，缺省deny。只有明确reviewed的v2可将404解释成没有可用robots规则；[RFC9309 §2.3.1.3](https://www.rfc-editor.org/rfc/rfc9309.html#section-2.3.1.3)支持这一处理，[§1](https://www.rfc-editor.org/rfc/rfc9309.html#section-1)同时说明robots不是访问授权。401/403/429、挑战页和明确Disallow均停止；网络/5xx不能继承404许可。生效/过期、用途、证据、附件许可、host/path仍每次检查。新版不会把旧policy自动升级。

运行先持久登记，再获取、校验原件、解析、排入有限后继；取消、恢复与已完成任务不重复访问沿用已有账本。终态失败不自动重试；改变来源/页段/DNS必须新请求与幂等键。保存原公告、更正、成交的独立身份及版本，不把新公告覆盖旧公告。

海南列表使用HTTP:80公告链接，09-30 22:43实测同路径官方301指向HTTPS:443，已有HTTPS正文200证据。因此海南公告身份统一HTTPS并省略协议默认端口，避免列表发现与手工URL产生重复；非默认端口仍拒绝。CCGP旧HTTP/HTTPS身份不追溯合并。附件保留原链接，只有当前策略允许的同域安全跳转可继续。

## 输出与验收

原件证据包仍v1；公开解析版本升级`public-acquisition-v3`，支持旧CCGP搜索、固定栏目和海南正文。导出按所属来源重解析并验证每份原件SHA256；processing接收JSON，catalog只接规范观察，禁止跨库读取。

本增量规范观察v3保留旧facts结构，增加`evidence_fields`：金额角色/冲突、获取窗口、登录/申请/付费等门槛、文件实际取得状态、分类/技术/资格/交付的原文与定位。v1/v2继续可读；同获取时刻当前解释v3优先于v2/v1，历史仍在。不得用关键词证据宣布供应商符合资格。

配套版本固定为`schema_version=3 / normalizer_version=procurement-facts-v3`，不接受混搭。`money`为数组，每项role/status/amount/currency/source_unit/package/unit_basis/evidence；金额仍是十进制字符串或null，包号与计量基础未知为null。`budget_assessment`为status/reasons/evidence，发生冲突、单价或多包作用域不清时，旧facts.budget也不能继续输出已知总额。`acquisition_window`为status/value/evidence，value是含start/end的日期对象或null，日期精度/时区仍按原文，不补零点。

获取窗口只在同段恰好两个日期token均有效且无延期/分批歧义时确认；非法日期不丢弃后再用第三项补位，多窗口不静默挑第一批。未知或冲突仍保留完整原文及定位，等待人工核对。

`access_conditions`为kind/status/evidence数组；`material_availability`含status和references，引用含url/name/locator/status/capture_id/raw_sha256，未取得时后两项null。只有导出提供实际成功捕获及哈希才计作取得；obtained仅表示已列引用均归档，不保证完整采购文件齐全。分类、技术、资格、交付各为原文定位数组，不能由这几组关键词得出公司满足条件。目录关键词可匹配标题、已知编号/买方与技术原文；仍是确定性检索，不调用模型。

结果公告与更正可形成同发布方、买方和项目编号的候选前序关系；原件链接支持更强证据等级，但已知包号冲突仍阻断。关系绑定两端观察版本，不合并公告或静默更新原截止。海南正文无发布日期时可引用同次已归档列表的日期，定位注明列表capture_id/SHA/行；明确正文入口无列表证据就保持missing。

必须通过真实原件到目录的整链，以及离线重放、重复导入、有限分页、拒绝和取消恢复测试。宁都1万元/100万元冲突、医疗云按人次单价、海南多包金额和更正→成交是业务回归样本；真实原文仅在本地，仓库测试用合成数据。验收记录必须区分模拟、离线真实原件和实时网络。实现/测试通过与负责人待阅读/待实践分开；最终审查后统一阅读，不用中间片段代替阶段交付。

## 本地运行与阅读

统一入口`python scripts/run_public_chain.py --help`。它顺序调用三个CLI，输出本次运行状态、导入计数、重复导入计数与本地证据目录；每次调用独立日志，失败不会覆盖上次成功清单。当前机器已审核policy在忽略目录`.bidradar-data/public-policy.json`，仅本地用途且有复核期限；文件不是公开仓库内容，不读取天津凭据。

```powershell
python scripts/run_public_chain.py --source ccgp --category zygg/jzxcs --pages 2 --max-notices 10 --title-term 软件 --title-term 智能 --title-term 平台 --key my-ccgp-sample-01 --policy .bidradar-data/public-policy.json --allow-network --dns-mode google-doh
python scripts/run_public_chain.py --source hainan --category cggg --pages 2 --max-notices 10 --title-term 平台 --title-term 云 --key my-hainan-sample-01 --policy .bidradar-data/public-policy.json --allow-network --dns-mode google-doh
python -m services.catalog --store .bidradar-data/public-chain/catalog query --keyword 智能体
```

`--notice-url`可替代栏目，明确URL入口不接受标题过滤或多页。`--attachments`仅对policy批准的具体文件路径生效，其他引用保留未授权状态。相同key与完全相同参数返回已有run；若此前中断则继续该run，若已终态则不再取网页。调整参数须另给key；不是用换key绕过来源拒绝。默认网络关闭，省略`--allow-network`会持久记阻塞。目录查询、重放与重复导入均不调用模型或网站。

退出0表示该运行声明的获取任务成功，2表示部分（如空附件链接或文件未授权），3阻塞，4失败，5取消。单次运行成功不等于标书齐全、资格满足或项目尚可报名。真实验收得到12条独立公告及1份公开PDF；当前成绩与遗漏见[DATA-001笔记](learning/DATA-001.md)，最终HEAD检查与独审见[PR #11](https://github.com/chasen2041maker/BidRadar/pull/11)。

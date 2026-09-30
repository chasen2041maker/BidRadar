# DATA-001｜真实来源与证据目录

摘要：第一层新增CCGP/海南有限发现、公开正文/附件、v3业务事实与目录整链；真实12条独立公告和1份PDF通过语义、幂等及原件校验。天津令牌拒绝仍保留；受限标书和生产发布不在已完成范围，最终head/全测/独审见PR #11。

## 本次改变

延续main 7c637f987b1b3de2d79bc706108f0649369e8bb8（PR #10）。Redis/K8s移至后续部署，按负责人最新目标完成整个数据阶段后集中学习。新增天津固定资源适配器、CCGP段落/表格证据、JSON导出、processing纯函数与catalog独立SQLite/CLI。中文注释解释身份、时区、金额口径、事务、失败和令牌边界。详细来源证据/命令/契约见[30](../30-r0-data-contract.md)。

## 阅读顺序

1. [pipeline.py](../../services/ingestion/pipeline.py)的public_request_spec、execute、replay：有限来源请求如何先登记，再归档、解析、恢复和离线重放；来源模板与传输由独立模块负责。
2. [normalize.py](../../services/processing/normalize.py)的normalize_bundle、_fact：如何从带位置的证据生成可空/冲突事实，而不是从一段文本猜投标结论。
3. [store.py](../../services/catalog/store.py)的Catalog.import_bundle/query/detail：事务/幂等、旧观察晚到、快照分页、更正候选关系。

以上三个文件为本阶段集中带读入口；金额/获取窗口的细节从normalize_bundle进入evidence_fields.extract。最终审查/提交见PR，不把合并或AI验证当本人已掌握。

## 数据流与失败边界

持久任务→固定API/受控CCGP获取→无凭据原字节和哈希→JSON证据包→规范观察→目录独立库→query/detail。processing/catalog不打开ingestion数据库。演示与真实身份分开；API快照没有稳定公告号时不伪造跨变更身份。未知契约拒绝，预览截断拒绝，失败包和成功零行分开。更正日期/金额不猜新旧作用域；原公告不改写。

## 验证与未验证

2026-09-30T20:20:42+08:00，工作目录为独立source-review/BidRadar工作树；Windows11 22621、Python3.13.12、SQLite3.51.1、标准库。被测为上述基线上的DATA-001工作树，尚未提交的成绩不冒充最终head。

- `python -m unittest discover -s tests -p 'test_*.py' -q`：退出0，242项（旧200+目录链23+天津19），全部离线/虚构响应。
- `python scripts/run_data_demo.py`：退出0，三个CLI子进程通过；目录1条、预算123400.00元、重复导入新增0、历史1。完整演示日志位于忽略目录`.bidradar-data/demo-chain/`，不提交原文。
- `git diff --check`：退出0；最终提交后须补基线差异、索引/结构、最终head全测和独立审查，结果放PR/运行摘要。

以上20:20离线记录保留为历史版本。随后本人完成开发者注册，凭据存仓库外；20:30及重新登录后的20:42两次客户端API均HTTP200/code500，返回令牌失效/禁用。开发者页面确认已注册、每日20次，官方在线测试先报未注册、重新登录后报null，根因不能凭客户端推断。实际记录见下节。真实成功响应、原公告唯一ID、材料、更正及业务时效仍待确认；CCGP限制保留。前端/HTTP私有鉴权、Redis/K8s/云及生产均未做。

## 我的参与

负责人明确采用天津官方免费接口，亲自完成平台账号与开发者注册、重新登录，并授权使用令牌。代码仍待阅读/待修改/待运行/待能解释，不把账号操作或AI测试等同代码掌握。练习保留：阶段验收后亲自给虚构预算添加一条矛盾证据，先预测目录状态再运行测试；尚未代做这项本人练习。

## 值得保留的决定与坑

- 天津page是条数、pageNum是页码；预览列数与行宽不一致是截断，不能用补空方式伪装完整API结果。
- 来源更新日期不是公告发布日期，更不是仍可投标。无时区的截止不自动补北京时间。
- 天津完整记录暂用内容快照身份；没有官方唯一ID不擅自用项目编号合并公告。更正仍是独立公告，项目号/采购方只支持候选关联。
- 失败且没有记录时，目录还要显示recent_runs，否则空目录容易掩盖采集失败。
- 不把令牌写命令行、查询日志、幂等键或公开PR；响应回显也拒绝保留。右侧浏览器与未连接的Edge是不同控制通道。

补充回归：不规范千分位保留为unparsed；API空页/短页与totalCount矛盾时明确失败，不当作成功零结果。开发过程曾触发AGENTS体积检查，压缩同义文字后通过，没有提高限额。

独立审查发现并已补修复/回归：JSON转义回显令牌需解码后逐层检查且拒绝落原件；明确原公告链接仍须挡住已知包号冲突；DNS/等待后实际HTTP前重查时间预算；天津证据定位必须为原数组行列下标。全242项离线通过，最终新head仍需审核者复验。

令牌检查追加重复JSON键负例：标准解码会丢弃早值，现用object_pairs_hook拒绝重复键，防转义令牌藏在被丢弃的值中。该响应安全检查失败，不落原件。

代码head cbac679e29c84eb2942f19ae0dbe71d1a5e1cbf0已在2026-09-30 20:21～20:22 +08:00由独立审核者复验通过，全242项及临时目录三CLI演示通过。后续仅校准README中旧Redis/K8s时机、来源选择和阶段节奏，状态指向草稿PR #11；没有变更代码/测试。最终提交/CI/独审报告见[PR #11](https://github.com/chasen2041maker/BidRadar/pull/11)，不为写入自身SHA无限提交。

## 注册后的真实排错与增量

上述文档版406888e8d9db2ca0fa953e02e4e6d65be7f0ea57之后新增代码：`tianjin.resolve_public_dns`只接受官方域名的直接公网A记录，DoH固定服务、TLS验证、响应/时间上限、无跳转/重试，不把198.18.1.97放进公网白名单。CLI显式选择并持久化DNS模式，恢复不能暗中换模式。`parse_page`的v2精确区分已观察的令牌拒绝，不猜未知业务错误；执行器将具体失败码交给证据包。8项回归覆盖DNS污染/错域/截断/重复键/限额/禁网/恢复模式和拒绝传播。

真实数据请求在406888e代码上执行：20:30:15 +08:00运行de16ec9c25d141e49f47f040a43356ec；重新登录后20:42:12 +08:00运行7bfee7dbca954bda85de614dfb5da9a0。均单次正式API调用、HTTP200、72字节同一拒绝响应，SHA256为081e4b012b5fdda5f6607fb6aa7483e13547d7ad552cb687b936e9c63081d86e。第一个系统DNS尝试在发送HTTP前被拦，不消耗API请求；官网测试消耗未知，不冒充全账号剩余额度。

20:46:40～20:46:41 +08:00，在同一source-review工作树、Windows11/Python3.13.12/SQLite3.51.1，执行本机忽略脚本`python .bidradar-data/data-001-real-checks.py`，退出0。仅额外查询一次公开DoH，不调用采购API。七个子命令依次为ingestion verify/export、processing、catalog import两次/query、ingestion replay，均退出0；日志`.bidradar-data/data-001-real-checks.json`包含完整参数、时间、当时HEAD与未提交标记。目录total=0但recent_runs=blocked/failures=1；重放得到v2令牌拒绝，原件SHA与旧捕获完全不变。它只证明真实失败传播，不证明真实采购数据取得。最终提交后必要检查/独审另见PR。

公开资源目录另核实[政府采购更正公告](https://open.data.tj.gov.cn/sjjk/67393436a7cd44f5a896b3e98153f7a7.htm)，以及有“其他附件文件下载链接”字段的[政府采购磋商公告](https://open.data.tj.gov.cn/sjjk/cafe018abce346a7a34682a33fb6c4c2.htm)。当时仅有字段定义、未接入；后续多资源实现见下节。至今未取得成功API数据，不能把字段定义当作已取得附件或稳定公告ID。

本次代码head 558d08da4b3b662b45880a892da12bb3efbafd42已独立审查通过：全250项及DoH/取消/期限/凭据隔离组合均通过。审查提示README仍有“待开发者凭据”的旧表述，现同步为“凭据已配置、平台拒绝”，仅文档校准，代码未变。最终文档head与CI/复核记录见PR，不以旧head成绩替代。

## 多资源与证据语义增量

在0b837cd2bd900f10c4f01be417527573ecd78ff9之后，先补[30](../30-r0-data-contract.md)增量契约，再增加三个固定资源选择/持久化/恢复，禁止传输资源与运行身份不一致。processing产出v2，目录保留v1兼容；旧观察不重写。更正与原公告可跨已核实的同发布方资源形成candidate/ambiguous，不跨CCGP、模拟标记或已知包号冲突；“原合同公告链接”不能被误当采购原公告，即使同项目号也返回待核实。首次公告日期单存，附件字段保留原文/定位，空白不标存在材料；没有请求这些地址。

阅读入口仍为上面三个核心文件；本次重点tianjin.request_spec/execute的资源绑定、normalize_bundle的新事实角色、catalog.relationships的发布方与合同引用判断。失败例见test_tianjin_resources：私网地址即使出现在附件字段中，也只作为文本证据，不触发下载；相同项目号不能证明不同发布方是同公告。

2026-09-30 21:14:46～21:14:50 +08:00，source-review/BidRadar工作树，Windows11 22621/Python3.13.12/SQLite3.51.1/标准库，父提交0b837cd加未提交增量：`python -m unittest discover -s tests -p 'test_*.py' -v`退出0，全263项；`python scripts/run_data_demo.py --root .bidradar-data/demo-resources-v2`退出0，独立新目录三CLI演示1条/重复0新增/历史1/123400.00元。日志`.bidradar-data/data-001-resource-working-checks.json`含实际时间/环境/命令/dirty状态。最终提交检查/审查另放PR，不套用父提交审查。

本轮无新数据API请求。上一正式CLI请求在0b837cd上于20:56:02～20:56:03 +08:00，key=tj-real-acceptance-04-final-cli，1页×1条/google-doh，退出3；run=9ac8a68cca1d43e3b54f3f1e7dcfe0ec，仍为api_credential_rejected，与前两次同一72字节拒绝哈希，脱敏日志`.bidradar-data/tj-real-acceptance-04.json`。未完成项仍包括真实成功解析、稳定身份、天津附件下载适配、真实原公告/更正及整链验收；不是只等令牌就自动完工。

独立审查5e64617fd34b93043d46828f89405e506bd5946c发现一项P2：先导入含原合同引用的v2，再导入同一捕获的v1，接收序号会使旧解释成为当前并错误恢复采购候选。已统一当前/详情的排序：获取时间优先，同时间按明确v2>v1，再按接收顺序；先截断分页快照再排序。SQLite连接每次注册本地纯函数比较规范版本，无可选JSON扩展依赖、不改写旧payload。新增逆序导入、数据库重开、查询/关系、固定旧快照及真正新获取仍优先的回归。21:23:21～21:23:25 +08:00，同环境/工作目录、5e64617加未提交修复，全265项退出0；日志`.bidradar-data/data-001-version-rank-checks.json`。最终SHA须重新独审，不能沿用被拒版本结论。

## 令牌申请路径与客户端差分诊断

代码head 3cb9b1c6fac197abe29581c455543ff8215ba7ba最终独审通过，P2已关闭；同SHA全265项/三CLI/完整基线检查/两项CI均通过，完整记录见PR。随后21:30:28～21:30:29 +08:00只做一次延迟正式CLI验证，仍blocked/api_credential_rejected；run=8cfc768502a64762aea28587ed959378，72字节拒绝哈希与前次相同。记录`.bidradar-data/tj-real-acceptance-05.json`。目标资源文件下载标签未开放，定向查找也未找到同类完整公告下载，不据此声称穷尽全平台。

负责人随后要求排查是否申请错误。已登录用户中心确认账号类型个人、状态正常；开发者页面明确已注册，当前令牌与此前核对值一致；我的申请为空。独立文档核查依据[官方技术规范](https://open.data.tj.gov.cn/docs/2022-11/d72a65c5684b4f92aea777b947ee944d.pdf)第14、23页：个人用户注册开发者自动取得调用令牌；第15页示例用GET query的authToken；第24页申请仅限有条件开放。当前资源无条件开放，不需另申authToken或先提交应用。没有发现必须等待审核/激活、换企业账号或重签域名令牌的官方依据。因此申请路径符合说明，不能把接口拒绝归咎于申请错误。

同代码/Windows11/Python3.13.12/SQLite3.51.1，在source-review/BidRadar于21:37:35～21:37:37 +08:00执行本机一次性诊断`python .bidradar-data/tj-token-method-diagnostic.py`（PYTHONPATH=工作树，UTF-8），脚本退出0表示诊断执行完毕；两项业务均blocked，非采集成功。官方允许GET/POST但未说明POST编码，此处只以form-urlencoded做方法对照，不升级正式适配器契约。谈判POST表单、磋商GET各1页×1条，各一次请求，均HTTP200/72字节同一令牌拒绝哈希；脱敏日志`.bidradar-data/tj-token-method-diagnostic.json`。累计6次客户端数据API调用，官网测试消耗未知；原字节均先经现有凭据回显检查再保存到忽略目录。

独立wire审计在内存mock socket/TLS/凭据读取，3资源×普通/特殊字符虚构令牌共6例：路径、page/pageNum、authToken大小写与解码还原、Host/TLS SNI/443、BOM/CRLF处理均符合调用契约；不读真实凭据、不联网、不改代码。已登录官网在线测试仍弹null；公开页面代码仅在测试地址生成接口返回status=0后才调用数据API，null弹窗来自该生成步骤的错误分支。平台登记与接口鉴权状态不一致是当前诊断方向，具体根因仍未知；没有为使测试通过而换字段、放宽鉴权或重复注册。本轮无需新增实现，阅读/实践状态不变。

负责人明确授权“允许提交脱敏反馈”后，通过当前天津账号在谈判资源页提交一次“其他”类别纠错，正文仅含资源、参数名、错误及排查请求，不含令牌值或联系人信息。2026-09-30 21:46:44 +08:00回读“我的纠错”及“查看”：唯一匹配记录为待处理，三段正文与获准草稿一致，平台反馈为“--”。平台列表显示创建时间21:36:27，与本地观察时间分开记录，不据此推断实际发送时刻；未重复提交。脱敏观察记录`.bidradar-data/tj-platform-feedback-result.json`，草稿`.bidradar-data/tj-platform-feedback-draft.txt`；UI操作无进程退出码。平台尚未答复，未恢复接口，目标仍未完成；无代码/测试变化，不重复执行未变的265项，本轮执行记忆一致性与差异检查。

## 免登录官方来源的有限测试

负责人追加要求同时寻找其他免登录来源测试。首个实际成功候选为湖北省广播电视局[政府采购公告开放接口说明](https://gdj.hubei.gov.cn/sjkf/sjkf/api/202011/t20201119_3039346.shtml)，标为无条件开放、无需条件直接开放，页面公开链接[采购JSON](https://gdj.hubei.gov.cn/zfxxgk/fdzdgknr/zfcg/list.json)。模板另写POST及“请登录后查看接口信息”，本轮只验证该公开静态链接的匿名GET，不声称POST契约或完整开放接口已经核实。未改页面状态、构造账号或绕过登录。仅本机有限样本研究，不扩大为全站抓取、原文再分发或模型处理；[隐私政策](https://gdj.hubei.gov.cn/qtmb/yszc/202209/t20220927_4324162.shtml)另有限制个人信息的收集传播，样本含联系人，原字节仅存忽略目录，公共记录不含该正文。

2026-09-30 21:50:48～21:50:49 +08:00，head debdc6b63a1f64c79ae3aa94251f9df9f0a21a35，source-review/BidRadar根目录，Windows11/Python3.13.12/标准库，`python .bidradar-data/research_anon_probe.py`（PYTHONPATH=工作树）退出0。固定官方域名、公开DNS查询并校验公网IP、原域名TLS、无Cookie/Token/代理/跳转，每URL一次、20秒/1MiB上限。robots返回404，不视为明确规则或持续采集许可；公开JSON匿名GET为HTTP200/application/json、134886字节、10条，SHA256=`27acbf77aab1c88db64ddf491569f40937e2934e6c4fab92bb77074757fe4371`。实际字段DocId/URL/TITLE/DOCHTMLCON/DOCRELTIME；其中正文为HTML字符串。完整脱敏诊断`.bidradar-data/research/hubei-probe-result.json`，真实字节`.bidradar-data/research/hubei-procurement-list.json`。这是一次性研究脚本，不修改正式传输器的准入规则。

21:52:08同目录/版本离线结构检查（`python -`标准库断言，退出0）：10行五字段均非空字符串、DocId唯一且与URL尾部对应、发布时间均可解析、正文非空；输入时间没有时区，不自行补+08。6条标题为成交公告，10条正文均无a链接。结果`.bidradar-data/research/hubei-sample-checks.json`。抽查[6019515采购公告](https://gdj.hubei.gov.cn/zfxxgk/fdzdgknr/zfcg/202609/t20260921_6019515.shtml)明确文件线下领取，无可下载附件；列表未显示分页/总数，不猜接口参数，不声称覆盖全量、持续更新或现行软件商机。可作为真实列表/正文/身份和状态负例候选，尚无正式适配器、原件账本导出、目录导入及真实更正整链验收。

两项独立只读候选核查未产生更完整路线：[浙江税务局意向详情](https://zhejiang.chinatax.gov.cn/art/2026/2/10/art_11895_649206.html)匿名正文可读，但栏目为动态列表、样本是采购意向而非招标、未取得材料/持续采集依据；[全国公共资源查询](https://www.ggzy.gov.cn/deal/dealList.html?HEADER_DEAL_TYPE=02)模板含验证码，当前公告单页虽可请求，全文/附件及再利用条件未核实。[全国网站声明](https://www.ggzy.gov.cn/home/webStated.html)仅指向原发布平台优先，不能充当采集许可。广东robots403，福建robots返回HTML，均停止该入口探测。这些是候选排除证据，不写成正式来源联调通过；不绕过访问限制、不登录新站、不启用轮询，优先保留湖北明确开放的样本路线。

## CCGP/海南公开来源完整第一层增量

09-30负责人要求沿软件/AI真实商机方向完成第一层。以26df180为本轮已审基线，三个独立工作树分别实现模板、事实/root集成，最终统一审查；没有引入Redis/K8s、模型、企业私有資料或生产部署。新契约与命令见[31](../31-public-source-layer.md)。

`collect-public`有显式联网、有限页段/标题筛选、DNS模式和来源规则；正常、部分、阻塞分别持久保存。v2 policy只在明确复核时允许robots404，v1默认不变；公网DoH校验有界CNAME链，不接受私网或任意目标。海南HTTP:80经官方301升级为同路径HTTPS:443，因此该来源统一HTTPS身份；CCGP旧身份保持。栏目误用jzxtp的开发期问题已核官方链接修为jzxtpgg，不能凭熟悉名称猜路径。

公开解析保留嵌套表格行、空附件href和未知模板；不会用标题冒充正文。海南无正文发布日期时，可引用同次归档列表的capture_id+SHA+行位置；直接正文入口无该证据仍为空。规范v3保持旧facts并添加金额角色/获取窗口/门槛/材料状态/资格与交付原文；同获取时间v3优先，v1/v2继续可读，不重写历史。公告类型和响应截止只解释公开状态，不证明公司能投。

真实正式获取在source-review工作树进行，Windows11/Python3.13.12/标准库、无Cookie/令牌：两站各2列表页/5正文；补海南澄清292834、成交293379、医保云293439，合计12个公告身份。精确授权成交PDF下载成功，SHA256为8c597daf62d6fdb6a6438c19c8b5ddd146e76a456bbf9940fe71ee2cb9ec0ec3；同页另外两份引用未授权下载，故材料为partially_obtained。完整标书仍有申请/登录/付费门槛，不把公开公告PDF当完整标书。

运行记录在本机忽略目录`.bidradar-data/public-chain/runs/<run>/<attempt>/`：acquisition/evidence/normalized/import/repeat-import/query/replay与manifest。manifest含实际命令、环境、开始/结束时间、HEAD/dirty标记、退出码和文件指纹，部分状态退出2不是失败空包。CCGP发现run=303a7d9f802f41cb9420aae291f64ccb；海南发现=a87d5e589d0b4fb1b5f7d1b567998f15；生命周期=5f606b80770745c392c6b0f1ba490585。采集当时是76f2392加root未提交变更，首次整链导入为c072e55加未提交变更，不能套成最终head联网成绩；最终重测/重放/提交证据放PR。

`.bidradar-data/check_public_real.py`（仅本地验收脚本，不是产品模块）以三个CLI输出检查：宁都1万/100万冲突不选值；甘肃48万、10月19日14:30、文件费500元；医保云0预算不当总价、5元/人次；遥感C包限价255600元不混总额；采购/澄清/成交独立身份与候选版本关系；公开PDF、原件和所有完成清单指纹。执行时间/版本/命令/结果见`.bidradar-data/public-real-acceptance.json`，9项真实原件离线语义检查退出0，无新网络。

独立预审发现整链脚本重复调用会自哈希旧manifest并覆盖，以及created=False不能证明复用终态；已改每次独立UUID目录、排除自身哈希、字段明确reused_existing_run，新增重复/中途失败证据回归。最终全套计数、完整基线差异及独立审查以PR #11为准，不沿用265项旧成绩。源码与测试都有中文意图注释；本人仍待阅读/待实践，原预算矛盾练习继续留给本人，不代做。

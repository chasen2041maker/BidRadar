# 28｜两阶段研究流程与来源复用

2026-09-15｜SOURCE-001。用户明确要求把竞品借鉴写入01及相关文档，并直接复用可用的数据源代码。本篇记录接受的产品约定、实际复用和未验证边界，不把提案或外部README当运行证据。

## 1. 接受的决定与本次范围

D-FLOW-01：采集与目录筛选回答“哪些值得打开”，深度分析回答“这个项目到底适不适合我们”。中间显式保留 `awaiting_decision`，由用户选择后才请求深度分析。目录筛选默认使用规则/索引，不因查看、刷新或搜索自动启动模型。

D-REUSE-01：吸收两阶段交互、逐条条件证据清单、来源故障反馈、模型失败保留目录、关键变化影响提示、通知去重及必要的逐渠道发送记录；不整仓替换、不按总分判断资格、不扩成标书生成平台。中国优先、少源、公司隔离、微服务、早期Redis/K8s、中文CMS和原总预算均不改变。

AUTH-SOURCE-001：允许本轮文档更新、竞争项目源码/公开官方接口资料核查、许可明确的来源适配小片及离线测试，通过独立分支提交PR。没有获得本任务自动合并、批量采集、注册/领取文件、付费模型、真实公司资料、采购或部署授权。原ACCESS-001合并授权不沿用；既有阅读练习不代做。

本次只增加一个核心文件 [ccgp.py](../services/ingestion/sources/ccgp.py) 和对应测试，其他新增内容是规范、来源台账和许可说明。`awaiting_decision`、数据库任务、页面和模型流程仍是待实现设计。

## 2. 两阶段状态和数据归属

公共采集运行有自己的终态；成功保存并发布目录后即可结束，不等待某家公司选择，也不占着Worker或数据库事务。公司在workspace拥有私有的候选选择会话，保存固定候选/程序/标段ID、目录版本、档案版本和选择意图；不向公共目录广播公司兴趣。

`awaiting_decision`属于这个选择会话的业务阶段，不直接加入analysis_run任务枚举，不混同人工跟进的needs_review或分析缺材料的waiting_input。界面可读取已有目录、原件清单和已有报告；新模型分析必须经过明确提交。外部枚举、字段和接口在后续实现前统一到14，不以本篇冒充已发布HTTP契约。

提交选择时校验当前身份、公司成员/动作、各输入可见性及外发许可、固定ID和版本、配额，然后调用research的持久任务入口；workspace不得直接写research库。重复命令按公司/动作/幂等键处理，同键不同内容冲突；跨服务中断后对账到同一任务，不靠两张表同时提交的假事务。

空选择和缺省选择都不能代表全部。明确分析全部时先冻结可见范围、数量及费用边界，经确认再提交；不使用会随分页、排序或刷新变化的数组下标。已选项目出现取消、更正、材料撤回或目录版本变化时，说明变化并要求确认新的输入范围，不静默换版本。

管理员和协作成员可以在完整授权/额度检查后提交分析；只读成员可看允许的目录/报告但不可建分析任务。移除成员后下一动作前重验；刷新/重登返回原任务，取消独立持久化。创建任务成功不代表分析成功，更不代表全部条件满足。

跟踪开关、人工跟进决定和自动复核委托分开。实质变化先标记受影响旧结论；只有有效委托、当前权限及额度都成立才自动新建复核任务，否则等用户选择。不能借定时任务绕过D-FLOW-01。

## 3. 报告、失败体验与验收

每项报告包含要求原文、采购材料版本和位置、项目/标段、企业证据及档案版本、五状态、解释、缺口与下一步。五状态沿用met/unmet/unknown/conflicting/not_applicable；未知资质不是没有资质，已识别条件均满足也不是全部要求已识别。引用存在和语义支持分别校验，人工承担最终判断。

返回来源实际检查时间、最近成功同步与材料缺口。full/partial/degraded若采用，只描述请求范围的可用性，不代表全国覆盖；零结果、访问受限、解析失败和资料陈旧不能混为一个“暂无商机”。模型失败保留目录、未评分项目和可访问旧报告，不伪造低分或无机会。

更正、截止、资格与交付条件变化要定位受影响结论；纯格式变化不自动烧模型额度。内部提醒去重；外部通知仍属P2，实施时独立记录邮件/短信各自attempt和状态，不用一个success标记所有渠道。CMS仅管理平台原创内容，不能编辑采购原件和企业档案。

| 验收 | 必须观察的行为 | 本次状态 |
| --- | --- | --- |
| 目录/详情/刷新 | 新模型调用为0，来源检查时间可见 | 待跨服务实现 |
| 空选择/缺省选择 | 不建分析任务，不预留或消耗模型费用 | 待实现 |
| 重排/分页后提交 | 使用稳定ID与固定版本，不串项目 | 待实现 |
| 重复提交/同键异请求 | 同一任务/明确冲突，不重复内部发布 | 待实现 |
| 只读/跨公司/撤权 | 按当前权限拒绝，Worker也复核 | 仅既有基础纯函数，完整链待验 |
| 来源模板坏/受限/部分行坏 | 与明确零结果区分，保留诊断 | 本次离线解析覆盖，真实网页待验 |
| 模型失败/预算暂停 | 保留目录和旧报告，有限重试 | 待实现 |
| 条件更正/重复事件 | 旧结论标记陈旧，只影响有关范围，唯一提醒 | 待实现 |
| 自动复核 | 没有有效委托/额度不启动模型 | 待实现 |

## 4. 六个参考项目的数据来源与取舍

本次查看的源码或README位置见第7节。来源台账见 [source-candidates.json](source-candidates.json)，是文档数据，不是可执行采集配置；所有network_enabled均为false。

| 参考项目 | 实际见到的数据来源/输入 | 本次处理 |
| --- | --- | --- |
| bidding-ai-analyzer | CCGP搜索列表，`search.ccgp.gov.cn/bxsearch` | MIT已核实；改编参数和列表解析，不搬线程任务和并发模型执行 |
| BidMonitor-AI | CCGP等导入项；注册表实际仅chinabidding专用类；另有大量网站首页交给通用/Selenium爬虫 | 记录采购与招标网、电力/电网/央企等候选；首页清单不算适配成功；根LICENSE读取404，未充分核实其他授权，不复制代码 |
| ai-tender-radar | 按日期提交的公开信息快照；脚本负责清洗/发布这些快照 |公开行白名单未保留原始公告URL；来源链不足，不搬成证据库。README区分网站MIT与原发布方数据权利 |
| bid-intelligence | 用户上传招标PDF/表格等材料，而非已验证的国内商机采集源 | 借条件、缺项、澄清问题和人工审核；行业模板/硬编码/许可未充分核查，不复制实现 |
| tender-mcp | UK Contracts Finder、EU TED、US SAM.gov三条查询函数 | MIT已核实；借返回契约，海外源保留后续；上游地址/参数/字段有需修正处，不整段搬客户端 |
| BidMaster-Pro | 预置政府/地方公共资源/招投标公共服务/运营商/电网等配置，另含AI新闻和GitHub搜索 | 仅登记采购候选；RSS可用性未验证，中央公告配置却指向dfgg；README MIT徽章与实际AGPL-3.0不一致，未引入其实现 |

BidMonitor-AI清单包含采购与招标网、中国电力招标网、电能e招采、华能、电建、能建、大唐、国家能源、国家电网、南方电网、三峡、华润、中广核、中交、五矿等平台；是否有软件项目、允许机器读取、首页是否仍为采购入口都待逐项核验。不能据此宣称几十个来源已经接入。

BidMaster-Pro中AI媒体、公司博客、科技新闻和GitHub仓库搜索是资讯/竞品线索，不是招标公告来源，本次排除。各地公共资源RSS路径未取得真实feed，不把配置中的enabled=true搬成批准状态。

## 5. 实际复用的CCGP小片

上游固定提交 `2e82be5e026b54516d8157700aea94cf1616747c`，策略文件blob `8a57fdbe3d9543361aff5c2edaa100ae3cc13749`。完整MIT和改编范围见 [第三方登记](../third_party/README.md)。

`build_search_params`输入关键词、正整数页码和可选YYYY-MM-DD发布日期区间，输出上游参数映射；日期格式/有效性/前后顺序错误拒绝，不补猜日期，不发请求。

`parse_search_page`只解析调用方传入的HTML，返回不可变列表结果：ok/empty/partial/blocked/parse_error、候选、问题和输入SHA256。候选保留标题、CCGP原链接、元数据文本、可辨发布日期文本、采购人/代理机构和行定位；不猜金额、地区、响应截止、资格或当前可参与状态。

本片新增参数校验、标准库离线解析、受限域名/路径、显式故障与虚构样本测试；去掉上游异常静默吞掉、最后一段必为地区、固定每页20条、空选择分析全部等假设。没有导入BaseSearchStrategy或网络会话，不新增BeautifulSoup/requests依赖。

输入大小上限为2MiB，仅为本片防护，不是生产容量结论。只支持已约定列表模板；空页须同时有指定容器与明确共0条，否则保守报错。URL白名单不等于未来DNS/重定向/下载安全完整实现；HTML显示时仍须前端转义，不能渲染为可信原始HTML。

22项单测使用手写虚构HTML，只证明该输入契约下的行为；没有取得真实列表/附件回归、分页稳定性、上游访问许可或持久归档证据。内容指纹不是完整原件存储，发布日期原文不是截止时间。正式联网前仍要准入和单页真实样本验证。

## 6. 官方接口核对与尚未接通的部分

2026-09-15读取官方TED API文档：已发布公告的Search API支持匿名访问，统一域名api.ted.europa.eu及v3检索；这只是文档核验，没有本项目客户端联调。入口： https://docs.ted.europa.eu/api/latest/index.html 。

同日读取GSA官方Get Opportunities Public API文档：生产入口为 `https://api.sam.gov/opportunities/v2/search`，需要用户API key；搜索字段列出title、postedFrom/postedTo等；返回description是进一步取得正文的链接。其latest-only接口不能单独提供全部历史版本。入口： https://open.gsa.gov/api/get-opportunities-public-api/ 。

tender-mcp实际使用的SAM路径带 `/prod/`，关键词参数为 `q`；与上述官方生产地址/已列参数不同，不能视作已核实可用。其normalizer还可能把description链接当摘要，或丢弃多标段日期/时区、默认币种；这些不能进入BidRadar证据模型。UK接口只确认上游函数的引用，官方文档本轮访问失败，未填联调通过。

本轮浏览CCGP中央栏目返回工具侧502，搜索入口未取得正文（浏览工具安全打开失败）；不能推断整个网站宕机或官方拒绝所有请求。没有用代理、验证码处理或账号绕过，也没有启动自动轮询。25的2026-09-12样本保留为历史观察，没有将它们重新标为今日有效项目。

## 7. 可复核代码与许可依据

- CCGP策略及MIT：https://github.com/ichthyoplanktonzyh/bidding-ai-analyzer/blob/2e82be5e026b54516d8157700aea94cf1616747c/backend/src/bidding_ai_analyzer/strategies/ccgp.py ；同提交根LICENSE。
- BidMonitor-AI： https://github.com/zhiqianzheng/BidMonitor-AI/blob/main/src/monitor_core.py ，本轮读取blob `8279e274fff9b4493d1986ee97887d11e666b81c`，关注get_all_crawlers/get_default_sites/run_once/_send_notifications。
- ai-tender-radar： https://github.com/HeyClioo/ai-tender-radar/blob/main/scripts/public-data.mjs ，blob `849ed94c6b53acf1cf835a2a3678ea1325d9ecd3`，PUBLIC_ROW_KEYS不含原URL；README说明数据权利边界。
- bid-intelligence： https://github.com/ghanoubenz/bid-intelligence/blob/f36dbade0728891041d38d46676342db341ff006/README.md 及api/app/extraction/schemas.py、checklist_filler.py；不采用本机模板路径及管道行业硬编码。
- tender-mcp： https://github.com/OjasKord/tender-mcp/blob/main/src/server.js ，blob `89ffc05986d3c5b9b17907c3af5102c768f29024`；MIT LICENSE blob `d692c52574fbb8accb47ac027bff01a7667b210d`。
- BidMaster-Pro： https://github.com/guangshu100/BidMaster-Pro/blob/cc89d9093651e3410f68384f926ceef672b76534/services/news/sources.yaml 及同提交LICENSE（AGPL-3.0），不以README徽章代替许可文件。

软件复用许可、网站访问规则、原始公告数据权利和部署方式分别审查；本次不作全面法律结论。没有转存竞争项目的数据集或完整公告，未核验许可证的代码只作研究参考。

## 8. 下一步与交付证明

先读本片参数→解析→受限页测试，理解“来源失败为什么不能返回空列表”。再按当前任务选择下一小片：来源许可/单页原文归档与真实样本适配，或选择会话到research的持久任务契约；不本轮连写两个模块。

全套治理回归、最终head差异和独立审查以本任务PR为准；本地不是完整clone，不把22项新片测试冒称全仓库通过。SOURCE-001记录在 [学习笔记](learning/SOURCE-001.md)，本人阅读/实践仍待反馈。主分支未合并前，任务分支内容不冒充main已生效。

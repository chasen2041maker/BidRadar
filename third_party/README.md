# 第三方复用登记

2026-09-15｜SOURCE-001。软件许可不等于采购网站的数据采集、模型处理或转载许可。本目录不变更BidRadar整体许可，不收集密钥、真实公司材料或完整招标文件。

## 本次实际复用：bidding-ai-analyzer / CCGP策略

- 上游：https://github.com/ichthyoplanktonzyh/bidding-ai-analyzer
- 读取提交：`2e82be5e026b54516d8157700aea94cf1616747c`。
- 上游文件：`backend/src/bidding_ai_analyzer/strategies/ccgp.py`，blob `8a57fdbe3d9543361aff5c2edaa100ae3cc13749`。
- 代码依据：https://github.com/ichthyoplanktonzyh/bidding-ai-analyzer/blob/2e82be5e026b54516d8157700aea94cf1616747c/backend/src/bidding_ai_analyzer/strategies/ccgp.py
- 上游LICENSE blob：`b2d63b7fb5030a081b9b5470bbcf5d71fd16aaaa`；完整文本保留在 [MIT许可](licenses/bidding-ai-analyzer-MIT.txt)。版权：Copyright (c) 2026 ichthyoplanktonzyh。
- 本地派生：[ccgp.py](../services/ingestion/sources/ccgp.py)；测试：[test_ccgp_source.py](../tests/test_ccgp_source.py)。

直接沿用查询参数键与默认映射（searchtype、page_index、bidSort、buyerName、projectId、pinMu、bidType、dbselect、kw、start_time、end_time、timeType、displayZone、zoneId、pppStatus、agentName）、日期分隔转换、列表CSS标识与竖线元数据拆分思路。该文件是有标注的改编，不是上游原样vendor副本。

BidRadar新增：严格参数/日期校验、标准库离线HTML解析、只接受登记公告域名/路径、输入内容指纹、可定位的条目、显式partial/blocked/parse_error、未知值保留和虚构样本测试。源侧HTTP入口按上游保留，不宣称HTTPS、分页和访问许可已验证。

没有导入上游BaseSearchStrategy、网络会话/浏览器、10并发模型执行、全局任务字典、守护线程、异常静默吞掉、空选择分析全部、固定每页20条或最后元数据必是地区的假设。本片不依赖BeautifulSoup/requests，不新增常驻进程或外部服务。

## 其他项目的处理

来源入口、软件结构或产品流程引用在 [28](../docs/28-two-stage-and-source-reuse.md)，不是直接复制其代码或数据集。tender-mcp的MIT已核查，但海外客户端不在本轮实施范围；BidMaster的LICENSE为AGPL-3.0，与README徽章不一致，本轮不引入其实现；其余许可未充分核验的实现不复制。

后续升级先固定上游提交、检查许可/差异/依赖与访问边界，再运行自有回归和独立审查。不能用对上游软件的信任代替网站准入、真实样本或本项目验收。

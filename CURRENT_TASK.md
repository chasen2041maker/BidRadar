# 当前任务｜SOURCE-001：两阶段流程与CCGP来源复用

更新：2026-09-15。用户要求把两阶段流程及竞品借鉴更新进01和相关docs，并直接复用可用的数据源部分。本轮范围替代此前只读讨论；不沿用ACCESS-001的main合并授权。

## 目标

将确认的两阶段流程固化为可验收约定，并交付许可明确、无网络副作用的CCGP适配小片。

## 范围

01/17同步“目录筛选→awaiting_decision→明确选择→持久分析任务”；新增28及来源候选台账，25保留历史样本并增补当前边界。直接改编MIT许可的CCGP查询参数和列表解析，1个核心实现文件、1个测试文件，加许可/状态/学习记录。不是选择页面、任务服务或整套爬虫已实现。

分支：feat/source-001-two-stage-20260915；main基线a3c5e8692da364820c2520a714ac9b2de76c187e。通过PR交付，不直推main；需最终检查、独立审查及适用的人工合并放行。未授权批量采集、账号/CA/付费领取、模型调用、数据库/CMS安装、采购或部署。

## 验收

services/ingestion/sources/ccgp.py：build_search_params接受关键词/页码/可选发布日期区间，输出字典；parse_search_page接受已解码HTML，返回候选/问题/指纹和ok/empty/partial/blocked/parse_error。函数不联网，不做并发抓取、后台任务或AI分析；链接检查不代替未来DNS/重定向安全。

tests/test_ccgp_source.py：25项手写虚构HTML测试覆盖正常字段、日期/页码错误、未知值、模板变化、明确零条、访问限制、部分坏行、异常HTML、URL范围和无网络副作用。元数据原文不冒充完整原件归档，发布日期不当截止。

两阶段系统验收另列在28：空选择不建任务、重复提交幂等、固定ID/版本、只读/跨公司/撤权拒绝、预算与有效复核委托、刷新不调模型。它们尚未实现，不能用解析单测冒充通过。

## 未执行与阻塞

局部目录/mnt/data/bidradar-source-task，Python3.13.5，2026-09-15T06:18:52Z执行 `python3 -m unittest discover -s tests -p 'test_ccgp_source.py' -v`，25项通过、退出0。

本地远端DNS失败，未取得完整clone；源码和旧记忆输入按Git blob字节核对，运行原索引脚本生成/检查派生INDEX。完整仓库回归、提交后--base/差异检查由实际PR CI提供，最终SHA/审查结果留PR，不预填通过。

## 下一步

读build_search_params→parse_search_page→test_block_page_is_not_empty，见[SOURCE-001](docs/learning/SOURCE-001.md)。小问题：为什么DOM变化和验证码不能返回empty？本人状态待阅读/待实践；不代做ACCESS-001改角色练习。

下一片在反馈后选来源许可/单页原文归档与真实样本，或选择会话到持久任务的契约，不能自行连续实施两条链。既有可信成员查询、DATA-001和CMS缓存流程仍保留。

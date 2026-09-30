# DATA-001｜真实来源与证据目录

摘要：天津官方接口接入、证据导出、字段规范化与独立目录已实现；全250项离线通过。本人已完成开发者注册，真实API仍拒绝令牌；本次补安全DoH、明确拒绝错误及真实失败链验证，整个第一阶段未完成。

## 本次改变

延续main 7c637f987b1b3de2d79bc706108f0649369e8bb8（PR #10）。Redis/K8s移至后续部署，按负责人最新目标完成整个数据阶段后集中学习。新增天津固定资源适配器、CCGP段落/表格证据、JSON导出、processing纯函数与catalog独立SQLite/CLI。中文注释解释身份、时区、金额口径、事务、失败和令牌边界。详细来源证据/命令/契约见[30](../30-r0-data-contract.md)。

## 阅读顺序

1. [tianjin.py](../../services/ingestion/tianjin.py)的request_spec、TianjinTransport.fetch_page、execute：参数语义、凭据只进请求、分页如何持久化。
2. [normalize.py](../../services/processing/normalize.py)的normalize_bundle、_fact：如何从带位置的证据生成可空/冲突事实，而不是从一段文本猜投标结论。
3. [store.py](../../services/catalog/store.py)的Catalog.import_bundle/query/detail：事务/幂等、旧观察晚到、快照分页、更正候选关系。

这些是最终带读入口的当前草案；独立审查和真实接口可能改变实现，阶段结束才集中带读，不把中间文件当已验收最终版。

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

公开资源目录另核实[政府采购更正公告](https://open.data.tj.gov.cn/sjjk/67393436a7cd44f5a896b3e98153f7a7.htm)，以及有“其他附件文件下载链接”字段的[政府采购磋商公告](https://open.data.tj.gov.cn/sjjk/cafe018abce346a7a34682a33fb6c4c2.htm)。仅有字段定义，未取API数据；不能把该字段当作已取得附件或把不同资源的项目号直接当稳定公告ID。待授权状态正常后按需接入。

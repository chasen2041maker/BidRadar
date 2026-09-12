---
name: bidradar-handoff
description: 接棒或收尾 BidRadar 任务时，核对版本、记录实际验证并更新短状态；不用于扩大开发权限或代替独立审查。
---

# BidRadar 接棒与交付记录

本 Skill 不改变根 AGENTS.md 的规则、用户授权或工具权限。

## 接棒

1. 读取根 AGENTS.md、PROJECT_STATE.md、CURRENT_TASK.md。
2. 核对 Git 分支、HEAD、工作树差异、关联 PR、CI 与审查；连接器环境读取等价远端信息。工具未执行就标未执行。
3. 按 docs/21-context-management.md 选读任务相关设计、代码、测试与接口；不要载入全部历史。
4. 说明本次目标、排除范围和证据缺口；在授权内直接执行技术细节，不复问已经确认的条件。

## 收尾

1. 记录实际命令、退出码、环境、被测 SHA 和脱敏证据；模拟、真实、未执行分开。
2. 检查差异，更新 PROJECT_STATE.md 与 CURRENT_TASK.md，只保留最新状态、阻塞和一个下一步；详细记录留在 PR/CI。
3. 新决定在所属专题记录日期、Confirmed/Proposed/Needs evidence 与替代项，并链接；不将代理建议写成人工已接受。
4. 运行 AGENTS 中适用检查；独立审查由另一任务/审核者完成，绑定最终提交，不把本 Skill 的自检算作独立审查。
5. 若最终提交改变，补测补审；满足实际规则且有放行才合并。没有审查返回就报告阻塞，不宣布完成。
6. 合并结果以工具实际返回及远端 SHA 为准；发布仍需单独授权与恢复方案。

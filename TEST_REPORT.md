# 测试报告

测试日期：2026-07-23

## 自动化结果

- `pytest -q`：79 passed，1 skipped（junit：80 tests / 0 failures）。
- 跳过项：真实 Windows Excel COM 集成测试，需要桌面 Excel 和显式环境变量。
- `coverage run --source=voucher_audit -m pytest -q`：总覆盖率约 **38%**。
- CI 覆盖率门槛：`--fail-under=38`。
- 新增覆盖：备份命名/恢复、Excel 占用检测、标注规则规格、rules template、`rules set-active`、cleanup 报告保护、runner 月份选择、report 辅助函数、guardrails 元数据。
- 说明：当前 worktree 环境下 pytest 会话清理可能因 Windows “不受信任的装入点”报 `OSError`，与用例结果无关；以 junit 统计为准。

## 已覆盖的关键风险

- CLI 缺少 OpenAI SDK 时返回明确错误。
- 清理命令默认拒绝删除；默认不删报告目录；`--include-reports` 才清理 `凭证审核输出`。
- active 规则指针拒绝仓库外路径。
- 备份使用 `*.xlsx.bak`；标注失败按显式备份路径恢复。
- 源表标注规格覆盖现行 6 条规则 ID。
- 同比波动使用历史月均金额和历史累计比率。
- 客户映射不一致能进入审核结果和报告明细。
- 非 UTF-8 文本不会被静默截断或忽略。

## 覆盖率门槛

CI 要求总体覆盖率不低于 38%。核心新增/强化模块中，同比规则、同比报告、cleanup、security、versioning 覆盖率较高。真实 Office 行为仍通过显式集成测试验证，不在普通 CI 中伪造成功。

# 测试报告

测试日期：2026-07-14

## 自动化结果

- `pytest -q`：48 passed，1 skipped。
- 跳过项：真实 Windows Excel COM 集成测试，需要桌面 Excel 和显式环境变量。
- `coverage run --source=voucher_audit -m pytest -q`：总覆盖率 36%。
- `ruff check voucher_audit tools tests`：通过。
- `python -m compileall -q voucher_audit tools tests`：通过。`pip wheel` 构建、包资源检查及隔离安装预览：通过。

## 已覆盖的关键风险

- CLI 缺少 OpenAI SDK 时返回明确错误。
- 清理命令默认拒绝删除，`--dry-run` 不修改文件，`--yes` 只清理指定工作目录。
- active 规则指针拒绝仓库外路径。
- 同比波动使用历史月均金额和历史累计比率。
- 客户映射不一致能进入审核结果和报告明细。
- Excel COM 写入失败恢复备份，并报告恢复失败的组合错误。
- 规则补丁可以预览、审查并生成版本文件。
- 非 UTF-8 文本不会被静默截断或忽略。

## 覆盖率门槛

CI 要求总体覆盖率不低于 35%。核心新增模块中，同比规则和同比报告覆盖率均达到 80% 以上。真实 Office 行为仍通过显式集成测试验证，不在普通 CI 中伪造成功。

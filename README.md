# voucher-audit-skill

面向月末凭证审核的 Excel 规则引擎与 AI Skill。项目读取辅助账、客户映射和收入成本数据，执行配置化检查，生成 Excel 审核报告，并可在 Windows Excel 中标注源文件。

## 主要能力

- 按 YAML 规则执行客户归属、收入成本、人次编码、负毛利、外包成本和同比波动检查。
- 输出便于复核的 Excel 报告，保留问题行、参考行、严重度和命中原因。
- 可选使用 Excel COM 对源工作簿做颜色标注和问题列回写。
- 支持规则预览、输入检查、版本化修复和规则补丁审查。

## 环境要求

- Python 3.10+
- 源文件标注：Windows、桌面版 Microsoft Excel、`pywin32`
- AI 复核：可选安装 OpenAI SDK

## 安装

```powershell
git clone https://github.com/Seller-1990/voucher-audit-skill.git
cd voucher-audit-skill
pip install -e .
```

可选功能：

```powershell
pip install -e ".[ai]"   # AI 复核
pip install -e ".[dev]"  # 测试、覆盖率和 Ruff
```

安装后可使用 `voucher-audit`，也可以始终使用 `python -m voucher_audit`。
源码仓库运行时读取顶层 `rules/`；wheel 安装首次运行时会把内置默认规则初始化到用户配置目录。

## 输入文件

工作目录默认包含：

```text
workdir/
├── 数据汇总.xlsx
└── 考核表输出.xlsx
```

文件名、Sheet 匹配和列映射由 `rules/app_rules.yaml` 配置。

## 基本使用

```powershell
# 预览规则和输入，不修改文件
python -m voucher_audit preview --workdir "D:\path\to\workdir" --show-inputs

# 检查文件、Sheet 和列结构
python -m voucher_audit inspect --workdir "D:\path\to\workdir"

# 生成报告，不标注源文件
python -m voucher_audit run --workdir "D:\path\to\workdir" --yes --no-annotate

# 生成报告并标注源文件；标注仍需单独确认
python -m voucher_audit run --workdir "D:\path\to\workdir" --yes --annotate --yes-annotate
```

报告输出到 `<workdir>/凭证审核输出/`。

## 当前规则

`rules/audit_rules.yaml` 是审核规则的唯一维护入口，当前包含 6 条合并规则：

| 规则 ID | 用途 |
|---|---|
| `INC_CUSTOMER_CONSISTENCY` | 客户归属一致性 |
| `INC_REV_COST_ZERO_MISMATCH` | 收入和成本零值不匹配 |
| `AUX_HEADCOUNT_DATA_CHECK` | 人次编码和符号检查 |
| `INC_NEG_GM_HIGH_RATIO` | 负毛利占比过高 |
| `INC_OUTSOURCING_NO_WAGE_OR_HANGKAO` | 外包成本结构缺失 |
| `INC_PP_CHANGE` | 指标、金额和比率同比波动 |

`rules/compiled_rules.yaml` 是运行时生成文件，不应手工维护，也不进入 Git。

## 辅助工具

```powershell
# 生成规则执行计划
python -m tools.generate_plan --rules rules/audit_rules.yaml --output plan.md

# 查看规则分布
python -m tools.visualize_rules --rules rules/audit_rules.yaml

# 检测规则冲突
python -m tools.check_conflicts --rules rules/audit_rules.yaml

# 分析已生成的审核报告
python -m tools.analyze_audit report.xlsx --format json
```

## 安全清理

清理命令只处理指定工作目录内由本工具生成的目录。默认不会删除，必须先预览并显式确认：

```powershell
python -m voucher_audit cleanup --workdir "D:\path\to\workdir" --dry-run
python -m voucher_audit cleanup --workdir "D:\path\to\workdir" --yes
```

## 测试

```powershell
pytest -q
coverage run --source=voucher_audit -m pytest -q
coverage report --fail-under=35
ruff check voucher_audit tools tests
python -m compileall -q voucher_audit tools tests
```

真实 Excel COM 集成测试默认跳过。请关闭目标工作簿，并在装有桌面 Excel 的 Windows 环境执行：

```powershell
$env:VOUCHER_AUDIT_EXCEL_INTEGRATION = "1"
pytest -q -m excel_com
```

## 代码结构

```text
voucher_audit/
├── cli.py                           # CLI 参数和命令编排
├── runner.py                        # 审核主流程
├── checks.py                        # 通用规则执行与分派
├── checks_customer*.py              # 客户一致性规则
├── checks_headcount.py              # 人次数据规则
├── checks_outsourcing.py            # 外包成本规则
├── checks_pp_change.py              # 同比波动规则
├── report.py                        # 报告总装配
├── report_customer_consistency.py   # 客户一致性报告
├── report_pp_change.py              # 同比波动报告
├── report_comparison.py             # 对比视图
├── report_cost_checks.py            # 成本检查报告
├── report_profit.py                 # 毛利报告
├── source_annotation.py             # 标注计划
└── excel_annotation_com.py          # Excel COM 写入与回滚
```

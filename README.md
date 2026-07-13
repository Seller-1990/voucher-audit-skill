# voucher-audit-skill

一个面向"月末凭证审核"的**规则 + 脚本**集合。

## 核心定位

**AI 驱动的凭证审核 Skill**：这是一个可在 IDE 中被 Model 直接调用的工具，用于：

- ✅ **自动审核凭证** - 按规则自动执行审核
- ✅ **生成审核报告** - 产出 Excel 格式的审核结果
- ✅ **源文件标注** - 自动在源文件中标注问题
- ✅ **规则维护** - 自然语言生成/修改审核规则
- ✅ **持续优化** - 可视化分析和规则管理

## 使用场景

### 在 IDE 中让 Model 调用

```python
# Model 直接调用 skill
voucher-audit run --workdir . --yes --no-annotate
# -> 自动审核并生成报告
```

### 规则维护与优化

```powershell
# 自然语言添加规则
python -m tools.edit_rules "检查摘要中金额为负数但不是冲销"

# 查看规则分布
python -m tools.visualize_rules --rules rules/compiled_rules.yaml

# 检测规则冲突
python -m tools.check_conflicts --rules rules/audit_rules.yaml
```

## 技术架构

```
Model / Claude Code
    ↓ (调用 skill)
voucher-audit CLI
    ↓ (内部调用)
├── 脚本工具 (tools/)
│   ├── extract_excel_info.py    # 提取 Excel 标题行信息
│   ├── analyze_audit.py         # 分析审核报告，生成优化建议
│   └── generate_plan.py         # 生成规则执行计划
├── 核心模块 (voucher_audit/)
│   ├── rules_engine.py          # 规则引擎
│   ├── checks.py                # 11种审核规则实现
│   ├── excel_io.py              # Excel 读写
│   └── runner.py                # 审核流程编排
└── 规则配置 (rules/)
    ├── app_rules.yaml            # 应用规则（文件映射）
    └── audit_rules.yaml          # 审核规则（checks定义）
```

## 快速开始

### 1. 安装依赖

```powershell
cd "D:\OneDrive - PowerBI学谦\Software Development\工具软件\voucher-audit-skill"
pip install -r requirements.txt
```

### 2. 准备工作目录（workdir）

workdir 内默认需要两份 Excel（文件名可在 `rules/app_rules.yaml` 配置）：

```
workdir/
├── 数据汇总.xlsx              # 调整后序时账、客户映射
└── 考核表输出.xlsx            # 收入成本表
```

### 3. 脚本化工具使用

#### 提取 Excel 信息
```powershell
python -m tools.extract_excel_info 数据汇总.xlsx --show-sample
python -m tools.extract_excel_info 考核表输出.xlsx --validate-cols 主体账簿 月 账载客户
```

#### 生成规则执行计划
```powershell
python -m tools.generate_plan --rules rules/compiled_rules.yaml --output plan.md
```

#### 分析审核报告
```powershell
python -m tools.analyze_audit report_202401.xlsx --format yaml
```

## 脚本化工具套件

### 1. extract_excel_info - Excel 信息提取

提取 Excel 文件的标题行、sheet 结构和数据样例，用于理解数据格式：

```powershell
python -m tools.extract_excel_info 数据汇总.xlsx --show-sample
python -m tools.extract_excel_info 考核表输出.xlsx --validate-cols 主体账簿 月
```

**输出示例**：
```
📄 数据汇总.xlsx
   Sheet数: 2 | 总行数: 1500

   📑 调整后序时账
      列 (14): 主体账簿, 月, 日, 凭证号, 摘要, 一级科目, ...
      数据行: 1499
      样例数据: A, 1, 1, V001, ...
```

### 2. generate_plan - 规则执行计划

生成可读性强的 Markdown/JSON 执行计划，用于预先审核和沟通：

```powershell
python -m tools.generate_plan --rules rules/compiled_rules.yaml --output plan.md
python -m tools.generate_plan --rules rules/compiled_rules.yaml --json plan.json
```

**输出示例** (Markdown):
```markdown
# 凭证审核执行计划

## 错误级别 (2 条)

### INC_FULL_COMBO_DRIFT - 完整组合主映射漂移
- **Scope**: `income_cost`
- **Type**: `combo_drift`
- **描述**: 按（主体账簿、账载客户）为主键，检查历史主映射是否一致
- **检查字段**: `主体账簿`, `账载客户`, `三级科目`, ...
```

### 3. analyze_audit - 审核报告分析

分析审核结果，统计问题分布并生成优化建议：

```powershell
python -m tools.analyze_audit report.xlsx --format json
python -m tools.analyze_audit report.xlsx --format yaml
```

**输出示例**:
```
📊 统计信息:
   总规则数: 16
   错误级别: 2
   需确认级别: 1
   信息级别: 13

💡 优化建议 (3 条):

   1. [高] 严重度分布
      错误级别问题过多（2 条），建议降级部分非关键规则为"需确认"级别
      ⚠️  影响: 减少紧急问题数量，优先处理真正重要的问题

   2. [中] 规则数量
      当前共有 16 条审核规则，建议考虑合并相似的规则以提升效率
      ⚠️  影响: 减少误报率，提高审核聚焦度
```

## 审核规则系统

### 规则类型（11种）

| 规则ID | 类型 | 严重度 | 检查维度 |
|--------|------|--------|---------|
| `INC_FULL_COMBO_DRIFT` | `combo_drift` | 错误 | 组合主映射稳定性 |
| `INC_REV_COST_ZERO_MISMATCH` | `rev_cost_zero_mismatch` | 错误 | 收入成本零值不匹配 |
| `INC_CUSTOMER_MAPPING_CHECK` | `mapping_check` | 错误 | 客户映射一致性 |
| `AUX_SUMMARY_ZY_PATTERN` | `forbidden_regex` | 需关注 | 摘要 Z*Y* 码 |
| `AUX_SUMMARY_YS_PATTERN` | `forbidden_regex` | 需关注 | 摘要 Y*S* 码 |
| `AUX_RED_FLUSH_ZS_NONPOSITIVE` | `hard_rule` | 错误 | 冲销格式合规性 |
| `AUX_COST_WAGE_CASHFLOW_MISMATCH` | `hard_rule` | 错误 | 成本口径一致性 |
| `AUX_SUMMARY_ZS_SUFFIX` | `summary_zs_suffix` | 错误 | 摘要 ZS 后缀格式 |
| `INC_PM_CENTER_CUSTOMER_MULTI_DEPT` | `distinct_count` | 需确认 | 多部门关联 |
| `INC_BOOK_CUSTOMER_MULTI_ACTUAL` | `distinct_count` | 错误 | 账载客户多实际客户 |
| `INC_METRIC_PP_CHANGE` | `metric_pp_change` | 需确认 | 指标同比波动 |

### 规则配置（YAML 格式）

所有规则定义在 `rules/audit_rules.yaml`，支持动态调整：

```yaml
checks:
  - id: INC_FULL_COMBO_DRIFT
    name: "完整组合主映射漂移"
    type: combo_drift
    scope: income_cost
    severity: "错误"  # 可改为"需确认"降低优先级
    params:
      key_fields: ["主体账簿", "账载客户"]
      min_amount_abs: 50000  # 调整阈值
```

### 核心特性

✅ **脚本化能力** - 3个独立工具支持自动化流程
✅ **配置驱动** - 规则定义与代码分离，易于维护
✅ **先预览再执行** - 确保审核前透明可控
✅ **版本化修复** - 规则变更可追溯、可回滚
✅ **智能修复** - 自动检测并生成修复补丁
✅ **安全标注** - 源文件修改前自动备份
✅ **自然语言交互** - 直接对话添加规则，无需编辑YAML
✅ **规则可视化** - ASCII图表展示规则分布

## 高级功能

### 修复闭环（repair）

当规则与文件结构不匹配时：
```powershell
python -m voucher_audit repair --workdir .
```

它会：
1. 基于报错生成 `app_rules.diff` / `audit_rules.diff`
2. 预演通过后确认
3. 写入 `rules/versions/` 并更新 `active_rules.json`

### 源文件标注（会修改源 Excel）

`run` 默认会根据 `rules/app_rules.yaml` 的 `annotation_policy.enabled_default` 决定是否启用标注：

```powershell
python -m voucher_audit run --workdir . --annotate
python -m voucher_audit run --workdir . --no-annotate
python -m voucher_audit run --workdir . --yes-annotate  # 跳过标注确认
```

标注依赖 Windows + Excel COM（`pywin32`），且源文件不要被 Excel 打开。

## 规则维护

### 新增规则

编辑 `rules/audit_rules.yaml`:

```yaml
checks:
  - id: MY_CUSTOM_RULE
    name: "我的自定义规则"
    type: combo_drift  # 或其他类型
    scope: income_cost
    severity: "需确认"
    description: "规则描述"
    params:
      key_fields: ["字段1", "字段2"]
      # ...
```

### 调整阈值

```yaml
params:
  min_amount_abs: 100000  # 提高门槛
  tolerance_ratio: 0.3    # 降低波动容忍度
```

### 降级规则

```yaml
severity: "需关注"  # 从"错误"改为"需确认"
```

## 输入输出

### 输入（workdir 目录）
- `数据汇总.xlsx` - 辅助账数据、客户映射
- `考核表输出.xlsx` - 收入成本数据

### 输出
```
凭证审核输出/
└── 凭证审核报告_YYYYMM_时间戳.xlsx
    ├── 概览
    ├── 入账规则违规_辅助帐
    ├── 疑似错科目_辅助帐
    ├── 维度异常_收入成本
    ├── 主映射异常_友好视图
    ├── 毛利异常_收入成本
    └── AI复核意见（可选）
```

## 命令行使用

### 预览规则
```powershell
python -m voucher_audit preview --workdir . --show-inputs
```

### 执行审核
```powershell
python -m voucher_audit run --workdir .
```

### 清理临时文件
```powershell
python -m voucher_audit cleanup --dry-run  # 预览
python -m voucher_audit cleanup            # 执行清理
```

### 查看规则状态
```powershell
python -m voucher_audit rules show-active
```

## 版本信息

- **Python**: 3.8+
- **依赖**: openpyxl>=3.1, pandas>=2.0, PyYAML>=6.0, pywin32>=306 (Windows)
- **作者**: Claude
- **许可**: 未指定
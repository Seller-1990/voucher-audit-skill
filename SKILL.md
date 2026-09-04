---
name: voucher-audit-skill
description: Use when the user needs month-end voucher audit / 凭证审核 / 月末审核 on Excel exports (数据汇总 + 考核表输出), preview-before-run confirmation, optional source Excel annotation, inspect/repair for missing file-sheet-column mismatches, or cleanup of audit outputs.
---

# Voucher Audit Skill

月末凭证审核：读取工作目录中的 Excel 导出，按 YAML 规则做一致性/波动/格式检查，输出汇总报告；可选对源表做颜色标注与问题列回写。

**核心约束（不可跳过）**

1. **先预览再执行**：必须先 `preview` / `inspect`，把将执行的规则与输入结构展示给使用人。
2. **源文件标注二次确认**：标注会改源 Excel；即使 `--yes` 也不能代替标注确认，必须 `--yes-annotate` 或交互确认。
3. **修复闭环**：缺文件/缺 sheet/缺列时用 `repair` 生成补丁 → 预演 → 确认后版本化落盘。

## Agent 工作流

```text
1. 确认 workdir（含 数据汇总.xlsx、考核表输出.xlsx）
2. preview --show-inputs  +  inspect
   （inspect 会输出每个匹配 sheet 的数据行数、月分布、目标月推断结果）
3. 向用户展示规则列表与输入匹配结果，请求确认
4. run --yes --no-annotate --quiet   （默认先出报告、不改源文件；--quiet 省略重复的规则明细表）
   run 结束会在日志输出"命中统计"（每规则命中数与严重度分布）
5. 若用户明确要求标注：run --yes --annotate --yes-annotate
   （需 Windows + 桌面 Excel + pywin32，且源文件未被占用）
6. 结构不匹配：repair → 展示 diff → 用户确认后 --yes 写入
7. 仅清理本工具产物：cleanup --dry-run → cleanup --yes
   （报告目录默认保留；确认后可加 --include-reports）
```

非交互场景（CI/脚本）必须显式带 `--yes`；需要标注时再加 `--yes-annotate`。

## 入口命令

在本仓库根目录（或已 `pip install -e .` 的环境）执行：

```powershell
pip install -e .                 # 首次
pip install -e ".[ai]"           # 可选 AI 复核
pip install -e ".[dev]"          # 测试/Ruff

python -m voucher_audit preview --workdir "D:\path\to\workdir" --show-inputs
python -m voucher_audit inspect --workdir "D:\path\to\workdir"
python -m voucher_audit run --workdir "D:\path\to\workdir" --yes --no-annotate
python -m voucher_audit run --workdir "D:\path\to\workdir" --yes --annotate --yes-annotate
python -m voucher_audit repair --workdir "D:\path\to\workdir"
python -m voucher_audit rules show-active
python -m voucher_audit cleanup --workdir "D:\path\to\workdir" --dry-run
python -m voucher_audit cleanup --workdir "D:\path\to\workdir" --yes
python -m voucher_audit cleanup --workdir "D:\path\to\workdir" --yes --include-reports
```

安装后也可使用 `voucher-audit` console script。

常用 `run` 参数：

| 参数 | 作用 |
|---|---|
| `--month N` | 指定目标月；默认从数据最大月推断 |
| `--include-rule-id ID` | 只跑指定规则（可重复） |
| `--enable-ai` | AI 复核（需 `openai` + `OPENAI_API_KEY`） |
| `--annotate` / `--no-annotate` | 覆盖 `annotation_policy.enabled_default` |
| `--yes` | 跳过审核事项确认 |
| `--yes-annotate` | 跳过源文件修改二次确认 |

## 输入与输出

**输入（workdir）**

- `数据汇总.xlsx`（必需，使用 调整后序时账、客户调整校验）
- `考核表输出.xlsx`（必需，使用 收入成本表）
- workdir 内其他常见文件（`账外调整*.xlsx`、`BDDL业绩补充.xlsx`、`商业险扣除方式台账/`、`数据导出/` 等）**不参与审核**，勿误以为已覆盖。

文件名、sheet、列映射见 `rules/app_rules.yaml`。

**输出**

- `<workdir>/凭证审核输出/凭证审核报告_YYYYMM_时间戳.xlsx`，包含：
  - `审核汇总`：工作目录/目标月/命中总数/AI 状态
  - `规则命中统计`：每规则命中数与严重度分布
  - `疑似数据错误清单`（**主出口**）：一行 = 一个修正事项，P1 疑似错误 / P2 需确认 / P3 波动参考，带建议修正动作与源行号，按优先级+金额排序；负毛利附应收账款逾期考核归因；优先级列按 P1 红 / P2 黄 / P3 灰 底色标注
  - `新增规则明细`：毛利偏高/花的比挣的多/结算人数和收入对不上/报了社保人数没交社保费/返费挂靠占比/费用占比/费用突然出现/和上个月比波动较大 等整合规则的全部命中明细
  - `规则关联分析`：按（主体账簿+三级科目+实际客户）聚合多规则命中，识别跨规则叠加模式并给综合风险分级
  - `客户综合分析`：实际客户级历史(1..N-1月) vs 本月画像（收入/成本/毛利/人次趋势、生命周期、背离检测、风险标签与处理建议）
  - 各规则明细页（问题行带 `源行号` 列，可回溯源表；数值已按金额 2 位/比率 4 位取整）
- 标注开启时：就地修改源工作簿（成功前会写旁路备份 `*.xlsx.bak`）

## 当前规则（14 条）

`rules/audit_rules.yaml` 是审核规则唯一维护入口；`rules/compiled_rules.yaml` 为运行时产物，勿手改、勿提交。

| 规则 ID | 用途 |
|---|---|
| `INC_CUSTOMER_CONSISTENCY` | 客户归属一致性（3 项子检查） |
| `INC_REV_COST_ZERO_MISMATCH` | 收入/成本零值不匹配 |
| `AUX_HEADCOUNT_DATA_CHECK` | 人次编码与符号 |
| `INC_NEG_GM_HIGH_RATIO` | 负毛利占比过高 |
| `INC_OUTSOURCING_NO_WAGE_OR_HANGKAO` | 外包成本结构缺失 |
| `INC_PP_CHANGE` | 指标/金额/比率同比波动 |
| `INC_MOM_CHANGE` | 环比波动（vs 上月） |
| `INC_GM_HIGH_RATIO` | 高毛利率（疑似少入成本） |
| `INC_REV_COST_INVERSION` | 收入成本倒挂（收入<成本） |
| `INC_HEADCOUNT_REV_MISMATCH` | 结算人次与收入背离 |
| `INC_SOCIAL_HEADCOUNT_MISMATCH` | 社保人数异常 |
| `INC_COST_RATIO_HIGH` | 返费/挂靠占收入比例过高 |
| `INC_EXPENSE_RATIO` | 福利费/其他费用占比异常 |
| `INC_COST_SUDDEN_APPEARANCE` | 成本项目突然出现 |

> 后 8 条（`INC_MOM_CHANGE` 起）整合自《收入成本表1.py》异常检测（2026-09-04）。`INC_MOM_CHANGE`/`INC_PP_CHANGE` 属波动参考，不进修正清单。

版本指针：`rules/versions/` + `rules/active_rules.json`（可选）。

## 规则维护

- 审核逻辑：编辑 `rules/audit_rules.yaml` 的 `checks`
- 应用配置：编辑 `rules/app_rules.yaml`（files/sheets/columns/report_format/annotation_policy）
- 手动切 active：`rules set-active`（危险；路径必须在仓库内）

## 安全与禁止事项

- **禁止**在未确认时对源 Excel 启用标注。
- **禁止**对仓库外路径设置 active 规则指针。
- `cleanup` 默认只删 `temp_*`；删除 `凭证审核输出` 必须加 `--include-reports`，且先 `--dry-run` 再 `--yes`。
- 文本按严格 UTF-8 读取；标注前检测 `~$*.xlsx` 锁文件并以读写打开探测占用。

## 常见失败

| 现象 | 处理 |
|---|---|
| 缺文件/缺 sheet/缺列 | `inspect` 定位 → `repair` 预演 → 确认写入 |
| AI 报缺 openai | `pip install -e ".[ai]"` 或去掉 `--enable-ai` |
| 标注失败/只读 | 关闭 Excel 占用；确认已装 pywin32 与桌面 Excel |
| 无 active 指针 | 默认使用 `rules/app_rules.yaml` + `rules/audit_rules.yaml` |

## 验证

```powershell
pytest -q
coverage run --source=voucher_audit -m pytest -q
coverage report --fail-under=38
ruff check voucher_audit tools tests
python -m compileall -q voucher_audit tools tests
```

真实 COM 集成（默认跳过）：

```powershell
$env:VOUCHER_AUDIT_EXCEL_INTEGRATION = "1"
pytest -q -m excel_com
```

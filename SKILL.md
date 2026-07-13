---
name: voucher-audit-skill
description: Use when performing month-end voucher audit on Excel exports (data summary + income/cost), needing preview-before-run confirmation, optional source Excel annotation with a second confirmation, and a repair loop for missing file/sheet/column rule mismatches.
---

# Voucher Audit Skill

## 应用场景

月末需要对凭证相关导出的 Excel 做一致性/波动性/格式性审核，并产出可回传的汇总报告（xlsx），必要时对源表进行颜色标注与“问题说明列”回写。

该 skill 的核心约束：

- **先预览再执行**：必须先列出“将执行的审核事项（表/字段/方法/输出）”，让使用人确认。
- **源文件标注二次确认**：标注会修改源 Excel，必须额外确认。
- **修复闭环**：如果规则与实际文件结构不匹配（缺文件/缺 sheet/缺列），可通过 `repair` 生成补丁、预演、确认后版本化落盘并切换 active。

## 入口命令

在本仓库根目录执行：

```powershell
python -m voucher_audit preview --workdir "D:\path\to\workdir" --show-inputs
python -m voucher_audit run --workdir "D:\path\to\workdir"
python -m voucher_audit run --workdir "D:\path\to\workdir" --no-annotate
python -m voucher_audit repair --workdir "D:\path\to\workdir"
python -m voucher_audit rules show-active
```

## 输入与输出

**输入（workdir 目录）**

- `数据汇总.xlsx`
- `考核表输出.xlsx`

（文件名、sheet 名、列映射均可在 `rules/app_rules.yaml` 配置）

**输出**

- `workdir/凭证审核输出/凭证审核报告_YYYYMM_时间戳.xlsx`

## 执行流程（run）

1. `preview`：打印将执行的审核事项列表（规则ID、scope/type、涉及字段、输出页）。
2. 确认1：确认无误后继续。
3. 执行审核：按规则产出报告 xlsx。
4. 若启用标注：确认2 后，通过 Excel COM 对源文件进行标注回写。

## 修复闭环（repair）

当 `run` 报错（常见为缺文件/缺 sheet/缺列）时：

1. 运行 `repair`。
2. 查看它生成的 `app_rules.diff` / `audit_rules.diff`。
3. 预演通过后确认写入。
4. 写入 `rules/versions/` 并更新 `rules/active_rules.json`，后续 `run` 会自动使用 active 版本。

## 规则维护

- 新增/调整“审核规则”：编辑 `rules/audit_rules.yaml` 的 `checks`。
- 新增/调整“应用规则”：编辑 `rules/app_rules.yaml`（files/sheets/columns/report_format/annotation_policy）。

注意：源文件标注依赖 Windows + Excel + `pywin32`，且源文件不要被 Excel 打开占用。

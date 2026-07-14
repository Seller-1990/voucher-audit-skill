# 凭证审核 Skill v1 设计（规则 + 审核规则 + 脚本集合）

> 历史归档：本文记录 2026-04-23 的初始设计，不代表当前规则数量、模块结构或运行命令。当前说明以根目录 `README.md` 为准。

日期：2026-04-23

## 1. 背景与目标

你希望把当前“凭证审核工具”从偏 GUI/打包的应用形态，收敛成一个任何 IDE/CLI 都能运行的 **Skill（规则 + 审核规则 + 脚本）集合**。

核心业务场景（v1 主线）：

- 月末对单个 `workdir` 执行凭证审核
- 运行前先把“将执行的审核事项（表/字段/方法）”逐条列出给人确认，允许当场修正
- 确认后执行审核并输出汇总 `xlsx`
- 源文件需进行颜色标记，并在表格右侧空 1 列后写 3 列标注（异常项/规则ID/命中原因）
- 如果新增/发现规则或脚本问题，需要能持续维护优化（至少做到：生成可审阅的补丁建议，确认后落盘新版本）

## 2. 非目标（v1 不做）

- 多 `workdir` 批处理与跨目录汇总
- 云端/多人在线平台
- “运行成功但结果分布异常”的自动触发优化（v1 只做最小高信号触发）
- 自动覆盖主分支规则（任何规则变更都需要人确认）

## 3. 术语

- `workdir`：月结业务目录，包含审核所需的源 Excel（例如 `数据汇总.xlsx`、`考核表输出.xlsx`）
- App Rules（应用规则）：输入文件名、sheet 匹配、列名候选、报表格式、标注策略等
- Audit Rules（审核规则）：审核 checks 列表（规则ID、类型、严重度、描述、params 等）
- Patch Actions：受控补丁动作集合（`actions: [...]`），用于修改 App Rules/Audit Rules

## 4. 仓库内文件结构（规则跟 skill 走）

规则不随 `workdir` 分散，统一放在仓库 `rules/` 目录。

- `rules/app_rules.yaml`
  - 输入文件名、sheet 匹配规则、列名候选映射
  - 报表输出格式（sheet 名、列 rename/keep/order）
  - 源文件标注默认策略（是否开启、gap 列数、可高亮列白名单等）

- `rules/audit_rules.yaml`
  - `checks: [...]` 列表
  - 每条 rule：`id/type/severity/description/source/params/...`

- `rules/versions/`
  - 由确认环节生成的规则快照目录（可包含 app+audit 两份）

- `rules/active_rules.json`
  - 生效指针，指向当前应使用的版本快照
  - 推荐内容：
    - `active.app_rules`: `rules/versions/app_rules_YYYYMMDDHHMMSS.yaml`
    - `active.audit_rules`: `rules/versions/audit_rules_YYYYMMDDHHMMSS.yaml`
    - `updated_at`: 时间戳

- `logs/skill_audit.log`
  - 运行与修复审计日志（JSONL，一行一个事件）

约束：

- 主分支（`main`）只合并 `rules/app_rules.yaml`、`rules/audit_rules.yaml` 和必要引擎变更。
- `rules/versions/*`、`rules/active_rules.json` 作为运行态/个人态产物，不合并进 `main`。

## 5. 使用方式（任何 IDE/CLI 可用）

主入口使用 `python -m voucher_audit ...`。

### 5.1 命令与行为

- `python -m voucher_audit preview --workdir <dir>`
  - 读取规则（active 版本优先，否则读取 base 规则）
  - 扫描/读取 `workdir` 必要源文件并做基础一致性检查
  - 输出“本次将执行的审核事项清单”（逐条说明：数据来源表、字段、方法、输出落点）
  - 输出“可修正项”提示（例如 sheet/列名未匹配）

- `python -m voucher_audit run --workdir <dir>`
  - 内置执行流程：
    1. 生成并展示审核事项清单（等价于 preview 输出）
    2. **确认 1**：是否按此清单继续执行（y/N）
    3. 执行审核并写出汇总报告 `xlsx`
    4. 若启用源文件标注：
       - **确认 2**：明确提示“将修改源文件（Excel COM 回写）”并要求二次确认（y/N）
       - 确认后执行源文件标注

- `python -m voucher_audit repair --workdir <dir>`
  - 仅在失败时使用（或未来可被 `run` 自动建议）
  - 基于失败原因生成 Patch Actions（允许修改 `inputs.sheets/inputs.columns`，以及 `checks/report_format/annotation_policy` 等）
  - 展示：diff + 预演摘要
  - y/N 确认后才落盘版本快照并更新 active 指针

### 5.2 交互确认约束

- 任何会“写规则文件（生成新版本/切换 active）”的动作都必须 `y` 明确确认
- 任何会“修改源 Excel（COM 回写）”的动作都必须二次确认

## 6. 预览清单（Preview Spec）

`preview`/`run` 的确认前输出必须逐条列出：

- 规则ID、严重度、规则描述、制度来源
- 使用的数据集（辅助账/收入成本/映射表）
- 对应的源文件（由 `app_rules.yaml` 的 inputs 映射到 `数据汇总.xlsx` 或 `考核表输出.xlsx`）
- 涉及字段（来自 `rule.type + rule.params`，必要时要求 rule 显式声明 `fields`）
- 检查方法（rule type 对应的算法，如：required_fields/allowed_values/regex/hard_rule/combo_drift/pp_change 等）
- 命中后输出到报告的 sheet（逻辑 key + 真实 sheet 名）
- 若启用标注：
  - 右侧 gap 列数（固定默认为 1，可配置）
  - 标注列头（固定三列）
  - 高亮列策略（按 rule_id 白名单映射）

预览阶段可修正：

- 输入文件名/路径策略（仅 `workdir` 内）
- sheet 匹配（名称/模糊匹配规则）
- 列名候选（增加/修正候选列表）
- 个别规则 params（阈值/容差等）
- 标注策略（哪些列可以高亮）

## 7. 执行与输出

### 7.1 审核执行

复用既有审核引擎能力（从原项目提取）：

- 读取 `workdir` 源 Excel → 标准化列名 → 选择目标月份 → `run_checks` 执行规则
- 输出汇总报告到 `<workdir>/凭证审核输出/<prefix>_<yyyymm>_<ts>.xlsx`

### 7.2 源文件标注（PQ 安全模式）

复用既有标注实现（从原项目提取）：

- 通过命中结果的 `_row_index` + 规则ID映射推导“源表行 + 高亮列”
- Excel COM 打开源工作簿，对查询表：
  - 表右侧留空 1 列后写 3 列标注（异常项/规则ID/命中原因）
  - 对命中单元格做底色高亮
  - 清理上一次标注痕迹后再写入（按已有实现）

约束：

- 仅 Windows + 本机 Excel + 交互桌面会话
- 失败时给出可读错误与处理建议（复用已有 friendly error 文本逻辑）

## 8. 修复闭环（持续维护优化）

v1 触发条件（最小高信号）：

- 缺少必需输入文件
- sheet 无法匹配
- 必需列缺失/列名不匹配
- 规则文件解析失败
- guardrails 校验失败
- 任何导致 `run` 无法完成的异常

修复闭环工作方式：

1. 收集失败上下文：异常信息 + 当前规则摘要 + `workdir` 文件扫描摘要
2. 生成 Patch Actions：
   - 允许修改：`app_rules.inputs.sheets`、`app_rules.inputs.columns`
   - 允许修改：`audit_rules.checks[*].params`（受 guardrails 限制）
   - 允许修改：`report_format`、标注策略（受 guardrails 限制）
3. guardrails 校验：
   - 白名单操作：`update_check/add_check/set_report_format`（以及新增 `set_inputs` 或等价动作）
   - 字段类型/范围校验
   - 风险等级评估
4. 预演（simulation）：
   - 至少保证 `load_audit_context` 能通过（文件/表/列已能解析）
   - 必要时对少量样本行做“预览命中”验证
5. 输出 diff + 预演摘要，等待 `y/N`
6. `y`：写出版本快照 + 更新 `rules/active_rules.json`

备注：生成补丁的策略可以先做“非 AI”的确定性建议（如：把缺失列名加入候选），后续再引入 AI 生成 patch。

## 9. 兼容性与迁移

从原项目迁移：

- `voucher_audit_rules.yaml` 拆分为：
  - `rules/app_rules.yaml`（inputs + report_format + annotation_policy）
  - `rules/audit_rules.yaml`（checks）

引擎层提取：

- `src/voucher_audit/runner.py`、`checks.py`、`report.py`
- `src/voucher_audit/source_annotation.py`、`excel_annotation_com.py`
- `src/voucher_audit/rule_patcher.py`、`rule_guardrails.py`

## 10. 日志与可审计性

建议日志采用 JSONL：

- `run_start/run_confirmed/run_done`
- `annotate_confirmed/annotate_done`
- `repair_suggested/repair_confirmed/repair_applied`

所有关键事件记录：时间、workdir、规则版本、输出路径、异常栈摘要。

## 11. 测试与验证（v1 最低要求）

- 单元测试：规则解析、patch 应用、guardrails 校验、preview 清单生成
- 集成测试：使用 `temp/smoke-data/` 的样例数据跑一次 `run`（不启用 COM 标注）
- 标注能力：提供 `probe_excel_annotation_environment` 自检命令（复用既有）

## 12. SKILL.md 行为约束（Codex/Agent）

Skill 的目的：把审核过程变成“先预览清单并确认，再执行脚本，再产出报告与标注”的固定流程；并在失败时走“修复建议 → 预演 → 确认 → 版本化落盘与切换生效”。

### 12.1 强制流程（不可跳过）

1. 询问并确认 `workdir`（必须是本机路径）。
2. 调用 `preview`：读取 `rules/app_rules.yaml` + `rules/audit_rules.yaml`（或 active 版本），并生成逐条审核事项清单。
3. 把清单用表格输出给用户确认（至少包含：规则ID、数据来源文件、sheet、字段、方法、输出 sheet、是否参与源文件标注）。
4. 若清单存在明显错误（文件缺失、sheet/列未匹配、规则无法解释字段来源），必须先进入修复：
   - 生成 Patch Actions（仅允许受控变更范围）
   - 展示 diff + 预演摘要
   - 明确 y/N 让用户确认
   - 应用后重跑 `preview`，直到预览通过
5. 用户确认清单后，才允许调用 `run`。
6. `run` 若将执行 Excel COM 回写，必须二次确认（明确提示会修改源文件）。

### 12.2 变更安全门（Hard Gates）

- 未经用户明确确认：禁止写入 `rules/versions/*` 与 `rules/active_rules.json`。
- 未经用户二次确认：禁止修改任何源 Excel（Excel COM 回写）。
- 任何修复建议必须可审阅：必须输出 diff + 预演摘要，不允许静默落盘。

### 12.3 预览清单字段推导

- 对于常见 rule type：从 `rule.params` 推导字段（例如 `field/required/key_fields/value_field/...`），并映射到 `app_rules.yaml` 的 inputs 表/列。
- 对于无法可靠推导字段的规则：预览输出必须标记“字段/表来源未知”，并要求用户补充或通过 patch 增加元信息（例如在 rule 中新增 `meta.fields_used` 供预览使用）。

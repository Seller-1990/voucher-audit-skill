# 凭证审核 Skill v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在新目录 `voucher-audit-skill/` 交付一个可在任何 IDE/CLI 运行的凭证审核 Skill：

- `python -m voucher_audit run --workdir <dir>`
- 运行前先“读取规则+读取文件”，逐条列出将执行的审核事项（表/字段/方法/输出）供人确认
- 确认后执行审核并输出汇总 `xlsx`
- 默认支持源文件标注（Excel COM 回写）：右侧空 1 列后写 3 列标注，并对命中单元格标色；回写前必须二次确认
- 失败时 `repair` 生成可审阅补丁（允许修改 `inputs.sheets/inputs.columns` 等），预演通过后 y/N 确认，确认后版本化落盘并切换 active

**Architecture:** 从旧项目提取“审核引擎 + 报表 + 标注”模块；新增“规则拆分读取/编译、preview 清单、repair 修复闭环、版本化与 active 指针、CLI”。

**Tech Stack:** Python 3.x, pandas, openpyxl, PyYAML, pywin32(可选, 标注), pytest

---

## 0. 路径

- 新目录：`D:\OneDrive - PowerBI学谦\Software Development\工具软件\voucher-audit-skill`
- 旧目录（仅用于提取，不做删除）：`D:\OneDrive - PowerBI学谦\Software Development\工具软件\凭证审核工具`

---

## 1. 目标文件结构（新仓库）

**Create:**
- `requirements.txt`
- `requirements-dev.txt`
- `.gitignore`
- `voucher_audit/__init__.py`
- `voucher_audit/__main__.py`
- `voucher_audit/cli.py`
- `voucher_audit/rules_io.py`（读取 app/audit 规则 + active 指针 + 生成 compiled 规则文件）
- `voucher_audit/preview.py`（生成预览事项清单）
- `voucher_audit/versioning.py`（版本化落盘 + 更新 active 指针）
- `voucher_audit/repair.py`（失败→补丁建议→diff→预演→确认→落盘）
- `rules/app_rules.yaml`
- `rules/audit_rules.yaml`
- `rules/versions/.gitkeep`
- `README.md`
- `SKILL.md`
- `tests/test_rules_io.py`
- `tests/test_preview.py`
- `tests/test_versioning.py`
- `tests/test_run_smoke.py`

**Copy (from old project):**
- `voucher_audit/ai_review.py`
- `voucher_audit/checks.py`
- `voucher_audit/config.py`
- `voucher_audit/constants.py`
- `voucher_audit/excel_annotation_com.py`
- `voucher_audit/excel_io.py`
- `voucher_audit/file_context.py`
- `voucher_audit/logging_util.py`
- `voucher_audit/report.py`
- `voucher_audit/rule_guardrails.py`
- `voucher_audit/rule_patcher.py`
- `voucher_audit/rules_engine.py`
- `voucher_audit/runner.py`
- `voucher_audit/source_annotation.py`
- `voucher_audit/url_utils.py`

---

### Task 1: 初始化仓库骨架

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `.gitignore`, `voucher_audit/__init__.py`, `voucher_audit/__main__.py`

- [ ] **Step 1: 写入 requirements 文件**

Create `requirements.txt`:
```text
openai>=1.0
openpyxl>=3.1
pandas>=2.0
PyYAML>=6.0
pywin32>=306; platform_system == "Windows"
```

Create `requirements-dev.txt`:
```text
pytest>=8.0
```

- [ ] **Step 2: 创建 .gitignore**

Create `.gitignore`:
```gitignore
__pycache__/
*.pyc
.pytest_cache/
.venv/

# 运行态输出
logs/

# 规则运行态产物：主分支不合并；如需在个人分支提交可 git add -f
rules/active_rules.json
rules/compiled_rules.yaml
rules/versions/
!rules/versions/.gitkeep

# workdir 输出不应进入仓库
凭证审核输出/
```

- [ ] **Step 3: 创建 Python 包入口**

Create `voucher_audit/__init__.py`:
```python
__all__ = []
```

Create `voucher_audit/__main__.py`:
```python
from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 安装依赖并验证 import**

Run:
```powershell
pip install -r .\requirements.txt
pip install -r .\requirements-dev.txt
python -c "import voucher_audit; print('ok')"
```
Expected: prints `ok`.

---

### Task 2: 提取旧项目引擎文件（保证能 import）

**Files:**
- Copy: 见清单

- [ ] **Step 1: 复制核心引擎文件**

Run:
```powershell
$old = "D:\OneDrive - PowerBI学谦\Software Development\工具软件\凭证审核工具\src\voucher_audit"
$new = "D:\OneDrive - PowerBI学谦\Software Development\工具软件\voucher-audit-skill\voucher_audit"

New-Item -ItemType Directory -Force $new | Out-Null

Copy-Item "$old\ai_review.py" -Destination "$new\ai_review.py" -Force
Copy-Item "$old\checks.py" -Destination "$new\checks.py" -Force
Copy-Item "$old\config.py" -Destination "$new\config.py" -Force
Copy-Item "$old\constants.py" -Destination "$new\constants.py" -Force
Copy-Item "$old\excel_annotation_com.py" -Destination "$new\excel_annotation_com.py" -Force
Copy-Item "$old\excel_io.py" -Destination "$new\excel_io.py" -Force
Copy-Item "$old\file_context.py" -Destination "$new\file_context.py" -Force
Copy-Item "$old\logging_util.py" -Destination "$new\logging_util.py" -Force
Copy-Item "$old\report.py" -Destination "$new\report.py" -Force
Copy-Item "$old\rule_guardrails.py" -Destination "$new\rule_guardrails.py" -Force
Copy-Item "$old\rule_patcher.py" -Destination "$new\rule_patcher.py" -Force
Copy-Item "$old\rules_engine.py" -Destination "$new\rules_engine.py" -Force
Copy-Item "$old\runner.py" -Destination "$new\runner.py" -Force
Copy-Item "$old\source_annotation.py" -Destination "$new\source_annotation.py" -Force
Copy-Item "$old\url_utils.py" -Destination "$new\url_utils.py" -Force
```

- [ ] **Step 2: 验证 runner 能 import**

Run:
```powershell
python -c "import voucher_audit.runner; print('import runner ok')"
```
Expected: prints `import runner ok`.

---

### Task 3: 创建 app_rules.yaml 与 audit_rules.yaml

**Files:**
- Create: `rules/app_rules.yaml`, `rules/audit_rules.yaml`, `rules/versions/.gitkeep`

- [ ] **Step 1: 创建 rules 目录与占位**

Run:
```powershell
New-Item -ItemType Directory -Force rules | Out-Null
New-Item -ItemType Directory -Force rules\versions | Out-Null
Set-Content -Encoding utf8 -NoNewline rules\versions\.gitkeep ""
```

- [ ] **Step 2: 写 app_rules.yaml**

Create `rules/app_rules.yaml`:
```yaml
inputs:
  files:
    data_summary: "数据汇总.xlsx"
    income_cost: "考核表输出.xlsx"
  sheets:
    aux_ledger:
      preferred: ["调整后序时账"]
      fuzzy_contains_any: ["序时账", "辅助帐"]
    income_cost:
      preferred: ["收入成本表"]
      fuzzy_contains_any: ["收入成本"]
  columns:
    aux_ledger:
      entity: ["主体账簿"]
      month: ["月", "月份"]
      day: ["日"]
      voucher_no: ["凭证号"]
      summary: ["摘要"]
      acct1: ["一级科目"]
      acct2: ["二级科目"]
      acct3: ["三级科目"]
      customer_book: ["账载客户"]
      customer_actual: ["实际客户"]
      cashflow_item: ["收支项目"]
      dept: ["部门"]
      project: ["项目"]
      amount: ["本币"]
      sealed: ["是否封存"]
    income_cost:
      entity: ["主体账簿"]
      month: ["月", "月份"]
      biz_type: ["三级科目"]
      customer_book: ["账载客户"]
      customer_actual: ["实际客户"]
      dept: ["部门"]
      project: ["项目"]
      revenue_net: ["净额收入"]
      revenue_gross: ["全额收入"]
      cost_total: ["成本合计"]
      profit: ["项目毛利润"]
      settlement_cnt: ["结算人次"]
      rebate: ["项目返费"]
      third_party_cost: ["第三方挂靠成本", "第三方挂靠"]

thresholds:
  drift_dominance_ratio: 0.7
  gross_margin: {}

aio:
  enabled_default: false
  model: "gpt-5.4"
  base_url: ""
  api_key_env: "OPENAI_API_KEY"
  max_items_per_section: 40
  max_output_tokens: 1200

annotation_policy:
  enabled_default: true
  gap_columns: 1

report_format:
  sheet_names:
    overview: "概览"
    aux_rule_violations: "入账规则违规_辅助帐"
    aux_suspect_wrong_account: "疑似错科目_辅助帐"
    income_dim_anomalies: "维度异常_收入成本"
    combo_drift_friendly: "主映射异常_友好视图"
    income_gm_anomalies: "毛利异常_收入成本"
    ai_review: "AI复核意见"
  column_layouts: {}
```

- [ ] **Step 3: 写 audit_rules.yaml**

Create `rules/audit_rules.yaml`:
```yaml
checks:
  - id: INC_FULL_COMBO_DRIFT
    type: combo_drift
    scope: income_cost
    severity: "错误"
    description: "按（主体账簿、账载客户）为主键，检查（三级科目、实际客户、部门、项目）是否与前置月份的历史主映射不一致。"
    source:
      doc: "（统计异常）"
      clause: "完整组合稳定性"
    params:
      key_fields: ["主体账簿", "账载客户"]
      value_fields: ["三级科目", "实际客户", "部门", "项目"]
      amount_field: "净额收入"
      min_amount_abs: 50000

  - id: INC_REV_COST_ZERO_MISMATCH
    type: rev_cost_zero_mismatch
    scope: income_cost
    severity: "错误"
    description: "检查组合（主体账簿+三级科目+实际客户+部门+项目）下是否出现：全额收入=0但成本合计≠0，或成本合计=0但全额收入≠0。"
    source:
      doc: "（一致性校验）"
      clause: "收入/成本同向性"
    params:
      key_fields: ["主体账簿", "三级科目", "实际客户", "部门", "项目"]
      revenue_field: "全额收入"
      cost_field: "成本合计"
      eps: 1e-6

  - id: AUX_SUMMARY_ZS_SUFFIX
    type: summary_zs_suffix
    scope: aux_ledger
    severity: "错误"
    description: "检查调整后序时账摘要中 Z\\d+S\\d+（如 Z5S0 / z5s1）后是否紧跟其它字符（含文字、- 等）。"
    source:
      doc: "（格式校验）"
      clause: "摘要 Z*S* 码规范"
    params:
      summary_field: "摘要"
      voucher_field: "凭证号"
      month_field: "月"
      pattern: "(?i)Z\\\\d+S\\\\d+"
      allowed_next_chars: ["", " ", "\\t", "，", ",", "。", ".", "；", ";", "：", ":", "、", "/", "\\\\", "|", ")", "）", "]", "】", "}", "）"]

  - id: INC_METRIC_PP_CHANGE
    type: metric_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查毛利率与单人毛利相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "指标稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      revenue_guard_field: "净额收入"
      min_revenue: 0
      metrics:
        - name: "毛利率"
          numerator: "项目毛利润"
          denominator: "净额收入"
        - name: "单人毛利"
          numerator: "项目毛利润"
          denominator: "结算人次"

  - id: INC_VALUE_PP_CHANGE
    type: value_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查项目返费、第三方挂靠成本相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "费用稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      value_fields: ["项目返费", "第三方挂靠成本"]
      min_abs: 0

  - id: INC_RATIO_PP_CHANGE
    type: ratio_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查返费率与挂靠占比相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "比率稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      ratios:
        - name: "项目返费/净额收入"
          numerator: "项目返费"
          denominator: "净额收入"
        - name: "第三方挂靠成本/全额收入"
          numerator: "第三方挂靠成本"
          denominator: "全额收入"
```

---

### Task 4: 规则读取与编译（支持 active 指针）

**Files:**
- Create: `voucher_audit/rules_io.py`
- Test: `tests/test_rules_io.py`

- [ ] **Step 1: 实现 rules_io.py**

Create `voucher_audit/rules_io.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .config import RuleConfig, load_rules_data


@dataclass(frozen=True)
class RulesPaths:
    app_rules: Path
    audit_rules: Path
    compiled_rules: Path


@dataclass(frozen=True)
class ActiveRulesPointer:
    app_rules: Path
    audit_rules: Path
    compiled_rules: Path


def repo_root_from_module() -> Path:
    return Path(__file__).resolve().parent.parent


def _read_yaml_obj(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"规则文件解析失败：{path}\n{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"规则文件顶层必须是对象：{path}")
    return dict(data)


def load_app_rules(path: Path) -> dict[str, Any]:
    data = _read_yaml_obj(path)
    data.pop("checks", None)
    return data


def load_audit_rules(path: Path) -> dict[str, Any]:
    data = _read_yaml_obj(path)
    checks = data.get("checks", []) or []
    if not isinstance(checks, list):
        raise ValueError(f"audit_rules.checks 必须是 list：{path}")
    return {"checks": list(checks)}


def compile_rules(app_rules: dict[str, Any], audit_rules: dict[str, Any]) -> dict[str, Any]:
    out = dict(app_rules)
    out["checks"] = list(audit_rules.get("checks", []) or [])
    return out


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def default_rules_paths(repo_root: Path) -> RulesPaths:
    rules_dir = (repo_root / "rules").resolve()
    return RulesPaths(
        app_rules=(rules_dir / "app_rules.yaml"),
        audit_rules=(rules_dir / "audit_rules.yaml"),
        compiled_rules=(rules_dir / "compiled_rules.yaml"),
    )


def load_active_pointer(repo_root: Path) -> ActiveRulesPointer | None:
    p = (repo_root / "rules" / "active_rules.json").resolve()
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8") or "{}")
    active = data.get("active", {}) or {}
    a = str(active.get("app_rules", "")).strip()
    b = str(active.get("audit_rules", "")).strip()
    c = str(active.get("compiled_rules", "")).strip()
    if not a or not b or not c:
        return None
    app = (repo_root / a).resolve()
    audit = (repo_root / b).resolve()
    compiled = (repo_root / c).resolve()
    if not app.exists() or not audit.exists() or not compiled.exists():
        return None
    return ActiveRulesPointer(app_rules=app, audit_rules=audit, compiled_rules=compiled)


def ensure_compiled_rules(repo_root: Path) -> RulesPaths:
    active = load_active_pointer(repo_root)
    if active is not None:
        return RulesPaths(app_rules=active.app_rules, audit_rules=active.audit_rules, compiled_rules=active.compiled_rules)

    base = default_rules_paths(repo_root)
    app = load_app_rules(base.app_rules)
    audit = load_audit_rules(base.audit_rules)
    compiled = compile_rules(app, audit)
    base.compiled_rules.write_text(dump_yaml(compiled).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    return base


def load_compiled_rule_config(paths: RulesPaths) -> RuleConfig:
    compiled = _read_yaml_obj(paths.compiled_rules)
    return load_rules_data(compiled)
```

- [ ] **Step 2: 单测 rules_io**

Create `tests/test_rules_io.py`:
```python
from __future__ import annotations

import json
from pathlib import Path

from voucher_audit.rules_io import compile_rules, load_active_pointer


def test_compile_rules_overrides_checks() -> None:
    app = {"inputs": {"files": {}}, "checks": [{"id": "OLD"}]}
    audit = {"checks": [{"id": "NEW"}]}
    out = compile_rules(app, audit)
    assert out["checks"][0]["id"] == "NEW"


def test_load_active_pointer_returns_none_when_missing(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "rules").mkdir()
    assert load_active_pointer(repo) is None


def test_load_active_pointer_ok(tmp_path: Path) -> None:
    repo = tmp_path
    rules = repo / "rules"
    rules.mkdir()
    (rules / "versions").mkdir()

    app = rules / "versions" / "app.yaml"
    audit = rules / "versions" / "audit.yaml"
    compiled = rules / "versions" / "compiled.yaml"
    app.write_text("inputs: {}\n", encoding="utf-8")
    audit.write_text("checks: []\n", encoding="utf-8")
    compiled.write_text("inputs: {}\nchecks: []\n", encoding="utf-8")

    pointer = {
        "active": {
            "app_rules": str(app.relative_to(repo)),
            "audit_rules": str(audit.relative_to(repo)),
            "compiled_rules": str(compiled.relative_to(repo)),
        }
    }
    (rules / "active_rules.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")

    got = load_active_pointer(repo)
    assert got is not None
    assert got.compiled_rules.name == "compiled.yaml"
```

- [ ] **Step 3: 运行测试**

Run:
```powershell
pytest -q
```
Expected: PASS.

---

### Task 5: Preview 清单生成（逐条列事项）

**Files:**
- Create: `voucher_audit/preview.py`
- Test: `tests/test_preview.py`

- [ ] **Step 1: 实现 preview.py（字段推导 + 输出分类）**

Create `voucher_audit/preview.py`:
```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import RuleConfig


@dataclass(frozen=True)
class PreviewItem:
    rule_id: str
    severity: str
    scope: str
    rule_type: str
    fields: tuple[str, ...]
    method: str
    output_logical_sheet: str


def _fields_from_rule(rule: dict[str, Any]) -> list[str]:
    rtype = str(rule.get("type", ""))
    params = rule.get("params", {}) or {}

    out: list[str] = []

    def add(v: Any) -> None:
        if v is None:
            return
        if isinstance(v, list):
            for x in v:
                add(x)
            return
        s = str(v).strip()
        if s and s not in out:
            out.append(s)

    if rtype == "hard_rule":
        when = params.get("when") or {}
        add(when.get("field"))
        expect = params.get("expect") or {}
        add(expect.get("field"))
    elif rtype == "allowed_values":
        add(params.get("field"))
    elif rtype == "required_fields":
        add(params.get("required"))
    elif rtype == "summary_zs_suffix":
        add(params.get("summary_field"))
        add(params.get("voucher_field"))
        add(params.get("month_field"))
    elif rtype == "combo_drift":
        add(params.get("key_fields"))
        add(params.get("value_fields"))
        add(params.get("amount_field"))
    elif rtype == "rev_cost_zero_mismatch":
        add(params.get("key_fields"))
        add(params.get("revenue_field"))
        add(params.get("cost_field"))
    elif rtype == "metric_pp_change":
        add(params.get("key_fields"))
        add(params.get("month_field"))
        add(params.get("revenue_guard_field"))
        for m in (params.get("metrics") or []):
            if isinstance(m, dict):
                add(m.get("numerator"))
                add(m.get("denominator"))
    elif rtype == "value_pp_change":
        add(params.get("key_fields"))
        add(params.get("month_field"))
        add(params.get("value_fields"))
    elif rtype == "ratio_pp_change":
        add(params.get("key_fields"))
        add(params.get("month_field"))
        for r in (params.get("ratios") or []):
            if isinstance(r, dict):
                add(r.get("numerator"))
                add(r.get("denominator"))
    elif rtype == "gross_margin":
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        add(params.get("cost_field"))
        add(params.get("profit_field"))

    return out


def _output_logical_sheet(scope: str, rule_type: str) -> str:
    if scope == "aux_ledger":
        if rule_type in {"hard_rule", "allowed_values", "required_fields"}:
            return "aux_rule_violations"
        return "aux_suspect_wrong_account"
    if scope == "income_cost":
        if rule_type == "gross_margin":
            return "income_gm_anomalies"
        return "income_dim_anomalies"
    return "overview"


def build_preview_items(rules: RuleConfig) -> list[PreviewItem]:
    items: list[PreviewItem] = []
    for rule in rules.checks:
        items.append(
            PreviewItem(
                rule_id=str(rule.get("id", "")).strip(),
                severity=str(rule.get("severity", "")) or "需确认",
                scope=str(rule.get("scope", "")).strip(),
                rule_type=str(rule.get("type", "")).strip(),
                fields=tuple(_fields_from_rule(rule)),
                method=str(rule.get("type", "")).strip(),
                output_logical_sheet=_output_logical_sheet(str(rule.get("scope", "")).strip(), str(rule.get("type", "")).strip()),
            )
        )
    return items
```

- [ ] **Step 2: Preview 单测**

Create `tests/test_preview.py`:
```python
from __future__ import annotations

from voucher_audit.preview import build_preview_items
from voucher_audit.config import load_rules_data


def test_preview_extracts_fields_for_combo_drift() -> None:
    rules = load_rules_data(
        {
            "inputs": {"files": {}, "sheets": {}, "columns": {}},
            "thresholds": {},
            "ai": {},
            "report_format": {},
            "checks": [
                {
                    "id": "R1",
                    "type": "combo_drift",
                    "scope": "income_cost",
                    "params": {"key_fields": ["主体账簿"], "value_fields": ["部门"], "amount_field": "净额收入"},
                }
            ],
        }
    )
    items = build_preview_items(rules)
    assert items[0].fields == ("主体账簿", "部门", "净额收入")
    assert items[0].output_logical_sheet == "income_dim_anomalies"
```

- [ ] **Step 3: 运行测试**

Run:
```powershell
pytest -q
```
Expected: PASS.

---

### Task 6: 版本化落盘与 active 指针

**Files:**
- Create: `voucher_audit/versioning.py`
- Test: `tests/test_versioning.py`

- [ ] **Step 1: 实现 versioning.py**

Create `voucher_audit/versioning.py`:
```python
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .rules_io import dump_yaml


@dataclass(frozen=True)
class VersionedRules:
    app_rules_path: Path
    audit_rules_path: Path
    compiled_rules_path: Path


def write_version_snapshot(*, repo_root: Path, app_rules: dict[str, Any], audit_rules: dict[str, Any], compiled_rules: dict[str, Any]) -> VersionedRules:
    out_dir = (repo_root / "rules" / "versions").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")

    app_path = out_dir / f"app_rules_{ts}.yaml"
    audit_path = out_dir / f"audit_rules_{ts}.yaml"
    compiled_path = out_dir / f"compiled_rules_{ts}.yaml"

    app_path.write_text(dump_yaml(app_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    audit_path.write_text(dump_yaml(audit_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    compiled_path.write_text(dump_yaml(compiled_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    return VersionedRules(app_rules_path=app_path, audit_rules_path=audit_path, compiled_rules_path=compiled_path)


def update_active_pointer(repo_root: Path, snap: VersionedRules) -> Path:
    p = (repo_root / "rules" / "active_rules.json").resolve()
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "active": {
            "app_rules": str(snap.app_rules_path.relative_to(repo_root)),
            "audit_rules": str(snap.audit_rules_path.relative_to(repo_root)),
            "compiled_rules": str(snap.compiled_rules_path.relative_to(repo_root)),
        },
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return p
```

- [ ] **Step 2: 单测 versioning**

Create `tests/test_versioning.py`:
```python
from __future__ import annotations

from pathlib import Path

from voucher_audit.versioning import update_active_pointer, write_version_snapshot


def test_write_snapshot_and_pointer(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "rules" / "versions").mkdir(parents=True)

    snap = write_version_snapshot(repo_root=repo, app_rules={"inputs": {}}, audit_rules={"checks": []}, compiled_rules={"inputs": {}, "checks": []})
    assert snap.compiled_rules_path.exists()

    p = update_active_pointer(repo, snap)
    assert p.exists()
    assert "compiled_rules" in p.read_text(encoding="utf-8")
```

- [ ] **Step 3: 运行测试**

Run:
```powershell
pytest -q
```
Expected: PASS.

---

### Task 7: Repair（失败→补丁→diff→预演→确认→版本化落盘）

**Files:**
- Create: `voucher_audit/repair.py`

- [ ] **Step 1: 实现 repair.py（v1 只修 inputs：files/sheets/columns）**

Create `voucher_audit/repair.py`:
```python
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .excel_io import match_sheet_name, open_workbook, read_sheet
from .rules_io import dump_yaml, load_app_rules, load_audit_rules


@dataclass(frozen=True)
class RepairSuggestion:
    ok: bool
    message: str
    app_rules_after: dict[str, Any]
    audit_rules_after: dict[str, Any]
    diff_app: str
    diff_audit: str


def _udiff(name: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    a = dump_yaml(before).splitlines(keepends=True)
    b = dump_yaml(after).splitlines(keepends=True)
    return "".join(difflib.unified_diff(a, b, fromfile=f"{name}:before", tofile=f"{name}:after", lineterm="")) or "(无变化)"


def _best_filename_candidate(workdir: Path, missing_name: str) -> str | None:
    cands = [p.name for p in workdir.glob("*.xlsx")]
    if not cands:
        return None
    best = difflib.get_close_matches(missing_name, cands, n=1, cutoff=0.2)
    return best[0] if best else None


def propose_repair_for_missing_file(app: dict[str, Any], workdir: Path, missing_name: str) -> dict[str, Any] | None:
    inputs = dict(app.get("inputs", {}) or {})
    files = dict((inputs.get("files", {}) or {}))

    # 优先按 key 推断：如果缺的就是 data_summary/income_cost 的默认名，直接尝试替换
    for key in ["data_summary", "income_cost"]:
        if str(files.get(key, "")).strip() == missing_name:
            cand = _best_filename_candidate(workdir, missing_name)
            if cand:
                files[key] = cand
                inputs["files"] = files
                out = dict(app)
                out["inputs"] = inputs
                return out
    return None


def _role_from_sheet_error(msg: str) -> str | None:
    if "辅助帐sheet" in msg or "辅助帐sheet" in msg or "辅助帐" in msg:
        return "aux_ledger"
    if "收入成本表sheet" in msg or "收入成本表" in msg:
        return "income_cost"
    return None


def _role_from_column_error_context(ctx: str) -> str | None:
    t = str(ctx)
    if "辅助帐" in t:
        return "aux_ledger"
    if "收入成本" in t:
        return "income_cost"
    if "客户调整校验" in t:
        return "customer_mapping"
    return None


def _best_sheet_candidate(names: list[str], preferred: list[str]) -> str:
    if not names:
        return ""
    target = preferred[0] if preferred else names[0]
    best = difflib.get_close_matches(target, names, n=1, cutoff=0.2)
    return best[0] if best else names[0]


def propose_repair_for_sheet(app: dict[str, Any], workbook_path: Path, role: str) -> dict[str, Any] | None:
    xls = open_workbook(workbook_path).xls
    names = [str(n) for n in xls.sheet_names]
    if not names:
        return None

    inputs = dict(app.get("inputs", {}) or {})
    sheets = dict((inputs.get("sheets", {}) or {}))
    matcher = dict((sheets.get(role, {}) or {}))
    preferred = [str(x) for x in (matcher.get("preferred", []) or [])]

    cand = _best_sheet_candidate(names, preferred)
    if not cand:
        return None
    if cand not in preferred:
        preferred = preferred + [cand]
    matcher["preferred"] = preferred
    sheets[role] = matcher
    inputs["sheets"] = sheets

    out = dict(app)
    out["inputs"] = inputs
    return out


def propose_repair_for_missing_column(app: dict[str, Any], role: str, missing_key: str, actual_columns: list[str]) -> dict[str, Any] | None:
    if not actual_columns:
        return None

    inputs = dict(app.get("inputs", {}) or {})
    columns = dict((inputs.get("columns", {}) or {}))
    role_map = dict((columns.get(role, {}) or {}))
    existing_candidates = [str(x) for x in (role_map.get(missing_key, []) or [])]

    chosen = ""
    # 优先用“原候选”去匹配实际列名
    for cand0 in existing_candidates:
        hits = difflib.get_close_matches(cand0, actual_columns, n=1, cutoff=0.6)
        if hits:
            chosen = hits[0]
            break
    if not chosen:
        # fallback：用 missing_key 做弱匹配
        hits = difflib.get_close_matches(missing_key, actual_columns, n=1, cutoff=0.2)
        chosen = hits[0] if hits else actual_columns[0]

    cur = existing_candidates
    if chosen not in cur:
        cur = cur + [chosen]
    role_map[missing_key] = cur
    columns[role] = role_map
    inputs["columns"] = columns

    out = dict(app)
    out["inputs"] = inputs
    return out


def suggest_repair(workdir: Path, app_rules_path: Path, audit_rules_path: Path, error: Exception) -> RepairSuggestion:
    app = load_app_rules(app_rules_path)
    audit = load_audit_rules(audit_rules_path)

    msg = str(error)
    app2 = app

    # v1: 针对常见错误做最小建议
    if isinstance(error, FileNotFoundError) and "缺少文件" in msg:
        missing = msg.split("：")[-1].strip()
        patched = propose_repair_for_missing_file(app, workdir, missing)
        if patched is not None:
            app2 = patched
    elif isinstance(error, ValueError) and "无法匹配" in msg and "sheet" in msg:
        role = _role_from_sheet_error(msg)
        if role:
            inputs = dict(app.get("inputs", {}) or {})
            files = dict((inputs.get("files", {}) or {}))
            file_key = "data_summary" if role == "aux_ledger" else "income_cost"
            workbook_path = (workdir / str(files.get(file_key, "")).strip()).resolve()
            if workbook_path.exists():
                patched = propose_repair_for_sheet(app, workbook_path, role)
                if patched is not None:
                    app2 = patched
    elif isinstance(error, KeyError) and "缺少必需列" in msg:
        # 格式示例：\"辅助帐 缺少必需列：month（候选：[...]）\"
        ctx = msg.split("缺少必需列", 1)[0].strip(" '\"")
        role = _role_from_column_error_context(ctx)
        if role:
            # 解析 missing_key
            after = msg.split("缺少必需列：", 1)[-1]
            missing_key = after.split("（", 1)[0].strip()

            inputs = dict(app.get("inputs", {}) or {})
            files = dict((inputs.get("files", {}) or {}))
            sheets = dict((inputs.get("sheets", {}) or {}))

            file_key = "data_summary" if role in {"aux_ledger", "customer_mapping"} else "income_cost"
            workbook_path = (workdir / str(files.get(file_key, "")).strip()).resolve()
            if workbook_path.exists():
                # 尽量用当前 matcher 匹配 sheet；匹配不到时退化为第一个 sheet
                matcher = sheets.get(role, {}) or {}
                preferred = [str(x) for x in (matcher.get("preferred", []) or [])]
                fuzzy = [str(x) for x in (matcher.get("fuzzy_contains_any", []) or [])]

                xls = open_workbook(workbook_path).xls
                # 构造临时 SheetMatcher 复用既有匹配逻辑
                from .config import SheetMatcher

                sheet = match_sheet_name(xls, SheetMatcher(preferred=preferred, fuzzy_contains_any=fuzzy))
                sheet = sheet or (str(xls.sheet_names[0]) if xls.sheet_names else "")
                if sheet:
                    header_df = read_sheet(xls, sheet, nrows=0)
                    actual_columns = [str(c) for c in header_df.columns]
                    patched = propose_repair_for_missing_column(app, role, missing_key, actual_columns)
                    if patched is not None:
                        app2 = patched

    return RepairSuggestion(
        ok=(app2 != app),
        message=("已生成修复建议。" if app2 != app else "未能自动生成修复建议，请手动修改 rules/app_rules.yaml。"),
        app_rules_after=app2,
        audit_rules_after=audit,
        diff_app=_udiff("app_rules", app, app2),
        diff_audit=_udiff("audit_rules", audit, audit),
    )
```

---

### Task 8: CLI（preview/run/repair/rules）与双确认

**Files:**
- Create: `voucher_audit/cli.py`

- [ ] **Step 1: 实现 cli.py（包含 preview/run/repair/rules）**

Create `voucher_audit/cli.py`:
```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .preview import build_preview_items
from .repair import suggest_repair
from .rules_io import ensure_compiled_rules, load_compiled_rule_config, repo_root_from_module
from .runner import load_audit_context, run_audit
from .rules_io import load_app_rules, load_audit_rules, compile_rules
from .versioning import update_active_pointer, write_version_snapshot


def _confirm(prompt: str) -> bool:
    raw = input(prompt).strip().lower()
    return raw in {"y", "yes"}


def _print_preview_table(items) -> None:
    # 简洁输出：后续可替换为更整齐的表格
    for it in items:
        fields = "，".join(it.fields) if it.fields else "（字段未知）"
        print(f"- {it.rule_id} [{it.severity}] scope={it.scope} type={it.rule_type} fields={fields} -> {it.output_logical_sheet}")


def cmd_preview(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    workdir = Path(args.workdir).expanduser().resolve()

    paths = ensure_compiled_rules(repo_root)
    rules = load_compiled_rule_config(paths)

    print("将执行以下审核事项（预览）：")
    _print_preview_table(build_preview_items(rules))

    # 读取文件以验证 inputs/sheets/columns 是否可用
    try:
        _ = load_audit_context(workdir=workdir, rules_path=paths.compiled_rules, target_month=None, logger=None)
    except Exception as e:
        print(f"预览校验失败：{type(e).__name__}: {e}")
        return 1

    print(f"workdir: {workdir}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    workdir = Path(args.workdir).expanduser().resolve()

    paths = ensure_compiled_rules(repo_root)
    rules = load_compiled_rule_config(paths)

    print("将执行以下审核事项（预览）：")
    _print_preview_table(build_preview_items(rules))

    # 预先校验可读
    try:
        _ = load_audit_context(workdir=workdir, rules_path=paths.compiled_rules, target_month=None, logger=None)
    except Exception as e:
        print(f"预览校验失败：{type(e).__name__}: {e}")
        print("可尝试运行：python -m voucher_audit repair --workdir <dir>")
        return 1

    if not _confirm("确认按以上清单执行审核？(y/N) "):
        print("已取消。")
        return 1

    # 默认启用标注，但必须二次确认
    annotate = False
    if _confirm("将修改源 Excel（标注与标色）。确认继续？(y/N) "):
        annotate = True

    r = run_audit(workdir=workdir, rules_path=paths.compiled_rules, enable_ai=False, annotate_source=annotate)
    print(r.message)
    if r.report_path is not None:
        print(f"报告：{r.report_path}")
    if r.annotation_requested:
        print(f"源文件标注：{'成功' if r.annotation_ok else '失败'} / {r.annotation_message}")

    return 0 if r.ok else 1


def cmd_repair(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    workdir = Path(args.workdir).expanduser().resolve()

    # 先尝试校验，拿到错误
    paths = ensure_compiled_rules(repo_root)
    try:
        _ = load_audit_context(workdir=workdir, rules_path=paths.compiled_rules, target_month=None, logger=None)
        print("未检测到预览级别错误，不需要 repair。")
        return 0
    except Exception as e:
        err = e

    sug = suggest_repair(workdir, paths.app_rules, paths.audit_rules, err)
    print(sug.message)
    print("--- app_rules diff ---")
    print(sug.diff_app)

    # 预演：尝试用补丁后的 app_rules+audit_rules 生成临时 compiled 并 load_audit_context
    app_after = sug.app_rules_after
    audit_after = sug.audit_rules_after
    compiled_after = compile_rules(app_after, audit_after)
    tmp_compiled = (repo_root / "rules" / "_compiled_repair_preview.yaml").resolve()
    tmp_compiled.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
    # 写 yaml
    from .rules_io import dump_yaml

    tmp_compiled.write_text(dump_yaml(compiled_after).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    try:
        _ = load_audit_context(workdir=workdir, rules_path=tmp_compiled, target_month=None, logger=None)
        print("预演通过：修复后 inputs/sheets/columns 可解析。")
    except Exception as e:
        print(f"预演失败：{type(e).__name__}: {e}")
        return 1
    finally:
        try:
            tmp_compiled.unlink(missing_ok=True)
        except Exception:
            pass

    if not _confirm("确认应用修复并生成规则新版本、切换为 active？(y/N) "):
        print("已取消。")
        return 1

    snap = write_version_snapshot(repo_root=repo_root, app_rules=app_after, audit_rules=audit_after, compiled_rules=compiled_after)
    p = update_active_pointer(repo_root, snap)
    print(f"已生成新版本并切换 active：{p}")
    return 0


def cmd_rules_show_active(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    p = (repo_root / "rules" / "active_rules.json").resolve()
    if not p.exists():
        print("当前无 active 指针（将使用 base app_rules/audit_rules）。")
        return 0
    print(p.read_text(encoding="utf-8"))
    return 0


def cmd_rules_set_active(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    app = Path(args.app_rules).expanduser().resolve()
    audit = Path(args.audit_rules).expanduser().resolve()
    compiled = Path(args.compiled_rules).expanduser().resolve()
    if not app.exists() or not audit.exists() or not compiled.exists():
        raise FileNotFoundError("set-active 指定的文件不存在")

    p = (repo_root / "rules" / "active_rules.json").resolve()
    data = {
        "updated_at": "manual",
        "active": {
            "app_rules": str(app.relative_to(repo_root)) if str(app).startswith(str(repo_root)) else str(app),
            "audit_rules": str(audit.relative_to(repo_root)) if str(audit).startswith(str(repo_root)) else str(audit),
            "compiled_rules": str(compiled.relative_to(repo_root)) if str(compiled).startswith(str(repo_root)) else str(compiled),
        },
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    print(f"已更新 active 指针：{p}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="voucher_audit", description="凭证审核 Skill")
    sub = parser.add_subparsers(dest="cmd")

    p_preview = sub.add_parser("preview")
    p_preview.add_argument("--workdir", required=True)
    p_preview.set_defaults(func=cmd_preview)

    p_run = sub.add_parser("run")
    p_run.add_argument("--workdir", required=True)
    p_run.set_defaults(func=cmd_run)

    p_repair = sub.add_parser("repair")
    p_repair.add_argument("--workdir", required=True)
    p_repair.set_defaults(func=cmd_repair)

    p_rules = sub.add_parser("rules")
    rules_sub = p_rules.add_subparsers(dest="rules_cmd")

    p_show = rules_sub.add_parser("show-active")
    p_show.set_defaults(func=cmd_rules_show_active)

    p_set = rules_sub.add_parser("set-active")
    p_set.add_argument("--app-rules", required=True)
    p_set.add_argument("--audit-rules", required=True)
    p_set.add_argument("--compiled-rules", required=True)
    p_set.set_defaults(func=cmd_rules_set_active)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))
```

- [ ] **Step 2: CLI 烟测**

Run:
```powershell
python -m voucher_audit rules show-active
python -m voucher_audit preview --workdir .
```
Expected: 不崩溃；preview 在 workdir 不满足时应提示校验失败。

---

### Task 9: 端到端 smoke test（不启用 COM）

**Files:**
- Create: `tests/test_run_smoke.py`

- [ ] **Step 1: 构造最小 workdir 并跑 run_audit**

Create `tests/test_run_smoke.py`:
```python
from __future__ import annotations

from pathlib import Path

import pandas as pd

from voucher_audit.runner import run_audit
from voucher_audit.rules_io import ensure_compiled_rules, repo_root_from_module


def _write_xlsx(path: Path, sheet: str, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        df.to_excel(w, sheet_name=sheet, index=False)


def test_run_audit_smoke(tmp_path: Path) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    aux = pd.DataFrame([
        {
            "月": 202601,
            "凭证号": "V1",
            "一级科目": "A",
            "二级科目": "B",
            "三级科目": "C",
            "实际客户": "X",
            "部门": "D",
            "收支项目": "P",
            "本币": 100,
            "摘要": "Z5S0",
        }
    ])
    inc = pd.DataFrame([
        {
            "月": 202601,
            "三级科目": "C",
            "账载客户": "BK",
            "实际客户": "X",
            "部门": "D",
            "项目": "PJ",
            "净额收入": 100000,
            "全额收入": 100000,
            "成本合计": 50000,
            "项目毛利润": 50000,
            "结算人次": 1,
            "项目返费": 0,
            "第三方挂靠成本": 0,
        }
    ])

    _write_xlsx(workdir / "数据汇总.xlsx", "调整后序时账", aux)
    _write_xlsx(workdir / "考核表输出.xlsx", "收入成本表", inc)

    repo_root = repo_root_from_module()
    paths = ensure_compiled_rules(repo_root)

    r = run_audit(workdir=workdir, rules_path=paths.compiled_rules, enable_ai=False, annotate_source=False)
    assert r.ok
    assert r.report_path is not None
    assert r.report_path.exists()
```

- [ ] **Step 2: 运行测试**

Run:
```powershell
pytest -q
```
Expected: PASS.

---

### Task 10: README + SKILL.md

**Files:**
- Create: `README.md`, `SKILL.md`

- [ ] **Step 1: README.md**

Create `README.md`（最少包含）：

- 输入文件要求（两张 Excel + sheet 名）
- 安装与运行命令
- 两次确认说明
- 输出目录说明（`<workdir>/凭证审核输出/`）
- Excel COM 常见错误与处理建议（从旧项目 README 抄到新 README）

- [ ] **Step 2: SKILL.md**

Create `SKILL.md`（最少包含）：

- 固定流程：preview → 确认1 → run → 确认2 → annotate
- 修复闭环：repair → diff+预演 → y/N → 版本化落盘与切换 active
- v1 repair 范围：仅 inputs（files/sheets/columns）

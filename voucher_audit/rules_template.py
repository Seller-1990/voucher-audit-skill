from __future__ import annotations

from copy import deepcopy
from importlib import resources
from typing import Any

import yaml

# Legacy rule shapes kept only so AI/guardrails still expose known addable types.
# Current production checks come from packaged default_rules (synced with rules/).
_LEGACY_ADDABLE_CHECKS: list[dict[str, Any]] = [
    {
        "id": "INC_FULL_COMBO_DRIFT",
        "type": "combo_drift",
        "scope": "income_cost",
        "severity": "错误",
        "description": "历史模板：组合漂移检查（用于可新增规则类型元数据）。",
        "source": {"doc": "（统计异常）", "clause": "完整组合稳定性"},
        "params": {
            "key_fields": ["主体账簿", "账载客户"],
            "value_fields": ["三级科目", "实际客户", "部门", "项目"],
            "amount_field": "净额收入",
            "min_amount_abs": 50000,
        },
    },
    {
        "id": "INC_METRIC_PP_CHANGE",
        "type": "metric_pp_change",
        "scope": "income_cost",
        "severity": "需确认",
        "description": "历史模板：指标同比（用于可新增规则类型元数据）。",
        "source": {"doc": "（同比波动）", "clause": "指标稳定性"},
        "params": {
            "key_fields": ["主体账簿", "实际客户", "部门"],
            "month_field": "月",
            "tolerance_ratio": 0.2,
            "revenue_guard_field": "净额收入",
            "min_revenue": 0,
            "metrics": [
                {"name": "毛利率", "numerator": "项目毛利润", "denominator": "净额收入"},
                {"name": "单人毛利", "numerator": "项目毛利润", "denominator": "结算人次"},
            ],
        },
    },
    {
        "id": "INC_VALUE_PP_CHANGE",
        "type": "value_pp_change",
        "scope": "income_cost",
        "severity": "需确认",
        "description": "历史模板：金额同比（用于可新增规则类型元数据）。",
        "source": {"doc": "（同比波动）", "clause": "费用稳定性"},
        "params": {
            "key_fields": ["主体账簿", "实际客户", "部门"],
            "month_field": "月",
            "tolerance_ratio": 0.2,
            "value_fields": ["项目返费", "第三方挂靠成本"],
            "min_abs": 0,
        },
    },
    {
        "id": "INC_RATIO_PP_CHANGE",
        "type": "ratio_pp_change",
        "scope": "income_cost",
        "severity": "需确认",
        "description": "历史模板：比率同比（用于可新增规则类型元数据）。",
        "source": {"doc": "（同比波动）", "clause": "比率稳定性"},
        "params": {
            "key_fields": ["主体账簿", "实际客户", "部门"],
            "month_field": "月",
            "tolerance_ratio": 0.2,
            "ratios": [
                {"name": "项目返费/净额收入", "numerator": "项目返费", "denominator": "净额收入"},
                {
                    "name": "第三方挂靠成本/全额收入",
                    "numerator": "第三方挂靠成本",
                    "denominator": "全额收入",
                },
            ],
        },
    },
]


def _read_packaged_yaml(filename: str) -> dict[str, Any]:
    packaged = resources.files("voucher_audit").joinpath("default_rules").joinpath(filename)
    raw = packaged.read_text(encoding="utf-8")
    data = yaml.safe_load(raw) or {}
    if not isinstance(data, dict):
        raise ValueError(f"packaged rule file must be a mapping: {filename}")
    return dict(data)


def build_template_rules() -> dict[str, Any]:
    """Build the template rule pack from packaged defaults + legacy addable types."""
    app = _read_packaged_yaml("app_rules.yaml")
    audit = _read_packaged_yaml("audit_rules.yaml")
    merged = dict(app)
    merged.pop("checks", None)
    checks = list(audit.get("checks") or [])
    seen_types = {str((c or {}).get("type", "")).strip() for c in checks}
    for legacy in _LEGACY_ADDABLE_CHECKS:
        rule_type = str(legacy.get("type", "")).strip()
        if rule_type and rule_type not in seen_types:
            checks.append(deepcopy(legacy))
            seen_types.add(rule_type)
    merged["checks"] = checks
    return merged


def render_template_yaml() -> str:
    return yaml.safe_dump(build_template_rules(), allow_unicode=True, sort_keys=False)


# Backward-compatible constant used by runner.generate_template_rules / guardrails.
# Built at import from packaged default_rules so template cannot drift from defaults.
try:
    TEMPLATE_YAML = render_template_yaml()
except Exception as exc:  # pragma: no cover - import-time guard
    raise RuntimeError(
        "无法从 voucher_audit.default_rules 构建 TEMPLATE_YAML；请确认 wheel 含 default_rules 资源"
    ) from exc

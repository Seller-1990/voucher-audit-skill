from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import RuleConfig


@dataclass(frozen=True)
class PreviewItem:
    rule_name: str
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
        for w in (params.get("when_any") or []):
            if isinstance(w, dict):
                add(w.get("field"))
        for w in (params.get("when_all") or []):
            if isinstance(w, dict):
                add(w.get("field"))
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
    elif rtype == "headcount_data_check":
        add(params.get("summary_field"))
        add(params.get("voucher_field"))
        add(params.get("month_field"))
    elif rtype == "customer_consistency_check":
        add(params.get("book_customer_multi_actual_group_fields"))
        add(params.get("book_customer_multi_actual_distinct_field"))
        add(params.get("combo_drift_key_fields"))
        add(params.get("combo_drift_value_fields"))
        add(params.get("combo_drift_amount_field"))
        add(params.get("actual_customer_multi_entity_group_fields"))
        add(params.get("actual_customer_multi_entity_distinct_field"))
        add(params.get("pm_center_multi_dept_group_fields"))
        add(params.get("pm_center_multi_dept_distinct_field"))
        add(params.get("pm_center_multi_dept_filter_field"))
        add(params.get("revenue_field", "全额收入"))
        add(params.get("cost_total_field", "成本合计"))
    elif rtype == "combo_drift":
        add(params.get("key_fields"))
        add(params.get("value_fields"))
        add(params.get("amount_field"))
    elif rtype == "mapping_check":
        # income_cost(三级科目->业务类型) vs 数据汇总.xlsx/客户调整校验
        add(["主体账簿", "月", "三级科目", "业务类型", "账载客户", "部门", "项目", "实际客户"])
    elif rtype == "forbidden_regex":
        add(params.get("field") or params.get("summary_field"))
        add(params.get("voucher_field"))
        add(params.get("month_field"))
    elif rtype == "distinct_count":
        add(params.get("group_fields"))
        add(params.get("distinct_field"))
        add(params.get("revenue_field"))
        add(params.get("profit_field"))
    elif rtype == "neg_profit_ratio":
        add(params.get("group_fields"))
        add(params.get("biz_type_field"))
        add(params.get("revenue_field"))
        add(params.get("profit_field"))
        add(params.get("dept_field"))
    elif rtype == "outsourcing_missing_cost":
        add(params.get("group_fields"))
        add(params.get("biz_type_field"))
        add(params.get("wage_field"))
        add(params.get("third_party_cost_field"))
        add(params.get("cost_total_field"))
        add(params.get("revenue_field"))
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
    elif rtype == "pp_change":
        add(params.get("key_fields"))
        add(params.get("month_field"))
        for it in (params.get("items") or []):
            if isinstance(it, dict):
                add(it.get("guard_field"))
                if it.get("field") is not None:
                    add(it.get("field"))
                else:
                    add(it.get("numerator"))
                    add(it.get("denominator"))
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
    elif rtype in {"gm_high_ratio", "neg_profit_ratio"}:
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        add(params.get("net_revenue_field"))
        add(params.get("profit_field"))
    elif rtype in {"rev_cost_inversion", "rev_cost_zero_mismatch"}:
        add(params.get("group_fields", params.get("key_fields")))
        add(params.get("revenue_field"))
        add(params.get("cost_field"))
    elif rtype in {"headcount_rev_mismatch", "social_headcount_mismatch"}:
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        add(params.get("headcount_field"))
        add(params.get("social_people_field"))
        add(params.get("social_fee_field"))
    elif rtype == "cost_ratio_high":
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        for r in (params.get("ratios") or []):
            if isinstance(r, dict):
                add(r.get("field"))
    elif rtype == "expense_ratio":
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        add(params.get("welfare_field"))
        add(params.get("other_field"))
    elif rtype == "cost_sudden_appearance":
        add(params.get("group_fields"))
        add(params.get("fields"))
    elif rtype == "mom_change":
        add(params.get("group_fields"))
        add(params.get("revenue_field"))
        add(params.get("cost_field"))
        add(params.get("profit_field"))
    elif rtype == "duplicate_row":
        add(params.get("key_fields"))
        add(params.get("amount_fields"))
    elif rtype == "group_hq_unsettled":
        add(params.get("group_fields"))
        add(params.get("amount_field"))
    elif rtype == "similar_customer_rename":
        add("实际客户")
    elif rtype == "aux_wage_wrong_customer":
        add(params.get("summary_field"))
        add(params.get("wage_keywords"))
    elif rtype == "mixed_biz_type":
        add(params.get("group_fields"))
        add(params.get("biz_type_field"))
        add(params.get("amount_field"))
    elif rtype == "rev_cost_biz_type_mismatch":
        add(params.get("group_fields"))
        add(params.get("biz_type_field"))
        add(params.get("revenue_field"))
        add(params.get("cost_field"))
    elif rtype == "same_amount_adjacent_months":
        add(params.get("key_fields"))
        add(params.get("amount_fields"))
    elif rtype == "small_amount_wrong_dept":
        add(params.get("group_fields"))
        add("成本合计")
        add("全额收入")
    elif rtype == "entity_switch_mapping_drift":
        add("账载客户")
        add("主体账簿")
        add("实际客户")
    elif rtype == "rebate_external_cost_reconcile":
        add(params.get("cost_level1"))
        add(params.get("cost_level2"))
        add("项目返费")
        add("第三方挂靠成本")

    return out


def _output_logical_sheet(scope: str, rule_type: str) -> str:
    if scope == "aux_ledger":
        if rule_type in {"hard_rule", "allowed_values", "required_fields", "forbidden_regex", "headcount_data_check"}:
            return "aux_rule_violations"
        if rule_type == "aux_wage_wrong_customer":
            # 工资挂错客户由 checks.run_checks 归入 income_dim 输出（severity=错误）
            return "income_dim_anomalies"
        return "aux_suspect_wrong_account"
    if scope == "income_cost":
        if rule_type in {"gross_margin", "neg_profit_ratio"}:
            return "income_gm_anomalies"
        return "income_dim_anomalies"
    return "overview"


def build_preview_items(rules: RuleConfig) -> list[PreviewItem]:
    items: list[PreviewItem] = []
    for rule in rules.checks:
        items.append(
            PreviewItem(
                rule_name=str(rule.get("name", "")).strip(),
                rule_id=str(rule.get("id", "")).strip(),
                severity=str(rule.get("severity", "")) or "需确认",
                scope=str(rule.get("scope", "")).strip(),
                rule_type=str(rule.get("type", "")).strip(),
                fields=tuple(_fields_from_rule(rule)),
                method=str(rule.get("type", "")).strip(),
                output_logical_sheet=_output_logical_sheet(
                    str(rule.get("scope", "")).strip(),
                    str(rule.get("type", "")).strip(),
                ),
            )
        )
    return items

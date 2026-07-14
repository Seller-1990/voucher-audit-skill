from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from .check_utils import _rule_name, _severity_rank
from .checks_customer_subchecks import (
    _combo_drift_income,
    _dept_multi_distinct_trigger_income,
    _distinct_count_income,
    _sub_mapping_check,
)

def _customer_consistency_check_income(
    df_inc: pd.DataFrame,
    df_map: Optional[pd.DataFrame],
    target_month: int,
    rule: dict[str, Any],
    dominance_ratio: float,
) -> pd.DataFrame:
    """
    客户归属一致性检查（合并5条子规则）：
    ① 实际客户与映射表不一致（错误）
    ② 账载客户对应多实际客户（错误）
    ③ 客户归属组合与上月主映射漂移（错误）
    ④ 实际客户对应多主体（需确认）
    ⑤ 项目管理中心客户多部门（需确认）
    """
    params = rule.get("params", {}) or {}
    rule_id = str(rule.get("id", ""))
    rule_name = _rule_name(rule)
    all_parts: list[pd.DataFrame] = []

    # --- 子检查1：映射不一致 ---
    if params.get("mapping_check_enabled", True):
        part = _sub_mapping_check(df_inc, df_map, target_month, rule)
        if part is not None and not part.empty:
            all_parts.append(part)

    # --- 子检查2：账载客户对应多实际客户 ---
    if params.get("book_customer_multi_actual_enabled", True):
        sub_rule = {
            "id": rule_id, "name": rule_name, "description": str(rule.get("description", "")),
            "source": rule.get("source", {}),
            "params": {
                "group_fields": params.get("book_customer_multi_actual_group_fields", ["主体账簿", "月", "三级科目", "账载客户", "项目"]),
                "distinct_field": params.get("book_customer_multi_actual_distinct_field", "实际客户"),
                "min_distinct": params.get("book_customer_multi_actual_min_distinct", 2),
                "min_gross_revenue": params.get("book_customer_multi_actual_min_gross_revenue", 10000),
            },
        }
        part = _distinct_count_income(df_inc, target_month, sub_rule)
        if part is not None and not part.empty:
            part["问题分类"] = "账载客户对应多实际客户"
            all_parts.append(part)

    # --- 子检查3：客户归属组合漂移 ---
    if params.get("combo_drift_enabled", True):
        sub_rule = {
            "id": rule_id, "name": rule_name, "description": str(rule.get("description", "")),
            "source": rule.get("source", {}),
            "params": {
                "key_fields": params.get("combo_drift_key_fields", ["主体账簿", "账载客户"]),
                "value_fields": params.get("combo_drift_value_fields", ["三级科目", "实际客户", "部门", "项目"]),
                "amount_field": params.get("combo_drift_amount_field", "净额收入"),
                "min_amount_abs": params.get("combo_drift_min_amount_abs", 50000),
            },
        }
        part = _combo_drift_income(df_inc, target_month, sub_rule, dominance_ratio=dominance_ratio)
        if part is not None and not part.empty:
            part["问题分类"] = "客户归属组合漂移"
            all_parts.append(part)

    # --- 子检查4：实际客户对应多主体 ---
    if params.get("actual_customer_multi_entity_enabled", True):
        sub_rule = {
            "id": rule_id, "name": rule_name, "description": str(rule.get("description", "")),
            "source": rule.get("source", {}),
            "params": {
                "group_fields": params.get("actual_customer_multi_entity_group_fields", ["月", "实际客户", "账载客户", "项目"]),
                "distinct_field": params.get("actual_customer_multi_entity_distinct_field", "主体账簿"),
                "min_distinct": params.get("actual_customer_multi_entity_min_distinct", 2),
                "min_gross_revenue": params.get("actual_customer_multi_entity_min_gross_revenue", 50000),
            },
        }
        part = _distinct_count_income(df_inc, target_month, sub_rule)
        if part is not None and not part.empty:
            part["严重度"] = "需确认"
            part["问题分类"] = "实际客户对应多主体"
            all_parts.append(part)

    # --- 子检查5：项目管理中心客户多部门 ---
    if params.get("pm_center_multi_dept_enabled", True):
        sub_rule = {
            "id": rule_id, "name": rule_name, "description": str(rule.get("description", "")),
            "source": rule.get("source", {}),
            "params": {
                "group_fields": params.get("pm_center_multi_dept_group_fields", ["主体账簿", "月", "三级科目", "账载客户", "实际客户", "项目"]),
                "distinct_field": params.get("pm_center_multi_dept_distinct_field", "部门"),
                "min_distinct": params.get("pm_center_multi_dept_min_distinct", 2),
                "trigger_keywords": params.get("pm_center_multi_dept_trigger_keywords", []),
                "exclude_dept_value": "集团本部",
                "min_gross_revenue": params.get("pm_center_multi_dept_min_gross_revenue", 10000),
            },
        }
        part = _dept_multi_distinct_trigger_income(df_inc, target_month, sub_rule)
        if part is not None and not part.empty:
            part["严重度"] = "需确认"
            part["问题分类"] = "项目管理中心客户多部门"
            all_parts.append(part)

    if not all_parts:
        return pd.DataFrame()

    out = pd.concat(all_parts, ignore_index=True)

    # Ensure 问题分类 column exists for all rows (fill empty)
    if "问题分类" not in out.columns:
        out["问题分类"] = ""
    else:
        out["问题分类"] = out["问题分类"].fillna("")

    # Ensure uniform columns: add 问题分类 after 命中原因 if present, else after 规则名称
    cols = list(out.columns)
    base_cols = ["严重度", "规则ID", "规则名称", "制度来源", "规则描述", "问题分类", "命中原因"]
    ordered = [c for c in base_cols if c in cols]
    rest = [c for c in cols if c not in ordered]
    out = out[ordered + rest]

    # Sort by severity then by 问题分类
    out = out.sort_values(by=["严重度", "问题分类"], key=lambda s: s.map(_severity_rank) if s.name == "严重度" else s)
    return out

from __future__ import annotations

from typing import Any

import pandas as pd

from .check_utils import _match_contains_any, _rule_name, _strip_percent_suffix


def _outsourcing_missing_cost_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    biz_type_keywords = [str(x) for x in (params.get("biz_type_keywords") or ["外包"])]
    wage_field = str(params.get("wage_field") or "工资")
    third_party_cost_field = str(params.get("third_party_cost_field") or "第三方挂靠成本")
    cost_total_field = str(params.get("cost_total_field") or "成本合计")
    revenue_field = str(params.get("revenue_field") or "全额收入")
    min_cost_abs = float(params.get("min_cost_abs", 1.0))
    min_revenue_abs = float(params.get("min_revenue_abs", 1.0))

    if not group_fields:
        return pd.DataFrame()
    needed = set(group_fields + ["月", biz_type_field, wage_field, third_party_cost_field, cost_total_field, revenue_field])
    if any(c not in df_inc.columns for c in needed):
        return pd.DataFrame()

    df = df_inc.copy()
    if "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_strip_percent_suffix)
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"].copy()
        if df.empty:
            return pd.DataFrame()

    df = df[df[biz_type_field].apply(lambda v: _match_contains_any(v, biz_type_keywords))]
    if df.empty:
        return pd.DataFrame()

    for c in [wage_field, third_party_cost_field, cost_total_field, revenue_field]:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    cur = df[df["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()
    hist = df[df["月"] < target_month].copy()

    g = (
        cur.groupby(group_fields, dropna=False)[[wage_field, third_party_cost_field, cost_total_field, revenue_field]]
        .sum()
        .reset_index()
    )
    g = g[(g[cost_total_field].abs() >= min_cost_abs) & (g[revenue_field].abs() >= min_revenue_abs)]
    if g.empty:
        return pd.DataFrame()

    signature_fields = [c for c in group_fields if c != "月"]
    if signature_fields and (not hist.empty):
        hist_ref = (
            hist.groupby(signature_fields, dropna=False)[third_party_cost_field]
            .apply(lambda s: float(pd.to_numeric(s, errors="coerce").fillna(0.0).abs().max()))
            .reset_index(name="历史第三方挂靠成本_max")
        )
        g = g.merge(hist_ref, on=signature_fields, how="left")
    if "历史第三方挂靠成本_max" not in g.columns:
        g["历史第三方挂靠成本_max"] = 0.0
    g["历史第三方挂靠成本_max"] = pd.to_numeric(g["历史第三方挂靠成本_max"], errors="coerce").fillna(0.0)

    cond_both_zero = (g[wage_field].abs() <= 1e-9) & (g[third_party_cost_field].abs() <= 1e-9)
    cond_hist_third_party_missing = (
        (g[third_party_cost_field].abs() <= 1e-9)
        & (g["历史第三方挂靠成本_max"].abs() >= min_cost_abs)
        & (~cond_both_zero)
    )

    out_parts: list[pd.DataFrame] = []
    if cond_both_zero.any():
        part = g[cond_both_zero].copy()
        part.insert(0, "命中原因", "外包业务类型下，工资=0 且 第三方挂靠成本=0")
        out_parts.append(part)
    if cond_hist_third_party_missing.any():
        part = g[cond_hist_third_party_missing].copy()
        part.insert(0, "命中原因", "历史第三方挂靠成本存在，但本月第三方挂靠成本=0")
        out_parts.append(part)

    if not out_parts:
        return pd.DataFrame()
    out = pd.concat(out_parts, ignore_index=True)

    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out.sort_values(by=[revenue_field], ascending=False)

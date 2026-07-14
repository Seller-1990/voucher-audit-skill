from __future__ import annotations

from typing import Any

import pandas as pd

from .check_utils import _pick_prev_month


def _metric_pp_change_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """
    以 key_fields 为主键，检查 metrics 在目标月相对前一期间的波动（默认±20%以内正常）。
    metrics: [{name, numerator, denominator}]
    """
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    metrics = params.get("metrics") or []
    month_col = str(params.get("month_field") or "月")
    tol = float(params.get("tolerance_ratio", 0.2))
    min_revenue = float(params.get("min_revenue", 0))
    revenue_guard_field = str(params.get("revenue_guard_field") or "净额收入")
    eps = float(params.get("eps", 1e-9))

    if not key_fields or month_col not in df_inc.columns:
        return pd.DataFrame()
    for c in key_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    for m in metrics:
        for c in [m.get("numerator"), m.get("denominator")]:
            if c and str(c) not in df_inc.columns:
                return pd.DataFrame()
    if revenue_guard_field not in df_inc.columns:
        return pd.DataFrame()

    prev_month = _pick_prev_month(df_inc, month_col, target_month)
    if prev_month is None:
        return pd.DataFrame()

    df = df_inc.copy()
    df[revenue_guard_field] = pd.to_numeric(df[revenue_guard_field], errors="coerce").fillna(0.0)

    for m in metrics:
        num = str(m.get("numerator"))
        den = str(m.get("denominator"))
        df[num] = pd.to_numeric(df[num], errors="coerce").fillna(0.0)
        df[den] = pd.to_numeric(df[den], errors="coerce").fillna(0.0)

    agg_cols_raw = [revenue_guard_field] + list(
        {str(m.get("numerator")) for m in metrics} | {str(m.get("denominator")) for m in metrics}
    )
    # 可能出现 revenue_guard_field 同时也是某个指标分母（如 净额收入），需去重，避免 groupby 后重复列名引发异常
    agg_cols = list(dict.fromkeys(agg_cols_raw))
    g = df.groupby(key_fields + [month_col], dropna=False)[agg_cols].sum().reset_index()

    cur = g[g[month_col] == target_month].copy()
    prev = g[g[month_col] == prev_month].copy()
    if cur.empty or prev.empty:
        return pd.DataFrame()

    cur = cur[cur[revenue_guard_field] >= min_revenue].copy()
    prev = prev[prev[revenue_guard_field] >= min_revenue].copy()
    if cur.empty or prev.empty:
        return pd.DataFrame()

    merged = cur.merge(prev, on=key_fields, how="inner", suffixes=("_cur", "_prev"))
    if merged.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []
    for m in metrics:
        name = str(m.get("name") or "")
        num = str(m.get("numerator"))
        den = str(m.get("denominator"))

        num_cur = merged[f"{num}_cur"]
        den_cur = merged[f"{den}_cur"]
        num_prev = merged[f"{num}_prev"]
        den_prev = merged[f"{den}_prev"]

        metric_cur = num_cur / den_cur.replace(0, float("nan"))
        metric_prev = num_prev / den_prev.replace(0, float("nan"))
        change = (metric_cur - metric_prev) / metric_prev.replace(0, float("nan"))

        tmp = merged[key_fields].copy()
        tmp["指标"] = name
        tmp["前期月份"] = prev_month
        tmp["本期月份"] = target_month
        tmp["前期值"] = metric_prev
        tmp["本期值"] = metric_cur
        tmp["变化率"] = change
        tmp = tmp[(tmp["前期值"].abs() > eps) & (tmp["变化率"].abs() > tol)]
        for _, r in tmp.iterrows():
            out_rows.append(r.to_dict())

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    out.insert(0, "命中原因", f"指标相对前期波动超过 ±{int(tol*100)}%")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=["变化率"], ascending=False, key=lambda s: s.abs())
    return out


def _value_pp_change_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    value_fields = [str(x) for x in (params.get("value_fields") or [])]
    month_col = str(params.get("month_field") or "月")
    tol = float(params.get("tolerance_ratio", 0.2))
    min_abs = float(params.get("min_abs", 0))
    eps = float(params.get("eps", 1e-9))

    if not key_fields or not value_fields or month_col not in df_inc.columns:
        return pd.DataFrame()
    for c in key_fields + value_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()

    prev_month = _pick_prev_month(df_inc, month_col, target_month)
    if prev_month is None:
        return pd.DataFrame()

    df = df_inc.copy()
    for f in value_fields:
        df[f] = pd.to_numeric(df[f], errors="coerce").fillna(0.0)

    g = df.groupby(key_fields + [month_col], dropna=False)[value_fields].sum().reset_index()
    cur = g[g[month_col] == target_month].copy()
    prev = g[g[month_col] == prev_month].copy()
    if cur.empty or prev.empty:
        return pd.DataFrame()

    merged = cur.merge(prev, on=key_fields, how="inner", suffixes=("_cur", "_prev"))
    if merged.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []
    for f in value_fields:
        cur_v = merged[f"{f}_cur"]
        prev_v = merged[f"{f}_prev"]
        change = (cur_v - prev_v) / prev_v.replace(0, float("nan"))

        tmp = merged[key_fields].copy()
        tmp["字段"] = f
        tmp["前期月份"] = prev_month
        tmp["本期月份"] = target_month
        tmp["前期值"] = prev_v
        tmp["本期值"] = cur_v
        tmp["变化率"] = change
        tmp = tmp[(tmp["前期值"].abs() > eps) & (tmp["本期值"].abs() >= min_abs) & (tmp["变化率"].abs() > tol)]
        for _, r in tmp.iterrows():
            out_rows.append(r.to_dict())

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    out.insert(0, "命中原因", f"金额相对前期波动超过 ±{int(tol*100)}%")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=["变化率"], ascending=False, key=lambda s: s.abs())
    return out


def _ratio_pp_change_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    ratios = params.get("ratios") or []
    month_col = str(params.get("month_field") or "月")
    tol = float(params.get("tolerance_ratio", 0.2))
    eps = float(params.get("eps", 1e-9))

    if not key_fields or not ratios or month_col not in df_inc.columns:
        return pd.DataFrame()
    for c in key_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()

    for r in ratios:
        n = str(r.get("numerator") or "")
        d = str(r.get("denominator") or "")
        if not n or not d:
            return pd.DataFrame()
        if n not in df_inc.columns or d not in df_inc.columns:
            return pd.DataFrame()

    prev_month = _pick_prev_month(df_inc, month_col, target_month)
    if prev_month is None:
        return pd.DataFrame()

    df = df_inc.copy()
    needed = set()
    for r in ratios:
        needed.add(str(r.get("numerator")))
        needed.add(str(r.get("denominator")))
    for f in needed:
        df[f] = pd.to_numeric(df[f], errors="coerce").fillna(0.0)

    g = df.groupby(key_fields + [month_col], dropna=False)[sorted(needed)].sum().reset_index()
    cur = g[g[month_col] == target_month].copy()
    prev = g[g[month_col] == prev_month].copy()
    if cur.empty or prev.empty:
        return pd.DataFrame()

    merged = cur.merge(prev, on=key_fields, how="inner", suffixes=("_cur", "_prev"))
    if merged.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []
    for r in ratios:
        name = str(r.get("name") or f"{r.get('numerator')}/{r.get('denominator')}")
        num = str(r.get("numerator"))
        den = str(r.get("denominator"))
        ratio_cur = merged[f"{num}_cur"] / merged[f"{den}_cur"].replace(0, float("nan"))
        ratio_prev = merged[f"{num}_prev"] / merged[f"{den}_prev"].replace(0, float("nan"))
        change = (ratio_cur - ratio_prev) / ratio_prev.replace(0, float("nan"))

        tmp = merged[key_fields].copy()
        tmp["比率"] = name
        tmp["前期月份"] = prev_month
        tmp["本期月份"] = target_month
        tmp["前期值"] = ratio_prev
        tmp["本期值"] = ratio_cur
        tmp["变化率"] = change
        tmp = tmp[(tmp["前期值"].abs() > eps) & (tmp["变化率"].abs() > tol)]
        for _, rr in tmp.iterrows():
            out_rows.append(rr.to_dict())

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    out.insert(0, "命中原因", f"比率相对前期波动超过 ±{int(tol*100)}%")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=["变化率"], ascending=False, key=lambda s: s.abs())
    return out

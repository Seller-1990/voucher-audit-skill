from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from .check_utils import _apply_filters, _match_contains_any, _pick_prev_month, _rule_name, _strip_percent_suffix

def _combo_drift_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any], dominance_ratio: float) -> pd.DataFrame:
    """
    检查：以 key_fields 为“组合主键”，看 value_fields 在目标月是否与历史主映射不一致。

    典型用法（符合用户口径）：
    - key_fields: [主体账簿, 三级科目, 账载客户]
    - value_fields: [实际客户, 部门]
    """
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    value_fields = [str(x) for x in (params.get("value_fields") or [])]
    amount_field = str(params.get("amount_field") or "净额收入")
    min_amount_abs = float(params.get("min_amount_abs", 50000))

    if not key_fields or not value_fields:
        return pd.DataFrame()
    for c in key_fields + value_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    if amount_field not in df_inc.columns:
        return pd.DataFrame()

    df = df_inc.copy()
    if "三级科目" in (key_fields + value_fields) and "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_strip_percent_suffix)
    df[amount_field] = pd.to_numeric(df[amount_field], errors="coerce").fillna(0.0)

    prev_month = _pick_prev_month(df, "月", target_month)
    if prev_month is None:
        return pd.DataFrame()

    hist = df[df["月"] == prev_month]
    cur = df[df["月"] == target_month]
    if hist.empty or cur.empty:
        return pd.DataFrame()

    # history dominant value tuple per key_fields
    hist_agg = hist.groupby(key_fields + value_fields, dropna=False)[amount_field].sum().reset_index()
    hist_agg["abs_amount"] = hist_agg[amount_field].abs()
    hist_total = hist_agg.groupby(key_fields, dropna=False)["abs_amount"].sum().reset_index(name="hist_total_abs")
    hist_rank = hist_agg.merge(hist_total, on=key_fields, how="left")
    hist_rank["ratio"] = hist_rank["abs_amount"] / hist_rank["hist_total_abs"].replace(0, float("nan"))
    hist_rank = hist_rank.sort_values(by=["ratio", "abs_amount"], ascending=False)
    hist_dom = hist_rank.groupby(key_fields, dropna=False).head(1).copy()
    hist_dom = hist_dom.rename(columns={"ratio": "hist_dominant_ratio", "abs_amount": "hist_dominant_abs"})
    for f in value_fields:
        hist_dom = hist_dom.rename(columns={f: f"历史主_{f}"})
    hist_keep = key_fields + [f"历史主_{f}" for f in value_fields] + ["hist_dominant_ratio", "hist_total_abs"]
    hist_dom = hist_dom[hist_keep]

    # current dominant value tuple per key_fields
    cur_agg = cur.groupby(key_fields + value_fields, dropna=False)[amount_field].sum().reset_index()
    cur_agg["abs_amount"] = cur_agg[amount_field].abs()
    cur_total = cur_agg.groupby(key_fields, dropna=False)["abs_amount"].sum().reset_index(name="cur_total_abs")
    cur_rank = cur_agg.merge(cur_total, on=key_fields, how="left")
    cur_rank["ratio"] = cur_rank["abs_amount"] / cur_rank["cur_total_abs"].replace(0, float("nan"))
    cur_rank = cur_rank.sort_values(by=["ratio", "abs_amount"], ascending=False)
    cur_dom = cur_rank.groupby(key_fields, dropna=False).head(1).copy()
    cur_dom = cur_dom.rename(columns={"ratio": "cur_dominant_ratio", "abs_amount": "cur_dominant_abs"})
    for f in value_fields:
        cur_dom = cur_dom.rename(columns={f: f"本月主_{f}"})
    cur_keep = key_fields + [f"本月主_{f}" for f in value_fields] + ["cur_dominant_ratio", "cur_total_abs"]
    cur_dom = cur_dom[cur_keep]

    merged = cur_dom.merge(hist_dom, on=key_fields, how="left")
    merged = merged[merged["cur_total_abs"] >= min_amount_abs]
    merged = merged[merged["hist_total_abs"].notna()]
    merged = merged[merged["hist_dominant_ratio"] >= dominance_ratio]
    mismatch_cols = [f for f in value_fields if f"历史主_{f}" in merged.columns and f"本月主_{f}" in merged.columns]
    if mismatch_cols:
        mismatch_mask = False
        for f in mismatch_cols:
            mismatch_mask = mismatch_mask | (merged[f"历史主_{f}"].astype(str) != merged[f"本月主_{f}"].astype(str))
        merged = merged[mismatch_mask]
    if merged.empty:
        return pd.DataFrame()

    field_priority = ["三级科目", "实际客户", "部门", "项目"]

    def _format_value(v: Any) -> str:
        if v is None:
            return "空"
        if isinstance(v, float) and pd.isna(v):
            return "空"
        s = str(v).strip()
        return s if s and s.lower() != "nan" else "空"

    def _build_issue_cols(row: pd.Series) -> pd.Series:
        points: list[str] = []
        history_info: list[str] = []
        primary = ""
        for f in field_priority:
            hist_col = f"历史主_{f}"
            cur_col = f"本月主_{f}"
            if hist_col not in row or cur_col not in row:
                continue
            if str(row.get(hist_col, "")) != str(row.get(cur_col, "")):
                tag = f"{f}疑似异常"
                points.append(tag)
                history_info.append(f"{f}历史={_format_value(row.get(hist_col))}")
                if not primary:
                    primary = tag
        return pd.Series({"主问题分类": primary, "问题点": "；".join(points), "历史对应信息": "；".join(history_info)})

    issue_cols = merged.apply(_build_issue_cols, axis=1)
    merged = pd.concat([merged, issue_cols], axis=1)

    merged.insert(0, "对比月份", f"{int(target_month)} vs {int(prev_month)}")
    merged.insert(0, "本期月份", int(target_month))
    merged.insert(0, "前期月份", int(prev_month))
    merged.insert(0, "命中原因", "完整组合主映射与上月不一致（按上月基线）")
    merged.insert(0, "规则描述", str(rule.get("description", "")))
    merged.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    merged.insert(0, "规则ID", str(rule.get("id")))
    merged.insert(0, "严重度", str(rule.get("severity", "需确认")))

    merged = merged.sort_values(by=["cur_total_abs"], ascending=False)
    return merged


def _dept_multi_distinct_trigger_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    dept_field = str(params.get("distinct_field") or "部门")
    min_distinct = int(params.get("min_distinct", 2))
    revenue_field = str(params.get("revenue_field") or "全额收入")
    net_revenue_field = str(params.get("net_revenue_field") or "净额收入")
    profit_field = str(params.get("profit_field") or "项目毛利润")
    min_gross_revenue = float(params.get("min_gross_revenue", 0))
    exclude_dept_value = str(params.get("exclude_dept_value") or "集团本部")
    trigger_keywords = [str(x) for x in (params.get("trigger_keywords") or params.get("pm_center_multi_dept_trigger_keywords") or []) if str(x).strip()]

    if not group_fields or not dept_field:
        return pd.DataFrame()
    for c in group_fields + ["月", dept_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()

    cur = df_inc[df_inc["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    if dept_field in cur.columns:
        cur[dept_field] = cur[dept_field].astype("string").str.strip()
        if exclude_dept_value:
            cur = cur[cur[dept_field] != exclude_dept_value]
            if cur.empty:
                return pd.DataFrame()

    for c in [revenue_field, net_revenue_field, profit_field]:
        if c in cur.columns:
            cur[c] = pd.to_numeric(cur[c], errors="coerce").fillna(0.0)

    g = cur.groupby(group_fields, dropna=False).agg(
        distinct_cnt=(dept_field, "nunique"),
        gross_rev=(revenue_field, "sum") if revenue_field in cur.columns else (dept_field, "size"),
        net_rev=(net_revenue_field, "sum") if net_revenue_field in cur.columns else (dept_field, "size"),
        profit=(profit_field, "sum") if profit_field in cur.columns else (dept_field, "size"),
    )
    g = g.reset_index()
    g = g[g["distinct_cnt"] >= min_distinct]
    if min_gross_revenue > 0 and revenue_field in cur.columns:
        g = g[g["gross_rev"].abs() >= min_gross_revenue]
    if g.empty:
        return pd.DataFrame()

    if trigger_keywords:
        trig = (
            cur.groupby(group_fields, dropna=False)[dept_field]
            .apply(lambda s: any(_match_contains_any(x, trigger_keywords) for x in s.dropna().astype(str)))
            .reset_index(name="_trigger_hit")
        )
        g = g.merge(trig, on=group_fields, how="left")
        g["_trigger_hit"] = g["_trigger_hit"].fillna(False)
        g = g[g["_trigger_hit"]]
        g = g.drop(columns=["_trigger_hit"], errors="ignore")
        if g.empty:
            return pd.DataFrame()

    vals = (
        cur.groupby(group_fields, dropna=False)[dept_field]
        .apply(lambda s: "，".join(sorted({str(x).strip() for x in s.dropna().astype(str) if str(x).strip()}))[:2000])
        .reset_index(name="不同值列表")
    )
    out = g.merge(vals, on=group_fields, how="left")

    out.insert(0, "命中原因", f"{dept_field} 不同值数 >= {min_distinct}")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out


def _distinct_count_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    distinct_field = str(params.get("distinct_field") or "")
    min_distinct = int(params.get("min_distinct", 2))
    filters = params.get("filters") or []
    exclude_filters = params.get("exclude_filters") or []
    revenue_field = str(params.get("revenue_field") or "全额收入")
    net_revenue_field = str(params.get("net_revenue_field") or "净额收入")
    profit_field = str(params.get("profit_field") or "项目毛利润")
    min_gross_revenue = float(params.get("min_gross_revenue", 0))

    if not group_fields or not distinct_field:
        return pd.DataFrame()
    for c in group_fields + ["月", distinct_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()

    cur = df_inc[df_inc["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()
    cur = _apply_filters(cur, filters if isinstance(filters, list) else [])
    if cur.empty:
        return pd.DataFrame()

    # Normalize text fields to avoid false positives from trailing spaces / empty strings
    text_cols = list(dict.fromkeys([c for c in (group_fields + [distinct_field]) if c and c != "月"]))
    for c in text_cols:
        if c not in cur.columns:
            continue
        # keep numeric-like columns as-is
        if pd.api.types.is_numeric_dtype(cur[c]) or pd.api.types.is_datetime64_any_dtype(cur[c]):
            continue
        cur[c] = cur[c].astype("string").str.strip()
    if distinct_field in cur.columns:
        cur[distinct_field] = cur[distinct_field].replace("", pd.NA)

    # Apply exclude filters (blacklist) before counting distinct values
    if isinstance(exclude_filters, list) and exclude_filters:
        out_df = cur
        for f in exclude_filters:
            if not isinstance(f, dict):
                continue
            field = str(f.get("field") or "").strip()
            if not field or field not in out_df.columns:
                continue
            s = out_df[field].astype("string")
            if "equals" in f:
                out_df = out_df[s != str(f.get("equals"))]
            elif "contains" in f:
                out_df = out_df[~s.str.contains(str(f.get("contains")), regex=False, na=False)]
            elif "regex" in f:
                out_df = out_df[~s.str.contains(str(f.get("regex")), regex=True, na=False)]
            elif "in" in f:
                banned = [str(x) for x in (f.get("in") or [])]
                out_df = out_df[~s.isin(banned)]
        cur = out_df
        if cur.empty:
            return pd.DataFrame()

    # Numeric guards (optional)
    if revenue_field in cur.columns:
        cur[revenue_field] = pd.to_numeric(cur[revenue_field], errors="coerce").fillna(0.0)
    if net_revenue_field in cur.columns:
        cur[net_revenue_field] = pd.to_numeric(cur[net_revenue_field], errors="coerce").fillna(0.0)
    if profit_field in cur.columns:
        cur[profit_field] = pd.to_numeric(cur[profit_field], errors="coerce").fillna(0.0)

    g = cur.groupby(group_fields, dropna=False).agg(
        distinct_cnt=(distinct_field, "nunique"),
        gross_rev=(revenue_field, "sum") if revenue_field in cur.columns else (distinct_field, "size"),
        net_rev=(net_revenue_field, "sum") if net_revenue_field in cur.columns else (distinct_field, "size"),
        profit=(profit_field, "sum") if profit_field in cur.columns else (distinct_field, "size"),
    )
    g = g.reset_index()
    g = g[g["distinct_cnt"] >= min_distinct]
    if min_gross_revenue > 0 and revenue_field in cur.columns:
        g = g[g["gross_rev"].abs() >= min_gross_revenue]
    if g.empty:
        return pd.DataFrame()

    # Add distinct values list (for actionability)
    vals = (
        cur.groupby(group_fields, dropna=False)[distinct_field]
        .apply(lambda s: "，".join(sorted({str(x) for x in s.dropna().astype(str)}))[:2000])
        .reset_index(name="不同值列表")
    )
    out = g.merge(vals, on=group_fields, how="left")

    out.insert(0, "命中原因", f"{distinct_field} 不同值数 >= {min_distinct}")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out


def _sub_mapping_check(
    df_inc: pd.DataFrame,
    df_map: Optional[pd.DataFrame],
    target_month: int,
    rule: dict[str, Any],
) -> pd.DataFrame:
    """子检查1：实际客户与映射表不一致。"""
    if df_map is None or df_map.empty:
        return pd.DataFrame()
    required = ["主体账簿", "月", "业务类型", "账载客户", "部门", "项目", "实际客户"]
    if any(c not in df_map.columns for c in required):
        return pd.DataFrame()
    if any(c not in df_inc.columns for c in ["主体账簿", "月", "三级科目", "账载客户", "部门", "项目", "实际客户"]):
        return pd.DataFrame()

    rule_id = str(rule.get("id", ""))
    rule_name = _rule_name(rule)
    src_doc = (rule.get("source") or {}).get("doc", "")
    src_clause = (rule.get("source") or {}).get("clause", "")
    source_str = f"{src_doc} | {src_clause}".strip(" |")

    cur = df_inc[df_inc["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()
    cur = cur.rename(columns={"三级科目": "业务类型"})

    key_cols = ["主体账簿", "业务类型", "账载客户", "部门", "项目"]

    map_df = df_map.copy()
    map_df["月"] = pd.to_numeric(map_df["月"], errors="coerce")
    hist_map = map_df[map_df["月"].notna() & (map_df["月"].astype("Int64") != int(target_month))]
    if hist_map.empty:
        hist_map = map_df

    ranked = (
        hist_map.groupby(key_cols + ["实际客户"], dropna=False)
        .agg(cnt=("实际客户", "size"), last_month=("月", "max"))
        .reset_index()
        .sort_values(by=["cnt", "last_month"], ascending=False)
    )
    ref = ranked.groupby(key_cols, dropna=False).head(1).rename(columns={"实际客户": "实际客户_映射"})

    # 当月映射：用于区分“映射已变更”与“真实不一致”
    cur_map = map_df[map_df["月"].notna() & (map_df["月"].astype("Int64") == int(target_month))]
    cur_ref = pd.DataFrame(columns=key_cols + ["实际客户_当月映射"])
    if not cur_map.empty:
        cur_ranked = (
            cur_map.groupby(key_cols + ["实际客户"], dropna=False)
            .agg(cnt=("实际客户", "size"))
            .reset_index()
            .sort_values(by=["cnt"], ascending=False)
        )
        cur_ref = cur_ranked.groupby(key_cols, dropna=False).head(1).rename(
            columns={"实际客户": "实际客户_当月映射"}
        )

    merged = cur.merge(ref[key_cols + ["实际客户_映射"]], how="left", on=key_cols)
    merged = merged.merge(cur_ref[key_cols + ["实际客户_当月映射"]], how="left", on=key_cols)
    mism = merged[(merged["实际客户_映射"].notna()) & (merged["实际客户"] != merged["实际客户_映射"])].copy()
    if mism.empty:
        return pd.DataFrame()

    def _fmt(v: Any) -> str:
        s = "" if v is None else str(v).strip()
        return s if s and s.lower() != "nan" else "（空）"

    # ① 与历史映射不一致，但与当月映射一致 → 映射变更，需确认变更依据
    # ② 与历史映射、当月映射均不一致（或当月无映射）→ 真实不一致，错误
    matches_cur_map = mism["实际客户_当月映射"].notna() & (mism["实际客户"] == mism["实际客户_当月映射"])

    def _reason(row: pd.Series) -> str:
        hist_v = _fmt(row.get("实际客户_映射"))
        cur_v = _fmt(row.get("实际客户_当月映射"))
        if bool(row.get("__matches_cur_map", False)):
            return (
                f"实际客户与历史映射不一致（历史映射：{hist_v}），与当月映射一致（当月：{cur_v}）"
                "——请确认映射变更依据"
            )
        return f"实际客户与映射表不一致（历史映射：{hist_v}；当月映射：{cur_v}）"

    mism["__matches_cur_map"] = matches_cur_map
    mism["命中原因"] = mism.apply(_reason, axis=1)
    mism["严重度"] = mism["__matches_cur_map"].map(lambda m: "需确认" if m else "错误")
    mism = mism.drop(columns=["实际客户_映射", "实际客户_当月映射", "__matches_cur_map"], errors="ignore")
    mism.insert(0, "问题分类", "实际客户与映射表不一致")
    mism.insert(1, "命中原因", mism.pop("命中原因"))
    mism.insert(1, "严重度", mism.pop("严重度"))
    mism.insert(0, "规则描述", str(rule.get("description", "")))
    mism.insert(0, "制度来源", source_str)
    mism.insert(0, "规则名称", rule_name)
    mism.insert(0, "规则ID", rule_id)
    mism.insert(0, "严重度", mism.pop("严重度"))
    return mism

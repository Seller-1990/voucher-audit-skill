from __future__ import annotations

from typing import Any, Optional

import pandas as pd


def build_neg_profit_ratio_sheet(
    df_income: Optional[pd.DataFrame],
    income_gm_anomalies: Optional[pd.DataFrame],
    checks: Optional[list[dict[str, Any]]],
    target_month: Optional[int],
) -> pd.DataFrame:
    if df_income is None or df_income.empty:
        return pd.DataFrame()
    if income_gm_anomalies is None or income_gm_anomalies.empty:
        return pd.DataFrame()
    if "规则ID" not in income_gm_anomalies.columns:
        return pd.DataFrame()

    hits = income_gm_anomalies[income_gm_anomalies["规则ID"].astype(str) == "INC_NEG_GM_HIGH_RATIO"].copy()
    if hits.empty:
        return pd.DataFrame()

    rule = None
    for r in (checks or []):
        if str((r or {}).get("id", "")) == "INC_NEG_GM_HIGH_RATIO":
            rule = r
            break
    params = (rule or {}).get("params", {}) or {}
    month_col = "月"
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    if not group_fields:
        group_fields = ["主体账簿", "月", "三级科目", "实际客户"]

    def _find_col_contains(df: pd.DataFrame, keyword: str) -> str:
        for c in df.columns:
            if keyword in str(c):
                return str(c)
        return ""

    other_operating_col = "项目其他运营成本"
    if other_operating_col not in df_income.columns:
        found = _find_col_contains(df_income, "其他运营成本")
        other_operating_col = found or other_operating_col

    cols_map = {
        "全额收入": "全额收入",
        "第三方挂靠": "第三方挂靠成本",
        "净额收入": "净额收入",
        "结算人次": "结算人次",
        "项目返费": "项目返费",
        "项目其他运营成本": other_operating_col,
        "项目毛利润": "项目毛利润",
    }

    df = df_income.copy()
    if "三级科目" in group_fields and "三级科目" in df.columns:
        s = df["三级科目"].astype(str).str.strip()
        s = s.str.replace(r"\s*\d+(?:\.\d+)?[%％]\s*$", "", regex=True).str.strip()
        df["三级科目"] = s
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"].copy()
        if df.empty:
            return pd.DataFrame()

    for c in group_fields:
        if c not in df.columns:
            return pd.DataFrame()

    needed_cols = {v for v in cols_map.values() if v}
    for c in sorted(needed_cols):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    g = (
        df.groupby(group_fields, dropna=False)[sorted(needed_cols)]
        .sum()
        .reset_index()
    )

    def _norm_key(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and pd.isna(v):
            return ""
        s = str(v).strip()
        return "" if not s or s.lower() == "nan" else s

    def _as_month_int(v: Any) -> Optional[int]:
        m = pd.to_numeric(v, errors="coerce")
        if pd.isna(m):
            return None
        try:
            return int(m)
        except Exception:
            return None

    out_rows: list[dict[str, Any]] = []
    for _, h in hits.iterrows():
        key_vals = {k: _norm_key(h.get(k, "")) for k in group_fields if k in g.columns or k in hits.columns}
        month_int = _as_month_int(key_vals.get(month_col)) if month_col in key_vals else _as_month_int(h.get(month_col))
        if month_col in group_fields:
            key_vals[month_col] = str(month_int) if month_int is not None else _norm_key(key_vals.get(month_col, ""))

        # robust match against aggregated g
        mask = pd.Series(True, index=g.index)
        for k in group_fields:
            if k not in g.columns:
                continue
            if k == month_col:
                m_series = pd.to_numeric(g[k], errors="coerce").fillna(-1).astype(int)
                mask = mask & (m_series == (month_int if month_int is not None else -999999))
            else:
                mask = mask & (g[k].astype(str).map(_norm_key) == _norm_key(key_vals.get(k, "")))
        g_row = g[mask]
        series_row = g_row.iloc[0] if not g_row.empty else None

        rec: dict[str, Any] = {k: key_vals.get(k, "") for k in group_fields}
        if month_col in group_fields and month_int is not None:
            rec[month_col] = month_int

        for out_col, src_col in cols_map.items():
            if series_row is not None:
                rec[out_col] = series_row.get(src_col, 0.0)
            else:
                # fallback: use hit row values if any
                rec[out_col] = h.get(src_col, 0.0)

        # filter: drop rows whose core numeric columns are all zero/empty
        core_numeric_out_cols = list(cols_map.keys())
        core_vals = [pd.to_numeric(rec.get(c), errors="coerce") for c in core_numeric_out_cols]
        core_vals = [0.0 if pd.isna(x) else float(x) for x in core_vals]
        if all(v == 0.0 for v in core_vals):
            continue

        sev = str(h.get("严重度", ""))
        rec["标注"] = "问题"
        rec["命中原因"] = str(h.get("命中原因", ""))
        rec["指标名称"] = "毛利/净额收入"

        ratio_val = pd.to_numeric(h.get("毛利/净额收入"), errors="coerce")
        if pd.isna(ratio_val):
            profit_v = pd.to_numeric(h.get("项目毛利润"), errors="coerce")
            net_v = pd.to_numeric(h.get("净额收入"), errors="coerce")
            if pd.notna(profit_v) and pd.notna(net_v) and float(net_v) != 0.0:
                ratio_val = abs(float(profit_v)) / float(net_v)
        rec["指标值"] = ratio_val if pd.notna(ratio_val) else ""
        rec["严重度"] = sev
        out_rows.append(rec)

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    final_cols = [c for c in group_fields if c != month_col] + [month_col] if month_col in group_fields else group_fields
    final_cols = final_cols + list(cols_map.keys()) + ["标注", "命中原因", "指标名称", "指标值", "严重度"]
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]

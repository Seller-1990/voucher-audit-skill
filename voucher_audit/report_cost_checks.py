from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from .check_utils import _src_rows_text


def build_rev_cost_zero_mismatch_sheet(
    df_income: Optional[pd.DataFrame],
    income_dim_anomalies: Optional[pd.DataFrame],
    checks: Optional[list[dict[str, Any]]],
    target_month: Optional[int],
) -> pd.DataFrame:
    if df_income is None or df_income.empty:
        return pd.DataFrame()
    if income_dim_anomalies is None or income_dim_anomalies.empty:
        return pd.DataFrame()
    if "规则ID" not in income_dim_anomalies.columns:
        return pd.DataFrame()

    hits = income_dim_anomalies[income_dim_anomalies["规则ID"].astype(str) == "INC_REV_COST_ZERO_MISMATCH"].copy()
    if hits.empty:
        return pd.DataFrame()

    rule = None
    for r in (checks or []):
        if str((r or {}).get("id", "")) == "INC_REV_COST_ZERO_MISMATCH":
            rule = r
            break
    params = (rule or {}).get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    if not key_fields:
        key_fields = ["主体账簿", "月", "三级科目", "实际客户", "部门", "项目"]
    revenue_field = str(params.get("revenue_field") or "净额收入")
    cost_field = str(params.get("cost_field") or "成本合计")

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
        "成本合计": cost_field,
        "结算人次": "结算人次",
        "项目返费": "项目返费",
        "项目其他运营成本": other_operating_col,
        "项目毛利润": "项目毛利润",
    }

    df = df_income.copy()
    if "三级科目" in df.columns:
        s = df["三级科目"].astype(str).str.strip()
        s = s.str.replace(r"\s*\d+(?:\.\d+)?[%％]\s*$", "", regex=True).str.strip()
        df["三级科目"] = s
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"].copy()
        if df.empty:
            return pd.DataFrame()

    for c in key_fields:
        if c not in df.columns:
            return pd.DataFrame()

    needed_cols = {v for v in cols_map.values() if v}
    for c in sorted(needed_cols):
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    g = (
        df.groupby(key_fields, dropna=False)[sorted(needed_cols)]
        .sum()
        .reset_index()
    )

    # 源行号回溯（Excel 实际行号）
    if "_src_row" in df.columns:
        src_map = (
            df.groupby(key_fields, dropna=False)["_src_row"]
            .apply(_src_rows_text)
            .reset_index(name="源行号")
        )
        g = g.merge(src_map, how="left", on=key_fields)

    month_col = "月"

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
        key_vals = {k: _norm_key(h.get(k, "")) for k in key_fields if k in g.columns or k in hits.columns}
        month_int = _as_month_int(key_vals.get(month_col)) if month_col in key_vals else _as_month_int(h.get(month_col))
        if month_col in key_fields:
            key_vals[month_col] = str(month_int) if month_int is not None else _norm_key(key_vals.get(month_col, ""))

        mask = pd.Series(True, index=g.index)
        for k in key_fields:
            if k not in g.columns:
                continue
            if k == month_col:
                m_series = pd.to_numeric(g[k], errors="coerce").fillna(-1).astype(int)
                mask = mask & (m_series == (month_int if month_int is not None else -999999))
            else:
                mask = mask & (g[k].astype(str).map(_norm_key) == _norm_key(key_vals.get(k, "")))
        g_row = g[mask]
        series_row = g_row.iloc[0] if not g_row.empty else None

        rec: dict[str, Any] = {k: key_vals.get(k, "") for k in key_fields}
        if month_col in key_fields and month_int is not None:
            rec[month_col] = month_int

        for out_col, src_col in cols_map.items():
            if series_row is not None:
                rec[out_col] = series_row.get(src_col, 0.0)
            else:
                rec[out_col] = h.get(src_col, 0.0)

        hit_reason = str(h.get("命中原因", ""))
        rec["标注"] = "问题"
        rec["命中原因"] = hit_reason
        rec["源行号"] = str(series_row.get("源行号", "")) if series_row is not None and "源行号" in g.columns else ""

        # 指标：展示“非零的那一侧”，便于快速判断
        rev_val = pd.to_numeric(rec.get(revenue_field, rec.get("净额收入")), errors="coerce")
        cost_val = pd.to_numeric(rec.get(cost_field, rec.get("成本合计")), errors="coerce")
        rev_val = 0.0 if pd.isna(rev_val) else float(rev_val)
        cost_val = 0.0 if pd.isna(cost_val) else float(cost_val)
        if abs(rev_val) <= 1e-9 and abs(cost_val) > 1e-9:
            rec["指标名称"] = cost_field
            rec["指标值"] = cost_val
        else:
            rec["指标名称"] = revenue_field
            rec["指标值"] = rev_val
        rec["严重度"] = str(h.get("严重度", ""))

        out_rows.append(rec)

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    final_cols = [c for c in key_fields if c != month_col] + [month_col] if month_col in key_fields else key_fields
    final_cols = final_cols + list(cols_map.keys()) + ["源行号", "标注", "命中原因", "指标名称", "指标值", "严重度"]
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]


def build_outsourcing_missing_cost_sheet(
    df_income: Optional[pd.DataFrame],
    income_dim_anomalies: Optional[pd.DataFrame],
    checks: Optional[list[dict[str, Any]]],
    target_month: Optional[int],
) -> pd.DataFrame:
    if df_income is None or df_income.empty:
        return pd.DataFrame()
    if income_dim_anomalies is None or income_dim_anomalies.empty:
        return pd.DataFrame()
    if "规则ID" not in income_dim_anomalies.columns:
        return pd.DataFrame()

    hits = income_dim_anomalies[income_dim_anomalies["规则ID"].astype(str) == "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO"].copy()
    if hits.empty:
        return pd.DataFrame()

    rule = None
    for r in (checks or []):
        if str((r or {}).get("id", "")) == "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO":
            rule = r
            break
    params = (rule or {}).get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    if not group_fields:
        group_fields = ["主体账簿", "月", "三级科目", "账载客户", "实际客户", "部门", "项目"]

    revenue_col = str(params.get("revenue_field") or "全额收入")
    cost_total_col = str(params.get("cost_total_field") or "成本合计")
    wage_col = str(params.get("wage_field") or "工资")
    third_party_col = str(params.get("third_party_cost_field") or "第三方挂靠成本")

    cols_map = {
        "全额收入": revenue_col,
        "成本合计": cost_total_col,
        "工资": wage_col,
        "第三方挂靠成本": third_party_col,
        "历史第三方挂靠成本_max": "历史第三方挂靠成本_max",
    }

    # 源行号回溯：命中行是聚合组，从源表按组键取回实际行号列表
    src_cols = [c for c in group_fields if c in df_income.columns]
    if "_src_row" in df_income.columns and src_cols:
        src_map = (
            df_income.groupby(src_cols, dropna=False)["_src_row"]
            .apply(_src_rows_text)
            .reset_index(name="源行号")
        )
        hits = hits.merge(src_map, how="left", on=src_cols)
    else:
        hits["源行号"] = ""

    month_col = "月"

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
        rec: dict[str, Any] = {}
        for k in group_fields:
            if k == month_col:
                m = _as_month_int(h.get(k))
                rec[k] = m if m is not None else _norm_key(h.get(k, ""))
            else:
                rec[k] = _norm_key(h.get(k, ""))

        for out_col, src_col in cols_map.items():
            rec[out_col] = h.get(src_col, 0.0)

        hit_reason = str(h.get("命中原因", ""))
        rec["标注"] = "问题"
        rec["命中原因"] = hit_reason
        rec["源行号"] = str(h.get("源行号", "") or "")
        if "历史第三方挂靠成本" in hit_reason:
            rec["指标名称"] = "历史第三方挂靠成本_max"
            rec["指标值"] = pd.to_numeric(h.get("历史第三方挂靠成本_max"), errors="coerce")
        else:
            rec["指标名称"] = "第三方挂靠成本"
            rec["指标值"] = pd.to_numeric(h.get(third_party_col), errors="coerce")
        rec["严重度"] = str(h.get("严重度", ""))

        out_rows.append(rec)

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    final_cols = [c for c in group_fields if c != month_col] + [month_col] if month_col in group_fields else group_fields
    final_cols = final_cols + list(cols_map.keys()) + ["源行号", "标注", "命中原因", "指标名称", "指标值", "严重度"]
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]

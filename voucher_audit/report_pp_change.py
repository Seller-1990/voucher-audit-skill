from __future__ import annotations

from typing import Any, Optional

import pandas as pd

def build_pp_change_sheet(
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

    hits = income_dim_anomalies[income_dim_anomalies["规则ID"].astype(str) == "INC_PP_CHANGE"].copy()
    if hits.empty:
        return pd.DataFrame()

    rule = None
    for r in (checks or []):
        if str((r or {}).get("id", "")) == "INC_PP_CHANGE":
            rule = r
            break
    params = (rule or {}).get("params", {}) or {}
    items = params.get("items") or []
    month_col = str(params.get("month_field") or "月")
    key_fields = [str(x) for x in (params.get("key_fields") or [])]

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
    if "三级科目" in key_fields and "三级科目" in df.columns:
        s = df["三级科目"].astype(str).str.strip()
        s = s.str.replace(r"\s*\d+(?:\.\d+)?[%％]\s*$", "", regex=True).str.strip()
        df["三级科目"] = s

    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"].copy()
        if df.empty:
            return pd.DataFrame()

    needed_cols: set[str] = set(cols_map.values())
    for it in items:
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or it.get("type") or "").strip().lower()
        if not kind:
            if it.get("field") is not None:
                kind = "value"
            elif it.get("numerator") is not None and it.get("denominator") is not None:
                kind = "ratio"
        if kind == "value":
            f = str(it.get("field") or "").strip()
            if f:
                needed_cols.add(f)
        else:
            n = str(it.get("numerator") or "").strip()
            d = str(it.get("denominator") or "").strip()
            if n:
                needed_cols.add(n)
            if d:
                needed_cols.add(d)

    for c in key_fields + [month_col]:
        if c and c not in df.columns:
            return pd.DataFrame()

    for c in sorted(needed_cols):
        if not c:
            continue
        if c not in df.columns:
            df[c] = 0.0
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    g = (
        df.groupby(key_fields + [month_col], dropna=False)[sorted({c for c in needed_cols if c})]
        .sum()
        .reset_index()
    )

    item_by_name: dict[str, dict[str, Any]] = {}
    for it in items:
        if isinstance(it, dict):
            nm = str(it.get("name") or "").strip()
            if nm:
                item_by_name[nm] = it

    def _calc_item_value(row: pd.Series, it: dict[str, Any]) -> float:
        kind = str(it.get("kind") or it.get("type") or "").strip().lower()
        if not kind:
            if it.get("field") is not None:
                kind = "value"
            elif it.get("numerator") is not None and it.get("denominator") is not None:
                kind = "ratio"
        if kind == "value":
            f = str(it.get("field") or "").strip()
            v = row.get(f)
            return float(pd.to_numeric(v, errors="coerce")) if pd.notna(pd.to_numeric(v, errors="coerce")) else float("nan")
        n = str(it.get("numerator") or "").strip()
        d = str(it.get("denominator") or "").strip()
        num = pd.to_numeric(row.get(n), errors="coerce")
        den = pd.to_numeric(row.get(d), errors="coerce")
        if pd.isna(num) or pd.isna(den) or float(den) == 0.0:
            return float("nan")
        return float(num) / float(den)

    def _calc_item_baseline_hist(g_hist: pd.DataFrame, it: dict[str, Any]) -> float:
        kind = str(it.get("kind") or it.get("type") or "").strip().lower()
        if not kind:
            if it.get("field") is not None:
                kind = "value"
            elif it.get("numerator") is not None and it.get("denominator") is not None:
                kind = "ratio"
        if kind == "value":
            f = str(it.get("field") or "").strip()
            if not f or f not in g_hist.columns:
                return float("nan")
            return float(pd.to_numeric(g_hist[f], errors="coerce").fillna(0.0).mean())

        n = str(it.get("numerator") or "").strip()
        d = str(it.get("denominator") or "").strip()
        if not n or not d or n not in g_hist.columns or d not in g_hist.columns:
            return float("nan")
        num = float(pd.to_numeric(g_hist[n], errors="coerce").fillna(0.0).sum())
        den = float(pd.to_numeric(g_hist[d], errors="coerce").fillna(0.0).sum())
        if den == 0.0:
            return float("nan")
        return num / den

    def _row_for_month(base: dict[str, Any], month_value: Any, series_row: Optional[pd.Series]) -> dict[str, Any]:
        rec = dict(base)
        rec[month_col] = month_value
        for out_col, src_col in cols_map.items():
            if series_row is None:
                rec[out_col] = 0.0
            else:
                rec[out_col] = series_row.get(src_col, 0.0)
        return rec

    out_rows: list[dict[str, Any]] = []

    for _, h in hits.iterrows():
        sev = str(h.get("严重度", ""))
        indicator_name = str(h.get("指标", ""))
        reason = str(h.get("命中原因", ""))
        cur_month = pd.to_numeric(h.get("本期月份"), errors="coerce")
        if pd.isna(cur_month):
            cur_month = float(target_month) if target_month is not None else float("nan")
        if pd.isna(cur_month):
            continue
        cur_month_i = int(cur_month)

        key_vals = {k: h.get(k, "") for k in key_fields}
        key_mask = pd.Series(True, index=g.index)
        for k, v in key_vals.items():
            if k not in g.columns:
                continue
            key_mask = key_mask & (g[k].astype(str) == str(v))

        g_key = g[key_mask].copy()
        if g_key.empty:
            continue

        g_cur = g_key[pd.to_numeric(g_key[month_col], errors="coerce").fillna(-1).astype(int) == cur_month_i]
        cur_row = g_cur.iloc[0] if not g_cur.empty else None

        base_problem = {**key_vals}
        rec_problem = _row_for_month(base_problem, cur_month_i, cur_row)
        rec_problem["标注"] = "问题"
        rec_problem["命中原因"] = reason
        rec_problem["指标名称"] = indicator_name
        rec_problem["指标值"] = h.get("本期值", "")
        rec_problem["严重度"] = sev
        out_rows.append(rec_problem)

        hist_mask = pd.to_numeric(g_key[month_col], errors="coerce").fillna(-1).astype(int) < cur_month_i
        g_hist = g_key[hist_mask].copy()

        it_cfg = item_by_name.get(indicator_name)

        hist_ref_row = None
        hist_label = "历史参考"
        hist_reason = "参考：历史参考"
        hist_indicator_val: Any = h.get("前期值", "")
        if not g_hist.empty:
            months_sorted = sorted({int(x) for x in pd.to_numeric(g_hist[month_col], errors="coerce").dropna().astype(int).tolist()})
            months_text = f"({ '，'.join(str(x) for x in months_sorted) })" if months_sorted else ""

            if it_cfg is not None:
                kind = str(it_cfg.get("kind") or it_cfg.get("type") or "").strip().lower()
                if not kind:
                    if it_cfg.get("field") is not None:
                        kind = "value"
                    else:
                        kind = "ratio"

                if kind == "value":
                    hist_reason = "参考：历史月均"
                    hist_label = f"历史月均{months_text}".strip()
                    hist_ref_row = g_hist[sorted({c for c in needed_cols if c})].mean(numeric_only=True)
                else:
                    hist_reason = "参考：历史累计"
                    hist_label = f"历史累计{months_text}".strip()
                    hist_ref_row = g_hist[sorted({c for c in needed_cols if c})].sum(numeric_only=True)

                hist_indicator_val = _calc_item_baseline_hist(g_hist, it_cfg)

        rec_hist = _row_for_month(base_problem, hist_label, hist_ref_row)
        rec_hist["标注"] = "参考"
        rec_hist["命中原因"] = hist_reason
        rec_hist["指标名称"] = indicator_name
        rec_hist["指标值"] = hist_indicator_val
        rec_hist["严重度"] = ""
        out_rows.append(rec_hist)

        if it_cfg and not g_hist.empty:
            months = (
                pd.to_numeric(g_hist[month_col], errors="coerce")
                .dropna()
                .astype(int)
                .drop_duplicates()
                .sort_values()
                .tolist()
            )
            months = [int(x) for x in months if int(x) < cur_month_i]
            last3 = months[-3:]
            for m in reversed(last3):
                g_m = g_key[pd.to_numeric(g_key[month_col], errors="coerce").fillna(-1).astype(int) == int(m)]
                if g_m.empty:
                    continue
                r_m = g_m.iloc[0]
                rec_m = _row_for_month(base_problem, int(m), r_m)
                rec_m["标注"] = "参考"
                rec_m["命中原因"] = "参考：最近月份"
                rec_m["指标名称"] = indicator_name
                rec_m["指标值"] = _calc_item_value(r_m, it_cfg)
                rec_m["严重度"] = ""
                out_rows.append(rec_m)

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    final_cols = key_fields + [month_col] + list(cols_map.keys()) + ["标注", "命中原因", "指标名称", "指标值", "严重度"]
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]

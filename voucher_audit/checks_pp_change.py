from __future__ import annotations

from typing import Any

import pandas as pd

def _pick_hist_months(df: pd.DataFrame, month_col: str, target_month: int) -> list[int]:
    m = pd.to_numeric(df[month_col], errors="coerce").dropna().astype(int)
    hist = m[m < target_month]
    if hist.empty:
        return []
    return sorted({int(x) for x in hist.tolist()})


def _pp_change_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    items = params.get("items") or []
    month_col = str(params.get("month_field") or "月")

    if not key_fields or not items or month_col not in df_inc.columns:
        return pd.DataFrame()
    for c in key_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()

    hist_months = _pick_hist_months(df_inc, month_col, target_month)
    if not hist_months:
        return pd.DataFrame()

    df = df_inc.copy()

    if "三级科目" in key_fields and "三级科目" in df.columns:
        s = df["三级科目"].astype(str).str.strip()
        s = s.str.replace(r"\s*\d+(?:\.\d+)?[%％]\s*$", "", regex=True).str.strip()
        df["三级科目"] = s

    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"].copy()
        if df.empty:
            return pd.DataFrame()

    needed_cols: set[str] = set()
    guard_fields: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            return pd.DataFrame()
        kind = str(it.get("kind") or it.get("type") or "").strip().lower()
        if not kind:
            if it.get("field") is not None:
                kind = "value"
            elif it.get("numerator") is not None and it.get("denominator") is not None:
                kind = "ratio"

        if kind == "value":
            f = str(it.get("field") or "").strip()
            if not f or f not in df.columns:
                return pd.DataFrame()
            needed_cols.add(f)
        else:
            n = str(it.get("numerator") or "").strip()
            d = str(it.get("denominator") or "").strip()
            if not n or not d:
                return pd.DataFrame()
            if n not in df.columns or d not in df.columns:
                return pd.DataFrame()
            needed_cols.add(n)
            needed_cols.add(d)

        gf = str(it.get("guard_field") or "").strip()
        if gf:
            if gf not in df.columns:
                return pd.DataFrame()
            guard_fields.add(gf)

    for c in sorted(needed_cols | guard_fields):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    g = df.groupby(key_fields + [month_col], dropna=False)[sorted(needed_cols | guard_fields)].sum().reset_index()
    cur_g = g[g[month_col] == target_month].copy()
    hist_g = g[g[month_col] < target_month].copy()
    if cur_g.empty or hist_g.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []

    for it in items:
        kind = str(it.get("kind") or it.get("type") or "").strip().lower()
        if not kind:
            if it.get("field") is not None:
                kind = "value"
            elif it.get("numerator") is not None and it.get("denominator") is not None:
                kind = "ratio"

        name = str(it.get("name") or "").strip()
        tol = float(it.get("tolerance_ratio", params.get("tolerance_ratio", 0.3)))
        eps = float(it.get("eps", params.get("eps", 1e-9)))
        min_abs = float(it.get("min_abs", 0))

        guard_field = str(it.get("guard_field") or "").strip()
        min_guard = float(it.get("min_guard", it.get("min_revenue", 0)))

        _cur = cur_g
        _hist = hist_g
        if guard_field:
            _cur = _cur[_cur[guard_field] >= min_guard].copy()
            _hist = _hist[_hist[guard_field] >= min_guard].copy()
            if _cur.empty or _hist.empty:
                continue

        if kind == "value":
            f = str(it.get("field") or "").strip()
            cur_series = _cur.set_index(key_fields)[f]
            hist_series = _hist[key_fields + [month_col, f]].copy()
            hist_series["_val"] = hist_series[f]
        else:
            n = str(it.get("numerator") or "").strip()
            d = str(it.get("denominator") or "").strip()
            cur_series = (_cur.set_index(key_fields)[n] / _cur.set_index(key_fields)[d].replace(0, float("nan")))
            hist_series = _hist[key_fields + [month_col, n, d]].copy()
            hist_series["_val"] = hist_series[n] / hist_series[d].replace(0, float("nan"))

        if hist_series.empty:
            continue

        if kind == "value":
            baseline = hist_series.groupby(key_fields, dropna=False)["_val"].mean().reset_index(name="前期值")
        else:
            # 指标/比率类：用累计口径作为历史参考值（sum(n)/sum(d)），而不是月度比率的均值。
            sums = hist_series.groupby(key_fields, dropna=False)[[n, d]].sum().reset_index()
            sums["前期值"] = sums[n] / sums[d].replace(0, float("nan"))
            baseline = sums[key_fields + ["前期值"]]
        months_s = (
            hist_series.groupby(key_fields, dropna=False)[month_col]
            .apply(lambda s: "，".join(str(int(x)) for x in sorted({int(v) for v in pd.to_numeric(s, errors="coerce").dropna().tolist()})))
            .reset_index(name="前期月份")
        )
        cur_df = cur_series.reset_index(name="本期值")
        cur_df["本期月份"] = target_month

        merged = baseline.merge(cur_df, on=key_fields, how="inner").merge(months_s, on=key_fields, how="left")
        if merged.empty:
            continue

        merged["变化率"] = (merged["本期值"] - merged["前期值"]) / merged["前期值"].replace(0, float("nan"))
        merged["指标"] = name or (f if kind == "value" else f"{n}/{d}")
        merged = merged[(merged["前期值"].abs() > eps) & (merged["变化率"].abs() > tol)]
        if kind == "value" and min_abs > 0:
            merged = merged[merged["本期值"].abs() >= min_abs]
        if merged.empty:
            continue

        cols = key_fields + ["指标", "前期月份", "本期月份", "前期值", "本期值", "变化率"]
        merged = merged[cols]
        for _, r in merged.iterrows():
            out_rows.append(r.to_dict())

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    out.insert(0, "命中原因", "相对历史基线（金额=历史月均；指标/比率=历史累计）波动超过阈值")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=["变化率"], ascending=False, key=lambda s: s.abs())
    return out

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from .config import RuleConfig
from .rules_engine import RuleHit, apply_allowed_values, apply_hard_rule, apply_required_fields


@dataclass(frozen=True)
class AuditTables:
    overview: pd.DataFrame
    aux_rule_violations: pd.DataFrame
    aux_suspect_wrong_account: pd.DataFrame
    income_dim_anomalies: pd.DataFrame
    income_gm_anomalies: pd.DataFrame
    ai_review: Optional[pd.DataFrame]


def _severity_rank(s: str) -> int:
    s = str(s)
    if s == "错误":
        return 0
    if s == "需确认":
        return 1
    return 2


def _build_hits_df(df: pd.DataFrame, hits: list[RuleHit]) -> pd.DataFrame:
    if not hits:
        return pd.DataFrame()
    rows = []
    for h in hits:
        src = f"{h.source.doc} | {h.source.clause}".strip(" |")
        rows.append(
            {
                "严重度": h.severity,
                "规则ID": h.rule_id,
                "规则类型": h.rule_type,
                "制度来源": src,
                "规则描述": h.description,
                "命中原因": h.reason,
                "_row_index": h.row_index,
            }
        )
    out = pd.DataFrame(rows)
    join_df = df.reset_index().rename(columns={"index": "_row_index"})
    out = out.merge(join_df, how="left", on="_row_index")
    out = out.sort_values(by=["严重度"], key=lambda s: s.map(_severity_rank))
    return out


def _rule_name(rule: dict[str, Any]) -> str:
    # Human-readable short name. Keep it stable and Chinese-first.
    name = str(rule.get("name", "") or "").strip()
    if name:
        return name
    desc = str(rule.get("description", "") or "").strip()
    if desc:
        return desc[:40]
    return str(rule.get("id", "") or "").strip()


def _with_rule_name(df: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    name = _rule_name(rule)
    out = df.copy()
    if "规则名称" in out.columns:
        out["规则名称"] = name
        return out
    if "规则ID" in out.columns:
        idx = list(out.columns).index("规则ID") + 1
        out.insert(idx, "规则名称", name)
        return out
    out.insert(0, "规则名称", name)
    return out


_PERCENT_SUFFIX_RE = re.compile(r"\s*\d+(?:\.\d+)?[%％]\s*$")


def _strip_percent_suffix(value: Any) -> str:
    """Normalize business-type strings like '劳务派遣3%' -> '劳务派遣'."""
    s = "" if value is None else str(value).strip()
    if not s:
        return ""
    return _PERCENT_SUFFIX_RE.sub("", s).strip()


def _match_contains_any(value: object, keywords: list[str]) -> bool:
    s = "" if value is None else str(value)
    return any(k in s for k in keywords if str(k).strip())


def _apply_filters(df: pd.DataFrame, filters: list[dict[str, Any]]) -> pd.DataFrame:
    if df is None or df.empty or not filters:
        return df
    out = df
    for f in filters:
        if not isinstance(f, dict):
            continue
        field = str(f.get("field") or "").strip()
        if not field or field not in out.columns:
            continue
        s = out[field].astype(str)
        if "equals" in f:
            out = out[s == str(f.get("equals"))]
        elif "contains" in f:
            out = out[s.str.contains(str(f.get("contains")), regex=False, na=False)]
        elif "regex" in f:
            out = out[s.str.contains(str(f.get("regex")), regex=True, na=False)]
        elif "in" in f:
            allowed = [str(x) for x in (f.get("in") or [])]
            out = out[s.isin(allowed)]
    return out


def _rare_combo_aux(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    min_history_count = int(params.get("min_history_count", 0))
    amount_abs_min = float(params.get("amount_abs_min", 0))

    df = df_aux.copy()
    df["本币"] = pd.to_numeric(df["本币"], errors="coerce").fillna(0.0)

    hist = df[df["月"] < target_month]
    cur = df[df["月"] == target_month]
    if hist.empty or cur.empty:
        return pd.DataFrame()

    hist_counts = hist.groupby(key_fields, dropna=False).size().reset_index(name="history_cnt")
    cur_agg = cur.groupby(key_fields, dropna=False)["本币"].agg(["count", "sum"]).reset_index()
    cur_agg = cur_agg.rename(columns={"count": "cur_cnt", "sum": "cur_sum"})

    merged = cur_agg.merge(hist_counts, how="left", on=key_fields)
    merged["history_cnt"] = merged["history_cnt"].fillna(0).astype(int)
    merged["abs_sum"] = merged["cur_sum"].abs()
    out = merged[(merged["history_cnt"] <= min_history_count) & (merged["abs_sum"] >= amount_abs_min)].copy()
    if out.empty:
        return pd.DataFrame()

    out.insert(0, "命中原因", "目标月出现历史罕见组合（按前置月份基线）")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    cols = ["严重度", "规则ID", "制度来源", "规则描述", "命中原因"] + key_fields + ["history_cnt", "cur_cnt", "cur_sum"]
    out = out.sort_values(by=["abs_sum"], ascending=False)
    return out[cols]


def _sealed_hint_aux(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    if "是否封存" not in df_aux.columns:
        return pd.DataFrame()
    params = rule.get("params", {}) or {}
    amount_abs_min = float(params.get("amount_abs_min", 200000))
    df = df_aux.copy()
    df["本币"] = pd.to_numeric(df["本币"], errors="coerce").fillna(0.0)
    cur = df[(df["月"] == target_month) & (df["是否封存"].astype(str) == "是") & (df["本币"].abs() >= amount_abs_min)]
    if cur.empty:
        return pd.DataFrame()
    out = cur.copy()
    out.insert(0, "_row_index", out.index.astype(int))
    out.insert(0, "命中原因", f"封存=是且本币绝对值≥{amount_abs_min}")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out


def _drift_check_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any], dominance_ratio: float) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    entity_field = str(params.get("entity_field") or "")
    value_field = str(params.get("value_field") or "")
    amount_field = str(params.get("amount_field") or "净额收入")
    min_amount_abs = float(params.get("min_amount_abs", 50000))
    if not entity_field or not value_field:
        return pd.DataFrame()
    if entity_field not in df_inc.columns or value_field not in df_inc.columns:
        return pd.DataFrame()

    df = df_inc.copy()
    df[amount_field] = pd.to_numeric(df[amount_field], errors="coerce").fillna(0.0)

    hist = df[df["月"] < target_month]
    cur = df[df["月"] == target_month]
    if hist.empty or cur.empty:
        return pd.DataFrame()

    hist_agg = hist.groupby([entity_field, value_field], dropna=False)[amount_field].sum().reset_index()
    hist_agg["abs_amount"] = hist_agg[amount_field].abs()
    hist_total = hist_agg.groupby(entity_field, dropna=False)["abs_amount"].sum().reset_index(name="hist_total_abs")
    hist_rank = hist_agg.merge(hist_total, on=entity_field, how="left")
    hist_rank["ratio"] = hist_rank["abs_amount"] / hist_rank["hist_total_abs"].replace(0, float("nan"))
    hist_rank = hist_rank.sort_values(by=["ratio", "abs_amount"], ascending=False)
    dominant = hist_rank.groupby(entity_field, dropna=False).head(1).rename(columns={value_field: "hist_dominant_value", "ratio": "hist_dominant_ratio"})
    dominant = dominant[[entity_field, "hist_dominant_value", "hist_dominant_ratio"]]

    cur_agg = cur.groupby([entity_field, value_field], dropna=False)[amount_field].sum().reset_index()
    cur_total = cur_agg.groupby(entity_field, dropna=False)[amount_field].apply(lambda s: s.abs().sum()).reset_index(name="cur_total_abs")
    cur_rank = cur_agg.copy()
    cur_rank["abs_amount"] = cur_rank[amount_field].abs()
    cur_rank = cur_rank.merge(cur_total, on=entity_field, how="left")
    cur_rank["ratio"] = cur_rank["abs_amount"] / cur_rank["cur_total_abs"].replace(0, float("nan"))
    cur_rank = cur_rank.sort_values(by=["ratio", "abs_amount"], ascending=False)
    cur_dom = cur_rank.groupby(entity_field, dropna=False).head(1).rename(columns={value_field: "cur_dominant_value", "ratio": "cur_dominant_ratio"})
    cur_dom = cur_dom[[entity_field, "cur_dominant_value", "cur_dominant_ratio", "cur_total_abs"]]

    merged = cur_dom.merge(dominant, on=entity_field, how="left")
    merged = merged[merged["cur_total_abs"] >= min_amount_abs]
    merged = merged[merged["hist_dominant_value"].notna()]
    merged = merged[merged["cur_dominant_value"] != merged["hist_dominant_value"]]
    merged = merged[merged["hist_dominant_ratio"] >= dominance_ratio]
    if merged.empty:
        return pd.DataFrame()

    merged.insert(0, "命中原因", "主映射突变（按前置月份基线）")
    merged.insert(0, "规则描述", str(rule.get("description", "")))
    merged.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    merged.insert(0, "规则ID", str(rule.get("id")))
    merged.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return merged


def _mapping_check_income(df_inc: pd.DataFrame, df_map: Optional[pd.DataFrame], target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    if df_map is None or df_map.empty:
        return pd.DataFrame()
    required = ["主体账簿", "月", "业务类型", "账载客户", "部门", "项目", "实际客户"]
    if any(c not in df_map.columns for c in required):
        return pd.DataFrame()
    if any(c not in df_inc.columns for c in ["主体账簿", "月", "三级科目", "账载客户", "部门", "项目", "实际客户"]):
        return pd.DataFrame()

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

    merged = cur.merge(ref[key_cols + ["实际客户_映射"]], how="left", on=key_cols)
    mism = merged[(merged["实际客户_映射"].notna()) & (merged["实际客户"] != merged["实际客户_映射"])].copy()
    if mism.empty:
        return pd.DataFrame()

    mism = mism.drop(columns=["实际客户_映射"], errors="ignore")
    mism.insert(0, "命中原因", "实际客户与映射表不一致")
    mism.insert(0, "规则描述", str(rule.get("description", "")))
    mism.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    mism.insert(0, "规则ID", str(rule.get("id")))
    mism.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return mism


def _gross_margin_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any], gm_cfg: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or ["实际客户"])]
    revenue_field = str(params.get("revenue_field") or "净额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    profit_field = str(params.get("profit_field") or "项目毛利润")

    min_revenue = float(gm_cfg.get("min_revenue", 50000))
    lower = float(gm_cfg.get("lower", 0.0))
    upper = float(gm_cfg.get("upper", 0.6))
    iqr_k = float(gm_cfg.get("iqr_k", 2.5))
    low_cost_rate_drop = float(gm_cfg.get("low_cost_rate_drop", 0.15))

    df = df_inc.copy()
    for c in [revenue_field, cost_field, profit_field]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    cur = df[df["月"] == target_month]
    hist = df[df["月"] < target_month]
    if cur.empty:
        return pd.DataFrame()

    def agg(d: pd.DataFrame) -> pd.DataFrame:
        g = d.groupby(group_fields, dropna=False)[[revenue_field, cost_field, profit_field]].sum().reset_index()
        g["毛利率"] = g.apply(lambda r: (r[profit_field] / r[revenue_field]) if r[revenue_field] > 0 else float("nan"), axis=1)
        g["成本率"] = g.apply(lambda r: (r[cost_field] / r[revenue_field]) if r[revenue_field] > 0 else float("nan"), axis=1)
        return g

    cur_g = agg(cur)
    cur_g = cur_g[cur_g[revenue_field] >= min_revenue].copy()
    if cur_g.empty:
        return pd.DataFrame()

    out_rows: list[dict[str, Any]] = []
    for _, r in cur_g.iterrows():
        gm = r["毛利率"]
        if math.isnan(gm):
            continue
        if gm < lower:
            out_rows.append({**r.to_dict(), "命中原因": f"毛利率<{lower:.2f}"})
        elif gm > upper:
            out_rows.append({**r.to_dict(), "命中原因": f"毛利率>{upper:.2f}"})

    out = pd.DataFrame(out_rows)

    if not hist.empty:
        hist_g = agg(hist)
        if not hist_g.empty:
            q = hist_g.groupby(group_fields, dropna=False)["毛利率"].quantile([0.25, 0.75]).unstack().reset_index()
            q = q.rename(columns={0.25: "q1", 0.75: "q3"})
            base = hist_g.groupby(group_fields, dropna=False)["成本率"].median().reset_index(name="hist_cost_rate_median")
            stats = q.merge(base, on=group_fields, how="left")
            merged = cur_g.merge(stats, on=group_fields, how="left")
            merged["iqr"] = (merged["q3"] - merged["q1"])

            rel = merged[(merged["iqr"].notna()) & (merged["iqr"] > 0) & ((merged["毛利率"] < (merged["q1"] - iqr_k * merged["iqr"])) | (merged["毛利率"] > (merged["q3"] + iqr_k * merged["iqr"])))]
            if not rel.empty:
                rel2 = rel.copy()
                rel2["命中原因"] = "毛利率偏离历史分布（IQR）"
                out = pd.concat([out, rel2[cur_g.columns.tolist() + ["命中原因"]]], ignore_index=True)

            cm = merged[(merged["hist_cost_rate_median"].notna()) & (merged["成本率"].notna()) & ((merged["hist_cost_rate_median"] - merged["成本率"]) >= low_cost_rate_drop)]
            if not cm.empty:
                cm2 = cm.copy()
                cm2["命中原因"] = "成本率显著低于历史中位数（疑似少入成本）"
                out = pd.concat([out, cm2[cur_g.columns.tolist() + ["命中原因"]]], ignore_index=True)

    if out.empty:
        return pd.DataFrame()

    out = out.drop_duplicates(subset=group_fields + ["命中原因"])
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=[revenue_field], ascending=False)
    return out


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


def _rev_cost_zero_mismatch_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """
    检查：同一主键下，全额收入=0但成本≠0，或 成本=0但全额收入≠0。
    """
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [])]
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    eps = float(params.get("eps", 1e-9))
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    biz_type_keywords = [str(x) for x in (params.get("biz_type_keywords") or [])]
    strip_percent_suffix = bool(params.get("biz_type_strip_percent_suffix", False))

    for c in key_fields + [revenue_field, cost_field, "月"]:
        if c not in df_inc.columns:
            return pd.DataFrame()

    cur = df_inc[df_inc["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    if strip_percent_suffix and biz_type_field in cur.columns:
        cur[biz_type_field] = cur[biz_type_field].map(_strip_percent_suffix)
    if "部门" in cur.columns:
        cur = cur[cur["部门"].astype(str).str.strip() != "集团本部"].copy()
        if cur.empty:
            return pd.DataFrame()

    # Reduce noise: only keep specific business types when configured.
    if biz_type_keywords and biz_type_field in cur.columns:
        cur = cur[cur[biz_type_field].apply(lambda v: _match_contains_any(v, biz_type_keywords))]
        if cur.empty:
            return pd.DataFrame()

    cur[revenue_field] = pd.to_numeric(cur[revenue_field], errors="coerce").fillna(0.0)
    cur[cost_field] = pd.to_numeric(cur[cost_field], errors="coerce").fillna(0.0)

    group_fields = list(key_fields)
    if strip_percent_suffix and biz_type_field and biz_type_field in group_fields:
        norm_col = f"__norm_{biz_type_field}"
        cur[norm_col] = cur[biz_type_field].map(_strip_percent_suffix)
        group_fields = [norm_col if c == biz_type_field else c for c in group_fields]

    g = cur.groupby(group_fields, dropna=False)[[revenue_field, cost_field]].sum().reset_index()
    if strip_percent_suffix and biz_type_field and biz_type_field in key_fields:
        # Keep output column name stable for report readability.
        g = g.rename(columns={f"__norm_{biz_type_field}": biz_type_field})

    g["rev_is_zero"] = g[revenue_field].abs() <= eps
    g["cost_is_zero"] = g[cost_field].abs() <= eps

    out = g[(g["rev_is_zero"] & (~g["cost_is_zero"])) | ((~g["rev_is_zero"]) & g["cost_is_zero"])].copy()
    if out.empty:
        return pd.DataFrame()

    out["命中原因"] = out.apply(
        lambda r: f"{revenue_field}=0且{cost_field}≠0" if bool(r["rev_is_zero"]) and not bool(r["cost_is_zero"]) else f"{cost_field}=0且{revenue_field}≠0",
        axis=1,
    )
    out = out.drop(columns=["rev_is_zero", "cost_is_zero"])
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    out = out.sort_values(by=[revenue_field], ascending=False)
    return out


def _forbidden_regex_aux(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    field = str(params.get("field") or params.get("summary_field") or "摘要")
    voucher_field = str(params.get("voucher_field") or "凭证号")
    month_field = str(params.get("month_field") or "月")
    pattern = str(params.get("regex") or params.get("pattern") or "")
    if not pattern:
        return pd.DataFrame()
    if field not in df_aux.columns or month_field not in df_aux.columns:
        return pd.DataFrame()

    cur = df_aux[df_aux[month_field] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    import re

    regex = re.compile(pattern)
    rows: list[dict[str, Any]] = []
    for idx, r in cur.iterrows():
        text = "" if r.get(field) is None else str(r.get(field))
        m = regex.search(text)
        if not m:
            continue
        rows.append(
            {
                "_row_index": int(idx),
                "凭证号": r.get(voucher_field, ""),
                "摘要": text,
                "命中片段": m.group(0),
            }
        )
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out.insert(0, "命中原因", f"{field} 命中了禁用模式：{pattern}")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out


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


def _neg_profit_ratio_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or [])]
    revenue_field = str(params.get("revenue_field") or "全额收入")
    net_revenue_field = str(params.get("net_revenue_field") or "净额收入")
    profit_field = str(params.get("profit_field") or "项目毛利润")
    ratio_abs_min = float(params.get("ratio_abs_min", 0.3))
    min_revenue = float(params.get("min_revenue", 10000))
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    biz_type_keywords = [str(x) for x in (params.get("biz_type_keywords") or [])]
    dept_field = str(params.get("dept_field") or "部门")
    exclude_dept_regex = str(params.get("exclude_dept_regex") or "")

    if not group_fields:
        return pd.DataFrame()
    for c in group_fields + ["月", revenue_field, net_revenue_field, profit_field, biz_type_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()

    cur = df_inc[df_inc["月"] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    if "三级科目" in cur.columns:
        cur["三级科目"] = cur["三级科目"].map(_strip_percent_suffix)

    if exclude_dept_regex and dept_field in cur.columns:
        cur = cur[~cur[dept_field].astype(str).str.contains(exclude_dept_regex, regex=True, na=False)]
        if cur.empty:
            return pd.DataFrame()

    if biz_type_keywords:
        cur = cur[cur[biz_type_field].apply(lambda v: _match_contains_any(v, biz_type_keywords))]
        if cur.empty:
            return pd.DataFrame()

    cur[revenue_field] = pd.to_numeric(cur[revenue_field], errors="coerce").fillna(0.0)
    cur[net_revenue_field] = pd.to_numeric(cur[net_revenue_field], errors="coerce").fillna(0.0)
    cur[profit_field] = pd.to_numeric(cur[profit_field], errors="coerce").fillna(0.0)

    g = cur.groupby(group_fields, dropna=False)[[revenue_field, net_revenue_field, profit_field]].sum().reset_index()
    g = g[g[net_revenue_field].abs() >= min_revenue]
    if g.empty:
        return pd.DataFrame()
    g["毛利/净额收入"] = g.apply(
        lambda r: (abs(float(r[profit_field])) / float(r[net_revenue_field])) if float(r[net_revenue_field]) != 0 else float("nan"),
        axis=1,
    )
    out = g[(g[profit_field] < 0) & (g["毛利/净额收入"] >= ratio_abs_min)].copy()
    if out.empty:
        return pd.DataFrame()
    out.insert(0, "命中原因", f"项目毛利润<0 且 |毛利/净额收入|>={ratio_abs_min:.2f}")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out.sort_values(by=[revenue_field], ascending=False)


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


def _summary_zs_suffix_aux(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """
    检查：摘要中出现 Z\\d+S\\d+（不区分大小写）后，紧跟其它字符（含 '-' 或文字）。
    允许：结束、空白、常见标点分隔符。
    """
    params = rule.get("params", {}) or {}
    summary_field = str(params.get("summary_field") or "摘要")
    voucher_field = str(params.get("voucher_field") or "凭证号")
    month_field = str(params.get("month_field") or "月")
    pattern = str(params.get("pattern") or r"(?i)Z\d+S\d+")
    allowed_next = params.get("allowed_next_chars") or [
        "",
        " ",
        "\t",
        "\n",
        "\r",
        "，",
        ",",
        "。",
        ".",
        "；",
        ";",
        "：",
        ":",
        "、",
        "/",
        "\\",
        "|",
        ")",
        "）",
        "]",
        "】",
        "}",
        "）",
    ]

    if summary_field not in df_aux.columns or month_field not in df_aux.columns:
        return pd.DataFrame()

    cur = df_aux[df_aux[month_field] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    import re

    regex = re.compile(pattern)

    def find_violations(text: str) -> list[dict[str, Any]]:
        s = "" if text is None else str(text)
        out: list[dict[str, Any]] = []
        for m in regex.finditer(s):
            end = m.end()
            next_char = s[end : end + 1]
            if next_char == "":
                continue
            if next_char in allowed_next:
                continue
            # 对于连续空白/标点已经覆盖；其余一律判为违规（含 '-'）
            out.append(
                {
                    "Z代码": m.group(0),
                    "后续字符": next_char,
                    "后续片段": s[end : min(len(s), end + 20)],
                }
            )
        return out

    rows = []
    for idx, r in cur.iterrows():
        violations = find_violations(r.get(summary_field))
        for v in violations:
            rows.append(
                {
                    "_row_index": int(idx),
                    "凭证号": r.get(voucher_field, ""),
                    "摘要": r.get(summary_field, ""),
                    **v,
                }
            )

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out.insert(0, "命中原因", "Z代码后紧跟了不允许的字符（含文字或 '-' 等）")
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    return out


def _headcount_data_check_aux(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """
    人次数据检查（合并原 ZY/YS/ZS_SUFFIX/ZS_NONPOSITIVE 四条规则）。

    业务口径：
    1. 摘要含 ZY 码或 YS 码 → 错误（人次数据填写错误）
    2. 人次码 Z<num>S<num> 后缀紧跟不允许的字符 → 错误
    3. 冲销/红冲/冲字开头场景下，ZS 数字应为负（如 Z-50S-20），若为正 → 需确认
    4. 非冲销/红冲场景下，ZS 数字应为正（如 Z50S20），若含负号 → 需确认
       例外：摘要以"调整"开头且本币为负数，视为冲销场景
    """
    params = rule.get("params", {}) or {}
    summary_field = str(params.get("summary_field") or "摘要")
    voucher_field = str(params.get("voucher_field") or "凭证号")
    month_field = str(params.get("month_field") or "月")
    local_currency_field = str(params.get("local_currency_field") or "本币")
    zy_pattern = str(params.get("zy_pattern") or r"(?i)Z\d+Y\d+")
    ys_pattern = str(params.get("ys_pattern") or r"(?i)Y\d+S\d+")
    zs_pattern = str(params.get("zs_pattern") or r"(?i)Z-?\d+S-?\d+")
    # 匹配：冲销、红冲、以"冲"开头
    red_flush_pattern = str(params.get("red_flush_pattern") or r"(冲销|红冲|^冲)")
    allowed_next_chars = params.get("allowed_next_chars") or [
        "", " ", "\t", "，", ",", "。", ".", "；", ";", "：", ":",
        "、", "/", "\\", "|", ")", "）", "]", "】", "}",
    ]

    if summary_field not in df_aux.columns or month_field not in df_aux.columns:
        return pd.DataFrame()

    cur = df_aux[df_aux[month_field] == target_month].copy()
    if cur.empty:
        return pd.DataFrame()

    import re as _re
    re_zy = _re.compile(zy_pattern)
    re_ys = _re.compile(ys_pattern)
    re_zs = _re.compile(zs_pattern)
    re_red = _re.compile(red_flush_pattern)
    re_adjust = _re.compile(r"^调整")  # 以"调整"开头

    rows: list[dict[str, Any]] = []
    for idx, r in cur.iterrows():
        text = "" if r.get(summary_field) is None else str(r.get(summary_field))
        voucher = r.get(voucher_field, "")

        # 判断冲销场景：
        # 1. 匹配 red_flush_pattern（冲销|红冲|^冲）
        # 2. 以"调整"开头且本币为负数
        is_red = bool(re_red.search(text))
        if not is_red and re_adjust.search(text):
            # 检查本币是否为负数
            local_currency = r.get(local_currency_field)
            if local_currency is not None:
                try:
                    if float(local_currency) < 0:
                        is_red = True
                except (ValueError, TypeError):
                    pass

        # 1) ZY 码 → 错误
        for m in re_zy.finditer(text):
            rows.append({
                "_row_index": int(idx),
                "凭证号": voucher,
                "摘要": text,
                "命中码": m.group(0),
                "问题分类": "人次数据填写错误",
                "严重度": "错误",
                "命中原因": f"摘要含 ZY 码 {m.group(0)}，属于人次数据填写错误",
            })

        # 2) YS 码 → 错误
        for m in re_ys.finditer(text):
            rows.append({
                "_row_index": int(idx),
                "凭证号": voucher,
                "摘要": text,
                "命中码": m.group(0),
                "问题分类": "人次数据填写错误",
                "严重度": "错误",
                "命中原因": f"摘要含 YS 码 {m.group(0)}，属于人次数据填写错误",
            })

        # 3) ZS 人次码 — 检查后缀 + 符号合规
        for m in re_zs.finditer(text):
            matched = m.group(0)
            end = m.end()

            # 3a) 后缀检查：紧跟的字符是否在允许列表中
            next_char = text[end: end + 1]
            suffix_violation = False
            if next_char != "" and next_char not in allowed_next_chars:
                suffix_violation = True

            # 3b) 符号合规检查：提取 Z/S 后的数字（含正负号）
            #     匹配 Z-?\d+S-?\d+ 中各部分
            inner = _re.compile(r"(?i)Z(-?\d+)S(-?\d+)", _re.IGNORECASE)
            im = inner.match(matched)
            z_num = int(im.group(1)) if im else 0
            s_num = int(im.group(2)) if im else 0

            sign_violation = False
            sign_reason = ""
            if is_red:
                # 冲销/红冲：Z 和 S 后数字应为负（或零）
                if z_num > 0 or s_num > 0:
                    sign_violation = True
                    sign_reason = "冲销/红冲场景下人次码应为负数格式（如 Z-50S-20）"
            else:
                # 非冲销/红冲：Z 和 S 后数字应为正（或零）
                if z_num < 0 or s_num < 0:
                    sign_violation = True
                    sign_reason = "非冲销/红冲场景下人次码数字不应为负（如 Z50S20）"

            # 优先报告后缀错误（错误级），再报告符号问题（需确认级）
            if suffix_violation:
                rows.append({
                    "_row_index": int(idx),
                    "凭证号": voucher,
                    "摘要": text,
                    "命中码": matched,
                    "问题分类": "人次数据填写错误",
                    "严重度": "错误",
                    "命中原因": f"人次码 {matched} 后紧跟不允许的字符 '{next_char}'",
                })
            if sign_violation:
                rows.append({
                    "_row_index": int(idx),
                    "凭证号": voucher,
                    "摘要": text,
                    "命中码": matched,
                    "问题分类": "人次码符号需确认",
                    "严重度": "需确认",
                    "命中原因": sign_reason + f"，实际为 {matched}",
                })

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    # Keep the per-row severity from the check logic (not the rule-level default)
    # so that "错误" and "需确认" can coexist in the same rule output.
    if "严重度" not in out.columns:
        out.insert(0, "严重度", str(rule.get("severity", "错误")))
    return out


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
    src_doc = (rule.get("source") or {}).get("doc", "")
    src_clause = (rule.get("source") or {}).get("clause", "")
    source_str = f"{src_doc} | {src_clause}".strip(" |")

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

    merged = cur.merge(ref[key_cols + ["实际客户_映射"]], how="left", on=key_cols)
    mism = merged[(merged["实际客户_映射"].notna()) & (merged["实际客户"] != merged["实际客户_映射"])].copy()
    if mism.empty:
        return pd.DataFrame()

    mism = mism.drop(columns=["实际客户_映射"], errors="ignore")
    mism.insert(0, "问题分类", "实际客户与映射表不一致")
    mism.insert(0, "命中原因", "实际客户与映射表不一致")
    mism.insert(0, "规则描述", str(rule.get("description", "")))
    mism.insert(0, "制度来源", source_str)
    mism.insert(0, "规则名称", rule_name)
    mism.insert(0, "规则ID", rule_id)
    mism.insert(0, "严重度", "错误")
    return mism


def _pick_prev_month(df: pd.DataFrame, month_col: str, target_month: int) -> Optional[int]:
    m = pd.to_numeric(df[month_col], errors="coerce").dropna().astype(int)
    prev = m[m < target_month]
    if prev.empty:
        return None
    return int(prev.max())


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


def run_checks(
    rules: RuleConfig,
    df_aux: pd.DataFrame,
    df_income: pd.DataFrame,
    df_mapping: Optional[pd.DataFrame],
    target_month: int,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    aux_hits: list[pd.DataFrame] = []
    aux_suspects: list[pd.DataFrame] = []
    inc_dim: list[pd.DataFrame] = []
    inc_gm: list[pd.DataFrame] = []

    for rule in rules.checks:
        rtype = str(rule.get("type", ""))
        scope = str(rule.get("scope", ""))
        if scope == "aux_ledger":
            if rtype == "hard_rule":
                aux_hits.append(_with_rule_name(_build_hits_df(df_aux, apply_hard_rule(df_aux, rule)), rule))
            elif rtype == "allowed_values":
                aux_hits.append(_with_rule_name(_build_hits_df(df_aux, apply_allowed_values(df_aux, rule)), rule))
            elif rtype == "required_fields":
                aux_hits.append(_with_rule_name(_build_hits_df(df_aux, apply_required_fields(df_aux, rule)), rule))
            elif rtype == "rare_combo":
                aux_suspects.append(_with_rule_name(_rare_combo_aux(df_aux, target_month, rule), rule))
            elif rtype == "sealed_hint":
                aux_suspects.append(_with_rule_name(_sealed_hint_aux(df_aux, target_month, rule), rule))
            elif rtype == "headcount_data_check":
                _hc = _headcount_data_check_aux(df_aux, target_month, rule)
                if _hc is not None and not _hc.empty:
                    _hc = _with_rule_name(_hc, rule)
                    err = _hc[_hc["严重度"] == "错误"]
                    sus = _hc[_hc["严重度"] != "错误"]
                    if not err.empty:
                        aux_hits.append(err)
                    if not sus.empty:
                        aux_suspects.append(sus)
            elif rtype == "forbidden_regex":
                aux_hits.append(_with_rule_name(_forbidden_regex_aux(df_aux, target_month, rule), rule))
        elif scope == "income_cost":
            if rtype == "customer_consistency_check":
                _cc = _customer_consistency_check_income(df_income, df_mapping, target_month, rule, dominance_ratio=rules.thresholds.drift_dominance_ratio)
                if _cc is not None and not _cc.empty:
                    _cc = _with_rule_name(_cc, rule)
                    err = _cc[_cc["严重度"] == "错误"]
                    sus = _cc[_cc["严重度"] != "错误"]
                    if not err.empty:
                        inc_dim.append(err)
                    if not sus.empty:
                        inc_dim.append(sus)
            elif rtype == "mapping_check":
                inc_dim.append(_with_rule_name(_mapping_check_income(df_income, df_mapping, target_month, rule), rule))
            elif rtype == "drift_check":
                inc_dim.append(_with_rule_name(_drift_check_income(df_income, target_month, rule, dominance_ratio=rules.thresholds.drift_dominance_ratio), rule))
            elif rtype == "combo_drift":
                inc_dim.append(_with_rule_name(_combo_drift_income(df_income, target_month, rule, dominance_ratio=rules.thresholds.drift_dominance_ratio), rule))
            elif rtype == "rev_cost_zero_mismatch":
                inc_dim.append(_with_rule_name(_rev_cost_zero_mismatch_income(df_income, target_month, rule), rule))
            elif rtype == "pp_change":
                inc_dim.append(_with_rule_name(_pp_change_income(df_income, target_month, rule), rule))
            elif rtype == "metric_pp_change":
                inc_dim.append(_with_rule_name(_metric_pp_change_income(df_income, target_month, rule), rule))
            elif rtype == "value_pp_change":
                inc_dim.append(_with_rule_name(_value_pp_change_income(df_income, target_month, rule), rule))
            elif rtype == "ratio_pp_change":
                inc_dim.append(_with_rule_name(_ratio_pp_change_income(df_income, target_month, rule), rule))
            elif rtype == "gross_margin":
                inc_gm.append(_with_rule_name(_gross_margin_income(df_income, target_month, rule, rules.thresholds.gross_margin), rule))
            elif rtype == "distinct_count":
                inc_dim.append(_with_rule_name(_distinct_count_income(df_income, target_month, rule), rule))
            elif rtype == "neg_profit_ratio":
                inc_gm.append(_with_rule_name(_neg_profit_ratio_income(df_income, target_month, rule), rule))
            elif rtype == "outsourcing_missing_cost":
                inc_dim.append(_with_rule_name(_outsourcing_missing_cost_income(df_income, target_month, rule), rule))

    aux_parts = [x for x in aux_hits if x is not None and not x.empty]
    suspect_parts = [x for x in aux_suspects if x is not None and not x.empty]
    dim_parts = [x for x in inc_dim if x is not None and not x.empty]
    gm_parts = [x for x in inc_gm if x is not None and not x.empty]

    aux_rule_violations = pd.concat(aux_parts, ignore_index=True) if aux_parts else pd.DataFrame()
    aux_suspect_wrong = pd.concat(suspect_parts, ignore_index=True) if suspect_parts else pd.DataFrame()
    income_dim = pd.concat(dim_parts, ignore_index=True) if dim_parts else pd.DataFrame()
    income_gm = pd.concat(gm_parts, ignore_index=True) if gm_parts else pd.DataFrame()

    return aux_rule_violations, aux_suspect_wrong, income_dim, income_gm

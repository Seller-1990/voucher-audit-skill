from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from .config import RuleConfig
from .rules_engine import RuleHit, apply_allowed_values, apply_hard_rule, apply_required_fields
from .check_utils import (
    _match_contains_any,
    _rule_name,
    _severity_rank,
    _strip_percent_suffix,
)
from .checks_customer import _customer_consistency_check_income
from .checks_customer_subchecks import _combo_drift_income, _distinct_count_income
from .checks_pp_change import _pp_change_income
from .checks_headcount import _headcount_data_check_aux
from .checks_legacy_pp import _metric_pp_change_income, _ratio_pp_change_income, _value_pp_change_income
from .checks_outsourcing import _outsourcing_missing_cost_income


@dataclass(frozen=True)
class AuditTables:
    overview: pd.DataFrame
    aux_rule_violations: pd.DataFrame
    aux_suspect_wrong_account: pd.DataFrame
    income_dim_anomalies: pd.DataFrame
    income_gm_anomalies: pd.DataFrame
    ai_review: Optional[pd.DataFrame]


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

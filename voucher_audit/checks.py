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


# ---------------------------------------------------------------------------
# 新增规则（整合自"收入成本表1.py"异常检测）：高毛利率/倒挂/人次社保背离/
# 返费挂靠占比/费用占比/成本突然出现/环比波动
# ---------------------------------------------------------------------------

def _fmt_money(v: Any) -> str:
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return str(v)


def _base_month_agg(df_inc: pd.DataFrame, target_month: int, params: dict[str, Any], num_fields: list[str]) -> pd.DataFrame:
    """按 group_fields 聚合目标月数据（排除集团本部、三级科目归一），返回聚合 df。"""
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "月", "三级科目", "实际客户"])]
    for c in group_fields + ["月"] + num_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    if "三级科目" in cur.columns:
        cur["三级科目"] = cur["三级科目"].map(_strip_percent_suffix)
    if "部门" in cur.columns:
        cur = cur[cur["部门"].astype(str).str.strip() != "集团本部"]
    if cur.empty:
        return pd.DataFrame()
    for c in num_fields:
        cur[c] = pd.to_numeric(cur[c], errors="coerce").fillna(0.0)
    return cur.groupby(group_fields, dropna=False)[num_fields].sum().reset_index()


def _annotate_hits(out: pd.DataFrame, rule: dict[str, Any], reason: str, sort_by: str) -> pd.DataFrame:
    if out is None or out.empty:
        return out
    out = out.copy()
    if "命中原因" in out.columns:
        if reason:
            out["命中原因"] = reason
    else:
        out.insert(0, "命中原因", reason)
    out.insert(0, "规则描述", str(rule.get("description", "")))
    out.insert(0, "制度来源", f"{(rule.get('source') or {}).get('doc','')} | {(rule.get('source') or {}).get('clause','')}".strip(" |"))
    out.insert(0, "规则名称", _rule_name(rule))
    out.insert(0, "规则ID", str(rule.get("id")))
    out.insert(0, "严重度", str(rule.get("severity", "需确认")))
    if sort_by in out.columns:
        out = out.sort_values(by=[sort_by], ascending=False)
    return out


def _gm_high_ratio_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """高毛利率（>阈值）：毛利高往往=少入成本/漏记成本。

    分母用 全额收入（而非净额收入）：净额收入在代收代付型业务（劳务派遣/外包）中占比很小，
    会把正常业务虚高成高毛利；全额收入口径更符合"成本占收入比例异常低"的检测语义。
    """
    params = rule.get("params", {}) or {}
    threshold = float(params.get("threshold", 0.5))
    min_revenue = float(params.get("min_revenue", 10000))
    revenue_field = str(params.get("revenue_field") or "全额收入")
    profit_field = str(params.get("profit_field") or "项目毛利润")
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, profit_field])
    if g.empty:
        return pd.DataFrame()
    g = g[g[revenue_field].abs() >= min_revenue]
    if g.empty:
        return pd.DataFrame()
    g["毛利率"] = g.apply(lambda r: (r[profit_field] / r[revenue_field]) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    out = g[(g["毛利率"].notna()) & (g["毛利率"] > threshold)].copy()
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out["毛利率"]
    return _annotate_hits(out, rule, "毛利偏高，可能漏记了成本", revenue_field)


def _rev_cost_inversion_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """收入>0 且 成本>0 且 收入<成本（入不敷出）。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, cost_field])
    if g.empty:
        return pd.DataFrame()
    g["倒挂差额"] = g[cost_field] - g[revenue_field]
    g["亏损率"] = g.apply(lambda r: (float(r["倒挂差额"]) / float(r[revenue_field])) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    out = g[(g[revenue_field] > 0) & (g[cost_field] > 0) & (g[revenue_field] < g[cost_field])].copy()
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out["倒挂差额"]
    out["命中原因"] = out.apply(
        lambda r: f"收入{_fmt_money(r[revenue_field])}元 < 成本{_fmt_money(r[cost_field])}元，花的比挣的多{_fmt_money(r['倒挂差额'])}元（{f'{r['亏损率']*100:.1f}%'}）" if pd.notna(r["亏损率"]) else f"收入{_fmt_money(r[revenue_field])}元 < 成本{_fmt_money(r[cost_field])}元，花的比挣的多{_fmt_money(r['倒挂差额'])}元",
        axis=1,
    )
    return _annotate_hits(out, rule, "", revenue_field)


def _headcount_rev_mismatch_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """结算人次与收入背离：人次>0但收入=0；收入>阈值但人次=0。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "净额收入")
    headcount_field = str(params.get("headcount_field") or "结算人次")
    income_min = float(params.get("income_min", 1000))
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, headcount_field])
    if g.empty:
        return pd.DataFrame()
    eps = 1e-9
    m_hc_no_rev = (g[headcount_field] > eps) & (g[revenue_field].abs() <= eps)
    m_rev_no_hc = (g[headcount_field].abs() <= eps) & (g[revenue_field].abs() > income_min)
    parts: list[pd.DataFrame] = []
    p1 = g[m_hc_no_rev].copy()
    if not p1.empty:
        p1["指标值"] = p1[headcount_field]
        p1["命中原因"] = p1.apply(lambda r: f"结算人数是 {int(r[headcount_field])} 人，但没有收入", axis=1)
        parts.append(p1)
    p2 = g[m_rev_no_hc].copy()
    if not p2.empty:
        p2["指标值"] = p2[revenue_field]
        p2["命中原因"] = p2.apply(lambda r: f"有收入 {_fmt_money(r[revenue_field])} 元，但结算人数是 0", axis=1)
        parts.append(p2)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return _annotate_hits(out, rule, "", revenue_field)


def _social_headcount_mismatch_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """社保人数异常：社保人数>0但收入=0；社保人数>0但社保费=0。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "净额收入")
    social_people_field = str(params.get("social_people_field") or "社保人数")
    social_fee_field = str(params.get("social_fee_field") or "社保")
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, social_people_field, social_fee_field])
    if g.empty:
        return pd.DataFrame()
    eps = 1e-9
    parts: list[pd.DataFrame] = []
    p1 = g[(g[social_people_field] > eps) & (g[revenue_field].abs() <= eps)].copy()
    if not p1.empty:
        p1["指标值"] = p1[social_people_field]
        p1["命中原因"] = p1.apply(lambda r: f"报了社保 {int(r[social_people_field])} 人，但没有收入", axis=1)
        parts.append(p1)
    p2 = g[(g[social_people_field] > eps) & (g[social_fee_field].abs() <= eps)].copy()
    if not p2.empty:
        p2["指标值"] = p2[social_people_field]
        p2["命中原因"] = p2.apply(lambda r: f"报了社保 {int(r[social_people_field])} 人，但没交社保费（可能漏记）", axis=1)
        parts.append(p2)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return _annotate_hits(out, rule, "", revenue_field)


def _cost_ratio_high_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """返费/挂靠等成本项占收入比例过高。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "全额收入")
    ratios = params.get("ratios") or []
    num_fields = [revenue_field] + [str(r.get("field")) for r in ratios if isinstance(r, dict) and r.get("field")]
    g = _base_month_agg(df_inc, target_month, params, num_fields)
    if g.empty:
        return pd.DataFrame()
    parts: list[pd.DataFrame] = []
    for it in ratios:
        if not isinstance(it, dict):
            continue
        f = str(it.get("field") or "")
        th = float(it.get("threshold", 0.3))
        if not f or f not in g.columns:
            continue
        g["_ratio"] = g.apply(lambda r: (abs(float(r[f])) / float(r[revenue_field])) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
        p = g[(g["_ratio"].notna()) & (g["_ratio"] > th) & (g[revenue_field].abs() > 0)].copy()
        if p.empty:
            continue
        p["指标值"] = p["_ratio"]
        p["命中原因"] = p.apply(lambda r: f"{f} {_fmt_money(r[f])} 元，占收入 {_fmt_money(r[revenue_field])} 元的 {f'{r['_ratio']*100:.1f}%'}，超过 {th:.0%}（可能记错科目）", axis=1)
        p = p.drop(columns=["_ratio"])
        parts.append(p)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return _annotate_hits(out, rule, "", revenue_field)


def _expense_ratio_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """福利费/其他费用占收入比例或绝对值异常。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "全额收入")
    welfare_field = str(params.get("welfare_field") or "项目福利费")
    other_field = str(params.get("other_field") or "项目其他费用")
    welfare_ratio = float(params.get("welfare_ratio", 0.03))
    welfare_abs = float(params.get("welfare_abs", 50000))
    other_ratio = float(params.get("other_ratio", 0.10))
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, welfare_field, other_field])
    if g.empty:
        return pd.DataFrame()
    parts: list[pd.DataFrame] = []
    p1 = g[(g[welfare_field].abs() > 0) & (g[revenue_field].abs() > 0) & (g[welfare_field].abs() > g[revenue_field].abs() * welfare_ratio)].copy()
    if not p1.empty:
        p1["指标值"] = p1[welfare_field]
        p1["命中原因"] = p1.apply(lambda r: f"福利费 {_fmt_money(r[welfare_field])} 元，占收入比例超过 {welfare_ratio:.0%}（可能记错科目）", axis=1)
        parts.append(p1)
    p2 = g[g[welfare_field].abs() > welfare_abs].copy()
    if not p2.empty:
        p2["指标值"] = p2[welfare_field]
        p2["命中原因"] = p2.apply(lambda r: f"福利费 {_fmt_money(r[welfare_field])} 元，金额超过 {welfare_abs:,.0f} 元", axis=1)
        parts.append(p2)
    p3 = g[(g[other_field].abs() > 0) & (g[revenue_field].abs() > 0) & (g[other_field].abs() > g[revenue_field].abs() * other_ratio)].copy()
    if not p3.empty:
        p3["指标值"] = p3[other_field]
        p3["命中原因"] = p3.apply(lambda r: f"其他费用 {_fmt_money(r[other_field])} 元，占收入比例超过 {other_ratio:.0%}（可能记错科目）", axis=1)
        parts.append(p3)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return _annotate_hits(out, rule, "", revenue_field)


def _cost_sudden_appearance_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """成本项目突然出现：历史各月均为0，本月突增超阈值。"""
    params = rule.get("params", {}) or {}
    fields = [str(x) for x in (params.get("fields") or ["项目福利费", "项目其他费用", "第三方挂靠成本"])]
    threshold = float(params.get("threshold", 10000))
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "三级科目", "实际客户", "部门", "项目"])]
    for c in group_fields + ["月"] + fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    df = df_inc.copy()
    if "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_strip_percent_suffix)
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"]
    if df.empty:
        return pd.DataFrame()
    for c in fields:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
    cur = df[df["_m"] == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    hist = df[df["_m"] < int(target_month)].copy()
    cur_g = cur.groupby(group_fields, dropna=False)[fields].sum().reset_index()
    out_rows: list[dict[str, Any]] = []
    for f in fields:
        sub = cur_g[(cur_g[f].abs() > threshold)].copy()
        if sub.empty:
            continue
        if hist.empty:
            sub = sub.assign(__hist_zero=True)
        else:
            hist_g = hist.groupby(group_fields, dropna=False)[f].sum().reset_index()
            merged = sub.merge(hist_g, on=group_fields, how="left", suffixes=("", "_hist"))
            # 历史无该组合（_hist 为 NaN）或历史累计≈0 都算"历史为0"——注意 NaN<=eps 恒为 False，须显式判空
            merged["__hist_zero"] = merged[f + "_hist"].isna() | (merged[f + "_hist"].abs() <= 1e-9)
            sub = merged.drop(columns=[f + "_hist"], errors="ignore")
        sub = sub[sub["__hist_zero"]].copy()
        if sub.empty:
            continue
        sub = sub.drop(columns=["__hist_zero"], errors="ignore")
        sub["指标值"] = sub[f]
        sub["命中原因"] = sub.apply(lambda r: f"{f} {_fmt_money(r[f])} 元，之前几个月都没有，这个月突然出现（可能记错科目）", axis=1)
        out_rows.append(sub)
    if not out_rows:
        return pd.DataFrame()
    out = pd.concat(out_rows, ignore_index=True)
    return _annotate_hits(out, rule, "", "指标值")


def _mom_change_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """环比波动（vs 上月）：收入/成本变动>阈值，毛利率变动>阈值。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    profit_field = str(params.get("profit_field") or "项目毛利润")
    revenue_th = float(params.get("revenue_threshold", 1.0))
    cost_th = float(params.get("cost_threshold", 1.0))
    gm_th = float(params.get("gm_threshold", 0.3))
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "三级科目", "实际客户", "部门"])]
    num_fields = [revenue_field, cost_field, profit_field]
    for c in group_fields + ["月"] + num_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    df = df_inc.copy()
    if "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_strip_percent_suffix)
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"]
    if df.empty:
        return pd.DataFrame()
    for c in num_fields:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
    tgt = int(target_month)
    prev = tgt - 1
    cur = df[df["_m"] == tgt].groupby(group_fields, dropna=False)[num_fields].sum().reset_index()
    if prev < 1:
        return pd.DataFrame()
    prev_g = df[df["_m"] == prev].groupby(group_fields, dropna=False)[num_fields].sum().reset_index().rename(columns={c: c + "_prev" for c in num_fields})
    m = cur.merge(prev_g, on=group_fields, how="inner")
    if m.empty:
        return pd.DataFrame()
    m["收入变动率"] = m.apply(lambda r: (r[revenue_field] - r[revenue_field + "_prev"]) / r[revenue_field + "_prev"] if float(r[revenue_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["成本变动率"] = m.apply(lambda r: (r[cost_field] - r[cost_field + "_prev"]) / r[cost_field + "_prev"] if float(r[cost_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["毛利率"] = m.apply(lambda r: (r[profit_field] / r[revenue_field]) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    m["毛利率_prev"] = m.apply(lambda r: (r[profit_field + "_prev"] / r[revenue_field + "_prev"]) if float(r[revenue_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["毛利率变动"] = (m["毛利率"] - m["毛利率_prev"]).abs()
    parts: list[pd.DataFrame] = []
    p1 = m[(m["收入变动率"].notna()) & (m["收入变动率"].abs() > revenue_th)].copy()
    if not p1.empty:
        p1["指标值"] = p1["收入变动率"]
        p1["命中原因"] = p1.apply(lambda r: f"收入比上个月{'多' if r['收入变动率']>0 else '少'}了 {r['收入变动率']*100:.0f}%（上月 {_fmt_money(r[revenue_field+'_prev'])} 元 → 本月 {_fmt_money(r[revenue_field])} 元）", axis=1)
        parts.append(p1)
    p2 = m[(m["成本变动率"].notna()) & (m["成本变动率"].abs() > cost_th)].copy()
    if not p2.empty:
        p2["指标值"] = p2["成本变动率"]
        p2["命中原因"] = p2.apply(lambda r: f"成本比上个月{'多' if r['成本变动率']>0 else '少'}了 {r['成本变动率']*100:.0f}%（上月 {_fmt_money(r[cost_field+'_prev'])} 元 → 本月 {_fmt_money(r[cost_field])} 元）", axis=1)
        parts.append(p2)
    p3 = m[(m["毛利率"].notna()) & (m["毛利率_prev"].notna()) & (m["毛利率变动"] > gm_th)].copy()
    if not p3.empty:
        p3["指标值"] = p3["毛利率变动"]
        p3["命中原因"] = p3.apply(lambda r: f"毛利率比上个月变动 {r['毛利率变动']*100:.1f} 个百分点（上月 {f'{r['毛利率_prev']*100:.1f}%'} → 本月 {f'{r['毛利率']*100:.1f}%'}）", axis=1)
        parts.append(p3)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    return _annotate_hits(out, rule, "", revenue_field)


def _duplicate_row_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """收入成本表重复行：同一组合键下金额完全相同的行（疑似重复录入）。"""
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or [
        "主体账簿", "月", "内外", "三级科目", "账载客户", "实际客户", "部门", "项目"])]
    amount_fields = [str(x) for x in (params.get("amount_fields") or ["全额收入", "成本合计", "净额收入", "项目毛利润"])]
    min_amount = float(params.get("min_amount", 1))
    for c in key_fields + amount_fields:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    for c in amount_fields:
        cur[c] = pd.to_numeric(cur[c], errors="coerce").fillna(0.0)
    cur["_amt_sig"] = cur[amount_fields].round(2).astype(str).agg("|".join, axis=1)
    dup_mask = cur.duplicated(subset=key_fields + ["_amt_sig"], keep=False)
    dup = cur[dup_mask].copy()
    if dup.empty:
        return pd.DataFrame()
    # 只留有金额的行（避免 0 元空行误报）
    dup = dup[dup[amount_fields].abs().max(axis=1) >= min_amount]
    if dup.empty:
        return pd.DataFrame()
    dup["指标值"] = dup[amount_fields[0]]
    dup["命中原因"] = dup.apply(
        lambda r: f"收入成本表中存在金额完全相同的重复行（{amount_fields[0]}={_fmt_money(r[amount_fields[0]])}）",
        axis=1,
    )
    return _annotate_hits(dup, rule, "", amount_fields[0])


def _group_hq_unsettled_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """集团本部暂估成本未调整完：集团本部行按主体+客户+科目归总后仍有大额余额。"""
    params = rule.get("params", {}) or {}
    hq_dept = str(params.get("hq_dept_value") or "集团本部")
    amount_field = str(params.get("amount_field") or "成本合计")
    min_abs = float(params.get("min_abs", 10000))
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "三级科目", "实际客户"])]
    for c in group_fields + ["月", "部门", amount_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    cur = cur[cur["部门"].astype(str).str.strip() == hq_dept]
    if cur.empty:
        return pd.DataFrame()
    cur[amount_field] = pd.to_numeric(cur[amount_field], errors="coerce").fillna(0.0)
    if "三级科目" in cur.columns:
        cur["三级科目"] = cur["三级科目"].map(_strip_percent_suffix)
    g = cur.groupby(group_fields, dropna=False)[amount_field].sum().reset_index()
    out = g[g[amount_field].abs() >= min_abs].copy()
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out[amount_field]
    out["命中原因"] = out.apply(
        lambda r: f"集团本部还挂着 {_fmt_money(r[amount_field])} 元成本没调整到实际主体/部门（暂估可能没冲完）",
        axis=1,
    )
    return _annotate_hits(out, rule, "", amount_field)


def _similar_customer_rename_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """疑似客商改名：历史月有、本月消失的客户 + 本月新出现且名字相似的新客户。"""
    params = rule.get("params", {}) or {}
    min_common_chars = int(params.get("min_common_chars", 4))
    if "实际客户" not in df_inc.columns:
        return pd.DataFrame()
    df = df_inc.copy()
    df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
    tgt = int(target_month)
    hist = df[df["_m"] < tgt]
    cur = df[df["_m"] == tgt]
    if hist.empty or cur.empty:
        return pd.DataFrame()
    hist_cust = set(str(x).strip() for x in hist["实际客户"].dropna().unique() if str(x).strip())
    cur_cust = set(str(x).strip() for x in cur["实际客户"].dropna().unique() if str(x).strip())
    gone = hist_cust - cur_cust
    new = cur_cust - hist_cust
    if not gone or not new:
        return pd.DataFrame()

    def _chars(s: str) -> set[str]:
        return set(c for c in s if c not in "（）()公司有限集团股份 ")

    matched: list[dict[str, Any]] = []
    for nv in sorted(new):
        best, best_score = "", 0
        for gv in gone:
            score = len(_chars(nv) & _chars(gv))
            if score > best_score:
                best, best_score = gv, score
        if best_score >= min_common_chars:
            matched.append({"旧客户名": best, "实际客户": nv, "共同字符数": best_score})
    if not matched:
        return pd.DataFrame()
    out = pd.DataFrame(matched)
    out["指标值"] = out["共同字符数"]
    out["命中原因"] = out.apply(
        lambda r: f"客户「{r['旧客户名']}」之前有、本月没了，同时出现相似的「{r['实际客户']}」——可能是改名，账上直接改客商名称即可，别新增客商",
        axis=1,
    )
    return _annotate_hits(out, rule, "", "实际客户")


def _aux_wage_wrong_customer(df_aux: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """序时账工资/社保行客户挂错：工资行的实际客户与收入成本表同键下登记的实际客户不一致。

    参照系用收入成本表（而非客户调整校验映射表）：映射表只覆盖部分客户，
    收入成本表按同键（主体+业务类型+账载客户+部门）记录了当月实际客户，覆盖率高得多。
    登记表序6 场景：发的是"北京汽车制造厂（青岛）"的工资，错记成"北京汽车制造厂有限公司青岛分公司"。
    """
    params = rule.get("params", {}) or {}
    income_df = params.get("_df_income")
    try:
        if income_df is None or income_df.empty:
            return pd.DataFrame()
    except Exception:
        return pd.DataFrame()
    summary_field = str(params.get("summary_field") or "摘要")
    wage_keywords = [str(x) for x in (params.get("wage_keywords") or ["工资", "社保", "公积金", "劳务费"])]
    min_amount = float(params.get("min_amount", 1))
    for c in ["主体账簿", "月", "账载客户", "部门", "实际客户", summary_field, "三级科目", "本币"]:
        if c not in df_aux.columns:
            return pd.DataFrame()
    key_cols = ["主体账簿", "业务类型", "账载客户", "部门"]
    for c in key_cols:
        if c not in income_df.columns and c != "业务类型":
            return pd.DataFrame()
    if "三级科目" not in income_df.columns:
        return pd.DataFrame()

    # 参照：收入成本表当月 (主体,业务类型,账载客户,部门) → 实际客户集合
    inc = income_df[pd.to_numeric(income_df["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if inc.empty:
        return pd.DataFrame()
    inc["业务类型"] = inc["三级科目"].map(_strip_percent_suffix)
    ref = inc.groupby(key_cols, dropna=False)["实际客户"].agg(
        lambda s: set(str(x).strip() for x in s.dropna() if str(x).strip())
    ).to_dict()

    cur = df_aux[pd.to_numeric(df_aux["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    text = cur[summary_field].astype(str) + "|" + cur["三级科目"].astype(str)
    cur = cur[text.apply(lambda v: any(k in v for k in wage_keywords))].copy()
    if cur.empty:
        return pd.DataFrame()
    cur["业务类型"] = cur["三级科目"].map(_strip_percent_suffix)
    cur["_ref_cust"] = cur.apply(
        lambda r: ref.get((r.get("主体账簿"), r.get("业务类型"), r.get("账载客户"), r.get("部门"))) or set(), axis=1
    )
    mism = cur[cur["_ref_cust"].apply(lambda s: len(s) > 0)].copy()
    mism = mism[~mism.apply(lambda r: str(r.get("实际客户")).strip() in r["_ref_cust"], axis=1)]
    if mism.empty:
        return pd.DataFrame()
    mism["本币"] = pd.to_numeric(mism["本币"], errors="coerce").fillna(0.0)
    mism = mism[mism["本币"].abs() >= min_amount]
    if mism.empty:
        return pd.DataFrame()
    mism = mism.sort_values(by=["本币"], ascending=False, key=lambda s: s.abs())
    mism["指标值"] = mism["本币"]
    mism["命中原因"] = mism.apply(
        lambda r: (
            f"工资/社保行实际客户「{r.get('实际客户')}」与收入成本表同业务登记的「{'、'.join(sorted(r['_ref_cust']))}」不一致"
            "（疑似工资挂错客户）"
        ),
        axis=1,
    )
    keep_cols = [c for c in ["主体账簿", "月", "日", "凭证号", "摘要", "一级科目", "二级科目", "三级科目",
                             "账载客户", "实际客户", "收支项目", "部门", "项目", "本币", "是否封存",
                             "指标值", "命中原因"] if c in mism.columns]
    return _annotate_hits(mism[keep_cols], rule, "", "本币")


def _mixed_biz_type_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """同一客户同一月混着做多种业务类型（如外包+代理招聘），可能合同换签没同步。"""
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "月", "账载客户"])]
    biz_field = str(params.get("biz_type_field") or "三级科目")
    amount_field = str(params.get("amount_field") or "全额收入")
    min_gross = float(params.get("min_gross_revenue", 10000))
    for c in group_fields + [biz_field, amount_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    if "三级科目" in cur.columns:
        cur["三级科目"] = cur["三级科目"].map(_strip_percent_suffix)
    cur[amount_field] = pd.to_numeric(cur[amount_field], errors="coerce").fillna(0.0)
    g = cur.groupby(group_fields, dropna=False).agg(
        types=(biz_field, lambda s: "、".join(sorted(set(str(x) for x in s.dropna() if str(x).strip())))),
        n_types=(biz_field, "nunique"),
        gross=(amount_field, "sum"),
    ).reset_index()
    out = g[(g["n_types"] >= 2) & (g["gross"].abs() >= min_gross)].copy()
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out["n_types"]
    out["命中原因"] = out.apply(
        lambda r: f"同一个客户同一个月做了 {r['types']} 多种业务（可能合同换签了但账上没同步）",
        axis=1,
    )
    return _annotate_hits(out, rule, "", "gross")


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
            elif rtype == "gm_high_ratio":
                inc_gm.append(_with_rule_name(_gm_high_ratio_income(df_income, target_month, rule), rule))
            elif rtype == "rev_cost_inversion":
                inc_dim.append(_with_rule_name(_rev_cost_inversion_income(df_income, target_month, rule), rule))
            elif rtype == "headcount_rev_mismatch":
                inc_dim.append(_with_rule_name(_headcount_rev_mismatch_income(df_income, target_month, rule), rule))
            elif rtype == "social_headcount_mismatch":
                inc_dim.append(_with_rule_name(_social_headcount_mismatch_income(df_income, target_month, rule), rule))
            elif rtype == "cost_ratio_high":
                inc_dim.append(_with_rule_name(_cost_ratio_high_income(df_income, target_month, rule), rule))
            elif rtype == "expense_ratio":
                inc_dim.append(_with_rule_name(_expense_ratio_income(df_income, target_month, rule), rule))
            elif rtype == "cost_sudden_appearance":
                inc_dim.append(_with_rule_name(_cost_sudden_appearance_income(df_income, target_month, rule), rule))
            elif rtype == "mom_change":
                inc_dim.append(_with_rule_name(_mom_change_income(df_income, target_month, rule), rule))
            elif rtype == "duplicate_row":
                inc_dim.append(_with_rule_name(_duplicate_row_income(df_income, target_month, rule), rule))
            elif rtype == "group_hq_unsettled":
                inc_dim.append(_with_rule_name(_group_hq_unsettled_income(df_income, target_month, rule), rule))
            elif rtype == "similar_customer_rename":
                inc_dim.append(_with_rule_name(_similar_customer_rename_income(df_income, target_month, rule), rule))
            elif rtype == "mixed_biz_type":
                inc_dim.append(_with_rule_name(_mixed_biz_type_income(df_income, target_month, rule), rule))
            elif rtype == "aux_wage_wrong_customer":
                sub_rule = {**rule, "params": {**(rule.get("params") or {}), "_df_income": df_income}}
                _aw = _aux_wage_wrong_customer(df_aux, target_month, sub_rule)
                if _aw is not None and not _aw.empty:
                    inc_dim.append(_with_rule_name(_aw, rule))

    aux_parts = [x for x in aux_hits if x is not None and not x.empty]
    suspect_parts = [x for x in aux_suspects if x is not None and not x.empty]
    dim_parts = [x for x in inc_dim if x is not None and not x.empty]
    gm_parts = [x for x in inc_gm if x is not None and not x.empty]

    aux_rule_violations = pd.concat(aux_parts, ignore_index=True) if aux_parts else pd.DataFrame()
    aux_suspect_wrong = pd.concat(suspect_parts, ignore_index=True) if suspect_parts else pd.DataFrame()
    income_dim = pd.concat(dim_parts, ignore_index=True) if dim_parts else pd.DataFrame()
    income_gm = pd.concat(gm_parts, ignore_index=True) if gm_parts else pd.DataFrame()

    return aux_rule_violations, aux_suspect_wrong, income_dim, income_gm

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

from .config import RuleConfig
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
from .rule_precheck import RuleSkip, check_rule_preconditions


@dataclass(frozen=True)
class AuditTables:
    overview: pd.DataFrame
    aux_rule_violations: pd.DataFrame
    aux_suspect_wrong_account: pd.DataFrame
    income_dim_anomalies: pd.DataFrame
    income_gm_anomalies: pd.DataFrame
    ai_review: Optional[pd.DataFrame]



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







def _rev_cost_zero_mismatch_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """
    检查：同一主键下，全额收入=0但成本≠0，或 成本=0但全额收入≠0。
    """
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or ["主体账簿", "月", "三级科目", "实际客户", "部门", "项目"])]
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    eps = float(params.get("eps", 1e-9))
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    biz_type_keywords = [str(x) for x in (params.get("biz_type_keywords") or [])]
    strip_percent_suffix = bool(params.get("biz_type_strip_percent_suffix", False))
    min_amount = float(params.get("min_amount", 0))

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
    if min_amount > 0:
        # 规模过滤：非零一侧金额太小的组合没有审计价值（多为结算时点错位的小尾差）。
        out = out[out[[revenue_field, cost_field]].abs().max(axis=1) >= min_amount]
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
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    biz_type_thresholds = [
        (ov.get("keywords") or [], float(ov.get("threshold", threshold)))
        for ov in (params.get("biz_type_thresholds") or []) if isinstance(ov, dict)
    ]

    def _threshold_for(biz: Any) -> float:
        for kws, th in biz_type_thresholds:
            if kws and _match_contains_any(biz, [str(k) for k in kws]):
                return th
        return threshold

    g = _base_month_agg(df_inc, target_month, params, [revenue_field, profit_field])
    if g.empty:
        return pd.DataFrame()
    g = g[g[revenue_field].abs() >= min_revenue]
    if g.empty:
        return pd.DataFrame()
    g["毛利率"] = g.apply(lambda r: (r[profit_field] / r[revenue_field]) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    out = g[g["毛利率"].notna()].copy()
    # 按业务类型取对应阈值（代理招聘/猎聘类天然高毛利，需更高阈值）。
    out = out[out.apply(lambda r: r["毛利率"] > _threshold_for(r.get(biz_type_field)), axis=1)]
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out["毛利率"]
    return _annotate_hits(out, rule, "毛利偏高，可能漏记了成本", revenue_field)


def _rev_cost_inversion_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """收入>0 且 成本>0 且 收入<成本（入不敷出）。"""
    params = rule.get("params", {}) or {}
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    min_cost_abs = float(params.get("min_cost_abs", 0))
    min_diff = float(params.get("min_diff", 0))
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, cost_field])
    if g.empty:
        return pd.DataFrame()
    if min_cost_abs > 0:
        # 规模过滤：成本太小的倒挂多为结算时点错位，不值得人工核对。
        g = g[g[cost_field].abs() >= min_cost_abs]
        if g.empty:
            return pd.DataFrame()
    g["倒挂差额"] = g[cost_field] - g[revenue_field]
    g["亏损率"] = g.apply(lambda r: (float(r["倒挂差额"]) / float(r[revenue_field])) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    out = g[(g[revenue_field] > 0) & (g[cost_field] > 0) & (g[revenue_field] < g[cost_field])].copy()
    if out.empty:
        return pd.DataFrame()
    if min_diff > 0:
        # 差额下限：亏得很少的倒挂只是毛利偏低，不是需要修正的错误。
        out = out[out["倒挂差额"] >= min_diff]
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
    cost_field = str(params.get("cost_field") or "成本合计")
    min_cost_abs = float(params.get("min_cost_abs", 0))
    biz_type_field = str(params.get("biz_type_field") or "三级科目")
    p2_biz_keywords = [str(x) for x in (params.get("p2_biz_type_keywords") or [])]
    num_fields = [revenue_field, headcount_field]
    if min_cost_abs > 0:
        num_fields.append(cost_field)
    g = _base_month_agg(df_inc, target_month, params, num_fields)
    if g.empty:
        return pd.DataFrame()
    eps = 1e-9
    m_hc_no_rev = (g[headcount_field] > eps) & (g[revenue_field].abs() <= eps)
    if min_cost_abs > 0 and cost_field in g.columns:
        # 规模过滤：没收入但有结算人的组合，只有成本达到一定规模才值得核对（漏结算影响有限）。
        m_hc_no_rev &= g[cost_field].abs() >= min_cost_abs
    m_rev_no_hc = (g[headcount_field].abs() <= eps) & (g[revenue_field].abs() > income_min)
    p2 = g[m_rev_no_hc].copy()
    if not p2.empty and p2_biz_keywords and biz_type_field in p2.columns:
        # 猎聘/代理招聘类按单收费，本无按月结算人次，"有收入没人次"属正常，只检查按人头结算的类型。
        p2 = p2[p2[biz_type_field].apply(lambda v: _match_contains_any(v, p2_biz_keywords))]
    parts: list[pd.DataFrame] = []
    p1 = g[m_hc_no_rev].copy()
    if not p1.empty:
        p1["指标值"] = p1[headcount_field]
        p1["命中原因"] = p1.apply(lambda r: f"结算人数是 {int(r[headcount_field])} 人，但没有收入", axis=1)
        parts.append(p1)
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
    min_revenue = float(params.get("min_revenue", 0))
    num_fields = [revenue_field] + [str(r.get("field")) for r in ratios if isinstance(r, dict) and r.get("field")]
    g = _base_month_agg(df_inc, target_month, params, num_fields)
    if g.empty:
        return pd.DataFrame()
    if min_revenue > 0:
        # 分母保护：收入规模太小的组合，占比指标全是噪声。
        g = g[g[revenue_field].abs() >= min_revenue]
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
    min_revenue = float(params.get("min_revenue", 0))
    g = _base_month_agg(df_inc, target_month, params, [revenue_field, welfare_field, other_field])
    if g.empty:
        return pd.DataFrame()
    if min_revenue > 0:
        # 分母保护：只对收入规模够大的组合比较占比，避免小组合噪声。
        g = g[g[revenue_field].abs() >= min_revenue]
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
    min_abs = float(params.get("min_abs", 0))
    min_base = float(params.get("min_base", 0))
    gm_prev_abs_max = float(params.get("gm_prev_abs_max", 1.0))
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
    if min_abs > 0:
        # 规模过滤：组合太小（本月 max(收入,成本) 低于阈值）的环比波动多为正常业务抖动。
        m = m[m[[revenue_field, cost_field]].abs().max(axis=1) >= min_abs]
        if m.empty:
            return pd.DataFrame()
    m["收入变动率"] = m.apply(lambda r: (r[revenue_field] - r[revenue_field + "_prev"]) / r[revenue_field + "_prev"] if float(r[revenue_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["成本变动率"] = m.apply(lambda r: (r[cost_field] - r[cost_field + "_prev"]) / r[cost_field + "_prev"] if float(r[cost_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["毛利率"] = m.apply(lambda r: (r[profit_field] / r[revenue_field]) if float(r[revenue_field]) != 0 else float("nan"), axis=1)
    m["毛利率_prev"] = m.apply(lambda r: (r[profit_field + "_prev"] / r[revenue_field + "_prev"]) if float(r[revenue_field + "_prev"]) != 0 else float("nan"), axis=1)
    m["毛利率变动"] = (m["毛利率"] - m["毛利率_prev"]).abs()
    parts: list[pd.DataFrame] = []
    p1_mask = (m["收入变动率"].notna()) & (m["收入变动率"].abs() > revenue_th)
    if min_base > 0:
        # 基数保护：上月金额太小时（新启动业务），环比比率失真，不作为波动处理。
        p1_mask &= m[revenue_field + "_prev"].abs() >= min_base
    p1 = m[p1_mask].copy()
    if not p1.empty:
        p1["指标值"] = p1["收入变动率"]
        p1["命中原因"] = p1.apply(lambda r: f"收入比上个月{'多' if r['收入变动率']>0 else '少'}了 {r['收入变动率']*100:.0f}%（上月 {_fmt_money(r[revenue_field+'_prev'])} 元 → 本月 {_fmt_money(r[revenue_field])} 元）", axis=1)
        parts.append(p1)
    p2_mask = (m["成本变动率"].notna()) & (m["成本变动率"].abs() > cost_th)
    if min_base > 0:
        p2_mask &= m[cost_field + "_prev"].abs() >= min_base
    p2 = m[p2_mask].copy()
    if not p2.empty:
        p2["指标值"] = p2["成本变动率"]
        p2["命中原因"] = p2.apply(lambda r: f"成本比上个月{'多' if r['成本变动率']>0 else '少'}了 {r['成本变动率']*100:.0f}%（上月 {_fmt_money(r[cost_field+'_prev'])} 元 → 本月 {_fmt_money(r[cost_field])} 元）", axis=1)
        parts.append(p2)
    p3_mask = (m["毛利率"].notna()) & (m["毛利率_prev"].notna()) & (m["毛利率变动"] > gm_th)
    if gm_prev_abs_max > 0:
        # 基线守卫：上月毛利率本身极端（如 -1119%）时变动没有意义，上月已异常的组合由倒挂/负毛利规则负责。
        p3_mask &= m["毛利率_prev"].abs() <= gm_prev_abs_max
    p3 = m[p3_mask].copy()
    if not p3.empty:
        p3["指标值"] = p3["毛利率变动"]
        p3["命中原因"] = p3.apply(lambda r: f"毛利率比上个月变动 {r['毛利率变动']*100:.1f} 个百分点（上月 {f'{r['毛利率_prev']*100:.1f}%'} → 本月 {f'{r['毛利率']*100:.1f}%'}）", axis=1)
        parts.append(p3)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True)
    # 同一组合的收入/成本/毛利率多条命中合并为一条（原因拼接），避免同一组合重复占报告行。
    out["_gkey"] = out.groupby(group_fields, dropna=False).ngroup()
    merged_reasons = out.groupby("_gkey")["命中原因"].apply(lambda s: "；".join(s)).to_dict()
    out = out.drop_duplicates(subset=group_fields, keep="first")
    out["命中原因"] = out["_gkey"].map(merged_reasons)
    out = out.drop(columns=["_gkey"])
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
    min_amount = float(params.get("min_amount", 0))
    if "实际客户" not in df_inc.columns:
        return pd.DataFrame()
    df = df_inc.copy()
    df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
    tgt = int(target_month)
    hist = df[df["_m"] < tgt]
    cur = df[df["_m"] == tgt]
    if hist.empty or cur.empty:
        return pd.DataFrame()
    # 金额门槛：只关注规模够大的客户改名（小客户改名没有审计价值）。
    amt_by_cust: dict[str, float] = {}
    if min_amount > 0 and "全额收入" in df.columns:
        amt = pd.to_numeric(df["全额收入"], errors="coerce").fillna(0.0).abs()
        amt_by_cust = amt.groupby([df["_m"], df["实际客户"].astype(str).str.strip()]).sum().to_dict()
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
            if min_amount > 0 and amt_by_cust:
                new_amt = amt_by_cust.get((tgt, nv), 0.0)
                old_amt = amt_by_cust.get((tgt - 1, best), 0.0)
                if max(new_amt, old_amt) < min_amount:
                    continue
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


def _rev_cost_biz_type_mismatch_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """收入和成本记的业务类型对不上：同一（主体+实际客户）下，有收入行的业务类型集合
    与有成本行的业务类型集合不同（如收入记劳务派遣、成本记外包）。登记表 2026/8 山东理想汽车电池。"""
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "实际客户"])]
    biz_field = str(params.get("biz_type_field") or "三级科目")
    revenue_field = str(params.get("revenue_field") or "全额收入")
    cost_field = str(params.get("cost_field") or "成本合计")
    min_amount = float(params.get("min_amount", 1000))
    min_combo_amount = float(params.get("min_combo_amount", 0))
    for c in group_fields + [biz_field, revenue_field, cost_field]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    if cur.empty:
        return pd.DataFrame()
    cur[biz_field] = cur[biz_field].map(_strip_percent_suffix)
    cur[revenue_field] = pd.to_numeric(cur[revenue_field], errors="coerce").fillna(0.0)
    cur[cost_field] = pd.to_numeric(cur[cost_field], errors="coerce").fillna(0.0)
    if min_combo_amount > 0:
        # 组合级规模过滤：客户总业务量太小（收入+成本低于阈值）时类型不一致没有审计价值。
        combo_tot = cur.groupby(group_fields, dropna=False)[[revenue_field, cost_field]].sum()
        combo_tot["__total"] = combo_tot[revenue_field].abs() + combo_tot[cost_field].abs()
        keep = combo_tot[combo_tot["__total"] >= min_combo_amount].reset_index()[group_fields]
        cur = cur.merge(keep, on=group_fields, how="inner")
        if cur.empty:
            return pd.DataFrame()
    cur = cur[(cur[revenue_field].abs() > min_amount) | (cur[cost_field].abs() > min_amount)]
    if cur.empty:
        return pd.DataFrame()
    g = cur.groupby(group_fields, dropna=False).agg(
        rev_types=(biz_field, lambda s: sorted({str(x) for x in s[cur.loc[s.index, revenue_field].abs() > min_amount]})),
        cost_types=(biz_field, lambda s: sorted({str(x) for x in s[cur.loc[s.index, cost_field].abs() > min_amount]})),
    ).reset_index()
    out = g[(g["rev_types"].map(len) > 0) & (g["cost_types"].map(len) > 0) & (g["rev_types"] != g["cost_types"])].copy()
    if out.empty:
        return pd.DataFrame()
    out["指标值"] = out.apply(lambda r: len(set(r["rev_types"]) ^ set(r["cost_types"])), axis=1)
    out["命中原因"] = out.apply(
        lambda r: (
            f"收入的业务类型是 {'、'.join(r['rev_types'])}，成本的却是 {'、'.join(r['cost_types'])}"
            "（两边业务类型对不上，可能有一边记错了）"
        ),
        axis=1,
    )
    return _annotate_hits(out, rule, "", "指标值")


def _same_amount_adjacent_months_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """相邻月份金额一模一样：同一组合键在 (目标月, 目标月-1) 的金额完全相等，
    可能重复暂估/重复确认。登记表 2026/8 苏州旭创（6/7 月同金额 239,972.08）。"""
    params = rule.get("params", {}) or {}
    key_fields = [str(x) for x in (params.get("key_fields") or ["主体账簿", "三级科目", "账载客户", "实际客户", "部门", "项目"])]
    amount_fields = [str(x) for x in (params.get("amount_fields") or ["全额收入", "成本合计"])]
    min_amount = float(params.get("min_amount", 10000))
    skip_if_identical_months = int(params.get("skip_if_identical_months", 0))
    for c in key_fields + amount_fields + ["月"]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    df = df_inc.copy()
    if "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_strip_percent_suffix)
    for c in amount_fields:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
    tgt = int(target_month)
    prev = tgt - 1
    if prev < 1:
        return pd.DataFrame()
    cur = df[df["_m"] == tgt].groupby(key_fields, dropna=False)[amount_fields].sum().reset_index()
    prev_g = df[df["_m"] == prev].groupby(key_fields, dropna=False)[amount_fields].sum().reset_index()
    if cur.empty or prev_g.empty:
        return pd.DataFrame()
    m = cur.merge(prev_g, on=key_fields, how="inner", suffixes=("", "_prev"))
    if m.empty:
        return pd.DataFrame()
    if skip_if_identical_months > 0:
        # 连续 skip 个月金额都一样 → 固定费用（如政府客户固定月费），不是重复暂估。
        first = tgt - skip_if_identical_months
        if first >= 1:
            earlier = df[df["_m"] == first].groupby(key_fields, dropna=False)[amount_fields].sum().reset_index()
            if not earlier.empty:
                m = m.merge(earlier, on=key_fields, how="left", suffixes=("", f"_p{skip_if_identical_months}"))
    parts: list[pd.DataFrame] = []
    for a in amount_fields:
        p = m[(m[a].abs() >= min_amount) & ((m[a] - m[a + "_prev"]).abs() < 0.01)].copy()
        if not p.empty and skip_if_identical_months > 0:
            earlier_col = f"{a}_p{skip_if_identical_months}"
            if earlier_col in p.columns:
                # 早于观察窗的月份金额也相同（连续相同≥skip个月）→ 视为固定费用，跳过。
                p = p[~((p[a + "_prev"] - p[earlier_col]).abs() < 0.01)]
        if not p.empty:
            p["指标值"] = p[a]
            p["命中原因"] = p.apply(
                lambda r: f"{a} {_fmt_money(r[a])} 元，{tgt-1} 月和 {tgt} 月一分不差（可能重复暂估/重复确认，也可能真没变化）",
                axis=1,
            )
            parts.append(p)
    if not parts:
        return pd.DataFrame()
    out = pd.concat(parts, ignore_index=True).drop_duplicates(subset=key_fields + ["命中原因"])
    return _annotate_hits(out, rule, "", "指标值")


def _small_amount_wrong_dept_income(df_inc: pd.DataFrame, target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """小额成本挂错部门：同一客户挂 2+ 部门，某部门金额极小而客户总额大
    （登记表 2026/8 云强 1,100 元挂在淄博项目部，主体在潍坊项目部）。"""
    params = rule.get("params", {}) or {}
    group_fields = [str(x) for x in (params.get("group_fields") or ["主体账簿", "实际客户", "部门"])]
    small_threshold = float(params.get("small_amount", 5000))
    total_min = float(params.get("customer_total_min", 50000))
    for c in group_fields + ["月"]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    cur = df_inc[pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
    cur = cur[~cur["部门"].astype(str).str.strip().eq("集团本部")] if "部门" in cur.columns else cur
    if cur.empty:
        return pd.DataFrame()
    g = cur.groupby(group_fields, dropna=False)[["成本合计", "全额收入"]].sum().reset_index()
    for c in ("成本合计", "全额收入"):
        g[c] = pd.to_numeric(g[c], errors="coerce").fillna(0.0)
    out_rows: list[dict[str, Any]] = []
    for (b, c), grp in g.groupby(["主体账簿", "实际客户"], dropna=False):
        if len(grp) < 2:
            continue
        total_cost = grp["成本合计"].abs().sum()
        if total_cost < total_min:
            continue
        for _, r in grp.iterrows():
            if abs(r["成本合计"]) < small_threshold and abs(r["全额收入"]) < small_threshold:
                out_rows.append({
                    "主体账簿": b, "实际客户": c, "部门": r["部门"],
                    "成本合计": r["成本合计"], "全额收入": r["全额收入"],
                    "客户总成本": total_cost,
                    "指标值": r["成本合计"],
                })
                break
    if not out_rows:
        return pd.DataFrame()
    out = pd.DataFrame(out_rows)
    out["命中原因"] = out.apply(
        lambda r: (
            f"客户总成本 {r['客户总成本']:,.0f} 元，但部门「{str(r['部门'])[-16:]}」只有成本 {r['成本合计']:,.0f} 元 / 收入 {r['全额收入']:,.0f} 元"
            "（这么小的零头挂在这个部门，可能挂错了）"
        ),
        axis=1,
    )
    return _annotate_hits(out, rule, "", "成本合计")


def _entity_switch_mapping_drift_income(df_inc: pd.DataFrame, df_map: Optional[pd.DataFrame], target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """换主体后映射没跟着改：同一账载客户在映射表出现 ≥2 个主体账簿，
    且历史实际客户 ≠ 当月实际客户（登记表 2026/8 上海中汐：3月烟台智择→海信冰箱，
    8月青岛众腾→海信家电产业园。现有按同键对比的映射检查抓不到这种主体切换）。"""
    if df_map is None or df_map.empty:
        return pd.DataFrame()
    params = rule.get("params", {}) or {}
    book_field = "主体账簿"
    for c in [book_field, "账载客户", "实际客户", "月"]:
        if c not in df_map.columns:
            return pd.DataFrame()
    map_df = df_map.copy()
    map_df["月"] = pd.to_numeric(map_df["月"], errors="coerce")
    tgt = int(target_month)
    # 每个账载客户的主体集合 + 历史实际客户 vs 当月实际客户
    entity_cnt = map_df.groupby("账载客户")[book_field].nunique()
    multi = entity_cnt[entity_cnt >= int(params.get("min_entities", 2))]
    if multi.empty:
        return pd.DataFrame()
    rows: list[dict[str, Any]] = []
    for cust in multi.index:
        sub = map_df[map_df["账载客户"] == cust]
        books = sorted(str(x) for x in sub[book_field].dropna().unique())
        hist = sub[sub["月"].notna() & (sub["月"].astype("Int64") != tgt)]
        cur_m = sub[sub["月"].astype("Int64") == tgt] if sub["月"].notna().any() else sub.iloc[:0]
        hist_custs = sorted({str(x).strip() for x in hist["实际客户"].dropna()})
        cur_custs = sorted({str(x).strip() for x in cur_m["实际客户"].dropna()}) if not cur_m.empty else []
        # 当月收入成本表的实际客户
        inc_custs: set[str] = set()
        if df_inc is not None and not df_inc.empty and "实际客户" in df_inc.columns and "账载客户" in df_inc.columns:
            cur_inc = df_inc[
                (pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == tgt)
                & (df_inc["账载客户"].astype(str).str.strip() == str(cust).strip())
            ]
            inc_custs = {str(x).strip() for x in cur_inc["实际客户"].dropna()}
        changed = (hist_custs and cur_custs and set(hist_custs) != set(cur_custs)) or (
            hist_custs and inc_custs and not (set(hist_custs) & inc_custs)
        )
        if not changed:
            continue
        rows.append({
            "账载客户": cust,
            "涉及主体": "、".join(books),
            "历史实际客户": "、".join(hist_custs) if hist_custs else "",
            "当月实际客户": "、".join(cur_custs) if cur_custs else ("、".join(sorted(inc_custs)) if inc_custs else ""),
            "指标值": len(books),
        })
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out["命中原因"] = out.apply(
        lambda r: (
            f"客户「{r['账载客户']}」在 {r['涉及主体']} 多个主体下都有映射，实际客户从「{r['历史实际客户']}」换成了「{r['当月实际客户']}」"
            "——客户换主体了，请确认映射切换依据"
        ),
        axis=1,
    )
    return _annotate_hits(out, rule, "", "指标值")


def _rebate_external_cost_reconcile_income(df_inc: pd.DataFrame, df_aux: Optional[pd.DataFrame], target_month: int, rule: dict[str, Any]) -> pd.DataFrame:
    """返费/挂靠与外部成本双向核对：收入成本表的返费/挂靠成本 与 序时账"主营业务成本→外部成本"
    的实际记账不一致（差额>阈值）。返费在序时账以"服务费"等摘要计入外部劳务成本（业务确认），
    因此按客户+业务类型汇总金额比对，不按摘要关键词。排除内部转包（客户为自家主体）。"""
    if df_aux is None or df_aux.empty:
        return pd.DataFrame()
    params = rule.get("params", {}) or {}
    cost_level1 = str(params.get("cost_level1") or "主营业务成本")
    cost_level2 = str(params.get("cost_level2") or "外部成本")
    diff_threshold = float(params.get("diff_threshold", 1000))
    min_amount = float(params.get("min_amount", 1000))
    own_pattern = str(params.get("own_entity_pattern") or "众腾|卓仕达|鲸才|智择|才航")
    for c in ["主体账簿", "账载客户", "实际客户", "部门", "三级科目"]:
        if c not in df_inc.columns:
            return pd.DataFrame()
    for c in ["主体账簿", "账载客户", "一级科目", "二级科目", "本币"]:
        if c not in df_aux.columns:
            return pd.DataFrame()
    ext = df_aux[
        (pd.to_numeric(df_aux["月"], errors="coerce").fillna(-1).astype(int) == int(target_month))
        & (df_aux["一级科目"].astype(str).str.strip() == cost_level1)
        & (df_aux["二级科目"].astype(str).str.strip() == cost_level2)
    ].copy()
    if ext.empty:
        return pd.DataFrame()
    # aux 侧同样排除集团本部（与收入成本表口径一致——集团本部行是内部对冲，如逾期考核的双边挂账）
    ext = ext[~ext["部门"].astype(str).str.strip().eq("集团本部")] if "部门" in ext.columns else ext
    ext["本币"] = pd.to_numeric(ext["本币"], errors="coerce").fillna(0.0)
    ext["三级科目"] = ext["三级科目"].map(_strip_percent_suffix)
    g_aux = ext.groupby(["主体账簿", "三级科目", "账载客户"], dropna=False)["本币"].sum().reset_index(name="aux_cost")

    inc = df_inc[
        (pd.to_numeric(df_inc["月"], errors="coerce").fillna(-1).astype(int) == int(target_month))
    ].copy()
    if inc.empty:
        return pd.DataFrame()
    inc = inc[~inc["部门"].astype(str).str.strip().eq("集团本部")] if "部门" in inc.columns else inc
    inc["三级科目"] = inc["三级科目"].map(_strip_percent_suffix)
    for c in ("项目返费", "第三方挂靠成本", "成本合计"):
        if c in inc.columns:
            inc[c] = pd.to_numeric(inc[c], errors="coerce").fillna(0.0)
    # 全成本键级比对（outer join，双向覆盖：收入成本表有账上无 / 账上有收入成本表无）
    g_inc = inc.groupby(["主体账簿", "三级科目", "账载客户"], dropna=False)["成本合计"].sum().reset_index(name="inc_cost")
    m = g_inc.merge(g_aux, on=["主体账簿", "三级科目", "账载客户"], how="outer").fillna({"aux_cost": 0.0, "inc_cost": 0.0})
    # 排除内部转包（客户是自家主体）
    m = m[~m["账载客户"].astype(str).str.contains(own_pattern, regex=True, na=False)]
    if m.empty:
        return pd.DataFrame()
    m["差额"] = m["aux_cost"] - m["inc_cost"]
    # 只报有金额意义的组合（任一侧 ≥ min_amount）
    m = m[(m["aux_cost"].abs() >= min_amount) | (m["inc_cost"].abs() >= min_amount)]
    out = m[m["差额"].abs() > diff_threshold].copy()
    if out.empty:
        return pd.DataFrame()
    out = out.sort_values(by=["差额"], key=lambda s: s.abs(), ascending=False)
    out["指标值"] = out["差额"]
    out["命中原因"] = out.apply(
        lambda r: (
            f"客户「{r['账载客户']}」（{r['三级科目']}）收入成本表成本 {r['inc_cost']:,.2f} 元，"
            f"序时账外部成本 {r['aux_cost']:,.2f} 元，差 {r['差额']:,.2f} 元"
            "（两边有一边没记全或金额错）"
        ),
        axis=1,
    )
    return _annotate_hits(out, rule, "", "差额")


def run_checks(
    rules: RuleConfig,
    df_aux: pd.DataFrame,
    df_income: pd.DataFrame,
    df_mapping: Optional[pd.DataFrame],
    target_month: int,
    target_month_aux: Optional[int] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, list["RuleSkip"]]:
    """执行全部规则。

    target_month：收入成本规则的目标月；target_month_aux：辅助帐规则的目标月
    （None 时用 target_month——两表错月时各自跑各自的目标月，见 F1 修复）。
    """
    if target_month_aux is None:
        target_month_aux = target_month
    aux_hits: list[pd.DataFrame] = []
    aux_suspects: list[pd.DataFrame] = []
    inc_dim: list[pd.DataFrame] = []
    inc_gm: list[pd.DataFrame] = []
    skipped_rules: list[RuleSkip] = []

    for rule in rules.checks:
        rtype = str(rule.get("type", ""))
        scope = str(rule.get("scope", ""))
        # 执行前预检：配置错误（缺列）→ 跳过并大声报告，而不是静默返回空结果
        _skip = check_rule_preconditions(rule, df_income, df_aux, df_mapping)
        if _skip is not None:
            skipped_rules.append(_skip)
            continue
        if scope == "aux_ledger":
            # F1 修复：辅助帐规则用序时账自己的目标月
            if rtype == "headcount_data_check":
                _hc = _headcount_data_check_aux(df_aux, target_month_aux, rule)
                if _hc is not None and not _hc.empty:
                    _hc = _with_rule_name(_hc, rule)
                    err = _hc[_hc["严重度"] == "错误"]
                    sus = _hc[_hc["严重度"] != "错误"]
                    if not err.empty:
                        aux_hits.append(err)
                    if not sus.empty:
                        aux_suspects.append(sus)
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
            elif rtype == "rev_cost_biz_type_mismatch":
                inc_dim.append(_with_rule_name(_rev_cost_biz_type_mismatch_income(df_income, target_month, rule), rule))
            elif rtype == "same_amount_adjacent_months":
                inc_dim.append(_with_rule_name(_same_amount_adjacent_months_income(df_income, target_month, rule), rule))
            elif rtype == "small_amount_wrong_dept":
                inc_dim.append(_with_rule_name(_small_amount_wrong_dept_income(df_income, target_month, rule), rule))
            elif rtype == "entity_switch_mapping_drift":
                inc_dim.append(_with_rule_name(_entity_switch_mapping_drift_income(df_income, df_mapping, target_month, rule), rule))
            elif rtype == "rebate_external_cost_reconcile":
                sub_rule = {**rule, "params": {**(rule.get("params") or {})}}
                _rc = _rebate_external_cost_reconcile_income(df_income, df_aux, target_month, sub_rule)
                if _rc is not None and not _rc.empty:
                    inc_dim.append(_with_rule_name(_rc, rule))
            elif rtype == "aux_wage_wrong_customer":
                sub_rule = {**rule, "params": {**(rule.get("params") or {}), "_df_income": df_income}}
                _aw = _aux_wage_wrong_customer(df_aux, target_month_aux, sub_rule)
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

    return aux_rule_violations, aux_suspect_wrong, income_dim, income_gm, skipped_rules

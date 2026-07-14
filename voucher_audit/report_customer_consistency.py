from __future__ import annotations

from typing import Any, Optional

import pandas as pd


def _severity_rank(value: str) -> int:
    if str(value) == "错误":
        return 0
    if str(value) == "需确认":
        return 1
    return 2

def build_customer_consistency_sheet(
    df_income: Optional[pd.DataFrame],
    income_dim_anomalies: Optional[pd.DataFrame],
    df_mapping: Optional[pd.DataFrame],
    target_month: Optional[int],
) -> pd.DataFrame:
    if df_income is None or df_income.empty:
        return pd.DataFrame()
    if income_dim_anomalies is None or income_dim_anomalies.empty:
        return pd.DataFrame()
    if "规则ID" not in income_dim_anomalies.columns:
        return pd.DataFrame()

    hits = income_dim_anomalies[income_dim_anomalies["规则ID"].astype(str) == "INC_CUSTOMER_CONSISTENCY"].copy()
    if hits.empty:
        return pd.DataFrame()

    original_cols = [
        c
        for c in df_income.columns
        if not str(c).startswith("_")
        and not str(c).startswith("Unnamed:")
        and "凭证审核" not in str(c)
    ]
    tail_cols = ["标注", "命中原因", "严重度"]

    def _norm(v: Any) -> str:
        if v is None:
            return ""
        if isinstance(v, float) and pd.isna(v):
            return ""
        s = str(v).strip()
        return "" if s.lower() == "nan" else s

    def _match_rows(month: Optional[int], filters: dict[str, Any]) -> pd.DataFrame:
        df = df_income
        if month is not None and "月" in df.columns:
            df = df[pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int) == int(month)]
        for k, v in filters.items():
            if k not in df.columns:
                continue
            df = df[df[k].astype(str) == str(v)]
        return df

    def _add_rows(df_rows: pd.DataFrame, *, mark: str, reason: str, severity: str) -> None:
        if df_rows is None or df_rows.empty:
            return
        for _, r in df_rows.iterrows():
            rec = {c: r.get(c, "") for c in original_cols}
            rec["标注"] = mark
            rec["命中原因"] = reason
            rec["严重度"] = severity
            out_rows.append(rec)

    def _drop_zero_revenue_rows(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if "全额收入" not in df.columns or "净额收入" not in df.columns:
            return df
        gross = pd.to_numeric(df["全额收入"], errors="coerce")
        net = pd.to_numeric(df["净额收入"], errors="coerce")
        keep = ~((gross.isna() | (gross == 0)) & (net.isna() | (net == 0)))
        return df[keep].copy()

    out_rows: list[dict[str, Any]] = []

    # 先按严重度简单排序，保证“错误”在上面
    if "严重度" in hits.columns:
        hits = hits.assign(__sev=hits["严重度"].map(_severity_rank)).sort_values(by=["__sev"]).drop(columns=["__sev"])

    for _, h in hits.iterrows():
        category = _norm(h.get("问题分类"))
        severity = _norm(h.get("严重度"))
        reason = _norm(h.get("命中原因"))

        # 1) 映射表不一致：问题行=当月收入成本行（不输出建议实际客户，不生成参考行）
        if category == "实际客户与映射表不一致":
            month = int(target_month) if target_month is not None else int(pd.to_numeric(h.get("月"), errors="coerce")) if pd.notna(pd.to_numeric(h.get("月"), errors="coerce")) else None
            biz = _norm(h.get("业务类型")) or _norm(h.get("三级科目"))
            filters = {
                "主体账簿": _norm(h.get("主体账簿")),
                "账载客户": _norm(h.get("账载客户")),
                "部门": _norm(h.get("部门")),
                "项目": _norm(h.get("项目")),
                "实际客户": _norm(h.get("实际客户")),
            }
            if biz:
                filters["三级科目"] = biz
            problem_rows = _match_rows(month, {k: v for k, v in filters.items() if v})
            problem_rows = _drop_zero_revenue_rows(problem_rows)
            if problem_rows.empty:
                rec = {c: h.get(c, "") for c in original_cols}
                if biz and "三级科目" in rec:
                    rec["三级科目"] = biz
                rec["标注"] = severity or "错误"
                rec["命中原因"] = reason
                rec["严重度"] = severity
                out_rows.append(rec)
                problem_rows = pd.DataFrame([rec])[original_cols]
            else:
                _add_rows(problem_rows, mark=severity or "错误", reason=reason, severity=severity)
            continue

        # 2) 组合漂移：问题行=本期主映射对应的全部明细；参考行=上期主映射对应的全部明细
        # 注意：不能用“列是否存在”来判断（concat 后所有行都会带这些列名），必须用问题分类来识别命中类型。
        if category == "客户归属组合漂移":
            cur_m = pd.to_numeric(h.get("本期月份"), errors="coerce")
            prev_m = pd.to_numeric(h.get("前期月份"), errors="coerce")
            cur_month = int(cur_m) if pd.notna(cur_m) else (int(target_month) if target_month is not None else None)
            prev_month = int(prev_m) if pd.notna(prev_m) else None

            key_filters = {}
            for k in ["主体账簿", "账载客户"]:
                v = _norm(h.get(k))
                if v:
                    key_filters[k] = v

            cur_df = _match_rows(cur_month, key_filters)
            prev_df = _match_rows(prev_month, key_filters) if prev_month is not None else pd.DataFrame()

            value_fields = ["三级科目", "实际客户", "部门", "项目"]
            cur_tuple = {f: _norm(h.get(f"本月主_{f}")) for f in value_fields}
            hist_tuple = {f: _norm(h.get(f"历史主_{f}")) for f in value_fields}

            if not cur_df.empty:
                cur_pick = cur_df.copy()
                for f, v in cur_tuple.items():
                    if v and f in cur_pick.columns:
                        cur_pick = cur_pick[cur_pick[f].astype(str) == v]
                if cur_pick.empty:
                    cur_pick = cur_df
                _add_rows(cur_pick, mark=severity or "错误", reason=reason, severity=severity)

            if prev_df is not None and not prev_df.empty:
                prev_pick = prev_df.copy()
                for f, v in hist_tuple.items():
                    if v and f in prev_pick.columns:
                        prev_pick = prev_pick[prev_pick[f].astype(str) == v]
                if prev_pick.empty:
                    prev_pick = prev_df
                _add_rows(prev_pick.head(3), mark="参考", reason="参考：上月主映射基线", severity="")
            continue

        # 3) distinct_count 类：问题行=非组内主值；参考行=组内主值
        if "distinct_cnt" in h.index and pd.notna(h.get("distinct_cnt")):
            month = int(target_month) if target_month is not None else int(pd.to_numeric(h.get("月"), errors="coerce")) if pd.notna(pd.to_numeric(h.get("月"), errors="coerce")) else None
            if month is None:
                continue

            if category == "账载客户对应多实际客户":
                group_fields = ["主体账簿", "月", "三级科目", "账载客户", "项目"]
                distinct_field = "实际客户"
            elif category == "实际客户对应多主体":
                group_fields = ["月", "实际客户", "账载客户", "项目"]
                distinct_field = "主体账簿"
            elif category == "项目管理中心客户多部门":
                group_fields = ["主体账簿", "月", "三级科目", "账载客户", "实际客户", "项目"]
                distinct_field = "部门"
            else:
                # fallback: 尽量用现有列推断
                distinct_field = ""
                s = _norm(h.get("命中原因"))
                if " 不同值数" in s:
                    distinct_field = s.split(" 不同值数", 1)[0].strip()
                group_fields = [c for c in ["主体账簿", "月", "账载客户", "实际客户"] if c in h.index]

            filters = {c: _norm(h.get(c)) for c in group_fields if _norm(h.get(c))}
            group_df = _match_rows(month, {k: v for k, v in filters.items() if k != "月"})
            if group_df.empty or distinct_field not in group_df.columns:
                continue

            # defensively apply the "集团本部" exclusion for dept-based distinct checks
            if distinct_field == "部门" and "部门" in group_df.columns:
                group_df = group_df[group_df["部门"].astype(str).str.strip() != "集团本部"].copy()
                if group_df.empty:
                    continue

            # Build evidence: actual distinct values list from the output base rows
            try:
                distinct_list = "，".join(
                    sorted({str(x).strip() for x in group_df[distinct_field].dropna().astype(str) if str(x).strip()})
                )
            except Exception:
                distinct_list = ""
            if not distinct_list:
                distinct_list = _norm(h.get("不同值列表"))
            reason_with_vals = reason
            if distinct_list and "（不同值:" not in reason_with_vals:
                reason_with_vals = f"{reason_with_vals}（不同值: {distinct_list[:2000]}）"

            weight_col = "全额收入" if "全额收入" in group_df.columns else ("净额收入" if "净额收入" in group_df.columns else "")
            if weight_col:
                tmp = group_df.copy()
                tmp[weight_col] = pd.to_numeric(tmp[weight_col], errors="coerce").fillna(0.0).abs()
                g = tmp.groupby(distinct_field, dropna=False)[weight_col].sum().reset_index(name="w")
                if g.empty:
                    dominant = ""
                else:
                    dominant = str(g.sort_values(by=["w"], ascending=False).iloc[0][distinct_field])
            else:
                dominant = str(group_df[distinct_field].dropna().astype(str).value_counts().idxmax()) if not group_df[distinct_field].dropna().empty else ""

            ref_df = group_df[group_df[distinct_field].astype(str) == dominant] if dominant else group_df.head(1)
            prob_df = group_df[group_df[distinct_field].astype(str) != dominant] if dominant else group_df

            _add_rows(prob_df, mark=severity or "错误", reason=reason_with_vals, severity=severity)
            _add_rows(ref_df.head(3), mark="参考", reason=f"参考：组内主值 {distinct_field}={dominant}" if dominant else "参考：组内主值", severity="")
            continue

    if not out_rows:
        return pd.DataFrame()

    out = pd.DataFrame(out_rows)
    final_cols = original_cols + tail_cols
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    out = out[final_cols]

    # Align display with audit口径：部门不同值数检查输出时剔除“集团本部”
    if "部门" in out.columns and "命中原因" in out.columns:
        reason_s = out["命中原因"].astype(str)
        dept_s = out["部门"].astype(str).str.strip()
        dept_distinct_mask = reason_s.str.startswith("部门 不同值数", na=False)
        if dept_distinct_mask.any():
            out = out.loc[~(dept_distinct_mask & (dept_s == "集团本部"))].copy()
    return out

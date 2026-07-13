from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml


def _severity_rank(s: str) -> int:
    s = str(s)
    if s == "错误":
        return 0
    if s == "需确认":
        return 1
    return 2


@dataclass(frozen=True)
class ReportPaths:
    output_dir: Path
    report_path: Path


@dataclass(frozen=True)
class RuleInfo:
    """规则信息数据类"""
    rule_id: str
    rule_name: str
    severity: str
    scope: str
    description: str
    source_doc: str
    source_clause: str
    params_summary: str = ""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def make_report_paths(workdir: Path, report_prefix: str, yyyymm: str) -> ReportPaths:
    out_dir = workdir / "凭证审核输出"
    ensure_dir(out_dir)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    report_path = out_dir / f"{report_prefix}_{yyyymm}_{ts}.xlsx"
    return ReportPaths(output_dir=out_dir, report_path=report_path)


def _safe_text(v: object) -> str:
    if v is None:
        return "空"
    if isinstance(v, float) and pd.isna(v):
        return "空"
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return "空"
    return s


def _sheet_name(report_format: dict[str, Any], logical_key: str, default_name: str) -> str:
    sheet_names = report_format.get("sheet_names", {}) if isinstance(report_format, dict) else {}
    name = default_name
    if isinstance(sheet_names, dict):
        cand = sheet_names.get(logical_key)
        if isinstance(cand, str) and cand.strip():
            name = cand.strip()
    return name[:31]


def _strip_internal_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    keep = [c for c in df.columns if not str(c).startswith("_")]
    return df[keep].copy() if keep else pd.DataFrame()


def _replace_rule_id_with_name(df: pd.DataFrame) -> pd.DataFrame:
    """Replace '规则ID' column values with '规则名称' values, then drop '规则ID' column."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if "规则ID" in out.columns and "规则名称" in out.columns:
        out["规则ID"] = out["规则名称"]
    if "规则名称" in out.columns:
        out = out.drop(columns=["规则名称"])
    if "规则ID" in out.columns:
        out = out.rename(columns={"规则ID": "规则名称"})
    return out


def _apply_column_layout(df: pd.DataFrame, report_format: dict[str, Any], logical_key: str) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    df = _strip_internal_columns(df)
    layouts = report_format.get("column_layouts", {}) if isinstance(report_format, dict) else {}
    if not isinstance(layouts, dict):
        return df
    layout = layouts.get(logical_key, {})
    if not isinstance(layout, dict):
        return df

    out = df.copy()
    rename_map = layout.get("rename", {})
    if isinstance(rename_map, dict):
        clean_rename = {str(k): str(v) for k, v in rename_map.items() if str(k) in out.columns and str(v).strip()}
        if clean_rename:
            out = out.rename(columns=clean_rename)

    keep_cols = layout.get("keep", None)
    if isinstance(keep_cols, list):
        keep = [str(x) for x in keep_cols if str(x) in out.columns]
        if keep:
            out = out[keep]

    order = layout.get("order", None)
    if isinstance(order, list):
        ordered = [str(x) for x in order if str(x) in out.columns]
        rest = [c for c in out.columns if c not in ordered]
        if ordered:
            out = out[ordered + rest]
    return out


def _build_combo_drift_friendly_view(income_dim_anomalies: pd.DataFrame) -> pd.DataFrame:
    if income_dim_anomalies is None or income_dim_anomalies.empty:
        return pd.DataFrame()
    if "规则ID" not in income_dim_anomalies.columns:
        return pd.DataFrame()

    df = income_dim_anomalies[income_dim_anomalies["规则ID"].astype(str) == "INC_CUSTOMER_CONSISTENCY"].copy()
    if df.empty:
        return pd.DataFrame()

    priorities = ["三级科目", "实际客户", "部门", "项目"]

    def build_issue(row: pd.Series) -> pd.Series:
        issue_points: list[str] = []
        history_items: list[str] = []
        primary = ""
        for f in priorities:
            hist_col = f"历史主_{f}"
            cur_col = f"本月主_{f}"
            if hist_col not in row or cur_col not in row:
                continue
            if _safe_text(row.get(hist_col)) != _safe_text(row.get(cur_col)):
                tag = f"{f}疑似异常"
                issue_points.append(tag)
                history_items.append(f"{f}历史={_safe_text(row.get(hist_col))}")
                if not primary:
                    primary = tag
        return pd.Series({"主问题分类": primary, "问题点": "；".join(issue_points), "历史对应信息": "；".join(history_items)})

    if not {"主问题分类", "问题点", "历史对应信息"}.issubset(df.columns):
        df = pd.concat([df, df.apply(build_issue, axis=1)], axis=1)
    else:
        df["主问题分类"] = df["主问题分类"].astype(str)
        df["问题点"] = df["问题点"].astype(str)
        df["历史对应信息"] = df["历史对应信息"].astype(str)
        missing_mask = df["问题点"].str.strip().isin(["", "nan"])
        if missing_mask.any():
            filled = df.loc[missing_mask].apply(build_issue, axis=1)
            df.loc[missing_mask, "主问题分类"] = filled["主问题分类"]
            df.loc[missing_mask, "问题点"] = filled["问题点"]
            df.loc[missing_mask, "历史对应信息"] = filled["历史对应信息"]

    if "对比月份" not in df.columns:
        if {"本期月份", "前期月份"}.issubset(df.columns):
            df["对比月份"] = df["本期月份"].map(lambda x: _safe_text(x)) + " vs " + df["前期月份"].map(lambda x: _safe_text(x))
        else:
            df["对比月份"] = ""

    if "影响金额" not in df.columns:
        if "cur_total_abs" in df.columns:
            df["影响金额"] = pd.to_numeric(df["cur_total_abs"], errors="coerce").fillna(0.0)
        else:
            df["影响金额"] = 0.0

    rename_map = {
        "本月主_三级科目": "本月三级科目",
        "本月主_实际客户": "本月实际客户",
        "本月主_部门": "本月部门",
        "本月主_项目": "本月项目",
    }
    df = df.rename(columns=rename_map)

    cols = [
        "严重度",
        "规则ID",
        "主问题分类",
        "问题点",
        "历史对应信息",
        "主体账簿",
        "账载客户",
        "本月三级科目",
        "本月实际客户",
        "本月部门",
        "本月项目",
        "对比月份",
        "影响金额",
    ]
    for c in cols:
        if c not in df.columns:
            df[c] = ""
    out = df[cols].copy()
    if "影响金额" in out.columns:
        out["影响金额"] = pd.to_numeric(out["影响金额"], errors="coerce").fillna(0.0)
        out = out.sort_values(by=["影响金额"], ascending=False)
    return out


def write_report(
    path: Path,
    overview: pd.DataFrame,
    overview_rule_breakdown: Optional[pd.DataFrame] = None,
    aux_rule_violations: pd.DataFrame = None,
    aux_suspect_wrong_account: pd.DataFrame = None,
    income_dim_anomalies: pd.DataFrame = None,
    income_gm_anomalies: pd.DataFrame = None,
    ai_review: Optional[pd.DataFrame] = None,
    report_format: Optional[dict[str, Any]] = None,
    # 新增参数
    checks: Optional[list[dict[str, Any]]] = None,
    df_income: Optional[pd.DataFrame] = None,
    df_aux: Optional[pd.DataFrame] = None,
    df_mapping: Optional[pd.DataFrame] = None,
    target_month: Optional[int] = None,
) -> None:
    fmt = report_format or {}
    with pd.ExcelWriter(path, engine="openpyxl") as w:
        rule_info_out = build_rule_info_sheet(checks or [])
        if rule_info_out is None or rule_info_out.empty:
            rule_info_out = pd.DataFrame({"提示": ["未提供规则清单"]})
        rule_info_out.to_excel(w, sheet_name="规则与内容", index=False)

        headcount_out = build_headcount_report(
            df_aux=df_aux,
            aux_rule_violations=aux_rule_violations,
            aux_suspect_wrong_account=aux_suspect_wrong_account,
        )
        if headcount_out is None or headcount_out.empty:
            headcount_out = pd.DataFrame({"提示": ["无命中"]})
        headcount_out.to_excel(w, sheet_name="人次数据检查", index=False)

        customer_out = build_customer_consistency_sheet(
            df_income=df_income,
            income_dim_anomalies=income_dim_anomalies,
            df_mapping=df_mapping,
            target_month=target_month,
        )
        if customer_out is None or customer_out.empty:
            customer_out = pd.DataFrame({"提示": ["无命中"]})
        customer_out.to_excel(w, sheet_name="客户归属一致性检查", index=False)

        rev_cost_out = build_rev_cost_zero_mismatch_sheet(
            df_income=df_income,
            income_dim_anomalies=income_dim_anomalies,
            checks=checks,
            target_month=target_month,
        )
        if rev_cost_out is None or rev_cost_out.empty:
            rev_cost_out = pd.DataFrame({"提示": ["无命中"]})
        rev_cost_out.to_excel(w, sheet_name="收入成本零值不匹配检查", index=False)

        pp_out = build_pp_change_sheet(
            df_income=df_income,
            income_dim_anomalies=income_dim_anomalies,
            checks=checks,
            target_month=target_month,
        )
        if pp_out is None or pp_out.empty:
            pp_out = pd.DataFrame({"提示": ["无命中"]})
        pp_out.to_excel(w, sheet_name="同比波动检查", index=False)

        outsourcing_out = build_outsourcing_missing_cost_sheet(
            df_income=df_income,
            income_dim_anomalies=income_dim_anomalies,
            checks=checks,
            target_month=target_month,
        )
        if outsourcing_out is None or outsourcing_out.empty:
            outsourcing_out = pd.DataFrame({"提示": ["无命中"]})
        outsourcing_out.to_excel(w, sheet_name="外包缺工资或挂靠检查", index=False)

        neg_gm_out = build_neg_profit_ratio_sheet(
            df_income=df_income,
            income_gm_anomalies=income_gm_anomalies,
            checks=checks,
            target_month=target_month,
        )
        if neg_gm_out is None or neg_gm_out.empty:
            neg_gm_out = pd.DataFrame({"提示": ["无命中"]})
        neg_gm_out.to_excel(w, sheet_name="负毛利占比检查", index=False)



def build_comparison_report(
    df_source: pd.DataFrame,
    hits_df: pd.DataFrame,
    rule_id: str,
) -> pd.DataFrame:
    """
    通用对比报告生成函数

    支持：
    - 客户归属一致性（历史主_xxx 字段）
    - 同比波动类（前期值/本期值/变化率 字段）

    格式：原记录列 + 命中原因，对比记录紧跟问题记录下方
    """
    if hits_df is None or hits_df.empty:
        return pd.DataFrame()

    # 获取需要输出的列
    hits_cols = list(hits_df.columns)

    # 定义要保留的核心业务列（严格限制，避免列数过多）
    core_cols = [
        "主体账簿", "月", "内外", "业务类型", "账载客户", "实际客户",
        "部门", "项目", "全额收入", "净额收入", "项目毛利润",
        "三级科目", "成本合计", "结算人次", "社保人数"
    ]

    # 汇总检查的关键列（如distinct_count）
    summary_cols = ["distinct_cnt", "不同值列表", "gross_rev", "net_rev", "profit"]

    # 定义需要过滤掉的内部列模式
    exclude_patterns = ["凭证审核", "Unnamed:", "_", "规则", "严重度", "制度来源", "规则描述", "问题分类", "建议实际客户", "实际客户_映射"]

    # 只保留实际存在的核心业务列
    output_cols = [c for c in core_cols if c in hits_cols]

    # 汇总列（不同值列表等）只用于构建命中原因，不直接输出
    summary_data_cols = ["不同值列表", "distinct_cnt", "gross_rev", "net_rev", "profit", "cur_total_abs", "hist_total_abs", "cur_dominant_ratio", "hist_dominant_ratio"]

    # 再过滤掉内部列
    output_cols = [c for c in output_cols if not any(p in str(c) for p in exclude_patterns)]

    all_rows: list[dict[str, Any]] = []

    for _, row in hits_df.iterrows():
        # 过滤：净额收入或全额收入为0或NaN的记录不参与审核
        net_rev = row.get("净额收入")
        gross_rev = row.get("全额收入")
        net_rev_val = pd.to_numeric(net_rev, errors="coerce")
        gross_rev_val = pd.to_numeric(gross_rev, errors="coerce")

        # 跳过净额收入和全额收入都为0或NaN的记录
        # 但保留以下类型记录（它们本身不包含金额信息）：
        # 1. distinct_count汇总记录（有distinct_cnt值）
        # 2. combo_drift跨月对比记录（有前期月份/对比月份列，或命中原因含"上月"）
        has_distinct_count = "distinct_cnt" in hits_cols and pd.notna(row.get("distinct_cnt"))
        is_combo_drift = (
            ("前期月份" in hits_cols and pd.notna(row.get("前期月份")))
            or ("对比月份" in hits_cols and pd.notna(row.get("对比月份")))
            or ("历史对应信息" in hits_cols and pd.notna(row.get("历史对应信息")))
        )
        if not has_distinct_count and not is_combo_drift:
            net_is_empty = pd.isna(net_rev_val) or net_rev_val == 0
            gross_is_empty = pd.isna(gross_rev_val) or gross_rev_val == 0
            if net_is_empty and gross_is_empty:
                continue

        # 基础命中信息（清理和简化）
        hit_reason = ""
        if "命中原因" in row and pd.notna(row["命中原因"]):
            hit_reason = str(row["命中原因"]).strip()

        # 对于 distinct_count 类型，添加具体不同值信息
        if "distinct_cnt" in row and pd.notna(row["distinct_cnt"]):
            distinct_count = int(row["distinct_cnt"])
            different_values = ""
            if "不同值列表" in row and pd.notna(row["不同值列表"]):
                different_values = str(row["不同值列表"]).strip()
            # 替换命中原因，显示具体不同值
            if different_values:
                hit_reason = f"{hit_reason}（不同值: {different_values}）"

        # 对于 combo_drift 类型，添加具体漂移字段信息
        if "历史对应信息" in row and pd.notna(row["历史对应信息"]):
            hist_info = str(row["历史对应信息"]).strip()
            if hist_info and hist_info != "nan":
                hit_reason = f"{hit_reason} | 上期映射: {hist_info}"

        # 构建问题记录行
        record: dict[str, Any] = {"_record_type": "问题"}

        # ① 原记录列（只输出有值的列，空值不显示）
        for col in output_cols:
            val = row.get(col)
            if pd.notna(val) and str(val).strip() not in ["", "nan", "0", "0.0"]:
                record[col] = val
            else:
                record[col] = ""  # 空值显示为空字符串

        # ② 命中原因列
        record["命中原因"] = hit_reason

        all_rows.append(record)

        # ③ 添加对比记录（如果存在对比数据）
        # 检测对比字段类型（检查值是否存在，而非列名）

        # 模式A: 历史主_xxx（客户归属一致性）- 检查是否有非空的历史主字段
        hist_cols = [c for c in hits_cols if c.startswith("历史主_")]
        has_hist = any(pd.notna(row.get(c)) and str(row.get(c)).strip() not in ["", "nan"] for c in hist_cols)

        # 模式B: 前期/本期 值（同比波动）- 检查前期值是否存在
        has_period = "前期值" in hits_cols and pd.notna(row.get("前期值"))

        if has_hist:
            # 客户归属一致性对比 - 只显示关键差异字段
            compare_record: dict[str, Any] = {"_record_type": "对比"}

            # 只显示与当前值不同的历史值
            diff_fields = []
            for col in output_cols:
                hist_col = f"历史主_{col}"
                if hist_col in row and pd.notna(row[hist_col]):
                    cur_val = str(row.get(col, "")).strip()
                    hist_val = str(row[hist_col]).strip()
                    if cur_val != hist_val and hist_val not in ["", "nan"]:
                        compare_record[col] = row[hist_col]
                        diff_fields.append(col)
                    else:
                        compare_record[col] = ""
                else:
                    compare_record[col] = ""

            # 只在有差异时添加对比记录
            if diff_fields:
                compare_record["命中原因"] = f"【对比记录】上期主映射数据 (差异: {', '.join(diff_fields)})"
                all_rows.append(compare_record)

        elif has_period:
            # 同比波动对比 - 添加前期数据行
            compare_record: dict[str, Any] = {"_record_type": "对比"}

            # 复制当前行数据
            for col in output_cols:
                if col in row and pd.notna(row[col]):
                    compare_record[col] = row[col]
                else:
                    compare_record[col] = ""

            # 标记为前期数据
            period_info = ""
            if "前期月份" in row and pd.notna(row["前期月份"]):
                period_info = f"{int(row['前期月份'])}月"
            elif "对比月份" in row and pd.notna(row["对比月份"]):
                period_info = str(row["对比月份"]).split("vs")[1].strip() if "vs" in str(row["对比月份"]) else "上期"

            # 添加对比信息
            extra_info = []
            if "前期值" in row and pd.notna(row["前期值"]):
                extra_info.append(f"值={row['前期值']}")
            if "变化率" in row and pd.notna(row["变化率"]):
                extra_info.append(f"变化率={row['变化率']:.1%}")

            compare_record["命中原因"] = f"【对比记录】{period_info}数据"
            if extra_info:
                compare_record["命中原因"] += f" ({', '.join(extra_info)})"

            all_rows.append(compare_record)

    if not all_rows:
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)

    # 调整列顺序：核心列 + 命中原因，排除内部汇总列
    final_cols = [c for c in output_cols if c not in summary_data_cols] + ["命中原因"]
    for col in final_cols:
        if col not in result.columns:
            result[col] = ""

    return result[final_cols]


def build_comparison_report_for_all_rules(
    df_income: pd.DataFrame,
    income_dim_anomalies: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """
    为所有有对比数据的规则生成专门报告
    当前只处理客户归属一致性检查（已完善），其他规则待后续迭代处理。

    返回: {sheet_name: report_df}
    """
    if income_dim_anomalies is None or income_dim_anomalies.empty:
        return {}

    if "规则ID" not in income_dim_anomalies.columns:
        return {}

    # 当前只保留已完善的规则：客户归属一致性检查
    comparison_rules = {
        "INC_CUSTOMER_CONSISTENCY": "客户归属一致性检查",
    }

    reports: dict[str, pd.DataFrame] = {}

    for rule_id, sheet_name in comparison_rules.items():
        rule_hits = income_dim_anomalies[income_dim_anomalies["规则ID"] == rule_id].copy()
        if not rule_hits.empty:
            report = build_comparison_report(
                df_source=df_income,
                hits_df=rule_hits,
                rule_id=rule_id,
            )
            if not report.empty:
                reports[sheet_name] = report

    return reports


def build_headcount_report(
    df_aux: Optional[pd.DataFrame],
    aux_rule_violations: Optional[pd.DataFrame],
    aux_suspect_wrong_account: Optional[pd.DataFrame],
) -> pd.DataFrame:
    """
    生成人次数据检查专门报告（简化格式）

    格式：
    1. 只显示原记录的关键列（凭证号、摘要、命中码、严重度、命中原因）
    2. 过滤掉内部字段
    """
    frames: list[pd.DataFrame] = []
    if aux_rule_violations is not None and not aux_rule_violations.empty:
        frames.append(aux_rule_violations)
    if aux_suspect_wrong_account is not None and not aux_suspect_wrong_account.empty:
        frames.append(aux_suspect_wrong_account)
    if not frames:
        return pd.DataFrame()

    all_hits = pd.concat(frames, ignore_index=True)
    if all_hits.empty:
        return pd.DataFrame()

    if "规则ID" in all_hits.columns:
        mask = all_hits["规则ID"].astype(str).str.contains("AUX_HEADCOUNT", na=False)
    elif "规则名称" in all_hits.columns:
        mask = all_hits["规则名称"].astype(str).str.contains("人次", na=False)
    else:
        mask = pd.Series(False, index=all_hits.index)
    hits = all_hits[mask].copy()
    if hits.empty:
        return pd.DataFrame()

    keep_hit_cols = [c for c in ["命中码", "问题分类", "命中原因", "严重度"] if c in hits.columns]
    if df_aux is None or df_aux.empty or "_row_index" not in hits.columns:
        out = hits[keep_hit_cols].copy() if keep_hit_cols else hits.copy()
        drop_cols = [c for c in out.columns if c in ["规则ID", "规则名称", "规则描述", "制度来源"]]
        if drop_cols:
            out = out.drop(columns=drop_cols)
        return out

    idx_list = pd.to_numeric(hits["_row_index"], errors="coerce").fillna(-1).astype(int).tolist()
    detail = df_aux.reindex(idx_list).reset_index(drop=True)
    detail_cols = [
        c
        for c in detail.columns
        if not str(c).startswith("_")
        and not str(c).startswith("Unnamed:")
        and "凭证审核" not in str(c)
    ]
    out = detail[detail_cols].copy() if detail_cols else detail.copy()

    for c in keep_hit_cols:
        out[c] = hits[c].reset_index(drop=True)

    final_cols = detail_cols + keep_hit_cols
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]


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
    final_cols = final_cols + list(cols_map.keys()) + ["标注", "命中原因", "指标名称", "指标值", "严重度"]
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
    final_cols = final_cols + list(cols_map.keys()) + ["标注", "命中原因", "指标名称", "指标值", "严重度"]
    for c in final_cols:
        if c not in out.columns:
            out[c] = ""
    return out[final_cols]


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


def build_rule_info_sheet(checks: list[dict[str, Any]]) -> pd.DataFrame:
    """生成规则说明 sheet"""
    rows: list[dict[str, Any]] = []

    def sheet_for_rule(rule: dict[str, Any]) -> str:
        rtype = str(rule.get("type", "")).strip()
        if rtype == "customer_consistency_check":
            return "客户归属一致性检查"
        if rtype == "headcount_data_check":
            return "人次数据检查"
        if rtype == "pp_change":
            return "同比波动检查"
        if rtype == "neg_profit_ratio":
            return "负毛利占比检查"
        return "其他"

    sheet_order = ["人次数据检查", "客户归属一致性检查", "同比波动检查", "负毛利占比检查"]
    sheet_rank = {name: i for i, name in enumerate(sheet_order)}

    for i, rule in enumerate(checks):
        if not rule:
            continue
        source = rule.get("source", {}) or {}
        params = rule.get("params", {}) or {}

        sheet_name = sheet_for_rule(rule)
        rank = int(sheet_rank.get(sheet_name, 999))

        params_yaml = ""
        if isinstance(params, dict):
            try:
                params_yaml = yaml.safe_dump(params, allow_unicode=True, sort_keys=False).strip()
            except Exception:
                params_yaml = str(params)
        else:
            params_yaml = str(params)

        # 提取关键参数摘要
        params_summary = ""
        if isinstance(params, dict):
            key_params = []
            for k, v in params.items():
                if k in ("min_amount_abs", "tolerance_ratio", "min_gross_revenue", "threshold"):
                    key_params.append(f"{k}={v}")
            params_summary = ", ".join(key_params)

        rows.append({
            "__sheet_rank": rank,
            "__rule_index": i,
            "所属Sheet": sheet_name,
            "规则ID": str(rule.get("id", "")),
            "规则名称": str(rule.get("name", "")),
            "规则类型": str(rule.get("type", "")),
            "严重度": str(rule.get("severity", "")),
            "数据范围": str(rule.get("scope", "")),
            "规则描述": str(rule.get("description", "")),
            "制度来源": str(source.get("doc", "")),
            "制度条款": str(source.get("clause", "")),
            "关键参数": params_summary,
            "规则内容": params_yaml,
        })

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows)
    out = out.sort_values(by=["__sheet_rank", "__rule_index"], ascending=True)
    out = out.drop(columns=["__sheet_rank", "__rule_index"], errors="ignore")
    return out


def _get_original_income_columns(df_income: pd.DataFrame) -> list[str]:
    """获取收入成本表的原始列名（用于保持原样输出）"""
    if df_income is None or df_income.empty:
        return []
    return [c for c in df_income.columns if not str(c).startswith("_")]


def build_customer_consistency_report(
    df_income: pd.DataFrame,
    customer_hits: pd.DataFrame,
    df_mapping: pd.DataFrame,
    target_month: int,
) -> pd.DataFrame:
    """
    生成客户归属一致性检查报告（简化格式）

    格式要求：
    1. 显示原记录（原始数据中的列，直接复制）
    2. 在原记录后增加"命中原因"列
    3. 对于对比类问题，在问题记录下方插入对比记录
    4. 命中原因可以有多个（不超过3条）
    """
    if customer_hits is None or customer_hits.empty:
        return pd.DataFrame()

    # 获取原始列名
    original_cols = _get_original_income_columns(df_income)

    # 定义需要对比的字段（组合漂移类问题）
    compare_fields = ["三级科目", "实际客户", "部门", "项目"]

    all_rows: list[dict[str, Any]] = []

    for _, row in customer_hits.iterrows():
        # 基础命中信息
        hit_reasons: list[str] = []
        if "命中原因" in row and pd.notna(row["命中原因"]):
            hit_reasons.append(str(row["命中原因"]))

        # 构建问题记录行
        record: dict[str, Any] = {"_record_type": "问题"}

        # ① 原记录列（直接从原始数据复制）
        for col in original_cols:
            record[col] = row.get(col, "")

        # ② 命中原因列
        record["命中原因"] = "；".join(hit_reasons) if hit_reasons else ""

        all_rows.append(record)

        # ③ 对于对比类问题，添加对比记录
        if "问题分类" in row and row["问题分类"] == "客户归属组合漂移":
            # 检查是否有历史主_xxx 字段
            compare_record: dict[str, Any] = {"_record_type": "对比"}

            for col in original_cols:
                # 尝试从历史主字段获取
                hist_col = f"历史主_{col}"
                if hist_col in row and pd.notna(row[hist_col]):
                    compare_record[col] = row[hist_col]
                else:
                    compare_record[col] = ""

            # 在命中原因中标注
            compare_record["命中原因"] = "【对比记录】上期主映射数据"

            all_rows.append(compare_record)

    if not all_rows:
        return pd.DataFrame()

    result = pd.DataFrame(all_rows)

    # 调整列顺序：原始列 + 命中原因
    final_cols = original_cols + ["命中原因"]
    # 确保所有列都存在
    for col in final_cols:
        if col not in result.columns:
            result[col] = ""

    return result[final_cols]

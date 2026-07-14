from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from .report_customer_consistency import build_customer_consistency_sheet
from .report_pp_change import build_pp_change_sheet
from .report_comparison import build_comparison_report
from .report_cost_checks import build_outsourcing_missing_cost_sheet, build_rev_cost_zero_mismatch_sheet
from .report_profit import build_neg_profit_ratio_sheet


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

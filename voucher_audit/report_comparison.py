from __future__ import annotations

from typing import Any

import pandas as pd


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

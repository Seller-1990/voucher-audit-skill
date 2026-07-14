from __future__ import annotations

import re
from typing import Any

import pandas as pd

from .check_utils import _rule_name


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

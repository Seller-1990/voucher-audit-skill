from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import pandas as pd

from .security import backup_file


ANNOTATION_HEADERS = ("凭证审核异常项", "凭证审核规则ID", "凭证审核命中原因")


@dataclass(frozen=True)
class RowAnnotation:
    row_index: int
    issue_text: str
    rule_ids: str
    reasons: str


@dataclass(frozen=True)
class CellHighlight:
    row_index: int
    column_name: str


@dataclass(frozen=True)
class QueryTableAnnotationPlan:
    workbook_path: Path
    worksheet_name: str
    table_name: str
    gap_columns: int
    headers: tuple[str, str, str]
    row_annotations: tuple[RowAnnotation, ...]
    cell_highlights: tuple[CellHighlight, ...]
    possible_highlight_columns: tuple[str, ...]


@dataclass(frozen=True)
class SourceAnnotationBundle:
    plans: tuple[QueryTableAnnotationPlan, ...]


@dataclass(frozen=True)
class _RuleMatchSpec:
    key_columns: tuple[str, ...]
    highlight_columns: tuple[str, ...]


_AUX_RULE_SPECS: dict[str, _RuleMatchSpec] = {
    "AUX_HEADCOUNT_DATA_CHECK": _RuleMatchSpec(key_columns=(), highlight_columns=("摘要",)),
}


_INCOME_RULE_SPECS: dict[str, _RuleMatchSpec] = {
    "INC_CUSTOMER_CONSISTENCY": _RuleMatchSpec(
        key_columns=("主体账簿", "账载客户", "实际客户"),
        highlight_columns=("三级科目", "实际客户", "部门", "项目", "全额收入", "成本合计"),
    ),
    "INC_REV_COST_ZERO_MISMATCH": _RuleMatchSpec(
        key_columns=("主体账簿", "三级科目", "实际客户", "部门", "项目"),
        highlight_columns=("全额收入", "成本合计"),
    ),
    "INC_METRIC_PP_CHANGE": _RuleMatchSpec(
        key_columns=("主体账簿", "实际客户", "部门"),
        highlight_columns=("项目毛利润", "净额收入", "结算人次"),
    ),
    "INC_VALUE_PP_CHANGE": _RuleMatchSpec(
        key_columns=("主体账簿", "实际客户", "部门"),
        highlight_columns=("项目返费", "第三方挂靠成本"),
    ),
    "INC_RATIO_PP_CHANGE": _RuleMatchSpec(
        key_columns=("主体账簿", "实际客户", "部门"),
        highlight_columns=("项目返费", "净额收入", "第三方挂靠成本", "全额收入"),
    ),
}


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    return text


def _norm_key(value: Any) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    try:
        num = float(text)
        if num.is_integer():
            return str(int(num))
    except Exception:
        pass
    return text


def _unique_join(values: Iterable[str]) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return "；".join(out)


def _extract_issue_text(row: pd.Series) -> str:
    primary = _clean_text(row.get("主问题分类", ""))
    points = _clean_text(row.get("问题点", ""))
    reason = _clean_text(row.get("命中原因", ""))
    if primary and points and points != primary:
        return f"{primary}：{points}"
    if points:
        return points
    if primary:
        return primary
    return reason


def _extract_row_index(row: pd.Series) -> Optional[int]:
    raw = row.get("_row_index", None)
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    try:
        idx = int(raw)
    except Exception:
        return None
    if idx < 0:
        return None
    return idx


def _match_source_rows(source_df: pd.DataFrame, row: pd.Series, key_columns: tuple[str, ...], target_month: Optional[int]) -> list[int]:
    if source_df.empty:
        return []
    mask = pd.Series(True, index=source_df.index)
    if target_month is not None and "月" in source_df.columns:
        month_series = pd.to_numeric(source_df["月"], errors="coerce")
        mask = mask & (month_series == int(target_month))
    for col in key_columns:
        if col not in source_df.columns:
            return []
        expected = _norm_key(row.get(col, ""))
        series = source_df[col].map(_norm_key)
        mask = mask & (series == expected)
    return [int(x) for x in source_df.index[mask].tolist()]


def _append_annotations(
    row_bucket: dict[int, dict[str, list[str]]],
    highlight_bucket: set[tuple[int, str]],
    row_indexes: Iterable[int],
    issue_text: str,
    rule_id: str,
    reason: str,
    highlight_columns: Iterable[str],
) -> None:
    for row_index in row_indexes:
        slot = row_bucket.setdefault(row_index, {"issues": [], "rule_ids": [], "reasons": []})
        if issue_text:
            slot["issues"].append(issue_text)
        if rule_id:
            slot["rule_ids"].append(rule_id)
        if reason:
            slot["reasons"].append(reason)
        for col in highlight_columns:
            c = _clean_text(col)
            if c:
                highlight_bucket.add((row_index, c))


def _finalize_plan(
    workbook_path: Path,
    worksheet_name: str,
    table_name: str,
    gap_columns: int,
    row_bucket: dict[int, dict[str, list[str]]],
    highlight_bucket: set[tuple[int, str]],
    possible_highlight_columns: Iterable[str],
) -> QueryTableAnnotationPlan:
    row_annotations = tuple(
        RowAnnotation(
            row_index=row_index,
            issue_text=_unique_join(values.get("issues", [])),
            rule_ids=_unique_join(values.get("rule_ids", [])),
            reasons=_unique_join(values.get("reasons", [])),
        )
        for row_index, values in sorted(row_bucket.items(), key=lambda item: item[0])
    )
    cell_highlights = tuple(
        CellHighlight(row_index=row_index, column_name=column_name)
        for row_index, column_name in sorted(highlight_bucket, key=lambda item: (item[0], item[1]))
    )
    unique_cols = [col for col in sorted({_clean_text(x) for x in possible_highlight_columns}) if col]
    return QueryTableAnnotationPlan(
        workbook_path=workbook_path.resolve(),
        worksheet_name=worksheet_name,
        table_name=table_name,
        gap_columns=max(0, int(gap_columns)),
        headers=ANNOTATION_HEADERS,
        row_annotations=row_annotations,
        cell_highlights=cell_highlights,
        possible_highlight_columns=tuple(unique_cols),
    )


def _build_aux_plan(
    workbook_path: Path,
    df_aux: pd.DataFrame,
    aux_tables: list[pd.DataFrame],
    gap_columns: int,
) -> QueryTableAnnotationPlan:
    row_bucket: dict[int, dict[str, list[str]]] = {}
    highlight_bucket: set[tuple[int, str]] = set()
    possible_highlights = {col for spec in _AUX_RULE_SPECS.values() for col in spec.highlight_columns}
    for table in aux_tables:
        if table is None or table.empty:
            continue
        for _, row in table.iterrows():
            rule_id = _clean_text(row.get("规则ID", ""))
            reason = _clean_text(row.get("命中原因", ""))
            issue_text = _extract_issue_text(row)
            spec = _AUX_RULE_SPECS.get(rule_id)
            row_indexes: list[int] = []
            direct_idx = _extract_row_index(row)
            if direct_idx is not None:
                row_indexes = [direct_idx]
            elif spec and spec.key_columns:
                row_indexes = _match_source_rows(df_aux, row, spec.key_columns, target_month=None)
            if not row_indexes:
                continue
            highlight_cols = spec.highlight_columns if spec else ()
            _append_annotations(row_bucket, highlight_bucket, row_indexes, issue_text, rule_id, reason, highlight_cols)
    return _finalize_plan(
        workbook_path=workbook_path,
        worksheet_name="调整后序时账",
        table_name="调整后序时账",
        gap_columns=gap_columns,
        row_bucket=row_bucket,
        highlight_bucket=highlight_bucket,
        possible_highlight_columns=sorted(possible_highlights),
    )


def _build_income_plan(
    workbook_path: Path,
    df_income: pd.DataFrame,
    income_tables: list[pd.DataFrame],
    target_month: int,
    gap_columns: int,
) -> QueryTableAnnotationPlan:
    row_bucket: dict[int, dict[str, list[str]]] = {}
    highlight_bucket: set[tuple[int, str]] = set()
    possible_highlights = {col for spec in _INCOME_RULE_SPECS.values() for col in spec.highlight_columns}
    for table in income_tables:
        if table is None or table.empty:
            continue
        for _, row in table.iterrows():
            rule_id = _clean_text(row.get("规则ID", ""))
            reason = _clean_text(row.get("命中原因", ""))
            issue_text = _extract_issue_text(row)
            spec = _INCOME_RULE_SPECS.get(rule_id)
            if spec:
                row_indexes = _match_source_rows(df_income, row, spec.key_columns, target_month=target_month)
                _append_annotations(row_bucket, highlight_bucket, row_indexes, issue_text, rule_id, reason, spec.highlight_columns)
                continue
            direct_idx = _extract_row_index(row)
            if direct_idx is None:
                continue
            _append_annotations(row_bucket, highlight_bucket, [direct_idx], issue_text, rule_id, reason, ())
    return _finalize_plan(
        workbook_path=workbook_path,
        worksheet_name="收入成本表",
        table_name="收入成本表",
        gap_columns=gap_columns,
        row_bucket=row_bucket,
        highlight_bucket=highlight_bucket,
        possible_highlight_columns=sorted(possible_highlights),
    )


def build_source_annotation_bundle(
    *,
    data_summary_path: Path,
    income_cost_path: Path,
    df_aux: pd.DataFrame,
    df_income: pd.DataFrame,
    aux_rule_violations: pd.DataFrame,
    aux_suspect_wrong_account: pd.DataFrame,
    income_dim_anomalies: pd.DataFrame,
    income_gm_anomalies: pd.DataFrame,
    target_month: int,
    gap_columns: int = 1,
) -> SourceAnnotationBundle:
    aux_plan = _build_aux_plan(
        workbook_path=data_summary_path,
        df_aux=df_aux,
        aux_tables=[aux_rule_violations, aux_suspect_wrong_account],
        gap_columns=gap_columns,
    )
    income_plan = _build_income_plan(
        workbook_path=income_cost_path,
        df_income=df_income,
        income_tables=[income_dim_anomalies, income_gm_anomalies],
        target_month=int(target_month),
        gap_columns=gap_columns,
    )
    return SourceAnnotationBundle(plans=(aux_plan, income_plan))

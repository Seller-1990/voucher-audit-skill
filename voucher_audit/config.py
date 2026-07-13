from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, Union

import yaml

from .constants import DEFAULT_OPENAI_BASE_URL, DEFAULT_OPENAI_MODEL


StrOrList = Union[str, Sequence[str]]


def _as_list(v: StrOrList) -> list[str]:
    if isinstance(v, str):
        return [v]
    return [str(x) for x in v]


@dataclass(frozen=True)
class SheetMatcher:
    preferred: list[str]
    fuzzy_contains_any: list[str]


@dataclass(frozen=True)
class InputsConfig:
    data_summary_file: str
    income_cost_file: str
    sheets: dict[str, SheetMatcher]
    columns: dict[str, dict[str, list[str]]]


@dataclass(frozen=True)
class ThresholdsConfig:
    amount_abs_min_for_anomaly: float
    rare_combo_min_history_count: int
    drift_min_amount_abs: float
    drift_dominance_ratio: float
    gross_margin: dict[str, Any]


@dataclass(frozen=True)
class AIConfig:
    enabled_default: bool
    model: str
    base_url: str
    api_key_env: str
    max_items_per_section: int
    max_output_tokens: int


@dataclass(frozen=True)
class RuleConfig:
    raw: dict[str, Any]
    inputs: InputsConfig
    thresholds: ThresholdsConfig
    ai: AIConfig
    report_format: dict[str, Any]
    checks: list[dict[str, Any]]


def load_rules_data(data: dict[str, Any]) -> RuleConfig:
    if not isinstance(data, dict):
        raise ValueError("规则文件顶层必须是对象")
    data = dict(data)

    inputs = data.get("inputs", {})
    files = inputs.get("files", {}) or {}
    sheets = inputs.get("sheets", {}) or {}
    columns = inputs.get("columns", {}) or {}

    sheet_matchers: dict[str, SheetMatcher] = {}
    for k, v in sheets.items():
        v = v or {}
        sheet_matchers[k] = SheetMatcher(
            preferred=_as_list(v.get("preferred", [])),
            fuzzy_contains_any=_as_list(v.get("fuzzy_contains_any", [])),
        )

    norm_columns: dict[str, dict[str, list[str]]] = {}
    for scope, mapping in columns.items():
        mapping = mapping or {}
        norm_columns[scope] = {k: _as_list(v) for k, v in mapping.items()}

    inputs_cfg = InputsConfig(
        data_summary_file=str(files.get("data_summary", "数据汇总.xlsx")),
        income_cost_file=str(files.get("income_cost", "考核表输出.xlsx")),
        sheets=sheet_matchers,
        columns=norm_columns,
    )

    thresholds = data.get("thresholds", {}) or {}
    thresholds_cfg = ThresholdsConfig(
        amount_abs_min_for_anomaly=float(thresholds.get("amount_abs_min_for_anomaly", 50000)),
        rare_combo_min_history_count=int(thresholds.get("rare_combo_min_history_count", 0)),
        drift_min_amount_abs=float(thresholds.get("drift_min_amount_abs", 50000)),
        drift_dominance_ratio=float(thresholds.get("drift_dominance_ratio", 0.7)),
        gross_margin=dict(thresholds.get("gross_margin", {}) or {}),
    )

    ai = data.get("ai", {}) or {}
    ai_cfg = AIConfig(
        enabled_default=bool(ai.get("enabled_default", False)),
        model=str(ai.get("model", DEFAULT_OPENAI_MODEL)),
        base_url=str(ai.get("base_url", DEFAULT_OPENAI_BASE_URL)),
        api_key_env=str(ai.get("api_key_env", "OPENAI_API_KEY")),
        max_items_per_section=int(ai.get("max_items_per_section", 40)),
        max_output_tokens=int(ai.get("max_output_tokens", 1200)),
    )
    report_format_cfg = dict(data.get("report_format", {}) or {})

    checks = data.get("checks", []) or []
    if not isinstance(checks, list):
        raise ValueError("rules.checks 必须是 list")

    return RuleConfig(
        raw=data,
        inputs=inputs_cfg,
        thresholds=thresholds_cfg,
        ai=ai_cfg,
        report_format=report_format_cfg,
        checks=checks,
    )


def load_rules(path: Path) -> RuleConfig:
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        msg = str(e).strip()
        hint = (
            "常见原因：在 YAML 的双引号字符串里写了正则转义（例如 \\d、\\s）。\n"
            "修复方式二选一：\n"
            "1) 把该字段改成单引号：pattern: '(?i)Z\\d+S\\d+'\n"
            "2) 或在双引号里把反斜杠写成双反斜杠：pattern: \"(?i)Z\\\\d+S\\\\d+\""
        )
        raise ValueError(f"规则文件解析失败：{path}\n\n{msg}\n\n{hint}") from e

    return load_rules_data(data)

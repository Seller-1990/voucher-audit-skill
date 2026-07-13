from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import yaml

from .checks import run_checks
from .config import RuleConfig, load_rules_data
from .rule_patcher import _apply_patch_dict, _dump_yaml, _load_yaml, _normalize_patch_action
from .runner import load_audit_context
from .rules_template import TEMPLATE_YAML


SAFE_PHASE_NAME = "safe_rule_editing_v2"
SAFE_ALLOWED_OPS = ("update_check", "add_check", "set_report_format")
SAFE_ALLOWED_TOP_FIELDS = ("short_name", "description", "severity", "params")
SAFE_SEVERITIES = ("错误", "需确认", "提示")
SAFE_ADD_CHECK_BASE_RISK = "高"
_EXCEL_SHEET_INVALID_CHARS = set("[]:*?/\\")


@dataclass(frozen=True)
class EditableFieldSpec:
    path: str
    label: str
    value_type: str
    description: str
    risk: str = "低"
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class RulePatchReview:
    ok: bool
    message: str
    normalized_patch: dict[str, Any]
    diff_text: str
    impact_text: str
    risk_level: str
    simulation_text: str
    changed_rule_ids: tuple[str, ...] = ()


_COMMON_EDITABLE_FIELDS: tuple[EditableFieldSpec, ...] = (
    EditableFieldSpec(path="short_name", label="规则简称", value_type="string", description="用于界面展示的简短名称", risk="低"),
    EditableFieldSpec(path="description", label="规则描述", value_type="string", description="用于解释规则含义的文本", risk="低"),
    EditableFieldSpec(path="severity", label="严重度", value_type="enum", description="规则命中后的严重程度", risk="中", choices=SAFE_SEVERITIES),
)


_RULE_TYPE_FIELD_SPECS: dict[str, tuple[EditableFieldSpec, ...]] = {
    "combo_drift": (
        EditableFieldSpec(
            path="params.min_amount_abs",
            label="最小影响金额",
            value_type="number",
            description="只有影响金额达到该阈值时才命中",
            risk="中",
            min_value=0,
            max_value=1_000_000_000,
        ),
    ),
    "rev_cost_zero_mismatch": (
        EditableFieldSpec(
            path="params.eps",
            label="零值容差",
            value_type="number",
            description="判断收入/成本是否视作 0 的容差",
            risk="中",
            min_value=0,
            max_value=1,
        ),
    ),
    "summary_zs_suffix": (
        EditableFieldSpec(
            path="params.allowed_next_chars",
            label="允许后缀字符",
            value_type="string_list",
            description="Z代码后允许紧跟的空白或标点字符列表",
            risk="中",
        ),
    ),
    "metric_pp_change": (
        EditableFieldSpec(
            path="params.tolerance_ratio",
            label="指标波动容差",
            value_type="number",
            description="指标相对前期允许的最大波动比例",
            risk="中",
            min_value=0,
            max_value=5,
        ),
        EditableFieldSpec(
            path="params.min_revenue",
            label="最小收入门槛",
            value_type="number",
            description="低于该净额收入时不判断波动",
            risk="中",
            min_value=0,
            max_value=1_000_000_000,
        ),
    ),
    "value_pp_change": (
        EditableFieldSpec(
            path="params.tolerance_ratio",
            label="金额波动容差",
            value_type="number",
            description="金额相对前期允许的最大波动比例",
            risk="中",
            min_value=0,
            max_value=5,
        ),
        EditableFieldSpec(
            path="params.min_abs",
            label="最小金额门槛",
            value_type="number",
            description="低于该绝对值时不判断波动",
            risk="中",
            min_value=0,
            max_value=1_000_000_000,
        ),
    ),
    "ratio_pp_change": (
        EditableFieldSpec(
            path="params.tolerance_ratio",
            label="比率波动容差",
            value_type="number",
            description="比率相对前期允许的最大波动比例",
            risk="中",
            min_value=0,
            max_value=5,
        ),
    ),
    "pp_change": (
        EditableFieldSpec(
            path="params.tolerance_ratio",
            label="默认波动容差",
            value_type="number",
            description="同比波动默认阈值（items 可单独覆盖）",
            risk="中",
            min_value=0,
            max_value=5,
        ),
    ),
}


_REPORT_FORMAT_LABELS: dict[str, str] = {
    "overview": "概览",
    "aux_rule_violations": "辅助账规则违规",
    "aux_suspect_wrong_account": "辅助账疑似错科目",
    "income_dim_anomalies": "收入成本维度异常",
    "combo_drift_friendly": "主映射异常友好视图",
    "income_gm_anomalies": "收入成本毛利异常",
    "ai_review": "AI复核意见",
}


def editable_field_specs_for_rule(rule: dict[str, Any]) -> tuple[EditableFieldSpec, ...]:
    rule_type = str((rule or {}).get("type", "")).strip()
    return _COMMON_EDITABLE_FIELDS + _RULE_TYPE_FIELD_SPECS.get(rule_type, tuple())


def report_format_field_specs() -> tuple[EditableFieldSpec, ...]:
    specs: list[EditableFieldSpec] = []
    for logical_key, label in _REPORT_FORMAT_LABELS.items():
        specs.extend(
            [
                EditableFieldSpec(
                    path=f"sheet_names.{logical_key}",
                    label=f"{label}工作表名",
                    value_type="sheet_name",
                    description="导出 Excel 中该逻辑结果对应的 sheet 名称",
                    risk="低",
                ),
                EditableFieldSpec(
                    path=f"column_layouts.{logical_key}.rename",
                    label=f"{label}列名映射",
                    value_type="string_map",
                    description="导出前对列名做 rename 映射",
                    risk="低",
                ),
                EditableFieldSpec(
                    path=f"column_layouts.{logical_key}.keep",
                    label=f"{label}保留列",
                    value_type="string_list",
                    description="仅保留这些列（其余列不导出）",
                    risk="中",
                ),
                EditableFieldSpec(
                    path=f"column_layouts.{logical_key}.order",
                    label=f"{label}列顺序",
                    value_type="string_list",
                    description="把这些列按给定顺序排到前面，其余列保持在后面",
                    risk="中",
                ),
            ]
        )
    return tuple(specs)


def _template_checks_by_type() -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(TEMPLATE_YAML) or {}
    rules = load_rules_data(data)
    out: dict[str, dict[str, Any]] = {}
    for rule in rules.checks:
        rule_type = str((rule or {}).get("type", "")).strip()
        if rule_type and rule_type not in out:
            out[rule_type] = copy.deepcopy(rule)
    return out


def addable_rule_types() -> tuple[str, ...]:
    return tuple(sorted(_template_checks_by_type().keys()))


def addable_field_meta_for_rule_type(rule_type: str) -> list[dict[str, Any]]:
    template = _template_checks_by_type().get(str(rule_type).strip())
    if not isinstance(template, dict):
        return []
    meta: list[dict[str, Any]] = [
        {
            "path": "id",
            "label": "规则ID",
            "type": "rule_id",
            "description": "新增规则的唯一标识，建议使用大写英文+下划线",
            "risk": "中",
        }
    ]
    meta.extend(editable_field_meta_for_rule(template))
    return meta


def editable_field_meta_for_rule(rule: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in editable_field_specs_for_rule(rule):
        out.append(
            {
                "path": spec.path,
                "label": spec.label,
                "type": spec.value_type,
                "description": spec.description,
                "risk": spec.risk,
                "min": spec.min_value,
                "max": spec.max_value,
                "choices": list(spec.choices),
            }
        )
    return out


def editable_field_meta_for_report_format() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in report_format_field_specs():
        out.append(
            {
                "path": spec.path,
                "label": spec.label,
                "type": spec.value_type,
                "description": spec.description,
                "risk": spec.risk,
                "min": spec.min_value,
                "max": spec.max_value,
                "choices": list(spec.choices),
            }
        )
    return out


def build_rules_guardrails_meta(rules: RuleConfig) -> dict[str, Any]:
    by_rule: list[dict[str, Any]] = []
    for rule in rules.checks:
        rid = str((rule or {}).get("id", "")).strip()
        if not rid:
            continue
        by_rule.append(
            {
                "id": rid,
                "type": str((rule or {}).get("type", "")).strip(),
                "editable_fields": editable_field_meta_for_rule(rule),
            }
        )
    return {
        "phase": SAFE_PHASE_NAME,
        "allowed_patch_ops": list(SAFE_ALLOWED_OPS),
        "forbidden_patch_ops": ["remove_check", "set_ai"],
        "rules": by_rule,
        "add_templates": [
            {
                "type": rule_type,
                "editable_fields": addable_field_meta_for_rule_type(rule_type),
            }
            for rule_type in addable_rule_types()
        ],
        "report_format_editable_fields": editable_field_meta_for_report_format(),
    }


def format_rule_guardrails_human(rules: RuleConfig) -> str:
    lines: list[str] = []
    lines.append("AI安全改规则范围（当前阶段）：")
    lines.append("- 允许修改现有规则的安全参数。")
    lines.append("- 允许安全调整 report_format（sheet 名、列改名、保留列、列顺序）。")
    lines.append("- 允许基于内置模板新增规则，但仍不允许删除规则或重构规则包。")
    lines.append("- 默认仍需人工确认后才落盘。")
    for rule in rules.checks:
        rid = str((rule or {}).get("id", "")).strip()
        if not rid:
            continue
        fields = editable_field_specs_for_rule(rule)
        if not fields:
            continue
        field_text = "、".join([f"{spec.label}（{spec.path}）" for spec in fields])
        lines.append(f"- {rid}: {field_text}")
    lines.append("")
    lines.append("支持模板化新增的规则类型：")
    for rule_type in addable_rule_types():
        add_fields = addable_field_meta_for_rule_type(rule_type)
        field_text = "、".join([f"{item['label']}（{item['path']}）" for item in add_fields])
        lines.append(f"- {rule_type}: {field_text}")
    lines.append("")
    lines.append("支持安全调整的报表输出格式字段：")
    lines.append("- sheet_names.<逻辑结果>: 修改各结果 sheet 名称（Excel 名称最长 31，且不能含 []:*?/\\）")
    lines.append("- column_layouts.<逻辑结果>.rename: 导出前对列名做映射")
    lines.append("- column_layouts.<逻辑结果>.keep / order: 调整保留列与列顺序")
    return "\n".join(lines)


def _field_spec_map(rule: dict[str, Any]) -> dict[str, EditableFieldSpec]:
    return {spec.path: spec for spec in editable_field_specs_for_rule(rule)}


def _report_format_field_spec_map() -> dict[str, EditableFieldSpec]:
    return {spec.path: spec for spec in report_format_field_specs()}


def _get_by_path(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in [x for x in path.split(".") if x]:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _set_by_path(data: dict[str, Any], path: str, value: Any) -> None:
    parts = [x for x in path.split(".") if x]
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce_by_spec(value: Any, spec: EditableFieldSpec) -> Any:
    if spec.value_type == "string":
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"字段 {spec.path} 不能为空")
        if len(text) > 200:
            raise ValueError(f"字段 {spec.path} 长度不能超过 200")
        return text
    if spec.value_type == "sheet_name":
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"字段 {spec.path} 不能为空")
        if len(text) > 31:
            raise ValueError(f"字段 {spec.path} 不能超过 31 个字符")
        if any(ch in _EXCEL_SHEET_INVALID_CHARS for ch in text):
            raise ValueError(f"字段 {spec.path} 不能包含 []:*?/\\")
        return text
    if spec.value_type == "enum":
        text = str(value or "").strip()
        if text not in spec.choices:
            raise ValueError(f"字段 {spec.path} 只允许：{', '.join(spec.choices)}")
        return text
    if spec.value_type == "number":
        try:
            number = float(value)
        except Exception as e:
            raise ValueError(f"字段 {spec.path} 必须是数字") from e
        if spec.min_value is not None and number < spec.min_value:
            raise ValueError(f"字段 {spec.path} 不能小于 {spec.min_value}")
        if spec.max_value is not None and number > spec.max_value:
            raise ValueError(f"字段 {spec.path} 不能大于 {spec.max_value}")
        return int(number) if float(number).is_integer() else number
    if spec.value_type == "string_list":
        if isinstance(value, str):
            items = [x for x in [v.strip() for v in value.split(",")] if x]
        elif isinstance(value, list):
            items = [str(x).strip() for x in value if str(x).strip()]
        else:
            raise ValueError(f"字段 {spec.path} 必须是字符串列表")
        if not items:
            raise ValueError(f"字段 {spec.path} 不能为空列表")
        if len(items) > 50:
            raise ValueError(f"字段 {spec.path} 列表项不能超过 50 个")
        return items
    if spec.value_type == "string_map":
        if not isinstance(value, dict) or not value:
            raise ValueError(f"字段 {spec.path} 必须是非空对象")
        out: dict[str, str] = {}
        for k, v in value.items():
            key = str(k).strip()
            val = str(v or "").strip()
            if not key or not val:
                raise ValueError(f"字段 {spec.path} 的键和值都不能为空")
            if len(key) > 100 or len(val) > 100:
                raise ValueError(f"字段 {spec.path} 的键和值长度都不能超过 100")
            out[key] = val
        return out
    return value


def _normalize_rule_id(value: Any) -> tuple[str, list[str]]:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("新增规则必须提供 id")
    norm = re.sub(r"[^A-Za-z0-9_]+", "_", raw.upper()).strip("_")
    if not norm:
        raise ValueError("新增规则 id 只允许英文、数字、下划线")
    warnings: list[str] = []
    if norm != raw:
        warnings.append(f"规则ID 已规范化：{raw} -> {norm}")
    return norm, warnings


def _normalize_update_set(rule: dict[str, Any], set_data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    warnings: list[str] = []
    spec_map = _field_spec_map(rule)
    if not isinstance(set_data, dict) or not set_data:
        raise ValueError("update_check.set 必须是非空对象")
    for key, value in set_data.items():
        if key in {"id", "type", "scope", "source"}:
            raise ValueError(f"当前阶段不允许修改 {key}")
        if key == "params":
            if not isinstance(value, dict) or not value:
                raise ValueError("params 必须是非空对象")
            params_out: dict[str, Any] = {}
            for p_key, p_val in value.items():
                path = f"params.{p_key}"
                spec = spec_map.get(path)
                if spec is None:
                    raise ValueError(f"当前阶段不允许修改字段：{path}")
                params_out[p_key] = _coerce_by_spec(p_val, spec)
            out["params"] = params_out
            continue
        spec = spec_map.get(key)
        if spec is None:
            raise ValueError(f"当前阶段不允许修改字段：{key}")
        coerced = _coerce_by_spec(value, spec)
        out[key] = coerced
    if not out:
        raise ValueError("未识别到可修改字段")
    return out, warnings


def _normalize_report_format_fields(fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    out: dict[str, Any] = {}
    warnings: list[str] = []
    spec_map = _report_format_field_spec_map()
    if not isinstance(fields, dict) or not fields:
        raise ValueError("set_report_format.fields 必须是非空对象")

    unknown_top = [str(k) for k in fields.keys() if str(k) not in {"sheet_names", "column_layouts"}]
    if unknown_top:
        raise ValueError(f"当前阶段不允许修改这些报表格式字段：{', '.join(unknown_top)}")

    sheet_names = fields.get("sheet_names")
    if sheet_names is not None:
        if not isinstance(sheet_names, dict) or not sheet_names:
            raise ValueError("sheet_names 必须是非空对象")
        normalized_sheet_names: dict[str, Any] = {}
        for logical_key, raw_value in sheet_names.items():
            path = f"sheet_names.{str(logical_key).strip()}"
            spec = spec_map.get(path)
            if spec is None:
                allowed = "、".join(_REPORT_FORMAT_LABELS.keys())
                raise ValueError(f"当前阶段只允许修改这些 sheet_names：{allowed}")
            normalized_sheet_names[str(logical_key).strip()] = _coerce_by_spec(raw_value, spec)
        out["sheet_names"] = normalized_sheet_names

    column_layouts = fields.get("column_layouts")
    if column_layouts is not None:
        if not isinstance(column_layouts, dict) or not column_layouts:
            raise ValueError("column_layouts 必须是非空对象")
        normalized_layouts: dict[str, dict[str, Any]] = {}
        for logical_key, layout in column_layouts.items():
            logical_key = str(logical_key).strip()
            if logical_key not in _REPORT_FORMAT_LABELS:
                allowed = "、".join(_REPORT_FORMAT_LABELS.keys())
                raise ValueError(f"当前阶段只允许修改这些 column_layouts：{allowed}")
            if not isinstance(layout, dict) or not layout:
                raise ValueError(f"column_layouts.{logical_key} 必须是非空对象")
            normalized_layout: dict[str, Any] = {}
            for sub_key, raw_value in layout.items():
                sub_key = str(sub_key).strip()
                path = f"column_layouts.{logical_key}.{sub_key}"
                spec = spec_map.get(path)
                if spec is None:
                    raise ValueError(f"当前阶段不允许修改字段：{path}")
                normalized_layout[sub_key] = _coerce_by_spec(raw_value, spec)
            normalized_layouts[logical_key] = normalized_layout
        out["column_layouts"] = normalized_layouts

    if not out:
        raise ValueError("未识别到可修改的报表格式字段")
    return out, warnings


def _normalize_add_check(
    raw_check: dict[str, Any],
    *,
    existing_ids: set[str],
) -> tuple[dict[str, Any], list[str], str, list[str], str]:
    if not isinstance(raw_check, dict):
        raise ValueError("add_check.check 必须是对象")
    rule_type = str(raw_check.get("type", "")).strip()
    template = _template_checks_by_type().get(rule_type)
    if not rule_type or not isinstance(template, dict):
        allowed = ", ".join(addable_rule_types())
        raise ValueError(f"当前阶段仅支持基于内置模板新增规则，type 只允许：{allowed}")
    rule_id, warnings = _normalize_rule_id(raw_check.get("id", ""))
    if rule_id in existing_ids:
        raise ValueError(f"新增规则 id 已存在：{rule_id}")

    out = copy.deepcopy(template)
    out["id"] = rule_id
    if "short_name" in out and not str(out.get("short_name", "")).strip():
        out.pop("short_name", None)

    raw_override = {k: v for k, v in raw_check.items() if k not in {"id", "type", "scope", "source"}}
    normalized_set, inner_warnings = _normalize_update_set(template, raw_override)
    warnings.extend(inner_warnings)
    out = _apply_patch_dict({"checks": [out]}, {"actions": [{"op": "update_check", "id": rule_id, "set": normalized_set}]})["checks"][0]
    source = dict(out.get("source", {}) or {})
    source["doc"] = "（AI模板新增）"
    source["clause"] = str(out.get("short_name") or out.get("description") or rule_id).strip()[:40]
    out["source"] = source

    spec_map = _field_spec_map(template)
    changed_paths: list[str] = ["id"]
    for top_key in normalized_set.keys():
        if top_key == "params":
            changed_paths.extend([f"params.{k}" for k in normalized_set["params"].keys()])
        else:
            changed_paths.append(top_key)
    impact_lines = [
        f"新增规则 {rule_id}（模板类型={rule_type}，范围={str(template.get('scope', '')) or '未定义'}）",
    ]
    for path in changed_paths:
        if path == "id":
            impact_lines.append(f"- 规则ID：新建为 {rule_id!r}，风险=中")
            continue
        spec = spec_map[path]
        before_v = _get_by_path(template, path)
        after_v = _get_by_path(out, path)
        impact_lines.append(f"- {spec.label}（{path}）：{before_v!r} -> {after_v!r}，风险={spec.risk}")
    return out, warnings, SAFE_ADD_CHECK_BASE_RISK, impact_lines, rule_id


def _risk_rank(level: str) -> int:
    return {"低": 0, "中": 1, "高": 2}.get(level, 1)


def _build_impact_lines(before_rule: dict[str, Any], after_rule: dict[str, Any], changed_paths: list[str], spec_map: dict[str, EditableFieldSpec]) -> tuple[list[str], str]:
    lines: list[str] = []
    max_risk = "低"
    for path in changed_paths:
        spec = spec_map[path]
        before_v = _get_by_path(before_rule, path)
        after_v = _get_by_path(after_rule, path)
        lines.append(f"- {spec.label}（{path}）：{before_v!r} -> {after_v!r}，风险={spec.risk}")
        if _risk_rank(spec.risk) > _risk_rank(max_risk):
            max_risk = spec.risk
    return lines, max_risk


def _build_report_format_impact_lines(
    before_format: dict[str, Any],
    after_format: dict[str, Any],
    changed_paths: list[str],
    spec_map: dict[str, EditableFieldSpec],
) -> tuple[list[str], str]:
    lines: list[str] = []
    max_risk = "低"
    for path in changed_paths:
        spec = spec_map[path]
        before_v = _get_by_path(before_format, path)
        after_v = _get_by_path(after_format, path)
        lines.append(f"- {spec.label}（{path}）：{before_v!r} -> {after_v!r}，风险={spec.risk}")
        if _risk_rank(spec.risk) > _risk_rank(max_risk):
            max_risk = spec.risk
    return lines, max_risk


def _count_frames_by_rule(rule_ids: set[str], *frames: pd.DataFrame) -> dict[str, int]:
    counts = {rid: 0 for rid in rule_ids}
    for frame in frames:
        if frame is None or frame.empty or "规则ID" not in frame.columns:
            continue
        series = frame["规则ID"].astype(str)
        for rid in rule_ids:
            counts[rid] += int((series == rid).sum())
    return counts


def _simulate_patch_effect(workdir: Path, rules_path: Path, normalized_patch: dict[str, Any], changed_rule_ids: list[str]) -> str:
    ctx = load_audit_context(workdir=workdir, rules_path=rules_path, target_month=None, logger=None)
    before_frames = run_checks(
        rules=ctx.rules,
        df_aux=ctx.df_aux,
        df_income=ctx.df_income,
        df_mapping=ctx.df_mapping,
        target_month=int(ctx.target_month),
    )
    after_data = _apply_patch_dict(_load_yaml(rules_path), normalized_patch)
    after_rules = load_rules_data(after_data)
    after_frames = run_checks(
        rules=after_rules,
        df_aux=ctx.df_aux,
        df_income=ctx.df_income,
        df_mapping=ctx.df_mapping,
        target_month=int(ctx.target_month),
    )
    before_totals = [0 if f is None or f.empty else len(f) for f in before_frames]
    after_totals = [0 if f is None or f.empty else len(f) for f in after_frames]
    lines = [f"预演（当前工作目录，目标月={int(ctx.target_month)}）："]
    labels = ["辅助账规则违规", "辅助账疑似错科目", "收入成本维度异常", "收入成本毛利异常"]
    for label, before_n, after_n in zip(labels, before_totals, after_totals):
        delta = int(after_n - before_n)
        if delta == 0:
            lines.append(f"- {label}：{before_n} -> {after_n}")
        else:
            sign = "+" if delta > 0 else ""
            lines.append(f"- {label}：{before_n} -> {after_n}（{sign}{delta}）")
    changed_set = set(changed_rule_ids)
    if changed_set:
        before_rule_hits = _count_frames_by_rule(changed_set, *before_frames)
        after_rule_hits = _count_frames_by_rule(changed_set, *after_frames)
        lines.append("- 变更规则命中数：")
        for rid in changed_rule_ids:
            b = before_rule_hits.get(rid, 0)
            a = after_rule_hits.get(rid, 0)
            delta = a - b
            if delta == 0:
                lines.append(f"  - {rid}: {b} -> {a}")
            else:
                sign = "+" if delta > 0 else ""
                lines.append(f"  - {rid}: {b} -> {a}（{sign}{delta}）")
    return "\n".join(lines)


def review_ai_rule_patch(rules_path: Path, patch: dict[str, Any], workdir: Optional[Path] = None) -> RulePatchReview:
    if not rules_path.exists():
        return RulePatchReview(False, f"规则文件不存在：{rules_path}", {"actions": []}, "", "", "中", "")
    try:
        before_data = _load_yaml(rules_path)
        before_rules = load_rules_data(before_data)
        actions = patch.get("actions", []) or []
        if not isinstance(actions, list) or not actions:
            raise ValueError("patch.actions 必须是非空 list")
        normalized_actions: list[dict[str, Any]] = []
        impact_sections: list[str] = []
        changed_rule_ids: list[str] = []
        max_risk = "低"
        needs_simulation = False
        checks_map = {str((c or {}).get("id", "")).strip(): dict(c) for c in before_rules.checks if str((c or {}).get("id", "")).strip()}
        for idx, act in enumerate(actions, start=1):
            if not isinstance(act, dict):
                raise ValueError(f"actions[{idx}] 必须是对象")
            item = _normalize_patch_action(act)
            op = str(item.get("op", "")).strip()
            if op not in SAFE_ALLOWED_OPS:
                raise ValueError("当前阶段仅支持安全调参、模板化新增规则或安全的报表格式改动，不支持删除规则或 AI 配置改动")
            if op == "update_check":
                rid = str(item.get("id", "")).strip()
                if not rid or rid not in checks_map:
                    raise ValueError(f"actions[{idx}] update_check 未找到规则：{rid}")
                before_rule = checks_map[rid]
                raw_set = item.get("set", {})
                normalized_set, warnings = _normalize_update_set(before_rule, raw_set)
                normalized_item = {"op": "update_check", "id": rid, "set": normalized_set}
                normalized_actions.append(normalized_item)
                after_rule = _apply_patch_dict({"checks": [before_rule]}, {"actions": [normalized_item]})["checks"][0]
                spec_map = _field_spec_map(before_rule)
                changed_paths: list[str] = []
                for top_key in normalized_set.keys():
                    if top_key == "params":
                        changed_paths.extend([f"params.{k}" for k in normalized_set["params"].keys()])
                    else:
                        changed_paths.append(top_key)
                lines, risk = _build_impact_lines(before_rule, after_rule, changed_paths, spec_map)
                if _risk_rank(risk) > _risk_rank(max_risk):
                    max_risk = risk
                title = f"规则 {rid}（{str(before_rule.get('short_name') or before_rule.get('description') or rid)}）"
                section = [title] + lines
                if warnings:
                    section.extend([f"- 提示：{w}" for w in warnings])
                impact_sections.append("\n".join(section))
                changed_rule_ids.append(rid)
                needs_simulation = True
                continue

            if op == "set_report_format":
                raw_fields = item.get("fields", {})
                normalized_fields, warnings = _normalize_report_format_fields(raw_fields)
                normalized_item = {"op": "set_report_format", "fields": normalized_fields}
                normalized_actions.append(normalized_item)
                before_format = dict(before_data.get("report_format", {}) or {})
                after_format = _apply_patch_dict({"checks": [], "report_format": before_format}, {"actions": [normalized_item]}).get(
                    "report_format", {}
                )
                spec_map = _report_format_field_spec_map()
                changed_paths: list[str] = []
                if "sheet_names" in normalized_fields:
                    changed_paths.extend([f"sheet_names.{k}" for k in normalized_fields["sheet_names"].keys()])
                if "column_layouts" in normalized_fields:
                    for logical_key, layout in normalized_fields["column_layouts"].items():
                        changed_paths.extend([f"column_layouts.{logical_key}.{sub_key}" for sub_key in layout.keys()])
                lines, risk = _build_report_format_impact_lines(before_format, after_format, changed_paths, spec_map)
                if _risk_rank(risk) > _risk_rank(max_risk):
                    max_risk = risk
                section = ["报表输出格式"] + lines
                if warnings:
                    section.extend([f"- 提示：{w}" for w in warnings])
                impact_sections.append("\n".join(section))
                continue

            raw_check = item.get("check")
            normalized_check, warnings, risk, lines, rid = _normalize_add_check(
                raw_check,
                existing_ids=set(checks_map.keys()).union(changed_rule_ids),
            )
            normalized_item = {"op": "add_check", "check": normalized_check}
            normalized_actions.append(normalized_item)
            if _risk_rank(risk) > _risk_rank(max_risk):
                max_risk = risk
            section = lines[:]
            if warnings:
                section.extend([f"- 提示：{w}" for w in warnings])
            impact_sections.append("\n".join(section))
            changed_rule_ids.append(rid)
            needs_simulation = True
        normalized_patch = {"actions": normalized_actions}
        after_data = _apply_patch_dict(before_data, normalized_patch)
        before_text = _dump_yaml(before_data).splitlines(keepends=True)
        after_text = _dump_yaml(after_data).splitlines(keepends=True)
        diff_text = "".join(
            difflib.unified_diff(before_text, after_text, fromfile=str(rules_path.name), tofile=f"{rules_path.stem}_patched.yaml", lineterm="")
        ) or "(无变化)"
        simulation_text = ""
        if workdir is not None and needs_simulation:
            simulation_text = _simulate_patch_effect(workdir=workdir, rules_path=rules_path, normalized_patch=normalized_patch, changed_rule_ids=changed_rule_ids)
        elif workdir is not None and any(str((x or {}).get("op", "")).strip() == "set_report_format" for x in normalized_actions):
            simulation_text = "本次仅调整报表输出格式，不影响规则命中结果，未执行样例重跑。"
        impact_text = "\n\n".join(impact_sections)
        return RulePatchReview(
            ok=True,
            message="规则补丁预览成功。",
            normalized_patch=normalized_patch,
            diff_text=diff_text,
            impact_text=impact_text,
            risk_level=max_risk,
            simulation_text=simulation_text,
            changed_rule_ids=tuple(changed_rule_ids),
        )
    except Exception as e:
        return RulePatchReview(False, f"规则补丁预览失败：{type(e).__name__}: {e}", {"actions": []}, "", "", "中", "")

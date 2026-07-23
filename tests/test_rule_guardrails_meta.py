from __future__ import annotations

from voucher_audit.rule_guardrails import (
    SAFE_SEVERITIES,
    addable_field_meta_for_rule_type,
    addable_rule_types,
    editable_field_meta_for_report_format,
    editable_field_meta_for_rule,
    editable_field_specs_for_rule,
)


def test_addable_rule_types_includes_current_and_legacy() -> None:
    types = set(addable_rule_types())
    assert "pp_change" in types
    assert "customer_consistency_check" in types
    assert "metric_pp_change" in types


def test_editable_field_specs_include_severity_choices() -> None:
    rule = {"type": "rev_cost_zero_mismatch", "params": {"eps": 1e-6}}
    specs = editable_field_specs_for_rule(rule)
    paths = {s.path for s in specs}
    assert "severity" in paths
    severity = next(s for s in specs if s.path == "severity")
    assert set(severity.choices) == set(SAFE_SEVERITIES)


def test_editable_field_meta_for_rule_is_json_friendly() -> None:
    rule = {"type": "pp_change", "params": {}}
    meta = editable_field_meta_for_rule(rule)
    assert isinstance(meta, list)
    assert all(isinstance(item, dict) and "path" in item for item in meta)


def test_addable_field_meta_for_known_type() -> None:
    meta = addable_field_meta_for_rule_type("rev_cost_zero_mismatch")
    paths = {m["path"] for m in meta}
    assert "id" in paths


def test_report_format_editable_meta_non_empty() -> None:
    meta = editable_field_meta_for_report_format()
    assert meta
    assert any("sheet" in str(m.get("path", "")).lower() or "sheet" in str(m.get("label", "")).lower() for m in meta)

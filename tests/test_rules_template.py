from __future__ import annotations

import yaml

from voucher_audit.rule_guardrails import addable_rule_types
from voucher_audit.rules_template import TEMPLATE_YAML, build_template_rules


def test_template_includes_current_six_rules() -> None:
    data = build_template_rules()
    ids = {str(c.get("id")) for c in data.get("checks") or []}
    assert {
        "INC_CUSTOMER_CONSISTENCY",
        "INC_REV_COST_ZERO_MISMATCH",
        "AUX_HEADCOUNT_DATA_CHECK",
        "INC_NEG_GM_HIGH_RATIO",
        "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO",
        "INC_PP_CHANGE",
    }.issubset(ids)


def test_template_yaml_parses_and_exposes_legacy_addable_types() -> None:
    data = yaml.safe_load(TEMPLATE_YAML) or {}
    types = {str(c.get("type")) for c in data.get("checks") or []}
    assert "pp_change" in types
    assert "customer_consistency_check" in types
    # legacy still addable for AI guardrails
    assert "metric_pp_change" in types
    assert "combo_drift" in types

    addable = set(addable_rule_types())
    assert "pp_change" in addable
    assert "headcount_data_check" in addable

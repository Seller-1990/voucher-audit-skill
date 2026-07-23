from __future__ import annotations

from voucher_audit.source_annotation import _INCOME_RULE_SPECS


def test_income_annotation_specs_cover_current_rules() -> None:
    required = {
        "INC_CUSTOMER_CONSISTENCY",
        "INC_REV_COST_ZERO_MISMATCH",
        "INC_NEG_GM_HIGH_RATIO",
        "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO",
        "INC_PP_CHANGE",
    }
    assert required.issubset(set(_INCOME_RULE_SPECS))


def test_pp_change_spec_uses_current_key_fields() -> None:
    spec = _INCOME_RULE_SPECS["INC_PP_CHANGE"]
    assert "主体账簿" in spec.key_columns
    assert "三级科目" in spec.key_columns
    assert "净额收入" in spec.highlight_columns

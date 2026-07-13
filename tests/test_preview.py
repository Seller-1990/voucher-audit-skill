from __future__ import annotations

from voucher_audit.preview import build_preview_items
from voucher_audit.config import load_rules_data


def test_preview_extracts_fields_for_combo_drift() -> None:
    rules = load_rules_data(
        {
            "inputs": {"files": {}, "sheets": {}, "columns": {}},
            "thresholds": {},
            "ai": {},
            "report_format": {},
            "checks": [
                {
                    "id": "R1",
                    "type": "combo_drift",
                    "scope": "income_cost",
                    "params": {"key_fields": ["主体账簿"], "value_fields": ["部门"], "amount_field": "净额收入"},
                }
            ],
        }
    )
    items = build_preview_items(rules)
    assert items[0].fields == ("主体账簿", "部门", "净额收入")
    assert items[0].output_logical_sheet == "income_dim_anomalies"

from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_conflicts import auto_fix_conflicts, detect_conflicts
from tools.edit_rules import load_batch_checks, merge_batch_checks


def test_auto_fix_conflicts_keeps_first_duplicate_and_unrelated_rules() -> None:
    rules = {
        "checks": [
            {"id": "A", "type": "one", "scope": "x", "params": {}},
            {"id": "B", "type": "two", "scope": "y", "params": {}},
            {"id": "A", "type": "three", "scope": "z", "params": {}},
        ]
    }
    conflicts = detect_conflicts(rules)

    assert len([item for item in conflicts if item["type"] == "duplicate_id"]) == 1
    assert auto_fix_conflicts(conflicts, rules) == 1
    assert [item["id"] for item in rules["checks"]] == ["A", "B"]


def test_auto_fix_conflicts_removes_second_high_overlap_rule() -> None:
    rules = {
        "checks": [
            {"id": "A", "type": "same", "scope": "x", "params": {"key_fields": ["客户"]}},
            {"id": "B", "type": "same", "scope": "x", "params": {"key_fields": ["客户"]}},
        ]
    }
    conflicts = detect_conflicts(rules)

    assert auto_fix_conflicts(conflicts, rules) == 1
    assert [item["id"] for item in rules["checks"]] == ["A"]


def test_structured_batch_rules_merge_without_interactive_parsing(tmp_path: Path) -> None:
    batch = tmp_path / "rules.yaml"
    batch.write_text("checks:\n  - id: B\n    type: pp_change\n", encoding="utf-8")

    incoming = load_batch_checks(batch)
    merged = merge_batch_checks([{"id": "A"}], incoming)

    assert [item["id"] for item in merged] == ["A", "B"]


def test_structured_batch_rejects_duplicate_ids(tmp_path: Path) -> None:
    batch = tmp_path / "rules.yaml"
    batch.write_text("checks:\n  - id: A\n", encoding="utf-8")

    with pytest.raises(ValueError, match="ID 重复"):
        merge_batch_checks([{"id": "A"}], load_batch_checks(batch))

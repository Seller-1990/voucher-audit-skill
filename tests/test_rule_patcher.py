from __future__ import annotations

from pathlib import Path

from voucher_audit.rule_guardrails import review_ai_rule_patch
from voucher_audit.rule_patcher import apply_rule_patch_versioned, preview_rule_patch


def _write_rules(path: Path) -> None:
    path.write_text(
        """checks:
  - id: TEST_RULE
    name: 测试规则
    type: rev_cost_zero_mismatch
    scope: income_cost
    severity: 需确认
    description: 测试
    params:
      min_revenue_abs: 1
""",
        encoding="utf-8",
    )


def test_rule_patch_preview_and_versioned_apply(tmp_path: Path) -> None:
    rules_path = tmp_path / "audit_rules.yaml"
    _write_rules(rules_path)
    patch = {"actions": [{"op": "update_check", "id": "TEST_RULE", "set": {"severity": "错误"}}]}

    preview = preview_rule_patch(rules_path, patch)
    applied = apply_rule_patch_versioned(rules_path, patch, tmp_path / "versions")

    assert preview.ok
    assert "severity" in preview.diff_text
    assert applied.ok
    assert applied.new_rules_path is not None
    assert "severity: 错误" in applied.new_rules_path.read_text(encoding="utf-8")


def test_guardrails_reject_unknown_edit_field(tmp_path: Path) -> None:
    rules_path = tmp_path / "audit_rules.yaml"
    _write_rules(rules_path)
    patch = {"actions": [{"op": "update_check", "id": "TEST_RULE", "set": {"unknown": 1}}]}

    review = review_ai_rule_patch(rules_path, patch)

    assert not review.ok
    assert "unknown" in review.message

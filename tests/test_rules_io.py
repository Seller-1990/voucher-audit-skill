from __future__ import annotations

import json
from pathlib import Path

from voucher_audit.rules_io import compile_rules, load_active_pointer


def test_compile_rules_overrides_checks() -> None:
    app = {"inputs": {"files": {}}, "checks": [{"id": "OLD"}]}
    audit = {"checks": [{"id": "NEW"}]}
    out = compile_rules(app, audit)
    assert out["checks"][0]["id"] == "NEW"


def test_load_active_pointer_returns_none_when_missing(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "rules").mkdir()
    assert load_active_pointer(repo) is None


def test_load_active_pointer_ok(tmp_path: Path) -> None:
    repo = tmp_path
    rules = repo / "rules"
    rules.mkdir()
    (rules / "versions").mkdir()

    app = rules / "versions" / "app.yaml"
    audit = rules / "versions" / "audit.yaml"
    compiled = rules / "versions" / "compiled.yaml"
    app.write_text("inputs: {}\n", encoding="utf-8")
    audit.write_text("checks: []\n", encoding="utf-8")
    compiled.write_text("inputs: {}\nchecks: []\n", encoding="utf-8")

    pointer = {
        "active": {
            "app_rules": str(app.relative_to(repo)),
            "audit_rules": str(audit.relative_to(repo)),
            "compiled_rules": str(compiled.relative_to(repo)),
        }
    }
    (rules / "active_rules.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")

    got = load_active_pointer(repo)
    assert got is not None
    assert got.compiled_rules.name == "compiled.yaml"

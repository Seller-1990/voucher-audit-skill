from __future__ import annotations

import json
from pathlib import Path

import pytest

from voucher_audit.rules_io import compile_rules, ensure_rules_root, load_active_pointer


def test_ensure_rules_root_uses_checkout_rules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rules = repo / "rules"
    rules.mkdir(parents=True)
    for filename in ("app_rules.yaml", "audit_rules.yaml"):
        (rules / filename).write_text("checks: []\n", encoding="utf-8")

    assert ensure_rules_root(repo, user_data_root=tmp_path / "config") == repo.resolve()


def test_ensure_rules_root_seeds_packaged_rules(tmp_path: Path) -> None:
    runtime_root = tmp_path / "config"

    result = ensure_rules_root(tmp_path / "installed", user_data_root=runtime_root)

    assert result == runtime_root.resolve()
    assert (runtime_root / "rules" / "app_rules.yaml").is_file()
    assert (runtime_root / "rules" / "audit_rules.yaml").is_file()

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


def test_load_active_pointer_rejects_paths_outside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rules = repo / "rules"
    rules.mkdir(parents=True)
    outside = tmp_path / "outside.yaml"
    outside.write_text("checks: []\n", encoding="utf-8")

    pointer = {
        "active": {
            "app_rules": "../../outside.yaml",
            "audit_rules": "../../outside.yaml",
            "compiled_rules": "../../outside.yaml",
        }
    }
    (rules / "active_rules.json").write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(ValueError, match="规则路径必须位于仓库内"):
        load_active_pointer(repo)


def test_packaged_default_rules_match_repository_rules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for filename in ("app_rules.yaml", "audit_rules.yaml"):
        assert (repo_root / "voucher_audit" / "default_rules" / filename).read_bytes() == (
            repo_root / "rules" / filename
        ).read_bytes()

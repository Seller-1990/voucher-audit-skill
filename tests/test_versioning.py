from __future__ import annotations

from pathlib import Path

from voucher_audit.versioning import update_active_pointer, write_version_snapshot


def test_write_snapshot_and_pointer(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "rules" / "versions").mkdir(parents=True)

    snap = write_version_snapshot(
        repo_root=repo,
        app_rules={"inputs": {}},
        audit_rules={"checks": []},
        compiled_rules={"inputs": {}, "checks": []},
    )
    assert snap.compiled_rules_path.exists()

    p = update_active_pointer(repo, snap)
    assert p.exists()
    assert "compiled_rules" in p.read_text(encoding="utf-8")

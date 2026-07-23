from __future__ import annotations

import argparse
import json
from pathlib import Path

from voucher_audit import cli


def test_rules_set_active_rejects_outside_repo(monkeypatch, tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    rules = repo / "rules"
    rules.mkdir(parents=True)
    inside = rules / "a.yaml"
    inside.write_text("x: 1\n", encoding="utf-8")
    outside = tmp_path / "outside.yaml"
    outside.write_text("x: 1\n", encoding="utf-8")

    monkeypatch.setattr(cli, "repo_root_from_module", lambda: repo)

    args = argparse.Namespace(app=str(outside), audit=str(inside), compiled=str(inside), yes=True)
    assert cli.cmd_rules_set_active(args) == 2
    assert "规则路径必须位于仓库内" in capsys.readouterr().err


def test_rules_set_active_writes_relative_pointer(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rules = repo / "rules" / "versions"
    rules.mkdir(parents=True)
    app = rules / "app.yaml"
    audit = rules / "audit.yaml"
    compiled = rules / "compiled.yaml"
    for p in (app, audit, compiled):
        p.write_text("checks: []\n", encoding="utf-8")

    monkeypatch.setattr(cli, "repo_root_from_module", lambda: repo)

    args = argparse.Namespace(app=str(app), audit=str(audit), compiled=str(compiled), yes=True)
    assert cli.cmd_rules_set_active(args) == 0

    pointer = json.loads((repo / "rules" / "active_rules.json").read_text(encoding="utf-8"))
    # path relative_to may use OS separator; normalize
    assert Path(pointer["active"]["app_rules"]).as_posix() == "rules/versions/app.yaml"
    assert Path(pointer["active"]["audit_rules"]).as_posix() == "rules/versions/audit.yaml"
    assert Path(pointer["active"]["compiled_rules"]).as_posix() == "rules/versions/compiled.yaml"

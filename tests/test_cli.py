from __future__ import annotations

import argparse
import builtins
from pathlib import Path

from voucher_audit import cli


def test_run_reports_missing_openai_dependency(monkeypatch, tmp_path: Path, capsys) -> None:
    compiled = tmp_path / "compiled_rules.yaml"
    compiled.write_text("checks: []\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_load_rules_for_execution", lambda _root: (compiled, {"annotation_policy": {}}))
    monkeypatch.setattr(cli, "ensure_compiled_rules", lambda _root: object())
    monkeypatch.setattr(cli, "load_compiled_rule_config", lambda _paths: object())
    monkeypatch.setattr(cli, "build_preview_items", lambda _rules: [])

    real_import = builtins.__import__

    def reject_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("openai is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_openai)
    args = argparse.Namespace(
        workdir=str(tmp_path),
        yes=True,
        month=None,
        include_rule_id=None,
        enable_ai=True,
        openai_api_key=None,
        openai_base_url=None,
        openai_model=None,
        annotate=False,
        yes_annotate=False,
    )

    assert cli.cmd_run(args) == 1
    assert "AI功能需要安装 openai 库" in capsys.readouterr().err

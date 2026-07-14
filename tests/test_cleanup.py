from __future__ import annotations

from pathlib import Path

from voucher_audit import cli


def _make_cleanup_targets(root: Path) -> tuple[Path, Path]:
    temp_dir = root / "temp_debug"
    output_dir = root / "凭证审核输出"
    temp_dir.mkdir()
    output_dir.mkdir()
    (temp_dir / "scratch.txt").write_text("temp", encoding="utf-8")
    (output_dir / "report.xlsx").write_bytes(b"report")
    return temp_dir, output_dir


def test_cleanup_requires_explicit_confirmation(tmp_path: Path, capsys) -> None:
    temp_dir, output_dir = _make_cleanup_targets(tmp_path)

    assert cli.main(["cleanup", "--workdir", str(tmp_path)]) == 2

    assert temp_dir.exists()
    assert output_dir.exists()
    assert "--yes" in capsys.readouterr().err


def test_cleanup_dry_run_never_deletes(tmp_path: Path) -> None:
    temp_dir, output_dir = _make_cleanup_targets(tmp_path)

    assert cli.main(["cleanup", "--workdir", str(tmp_path), "--dry-run"]) == 0

    assert temp_dir.exists()
    assert output_dir.exists()


def test_cleanup_deletes_only_confirmed_workdir_targets(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    temp_dir, output_dir = _make_cleanup_targets(workdir)
    outside = tmp_path / "temp_debug"
    outside.mkdir()

    assert cli.main(["cleanup", "--workdir", str(workdir), "--yes"]) == 0

    assert not temp_dir.exists()
    assert not output_dir.exists()
    assert outside.exists()

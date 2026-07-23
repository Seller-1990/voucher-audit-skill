from __future__ import annotations

from pathlib import Path

from voucher_audit import cli
from voucher_audit.cleanup import cleanup_targets


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
    err = capsys.readouterr().err
    assert "--yes" in err
    assert "临时目录" in err


def test_cleanup_dry_run_never_deletes(tmp_path: Path) -> None:
    temp_dir, output_dir = _make_cleanup_targets(tmp_path)

    assert cli.main(["cleanup", "--workdir", str(tmp_path), "--dry-run"]) == 0

    assert temp_dir.exists()
    assert output_dir.exists()


def test_cleanup_default_keeps_report_output(tmp_path: Path, capsys) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    temp_dir, output_dir = _make_cleanup_targets(workdir)

    assert cli.main(["cleanup", "--workdir", str(workdir), "--yes"]) == 0

    assert not temp_dir.exists()
    assert output_dir.exists()
    assert "--include-reports" in capsys.readouterr().err


def test_cleanup_include_reports_deletes_report_dir(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    temp_dir, output_dir = _make_cleanup_targets(workdir)
    outside = tmp_path / "temp_debug"
    outside.mkdir()

    assert cli.main(["cleanup", "--workdir", str(workdir), "--yes", "--include-reports"]) == 0

    assert not temp_dir.exists()
    assert not output_dir.exists()
    assert outside.exists()


def test_cleanup_targets_api_respects_include_reports(tmp_path: Path) -> None:
    temp_dir, output_dir = _make_cleanup_targets(tmp_path)
    default_targets = cleanup_targets(tmp_path)
    assert temp_dir in default_targets
    assert output_dir not in default_targets

    with_reports = cleanup_targets(tmp_path, include_reports=True)
    assert temp_dir in with_reports
    assert output_dir in with_reports


def test_delete_cleanup_targets_rejects_unknown_names(tmp_path: Path) -> None:
    from voucher_audit.cleanup import delete_cleanup_targets

    evil = tmp_path / "important_data"
    evil.mkdir()
    (evil / "keep.txt").write_text("x", encoding="utf-8")

    result = delete_cleanup_targets((evil,))
    assert evil.exists()
    assert result.deleted == ()
    assert result.failed
    assert "白名单" in result.failed[0][1]

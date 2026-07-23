from __future__ import annotations

from pathlib import Path

import pytest

from voucher_audit.security import (
    backup_file,
    ensure_no_open_workbook,
    excel_lock_sidecar_path,
    restore_from_backup,
)


def test_backup_file_keeps_original_extension(tmp_path: Path) -> None:
    source = tmp_path / "数据汇总.xlsx"
    source.write_bytes(b"PK-original")

    backup = backup_file(source)

    assert backup.name == "数据汇总.xlsx.bak"
    assert backup.read_bytes() == b"PK-original"
    assert source.exists()


def test_backup_file_uses_timestamp_when_default_exists(tmp_path: Path) -> None:
    source = tmp_path / "考核表输出.xlsx"
    source.write_bytes(b"PK-v2")
    (tmp_path / "考核表输出.xlsx.bak").write_bytes(b"PK-old")

    backup = backup_file(source)

    assert backup.name.startswith("考核表输出.xlsx.")
    assert backup.name.endswith(".bak")
    assert backup.name != "考核表输出.xlsx.bak"
    assert backup.read_bytes() == b"PK-v2"


def test_restore_from_backup_prefers_explicit_path(tmp_path: Path) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"broken")
    backup = tmp_path / "source.xlsx.custom.bak"
    backup.write_bytes(b"good")

    restore_from_backup(source, backup_path=backup)

    assert source.read_bytes() == b"good"


def test_restore_from_backup_falls_back_to_legacy_suffix(tmp_path: Path) -> None:
    source = tmp_path / "legacy.xlsx"
    source.write_bytes(b"broken")
    (tmp_path / "legacy.bak").write_bytes(b"legacy-good")

    restore_from_backup(source)

    assert source.read_bytes() == b"legacy-good"


def test_restore_from_backup_missing_raises(tmp_path: Path) -> None:
    source = tmp_path / "missing.xlsx"
    source.write_bytes(b"x")
    with pytest.raises(FileNotFoundError):
        restore_from_backup(source)


def test_ensure_no_open_workbook_ok_for_free_file(tmp_path: Path) -> None:
    source = tmp_path / "free.xlsx"
    source.write_bytes(b"PK")
    ensure_no_open_workbook(source)


def test_ensure_no_open_workbook_detects_excel_lock_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "locked.xlsx"
    source.write_bytes(b"PK")
    excel_lock_sidecar_path(source).write_bytes(b"lock")
    with pytest.raises(ValueError, match="临时锁文件"):
        ensure_no_open_workbook(source)


def test_ensure_no_open_workbook_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ensure_no_open_workbook(tmp_path / "nope.xlsx")


def test_ensure_no_open_workbook_ignores_non_excel(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("ok", encoding="utf-8")
    ensure_no_open_workbook(source)

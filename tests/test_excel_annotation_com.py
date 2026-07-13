from __future__ import annotations

from pathlib import Path

import pytest

from voucher_audit import excel_annotation_com
from voucher_audit.source_annotation import QueryTableAnnotationPlan, SourceAnnotationBundle


class _FakePythonCom:
    COINIT_APARTMENTTHREADED = 2

    def CoInitializeEx(self, _mode: int) -> None:
        return None

    def CoUninitialize(self) -> None:
        return None


class _FakeWorkbook:
    FullName = "source.xlsx"
    ReadOnly = False
    Saved = False

    def Save(self) -> None:
        return None

    def Close(self, SaveChanges: bool = False) -> None:
        return None


class _FakeWorkbooks:
    def __init__(self, workbook: _FakeWorkbook) -> None:
        self._workbook = workbook

    def Open(self, *_args, **_kwargs) -> _FakeWorkbook:
        return self._workbook


class _FakeExcel:
    def __init__(self) -> None:
        self.Workbooks = _FakeWorkbooks(_FakeWorkbook())
        self.Calculation = 0

    def Quit(self) -> None:
        return None


class _FakeWin32Client:
    def DispatchEx(self, _name: str) -> _FakeExcel:
        return _FakeExcel()


def test_annotation_failure_restores_backup(monkeypatch, tmp_path: Path) -> None:
    workbook_path = tmp_path / "source.xlsx"
    workbook_path.write_bytes(b"original")
    backup_path = tmp_path / "source.bak"
    restored: list[Path] = []

    plan = QueryTableAnnotationPlan(
        workbook_path=workbook_path,
        worksheet_name="Sheet1",
        table_name="Table1",
        gap_columns=1,
        headers=("异常项", "规则ID", "原因"),
        row_annotations=(),
        cell_highlights=(),
        possible_highlight_columns=(),
    )

    monkeypatch.setattr(excel_annotation_com, "_load_com_modules", lambda: (_FakePythonCom(), _FakeWin32Client()))
    monkeypatch.setattr(excel_annotation_com, "ensure_no_open_workbook", lambda _path: None)
    monkeypatch.setattr(excel_annotation_com, "backup_file", lambda _path: backup_path)
    monkeypatch.setattr(excel_annotation_com, "restore_from_backup", lambda path: restored.append(path))
    monkeypatch.setattr(
        excel_annotation_com,
        "_write_plan_to_sheet",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("write failed")),
    )

    with pytest.raises(RuntimeError, match="write failed"):
        excel_annotation_com.write_source_annotations(SourceAnnotationBundle(plans=(plan,)))

    assert restored == [workbook_path]

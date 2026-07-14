from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from voucher_audit.file_context import load_table_file, read_text_file, resolve_in_workdir


def test_resolve_in_workdir_rejects_parent_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="路径越界"):
        resolve_in_workdir(tmp_path, "../secret.txt")


def test_read_text_file_rejects_non_utf8_without_silent_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.txt"
    path.write_bytes("中文".encode("gbk"))

    with pytest.raises(ValueError, match="UTF-8"):
        read_text_file(tmp_path, path.name)


def test_load_table_file_reads_csv_with_row_limit(tmp_path: Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")

    out = load_table_file(tmp_path, path.name, nrows=1)

    pd.testing.assert_frame_equal(out, pd.DataFrame({"a": [1], "b": [2]}))

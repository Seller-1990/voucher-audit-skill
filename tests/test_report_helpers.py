from __future__ import annotations

from pathlib import Path

import pandas as pd

from voucher_audit.report import (
    _replace_rule_id_with_name,
    _sheet_name,
    _strip_internal_columns,
    build_rule_info_sheet,
    make_report_paths,
)


def test_make_report_paths_creates_output_dir(tmp_path: Path) -> None:
    paths = make_report_paths(tmp_path, "凭证审核报告", "202603")
    assert paths.output_dir == tmp_path / "凭证审核输出"
    assert paths.output_dir.is_dir()
    assert paths.report_path.name.startswith("凭证审核报告_202603_")
    assert paths.report_path.suffix == ".xlsx"


def test_strip_internal_columns() -> None:
    df = pd.DataFrame({"规则ID": ["R1"], "_row_index": [1], "命中原因": ["x"]})
    out = _strip_internal_columns(df)
    assert list(out.columns) == ["规则ID", "命中原因"]


def test_replace_rule_id_with_name() -> None:
    df = pd.DataFrame({"规则ID": ["INC_X"], "规则名称": ["名称X"], "命中原因": ["a"]})
    out = _replace_rule_id_with_name(df)
    assert "规则ID" not in out.columns
    assert list(out["规则名称"]) == ["名称X"]


def test_sheet_name_respects_report_format_and_excel_limit() -> None:
    long_name = "这是一个非常非常非常非常非常长的工作表名称超过三十一字符"
    name = _sheet_name({"sheet_names": {"overview": long_name}}, "overview", "概览")
    assert len(name) <= 31
    assert _sheet_name({}, "overview", "概览") == "概览"


def test_build_rule_info_sheet_basic() -> None:
    checks = [
        {
            "id": "R1",
            "name": "规则一",
            "severity": "错误",
            "scope": "income_cost",
            "description": "desc",
            "source": {"doc": "doc", "clause": "clause"},
            "params": {"eps": 0.1},
        }
    ]
    df = build_rule_info_sheet(checks)
    assert not df.empty
    assert "R1" in set(df.astype(str).stack().tolist())

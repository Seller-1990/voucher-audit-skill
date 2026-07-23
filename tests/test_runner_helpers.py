from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from voucher_audit.runner import detect_year_from_workdir, pick_target_month


def test_pick_target_month_prefers_income_for_default_scope() -> None:
    df_aux = pd.DataFrame({"月": [1, 2]})
    df_income = pd.DataFrame({"月": [1, 3]})
    assert pick_target_month(df_aux, df_income, "月", "月") == 3


def test_pick_target_month_prefers_aux_when_scope_is_ledger() -> None:
    df_aux = pd.DataFrame({"月": [1, 4]})
    df_income = pd.DataFrame({"月": [1, 3]})
    assert pick_target_month(df_aux, df_income, "月", "月", aux_scope_suffix="aux_ledger") == 4


def test_pick_target_month_raises_when_both_empty() -> None:
    with pytest.raises(ValueError, match="无法解析"):
        pick_target_month(pd.DataFrame({"月": []}), pd.DataFrame({"月": []}), "月", "月")


def test_detect_year_from_workdir_name() -> None:
    assert detect_year_from_workdir(Path("D:/data/202603_month_end")) == 2026

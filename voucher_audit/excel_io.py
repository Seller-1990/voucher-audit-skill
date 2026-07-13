from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .config import SheetMatcher


@dataclass(frozen=True)
class LoadedWorkbook:
    path: Path
    xls: pd.ExcelFile


def open_workbook(path: Path) -> LoadedWorkbook:
    xls = pd.ExcelFile(path)
    return LoadedWorkbook(path=path, xls=xls)


def match_sheet_name(xls: pd.ExcelFile, matcher: SheetMatcher) -> Optional[str]:
    names = [str(n) for n in xls.sheet_names]
    for p in matcher.preferred:
        if p in names:
            return p
    if matcher.fuzzy_contains_any:
        for n in names:
            if any(k in n for k in matcher.fuzzy_contains_any):
                return n
    return None


def read_sheet(xls: pd.ExcelFile, sheet_name: str, **kwargs: Any) -> pd.DataFrame:
    return pd.read_excel(xls, sheet_name=sheet_name, **kwargs)


def resolve_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    cols = set(str(c) for c in df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def require_column(df: pd.DataFrame, candidates: list[str], friendly: str, context: str) -> str:
    col = resolve_column(df, candidates)
    if col is None:
        raise KeyError(f"{context} 缺少必需列：{friendly}（候选：{candidates}）")
    return col


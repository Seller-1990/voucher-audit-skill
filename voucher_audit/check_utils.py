from __future__ import annotations

import re
from typing import Any, Optional

import pandas as pd


_PERCENT_SUFFIX_RE = re.compile(r"\s*\d+(?:\.\d+)?[%％]\s*$")

def _severity_rank(s: str) -> int:
    s = str(s)
    if s == "错误":
        return 0
    if s == "需确认":
        return 1
    return 2


def _rule_name(rule: dict[str, Any]) -> str:
    # Human-readable short name. Keep it stable and Chinese-first.
    name = str(rule.get("name", "") or "").strip()
    if name:
        return name
    desc = str(rule.get("description", "") or "").strip()
    if desc:
        return desc[:40]
    return str(rule.get("id", "") or "").strip()


def _strip_percent_suffix(value: Any) -> str:
    """Normalize business-type strings like '劳务派遣3%' -> '劳务派遣'."""
    s = "" if value is None else str(value).strip()
    if not s:
        return ""
    return _PERCENT_SUFFIX_RE.sub("", s).strip()


def _match_contains_any(value: object, keywords: list[str]) -> bool:
    s = "" if value is None else str(value)
    return any(k in s for k in keywords if str(k).strip())


def _apply_filters(df: pd.DataFrame, filters: list[dict[str, Any]]) -> pd.DataFrame:
    if df is None or df.empty or not filters:
        return df
    out = df
    for f in filters:
        if not isinstance(f, dict):
            continue
        field = str(f.get("field") or "").strip()
        if not field or field not in out.columns:
            continue
        s = out[field].astype(str)
        if "equals" in f:
            out = out[s == str(f.get("equals"))]
        elif "contains" in f:
            out = out[s.str.contains(str(f.get("contains")), regex=False, na=False)]
        elif "regex" in f:
            out = out[s.str.contains(str(f.get("regex")), regex=True, na=False)]
        elif "in" in f:
            allowed = [str(x) for x in (f.get("in") or [])]
            out = out[s.isin(allowed)]
    return out


def _pick_prev_month(df: pd.DataFrame, month_col: str, target_month: int) -> Optional[int]:
    m = pd.to_numeric(df[month_col], errors="coerce").dropna().astype(int)
    prev = m[m < target_month]
    if prev.empty:
        return None
    return int(prev.max())

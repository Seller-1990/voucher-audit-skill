from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class RuleSource:
    doc: str
    clause: str


@dataclass(frozen=True)
class RuleHit:
    severity: str
    rule_id: str
    rule_type: str
    description: str
    source: RuleSource
    reason: str
    row_index: int


def _is_empty(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and pd.isna(v):
        return True
    s = str(v).strip()
    return s == "" or s.lower() == "nan"


def _match_predicate(value: Any, pred: dict[str, Any]) -> bool:
    if "equals" in pred:
        return str(value) == str(pred["equals"])
    if "contains" in pred:
        return str(pred["contains"]) in str(value)
    if "in" in pred:
        allowed = [str(x) for x in (pred["in"] or [])]
        return str(value) in allowed
    if "regex" in pred:
        import re

        return re.search(str(pred["regex"]), str(value) or "") is not None
    if "not_empty" in pred:
        return not _is_empty(value)
    return False


def _row_matches_when(row: pd.Series, when: dict[str, Any]) -> bool:
    field = when.get("field")
    if not field:
        return False
    v = row.get(field)
    pred = {k: v for k, v in when.items() if k != "field"}
    return _match_predicate(v, pred)


def row_matches(row: pd.Series, params: dict[str, Any]) -> bool:
    if "when" in params:
        return _row_matches_when(row, params["when"] or {})
    if "when_any" in params:
        return any(_row_matches_when(row, w or {}) for w in (params["when_any"] or []))
    if "when_all" in params:
        return all(_row_matches_when(row, w or {}) for w in (params["when_all"] or []))
    return True





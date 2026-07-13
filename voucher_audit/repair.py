from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from .excel_io import match_sheet_name, open_workbook, read_sheet
from .rules_io import dump_yaml, load_app_rules, load_audit_rules


@dataclass(frozen=True)
class RepairSuggestion:
    ok: bool
    message: str
    app_rules_after: dict[str, Any]
    audit_rules_after: dict[str, Any]
    diff_app: str
    diff_audit: str


def _udiff(name: str, before: dict[str, Any], after: dict[str, Any]) -> str:
    a = dump_yaml(before).splitlines(keepends=True)
    b = dump_yaml(after).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            a,
            b,
            fromfile=f"{name}:before",
            tofile=f"{name}:after",
            lineterm="",
        )
    ) or "(无变化)"


def _best_filename_candidate(workdir: Path, missing_name: str) -> str | None:
    cands = [p.name for p in workdir.glob("*.xlsx")]
    if not cands:
        return None
    best = difflib.get_close_matches(missing_name, cands, n=1, cutoff=0.2)
    return best[0] if best else None


def propose_repair_for_missing_file(app: dict[str, Any], workdir: Path, missing_name: str) -> dict[str, Any] | None:
    inputs = dict(app.get("inputs", {}) or {})
    files = dict((inputs.get("files", {}) or {}))

    for key in ["data_summary", "income_cost"]:
        if str(files.get(key, "")).strip() == missing_name:
            cand = _best_filename_candidate(workdir, missing_name)
            if cand:
                files[key] = cand
                inputs["files"] = files
                out = dict(app)
                out["inputs"] = inputs
                return out
    return None


def _role_from_sheet_error(msg: str) -> str | None:
    text = str(msg)
    if "辅助帐" in text:
        return "aux_ledger"
    if "收入成本" in text:
        return "income_cost"
    return None


def _role_from_column_error_context(ctx: str) -> str | None:
    t = str(ctx)
    if "辅助帐" in t:
        return "aux_ledger"
    if "收入成本" in t:
        return "income_cost"
    if "客户调整校验" in t:
        return "customer_mapping"
    return None


def _best_sheet_candidate(names: list[str], preferred: list[str]) -> str:
    if not names:
        return ""
    target = preferred[0] if preferred else names[0]
    best = difflib.get_close_matches(target, names, n=1, cutoff=0.2)
    return best[0] if best else names[0]


def propose_repair_for_sheet(app: dict[str, Any], workbook_path: Path, role: str) -> dict[str, Any] | None:
    xls = open_workbook(workbook_path).xls
    names = [str(n) for n in xls.sheet_names]
    if not names:
        return None

    inputs = dict(app.get("inputs", {}) or {})
    sheets = dict((inputs.get("sheets", {}) or {}))
    matcher = dict((sheets.get(role, {}) or {}))
    preferred = [str(x) for x in (matcher.get("preferred", []) or [])]

    cand = _best_sheet_candidate(names, preferred)
    if not cand:
        return None
    if cand not in preferred:
        preferred = preferred + [cand]
    matcher["preferred"] = preferred
    sheets[role] = matcher
    inputs["sheets"] = sheets

    out = dict(app)
    out["inputs"] = inputs
    return out


def propose_repair_for_missing_column(app: dict[str, Any], role: str, missing_key: str, actual_columns: list[str]) -> dict[str, Any] | None:
    if not actual_columns:
        return None

    inputs = dict(app.get("inputs", {}) or {})
    columns = dict((inputs.get("columns", {}) or {}))
    role_map = dict((columns.get(role, {}) or {}))
    existing_candidates = [str(x) for x in (role_map.get(missing_key, []) or [])]

    chosen = ""
    for cand0 in existing_candidates:
        hits = difflib.get_close_matches(cand0, actual_columns, n=1, cutoff=0.6)
        if hits:
            chosen = hits[0]
            break
    if not chosen:
        hits = difflib.get_close_matches(missing_key, actual_columns, n=1, cutoff=0.2)
        chosen = hits[0] if hits else actual_columns[0]

    cur = existing_candidates
    if chosen not in cur:
        cur = cur + [chosen]
    role_map[missing_key] = cur
    columns[role] = role_map
    inputs["columns"] = columns

    out = dict(app)
    out["inputs"] = inputs
    return out


def suggest_repair(workdir: Path, app_rules_path: Path, audit_rules_path: Path, error: Exception) -> RepairSuggestion:
    app = load_app_rules(app_rules_path)
    audit = load_audit_rules(audit_rules_path)

    msg = str(error)
    app2 = app

    if isinstance(error, FileNotFoundError) and "缺少文件" in msg:
        missing = msg.split("：")[-1].strip()
        patched = propose_repair_for_missing_file(app, workdir, missing)
        if patched is not None:
            app2 = patched

    elif isinstance(error, ValueError) and "无法匹配" in msg and "sheet" in msg:
        role = _role_from_sheet_error(msg)
        if role:
            inputs = dict(app.get("inputs", {}) or {})
            files = dict((inputs.get("files", {}) or {}))
            file_key = "data_summary" if role == "aux_ledger" else "income_cost"
            workbook_path = (workdir / str(files.get(file_key, "")).strip()).resolve()
            if workbook_path.exists():
                patched = propose_repair_for_sheet(app, workbook_path, role)
                if patched is not None:
                    app2 = patched

    elif isinstance(error, KeyError) and "缺少必需列" in msg:
        ctx = msg.split("缺少必需列", 1)[0].strip(" ' \"")
        role = _role_from_column_error_context(ctx)
        if role:
            after = msg.split("缺少必需列：", 1)[-1]
            missing_key = after.split("（", 1)[0].strip()

            inputs = dict(app.get("inputs", {}) or {})
            files = dict((inputs.get("files", {}) or {}))
            sheets = dict((inputs.get("sheets", {}) or {}))

            file_key = "data_summary" if role in {"aux_ledger", "customer_mapping"} else "income_cost"
            workbook_path = (workdir / str(files.get(file_key, "")).strip()).resolve()
            if workbook_path.exists():
                matcher = sheets.get(role, {}) or {}
                preferred = [str(x) for x in (matcher.get("preferred", []) or [])]
                fuzzy = [str(x) for x in (matcher.get("fuzzy_contains_any", []) or [])]

                xls = open_workbook(workbook_path).xls
                from .config import SheetMatcher

                sheet = match_sheet_name(xls, SheetMatcher(preferred=preferred, fuzzy_contains_any=fuzzy))
                sheet = sheet or (str(xls.sheet_names[0]) if xls.sheet_names else "")
                if sheet:
                    header_df = read_sheet(xls, sheet, nrows=0)
                    actual_columns = [str(c) for c in header_df.columns]
                    patched = propose_repair_for_missing_column(app, role, missing_key, actual_columns)
                    if patched is not None:
                        app2 = patched

    return RepairSuggestion(
        ok=(app2 != app),
        message=("已生成修复建议。" if app2 != app else "未能自动生成修复建议，请手动修改 rules/app_rules.yaml。"),
        app_rules_after=app2,
        audit_rules_after=audit,
        diff_app=_udiff("app_rules", app, app2),
        diff_audit=_udiff("audit_rules", audit, audit),
    )

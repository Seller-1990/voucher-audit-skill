from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except Exception:
        return False


def resolve_in_workdir(workdir: Path, input_path: str) -> Path:
    raw = Path((input_path or "").strip())
    if raw.is_absolute():
        resolved = raw.resolve()
    else:
        resolved = (workdir / raw).resolve()
    if not _is_inside(resolved, workdir):
        raise ValueError(f"路径越界：{input_path}")
    return resolved


def scan_workdir_files(
    workdir: Path,
    *,
    max_files: int = 5000,
    include_hidden: bool = False,
    exclude_dirs: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    root = workdir.resolve()
    ex = set(exclude_dirs or ["__pycache__"])
    items: list[dict[str, Any]] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        rel_parts = set(rel.parts[:-1])
        if ex.intersection(rel_parts):
            continue
        name = p.name
        if not include_hidden and name.startswith("."):
            continue
        stat = p.stat()
        items.append(
            {
                "path": str(rel).replace("\\", "/"),
                "size_bytes": int(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "ext": p.suffix.lower(),
            }
        )
        if len(items) >= max_files:
            break
    items.sort(key=lambda x: str(x.get("path", "")).lower())
    return items


def read_text_file(workdir: Path, input_path: str, max_chars: int = 12000) -> str:
    p = resolve_in_workdir(workdir, input_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    text_ext = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".py", ".sql"}
    if p.suffix.lower() not in text_ext:
        raise ValueError(f"不支持按文本读取该类型：{p.suffix}")
    content = p.read_text(encoding="utf-8", errors="ignore")
    if max_chars > 0:
        content = content[:max_chars]
    return content


def load_table_file(
    workdir: Path,
    input_path: str,
    *,
    sheet: str = "",
    nrows: int = 0,
) -> pd.DataFrame:
    p = resolve_in_workdir(workdir, input_path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"文件不存在：{p}")
    ext = p.suffix.lower()
    max_rows = int(nrows) if nrows and int(nrows) > 0 else None
    if ext in {".csv"}:
        return pd.read_csv(p, nrows=max_rows)
    if ext in {".xlsx", ".xlsm", ".xls"}:
        sheet_name: Any = sheet.strip() if sheet else 0
        return pd.read_excel(p, sheet_name=sheet_name, nrows=max_rows)
    raise ValueError(f"不支持按表格读取该类型：{ext}")


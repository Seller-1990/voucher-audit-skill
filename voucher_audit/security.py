"""
安全模块 - 文件备份、异常恢复、占用检测
"""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

_EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".xlsb"}
# Windows ERROR_SHARING_VIOLATION
_WIN_SHARING_VIOLATION = 32


def _default_backup_path(file_path: Path, suffix: str = ".bak") -> Path:
    """旁路备份路径：保留原扩展名（data.xlsx -> data.xlsx.bak）。"""
    return Path(str(file_path) + suffix)


def backup_file(file_path: Path, suffix: str = ".bak") -> Path:
    """
    备份 Excel 文件，避免修改源文件导致数据丢失。

    使用 ``原路径 + suffix``（例如 ``数据汇总.xlsx.bak``），而不是
    ``Path.with_suffix``，以免把 ``.xlsx`` 替换成 ``.bak`` 丢掉原扩展名。
    若目标已存在，则追加时间戳，避免覆盖上一份备份。
    """
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    backup = _default_backup_path(file_path, suffix)
    if backup.exists():
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = Path(str(file_path) + f".{ts}{suffix}")
        # 同一秒内重复备份时追加序号，避免覆盖
        seq = 1
        while backup.exists():
            backup = Path(str(file_path) + f".{ts}-{seq}{suffix}")
            seq += 1

    shutil.copy2(file_path, backup)
    return backup


def restore_from_backup(file_path: Path, backup_path: Path | None = None) -> None:
    """
    从备份恢复文件。

    优先使用显式 ``backup_path``（标注流程应传入 ``backup_file`` 的返回值）。
    未传入时依次尝试 ``*.xlsx.bak`` 与历史命名 ``*.bak``。
    """
    candidates: list[Path] = []
    if backup_path is not None:
        candidates.append(Path(backup_path))
    else:
        candidates.append(_default_backup_path(file_path))
        candidates.append(file_path.with_suffix(".bak"))

    for backup in candidates:
        if backup.exists():
            shutil.copy2(backup, file_path)
            return
    tried = ", ".join(str(p) for p in candidates)
    raise FileNotFoundError(f"备份文件不存在: {tried}")


def excel_lock_sidecar_path(file_path: Path) -> Path:
    """Excel 打开工作簿时常见的临时锁文件：``~$文件名.xlsx``。"""
    return file_path.with_name(f"~${file_path.name}")


def _is_sharing_violation(exc: BaseException) -> bool:
    winerror = getattr(exc, "winerror", None)
    if winerror == _WIN_SHARING_VIOLATION:
        return True
    # errno 13 = EACCES, 11 = EAGAIN (some platforms)
    errno = getattr(exc, "errno", None)
    return errno in {11, 13}


def ensure_no_open_workbook(file_path: Path) -> None:
    """
    检查 Excel 工作簿是否被其他程序占用。

    检测顺序：
    1. 存在性
    2. Excel 临时锁文件 ``~$name.xlsx``
    3. 以读写方式打开文件（Excel 独占锁时通常触发 PermissionError / sharing violation）

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 文件被占用或不可写
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not path.is_file():
        raise ValueError(f"目标不是普通文件: {path}")

    if path.suffix.lower() not in _EXCEL_SUFFIXES:
        return

    lock_sidecar = excel_lock_sidecar_path(path)
    if lock_sidecar.exists():
        raise ValueError(
            f"检测到 Excel 临时锁文件，源工作簿可能仍被打开：{path.name}（{lock_sidecar.name}）。请关闭 Excel 后重试。"
        )

    if not os.access(path, os.R_OK | os.W_OK):
        raise ValueError(f"文件不可读写，可能被其他程序占用: {path}")

    try:
        # Exclusive-ish probe: open for read/write. Excel typically denies share write.
        with path.open("r+b"):
            pass
    except PermissionError as e:
        raise ValueError(f"文件可能被其他程序占用: {path}") from e
    except OSError as e:
        if _is_sharing_violation(e):
            raise ValueError(f"文件可能被其他程序占用: {path}") from e
        raise

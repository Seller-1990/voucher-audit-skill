"""
安全模块 - 文件备份、异常恢复等安全相关功能
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional


def backup_file(file_path: Path, suffix: str = ".bak") -> Path:
    """
    备份Excel文件，避免修改源文件导致数据丢失

    Args:
        file_path: 要备份的文件路径
        suffix: 备份文件后缀

    Returns:
        备份文件的完整路径
    """
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    backup = file_path.with_suffix(suffix)
    # 如果备份已存在，先删除
    if backup.exists():
        backup.unlink()

    # 复制文件（保留元数据）
    shutil.copy2(file_path, backup)
    return backup


def restore_from_backup(file_path: Path) -> None:
    """
    从备份恢复文件

    Args:
        file_path: 要恢复的文件路径
    """
    backup = file_path.with_suffix(".bak")
    if backup.exists():
        backup.replace(file_path)
    else:
        raise FileNotFoundError(f"备份文件不存在: {backup}")


def ensure_no_open_workbook(file_path: Path) -> None:
    """
    检查文件是否被其他程序打开（仅Windows平台）

    Args:
        file_path: 要检查的文件路径

    Raises:
        ValueError: 文件被占用
    """
    if file_path.suffix.lower() in (".xlsx", ".xls", ".xlsm"):
        import os

        if not hasattr(os, "access"):
            return  # 非Windows平台，无法检查

        try:
            # 尝试独占访问
            if not os.access(file_path, os.R_OK | os.W_OK):
                raise ValueError(f"文件可能被其他程序占用: {file_path}")
        except PermissionError as e:
            raise ValueError(f"文件可能被其他程序占用: {file_path}") from e
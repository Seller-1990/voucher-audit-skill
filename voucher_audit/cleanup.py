from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


# 调试/临时目录：默认清理
TEMP_CLEANUP_DIR_NAMES = (
    "temp_cn",
    "temp_debug",
    "temp_debug2",
    "temp_debug3",
    "temp_debug4",
    "temp_debug_run",
    "temp_debug_write",
)

# 审核报告输出：需显式 --include-reports 才清理，避免误删报告
REPORT_CLEANUP_DIR_NAMES = (
    "凭证审核输出",
)

# 向后兼容：历史代码可能引用全集
CLEANUP_DIR_NAMES = TEMP_CLEANUP_DIR_NAMES + REPORT_CLEANUP_DIR_NAMES


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...]
    failed: tuple[tuple[Path, str], ...]


def cleanup_targets(workdir: Path, *, include_reports: bool = False) -> tuple[Path, ...]:
    """
    列出可清理目标。

    - 默认仅 temp_* 调试目录
    - ``include_reports=True`` 时额外包含 ``凭证审核输出``
    """
    root = workdir.resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"工作目录不存在或不是目录：{root}")

    names = list(TEMP_CLEANUP_DIR_NAMES)
    if include_reports:
        names.extend(REPORT_CLEANUP_DIR_NAMES)

    targets: list[Path] = []
    for name in names:
        target = (root / name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"清理路径越界：{target}") from exc
        if target == root:
            raise ValueError(f"拒绝删除工作目录本身：{root}")
        if target.exists():
            targets.append(target)
    return tuple(targets)


def classify_cleanup_target(target: Path) -> str:
    """返回目标类别：report / temp / other。"""
    name = target.name
    if name in REPORT_CLEANUP_DIR_NAMES:
        return "report"
    if name in TEMP_CLEANUP_DIR_NAMES:
        return "temp"
    return "other"


def delete_cleanup_targets(targets: tuple[Path, ...]) -> CleanupResult:
    """
    删除 cleanup 目标。仅允许已知目录名（temp_* / 凭证审核输出），
    且目标必须是已存在的目录，避免误删普通文件或未知路径。
    """
    allowed_names = set(CLEANUP_DIR_NAMES)
    deleted: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for target in targets:
        path = Path(target).resolve()
        if path.name not in allowed_names:
            failed.append((path, "拒绝删除：非本工具清理白名单目录"))
            continue
        if not path.exists():
            continue
        if not path.is_dir():
            failed.append((path, "拒绝删除：目标不是目录"))
            continue
        try:
            shutil.rmtree(path)
            deleted.append(path)
        except Exception as exc:
            failed.append((path, f"{type(exc).__name__}: {exc}"))
    return CleanupResult(deleted=tuple(deleted), failed=tuple(failed))

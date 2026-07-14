from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


CLEANUP_DIR_NAMES = (
    "temp_cn",
    "temp_debug",
    "temp_debug2",
    "temp_debug3",
    "temp_debug4",
    "temp_debug_run",
    "temp_debug_write",
    "凭证审核输出",
)


@dataclass(frozen=True)
class CleanupResult:
    deleted: tuple[Path, ...]
    failed: tuple[tuple[Path, str], ...]


def cleanup_targets(workdir: Path) -> tuple[Path, ...]:
    root = workdir.resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"工作目录不存在或不是目录：{root}")
    targets: list[Path] = []
    for name in CLEANUP_DIR_NAMES:
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


def delete_cleanup_targets(targets: tuple[Path, ...]) -> CleanupResult:
    deleted: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for target in targets:
        try:
            shutil.rmtree(target)
            deleted.append(target)
        except Exception as exc:
            failed.append((target, f"{type(exc).__name__}: {exc}"))
    return CleanupResult(deleted=tuple(deleted), failed=tuple(failed))

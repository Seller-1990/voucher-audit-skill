from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .rules_io import dump_yaml


@dataclass(frozen=True)
class VersionedRules:
    app_rules_path: Path
    audit_rules_path: Path
    compiled_rules_path: Path


def write_version_snapshot(
    *,
    repo_root: Path,
    app_rules: dict[str, Any],
    audit_rules: dict[str, Any],
    compiled_rules: dict[str, Any],
) -> VersionedRules:
    out_dir = (repo_root / "rules" / "versions").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")

    app_path = out_dir / f"app_rules_{ts}.yaml"
    audit_path = out_dir / f"audit_rules_{ts}.yaml"
    compiled_path = out_dir / f"compiled_rules_{ts}.yaml"

    app_path.write_text(dump_yaml(app_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    audit_path.write_text(dump_yaml(audit_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    compiled_path.write_text(dump_yaml(compiled_rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    return VersionedRules(app_rules_path=app_path, audit_rules_path=audit_path, compiled_rules_path=compiled_path)


def update_active_pointer(repo_root: Path, snap: VersionedRules) -> Path:
    p = (repo_root / "rules" / "active_rules.json").resolve()
    data = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "active": {
            "app_rules": str(snap.app_rules_path.relative_to(repo_root)),
            "audit_rules": str(snap.audit_rules_path.relative_to(repo_root)),
            "compiled_rules": str(snap.compiled_rules_path.relative_to(repo_root)),
        },
    }
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return p

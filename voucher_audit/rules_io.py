from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from .config import RuleConfig, load_rules_data


@dataclass(frozen=True)
class RulesPaths:
    app_rules: Path
    audit_rules: Path
    compiled_rules: Path


@dataclass(frozen=True)
class ActiveRulesPointer:
    app_rules: Path
    audit_rules: Path
    compiled_rules: Path


DEFAULT_RULE_FILENAMES = ("app_rules.yaml", "audit_rules.yaml")


def _has_default_rules(root: Path) -> bool:
    rules_dir = root / "rules"
    return all((rules_dir / filename).is_file() for filename in DEFAULT_RULE_FILENAMES)


def _user_data_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "voucher-audit-skill"


def ensure_rules_root(checkout_root: Path, *, user_data_root: Path | None = None) -> Path:
    checkout_root = checkout_root.resolve()
    if _has_default_rules(checkout_root):
        return checkout_root

    runtime_root = (user_data_root or _user_data_root()).resolve()
    rules_dir = runtime_root / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    packaged_rules = resources.files("voucher_audit").joinpath("default_rules")
    for filename in DEFAULT_RULE_FILENAMES:
        target = rules_dir / filename
        if not target.exists():
            content = packaged_rules.joinpath(filename).read_text(encoding="utf-8")
            target.write_text(content.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    return runtime_root


def repo_root_from_module() -> Path:
    return ensure_rules_root(Path(__file__).resolve().parent.parent)


def _read_yaml_obj(path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"规则文件解析失败：{path}\n{e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"规则文件顶层必须是对象：{path}")
    return dict(data)


def dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def load_app_rules(path: Path) -> dict[str, Any]:
    data = _read_yaml_obj(path)
    data.pop("checks", None)
    return data


def load_audit_rules(path: Path) -> dict[str, Any]:
    data = _read_yaml_obj(path)
    checks = data.get("checks", []) or []
    if not isinstance(checks, list):
        raise ValueError(f"audit_rules.checks 必须是 list：{path}")
    return {"checks": list(checks)}


def compile_rules(app_rules: dict[str, Any], audit_rules: dict[str, Any]) -> dict[str, Any]:
    out = dict(app_rules)
    out["checks"] = list(audit_rules.get("checks", []) or [])
    return out


def default_rules_paths(repo_root: Path) -> RulesPaths:
    rules_dir = (repo_root / "rules").resolve()
    return RulesPaths(
        app_rules=(rules_dir / "app_rules.yaml"),
        audit_rules=(rules_dir / "audit_rules.yaml"),
        compiled_rules=(rules_dir / "compiled_rules.yaml"),
    )


def load_active_pointer(repo_root: Path) -> ActiveRulesPointer | None:
    p = (repo_root / "rules" / "active_rules.json").resolve()
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8") or "{}")
    active = data.get("active", {}) or {}
    a = str(active.get("app_rules", "")).strip()
    b = str(active.get("audit_rules", "")).strip()
    c = str(active.get("compiled_rules", "")).strip()
    if not a or not b or not c:
        return None
    root = repo_root.resolve()

    def resolve_repo_path(value: str) -> Path:
        resolved = (root / value).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as e:
            raise ValueError(f"规则路径必须位于仓库内：{value}") from e
        return resolved

    app = resolve_repo_path(a)
    audit = resolve_repo_path(b)
    compiled = resolve_repo_path(c)
    if not app.exists() or not audit.exists() or not compiled.exists():
        return None
    return ActiveRulesPointer(app_rules=app, audit_rules=audit, compiled_rules=compiled)


def ensure_compiled_rules(repo_root: Path) -> RulesPaths:
    active = load_active_pointer(repo_root)
    if active is not None:
        return RulesPaths(app_rules=active.app_rules, audit_rules=active.audit_rules, compiled_rules=active.compiled_rules)

    base = default_rules_paths(repo_root)
    app = load_app_rules(base.app_rules)
    audit = load_audit_rules(base.audit_rules)
    compiled = compile_rules(app, audit)
    base.compiled_rules.write_text(dump_yaml(compiled).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
    return base


def load_compiled_rule_config(paths: RulesPaths) -> RuleConfig:
    compiled = _read_yaml_obj(paths.compiled_rules)
    return load_rules_data(compiled)

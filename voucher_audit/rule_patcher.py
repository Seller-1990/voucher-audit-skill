from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class RulePatchResult:
    ok: bool
    message: str
    new_rules_path: Path | None
    diff_text: str


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("规则文件顶层必须是对象")
    return data


def _dump_yaml(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def _checks(data: dict[str, Any]) -> list[dict[str, Any]]:
    checks = data.get("checks", []) or []
    if not isinstance(checks, list):
        raise ValueError("rules.checks 必须是 list")
    out: list[dict[str, Any]] = []
    for c in checks:
        if isinstance(c, dict):
            out.append(dict(c))
    return out


def _normalize_patch_op(op_raw: Any) -> str:
    op = str(op_raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    mapping = {
        "add": "add_check",
        "append": "add_check",
        "addcheck": "add_check",
        "new_check": "add_check",
        "new_rule": "add_check",
        "remove": "remove_check",
        "delete": "remove_check",
        "del": "remove_check",
        "removecheck": "remove_check",
        "delete_check": "remove_check",
        "update": "update_check",
        "edit": "update_check",
        "modify": "update_check",
        "patch": "update_check",
        "updatecheck": "update_check",
        "modify_check": "update_check",
        "setai": "set_ai",
        "update_ai": "set_ai",
        "set_ai_config": "set_ai",
        "ai_settings": "set_ai",
        "set_report": "set_report_format",
        "set_report_format": "set_report_format",
        "update_report": "set_report_format",
        "update_report_format": "set_report_format",
        "report_format": "set_report_format",
    }
    return mapping.get(op, op)


def _normalize_patch_action(act: dict[str, Any]) -> dict[str, Any]:
    item = dict(act)
    op = _normalize_patch_op(item.get("op") or item.get("type") or item.get("action"))
    if not op:
        if isinstance(item.get("check"), dict):
            op = "add_check"
        elif isinstance(item.get("fields"), dict) and not str(item.get("id", "")).strip():
            op = "set_ai"
        elif isinstance(item.get("set"), dict) or isinstance(item.get("changes"), dict) or isinstance(item.get("update"), dict):
            op = "update_check"
    item["op"] = op

    if op == "update_check" and not isinstance(item.get("set"), dict):
        if isinstance(item.get("changes"), dict):
            item["set"] = dict(item["changes"])
        elif isinstance(item.get("update"), dict):
            item["set"] = dict(item["update"])
        elif isinstance(item.get("fields"), dict):
            item["set"] = dict(item["fields"])
    if op == "set_ai" and not isinstance(item.get("fields"), dict):
        if isinstance(item.get("set"), dict):
            item["fields"] = dict(item["set"])
        elif isinstance(item.get("update"), dict):
            item["fields"] = dict(item["update"])
    if op == "set_report_format" and not isinstance(item.get("fields"), dict):
        if isinstance(item.get("set"), dict):
            item["fields"] = dict(item["set"])
        elif isinstance(item.get("update"), dict):
            item["fields"] = dict(item["update"])
        elif isinstance(item.get("report_format"), dict):
            item["fields"] = dict(item["report_format"])
        else:
            auto_fields = {k: v for k, v in item.items() if k not in {"op", "type", "action", "id"}}
            if auto_fields:
                item["fields"] = auto_fields
    return item


def _deep_update(dst: dict[str, Any], src: dict[str, Any]) -> dict[str, Any]:
    out = dict(dst)
    for k, v in src.items():
        key = str(k)
        if isinstance(v, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_update(dict(out[key]), v)
        else:
            out[key] = v
    return out


def _apply_patch_dict(data: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    checks = _checks(out)
    actions = patch.get("actions", []) or []
    if not isinstance(actions, list):
        raise ValueError("patch.actions 必须是 list")

    for idx, act in enumerate(actions, start=1):
        if not isinstance(act, dict):
            raise ValueError(f"actions[{idx}] 必须是对象")
        item = _normalize_patch_action(act)
        op = str(item.get("op", "")).strip()
        if op == "add_check":
            check = item.get("check")
            if not isinstance(check, dict):
                raise ValueError(f"actions[{idx}] add_check 缺少 check 对象")
            cid = str(check.get("id", "")).strip()
            if not cid:
                raise ValueError(f"actions[{idx}] add_check.check.id 不能为空")
            if any(str(c.get("id", "")) == cid for c in checks):
                raise ValueError(f"actions[{idx}] add_check.id 已存在：{cid}")
            checks.append(dict(check))
        elif op == "remove_check":
            cid = str(item.get("id", "")).strip()
            if not cid:
                raise ValueError(f"actions[{idx}] remove_check.id 不能为空")
            before = len(checks)
            checks = [c for c in checks if str(c.get("id", "")) != cid]
            if len(checks) == before:
                raise ValueError(f"actions[{idx}] remove_check 未找到 id：{cid}")
        elif op == "update_check":
            cid = str(item.get("id", "")).strip()
            set_data = item.get("set", {})
            if not cid or not isinstance(set_data, dict):
                raise ValueError(f"actions[{idx}] update_check 参数非法")
            found = False
            for c in checks:
                if str(c.get("id", "")) == cid:
                    merged = _deep_update(dict(c), set_data)
                    c.clear()
                    c.update(merged)
                    found = True
                    break
            if not found:
                raise ValueError(f"actions[{idx}] update_check 未找到 id：{cid}")
        elif op == "set_ai":
            fields = item.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError(f"actions[{idx}] set_ai.fields 必须是对象")
            ai = dict(out.get("ai", {}) or {})
            ai.update(fields)
            out["ai"] = ai
        elif op == "set_report_format":
            fields = item.get("fields", {})
            if not isinstance(fields, dict):
                raise ValueError(f"actions[{idx}] set_report_format.fields 必须是对象")
            report_format = dict(out.get("report_format", {}) or {})
            out["report_format"] = _deep_update(report_format, fields)
        else:
            raise ValueError(
                f"actions[{idx}] 不支持 op：{op}（允许：add_check/remove_check/update_check/set_ai/set_report_format）"
            )

    out["checks"] = checks
    return out


def _strip_version_suffix(stem: str) -> str:
    s = str(stem or "").strip()
    if not s:
        return "voucher_audit_rules"
    base = re.sub(r"(?:_\d{14})+$", "", s)
    return base or s


def preview_rule_patch(rules_path: Path, patch: dict[str, Any]) -> RulePatchResult:
    if not rules_path.exists():
        return RulePatchResult(ok=False, message=f"规则文件不存在：{rules_path}", new_rules_path=None, diff_text="")
    try:
        before = _load_yaml(rules_path)
        after = _apply_patch_dict(before, patch)
        before_text = _dump_yaml(before).splitlines(keepends=True)
        after_text = _dump_yaml(after).splitlines(keepends=True)
        diff = "".join(
            difflib.unified_diff(
                before_text,
                after_text,
                fromfile=str(rules_path.name),
                tofile=f"{rules_path.stem}_patched.yaml",
                lineterm="",
            )
        )
        return RulePatchResult(ok=True, message="规则补丁预览成功。", new_rules_path=None, diff_text=diff or "(无变化)")
    except Exception as e:
        return RulePatchResult(ok=False, message=f"规则补丁预览失败：{type(e).__name__}: {e}", new_rules_path=None, diff_text="")


def apply_rule_patch_versioned(rules_path: Path, patch: dict[str, Any], out_dir: Path) -> RulePatchResult:
    if not rules_path.exists():
        return RulePatchResult(ok=False, message=f"规则文件不存在：{rules_path}", new_rules_path=None, diff_text="")
    try:
        before = _load_yaml(rules_path)
        after = _apply_patch_dict(before, patch)

        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        base_stem = _strip_version_suffix(rules_path.stem)
        new_name = f"{base_stem}_{ts}{rules_path.suffix or '.yaml'}"
        out_path = (out_dir / new_name).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_dump_yaml(after).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

        diff_preview = preview_rule_patch(rules_path, patch).diff_text
        return RulePatchResult(ok=True, message=f"规则新版本已生成：{out_path}", new_rules_path=out_path, diff_text=diff_preview)
    except Exception as e:
        return RulePatchResult(ok=False, message=f"规则补丁应用失败：{type(e).__name__}: {e}", new_rules_path=None, diff_text="")

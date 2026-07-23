from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .cleanup import classify_cleanup_target, cleanup_targets, delete_cleanup_targets
from .logging_util import make_logger
from .preview import PreviewItem, build_preview_items
from .repair import suggest_repair
from .rules_io import (
    compile_rules,
    dump_yaml,
    ensure_compiled_rules,
    load_active_pointer,
    load_app_rules,
    load_compiled_rule_config,
    repo_root_from_module,
)
from .runner import load_audit_context, run_audit
from .versioning import VersionedRules, update_active_pointer, write_version_snapshot


def _prompt_yes_no(question: str, *, default_no: bool = True) -> bool:
    prompt = " [y/N] " if default_no else " [Y/n] "
    while True:
        s = input(question + prompt).strip().lower()
        if not s:
            return not default_no
        if s in {"y", "yes"}:
            return True
        if s in {"n", "no"}:
            return False
        print("请输入 y/yes 或 n/no。", file=sys.stderr)


def _format_preview_table(items: Sequence[PreviewItem]) -> str:
    """格式化预览表格"""
    cols = ["规则名称", "规则ID", "严重度", "Scope", "Type", "字段", "输出页"]
    rows: list[list[str]] = []
    for it in items:
        rows.append(
            [
                str(it.rule_name),
                str(it.rule_id),
                str(it.severity),
                str(it.scope),
                str(it.rule_type),
                "，".join(it.fields or ()),
                str(it.output_logical_sheet),
            ]
        )

    widths = [len(c) for c in cols]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))

    def fmt_row(r: list[str]) -> str:
        return " | ".join(v.ljust(widths[i]) for i, v in enumerate(r))

    line = "-+-".join("-" * w for w in widths)
    out = [fmt_row(cols), line]
    out.extend(fmt_row(r) for r in rows)
    return "\n".join(out)


def _load_rules_for_execution(repo_root: Path) -> tuple[Path, dict[str, Any]]:
    paths = ensure_compiled_rules(repo_root)
    app = load_app_rules(paths.app_rules)
    return paths.compiled_rules, app


def cmd_preview(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    compiled_rules_path, _app = _load_rules_for_execution(repo_root)
    rules = load_compiled_rule_config(ensure_compiled_rules(repo_root))

    items = build_preview_items(rules)
    print(f"规则文件（compiled）：{compiled_rules_path}")
    print(f"规则数：{len(items)}")
    if items:
        print(_format_preview_table(items))
    else:
        print("（无审核规则）")

    if args.show_inputs:
        print("\n输入配置：")
        print(f"- 数据汇总：{rules.inputs.data_summary_file}")
        print(f"- 考核表输出：{rules.inputs.income_cost_file}")
        print("- Sheet 匹配：")
        for k, m in rules.inputs.sheets.items():
            print(f"  - {k}: preferred={m.preferred} fuzzy_contains_any={m.fuzzy_contains_any}")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """脚本化检查：列出输入文件/sheet/列，并展示规则期望的 sheet 匹配结果。"""
    repo_root = repo_root_from_module()
    rules = load_compiled_rule_config(ensure_compiled_rules(repo_root))

    workdir = Path(args.workdir).resolve()
    data_summary = workdir / rules.inputs.data_summary_file
    income_cost = workdir / rules.inputs.income_cost_file

    print(f"workdir: {workdir}")
    print(f"数据汇总: {data_summary}")
    print(f"考核表输出: {income_cost}")

    import pandas as pd

    from .excel_io import match_sheet_name, open_workbook

    for p in [data_summary, income_cost]:
        if not p.exists():
            print(f"\n== {p.name} ==")
            print("(文件不存在)")
            continue
        xls = open_workbook(p).xls
        print(f"\n== {p.name} ==")
        print("sheets:")
        for sh in xls.sheet_names:
            print(f"- {sh}")

        # Show matched logical sheets for this workbook.
        for logical, matcher in rules.inputs.sheets.items():
            is_sum = logical in {"aux_ledger", "customer_mapping"}
            if (p == data_summary and not is_sum) or (p == income_cost and is_sum):
                continue
            matched = match_sheet_name(xls, matcher)
            if not matched:
                continue
            df0 = pd.read_excel(xls, sheet_name=matched, nrows=0)
            cols = [str(c) for c in df0.columns]
            print(f"\n匹配到 sheet: {logical} -> {matched}")
            print(f"列({len(cols)}): {cols}")

    return 0


def cmd_run(args: argparse.Namespace) -> int:
    log = make_logger()
    repo_root = repo_root_from_module()
    compiled_rules_path, app_rules = _load_rules_for_execution(repo_root)

    rules = load_compiled_rule_config(ensure_compiled_rules(repo_root))
    items = build_preview_items(rules)

    print("将执行的审核事项（预览）：")
    print(f"- workdir: {Path(args.workdir).resolve()}")
    print(f"- compiled rules: {compiled_rules_path}")
    print(f"- 规则数: {len(items)}")
    if items:
        print(_format_preview_table(items))
    else:
        print("（无审核规则）")

    if not args.yes:
        ok = _prompt_yes_no("确认以上审核事项无误，并开始执行审核吗？", default_no=True)
        if not ok:
            print("已取消。")
            return 2

    annotate_default = bool((app_rules.get("annotation_policy", {}) or {}).get("enabled_default", True))
    annotate = annotate_default if args.annotate is None else bool(args.annotate)

    # AI 相关处理（延迟导入，避免不必要的依赖）
    if args.enable_ai:
        try:
            _ = __import__("openai")
        except ImportError:
            message = "AI功能需要安装 openai 库: pip install openai"
            log.error(message)
            print(message, file=sys.stderr)
            return 1

    if annotate:
        warn = "将对源 Excel 进行颜色标记并在右侧新增列写入问题说明（会修改源文件）。继续吗？"
        # 第二确认门：即使 --yes 也不跳过，必须显式 --yes-annotate。
        if not args.yes_annotate:
            ok2 = _prompt_yes_no(warn, default_no=True)
            if not ok2:
                annotate = False

    res = run_audit(
        workdir=Path(args.workdir),
        rules_path=compiled_rules_path,
        target_month=args.month,
        include_rule_ids=args.include_rule_id,
        enable_ai=args.enable_ai,
        openai_api_key=args.openai_api_key or "",
        openai_base_url=args.openai_base_url or "",
        openai_model=args.openai_model or "",
        annotate_source=annotate,
        logger=log,
    )

    if not res.ok:
        print(f"失败：{res.message}", file=sys.stderr)
        return 1

    print(f"完成：{res.message}")
    if res.report_path:
        print(f"报告：{res.report_path}")
    if res.annotation_requested:
        if res.annotation_ok:
            print(f"源文件标注：成功。{res.annotation_message}".strip())
        else:
            print(f"源文件标注：失败。{res.annotation_message}".strip(), file=sys.stderr)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    paths = ensure_compiled_rules(repo_root)
    workdir = Path(args.workdir).resolve()

    try:
        load_audit_context(workdir=workdir, rules_path=paths.compiled_rules)
        print("规则与文件读取正常，无需 repair。")
        return 0
    except Exception as e:
        err = e

    suggestion = suggest_repair(
        workdir=workdir,
        app_rules_path=paths.app_rules,
        audit_rules_path=paths.audit_rules,
        error=err,
    )

    print(f"触发错误：{type(err).__name__}: {err}")
    print(suggestion.message)
    if not suggestion.ok:
        return 2

    print("\n--- app_rules.diff ---")
    print(suggestion.diff_app)
    print("\n--- audit_rules.diff ---")
    print(suggestion.diff_audit)

    compiled2 = compile_rules(suggestion.app_rules_after, suggestion.audit_rules_after)

    # Preflight with patched compiled rules (no mutation).
    with tempfile.TemporaryDirectory(prefix="voucher-audit-repair-") as td:
        temp_rules = Path(td) / "compiled_rules.yaml"
        temp_rules.write_text(dump_yaml(compiled2).replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        try:
            load_audit_context(workdir=workdir, rules_path=temp_rules)
        except Exception as e:
            print(f"\n预演失败：{type(e).__name__}: {e}", file=sys.stderr)
            print("未落盘任何规则变更。", file=sys.stderr)
            return 3

    if not args.yes:
        ok = _prompt_yes_no("预演通过。是否将该修复写入版本并切换为 active 规则？", default_no=True)
        if not ok:
            print("已取消。")
            return 2

    snap = write_version_snapshot(
        repo_root=repo_root,
        app_rules=suggestion.app_rules_after,
        audit_rules=suggestion.audit_rules_after,
        compiled_rules=compiled2,
    )
    pointer_path = update_active_pointer(repo_root, snap)

    print("已写入版本并切换 active：")
    print(f"- app: {snap.app_rules_path}")
    print(f"- audit: {snap.audit_rules_path}")
    print(f"- compiled: {snap.compiled_rules_path}")
    print(f"- active pointer: {pointer_path}")
    return 0


def cmd_rules_show_active(_args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module()
    active = load_active_pointer(repo_root)
    if active is None:
        print("当前未设置 active_rules.json，将使用 rules/app_rules.yaml + rules/audit_rules.yaml（并编译到 rules/compiled_rules.yaml）。")
        return 0
    print("当前 active 规则：")
    print(f"- app: {active.app_rules}")
    print(f"- audit: {active.audit_rules}")
    print(f"- compiled: {active.compiled_rules}")
    return 0


def cmd_rules_set_active(args: argparse.Namespace) -> int:
    repo_root = repo_root_from_module().resolve()

    paths = [Path(args.app).resolve(), Path(args.audit).resolve(), Path(args.compiled).resolve()]
    for p in paths:
        if not p.exists():
            print(f"文件不存在：{p}", file=sys.stderr)
            return 2
        try:
            p.relative_to(repo_root)
        except ValueError:
            print(f"规则路径必须位于仓库内：{p}", file=sys.stderr)
            return 2

    if not args.yes:
        ok = _prompt_yes_no("将直接更新 active_rules.json（不校验内容正确性）。继续吗？", default_no=True)
        if not ok:
            print("已取消。")
            return 2

    snap = VersionedRules(
        app_rules_path=paths[0],
        audit_rules_path=paths[1],
        compiled_rules_path=paths[2],
    )
    pointer_path = update_active_pointer(repo_root, snap)
    print(f"已更新：{pointer_path}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """清理指定工作目录内由本工具生成的目录。"""
    include_reports = bool(getattr(args, "include_reports", False))
    try:
        targets = cleanup_targets(Path(args.workdir), include_reports=include_reports)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    report_dir = Path(args.workdir).resolve() / "凭证审核输出"
    if not include_reports and report_dir.exists():
        print(
            f"提示：检测到报告目录未纳入清理：{report_dir}（如需删除请加 --include-reports）",
            file=sys.stderr,
        )

    if not targets:
        print("没有需要清理的文件或目录。")
        return 0

    def _label(target: Path) -> str:
        kind = classify_cleanup_target(target)
        tag = {"report": "报告输出", "temp": "临时目录"}.get(kind, "其他")
        return f"[{tag}] {target}"

    if args.dry_run:
        for target in targets:
            print(f"[DRY-RUN] 将删除: {_label(target)}")
        print(f"\n[DRY-RUN] 以下项目将被删除: {len(targets)} 项")
        return 0
    if not args.yes:
        for target in targets:
            print(f"将删除: {_label(target)}", file=sys.stderr)
        print(
            "清理会永久删除以上目录。请先使用 --dry-run 检查，并显式传入 --yes。"
            + ("（已包含报告输出）" if include_reports else ""),
            file=sys.stderr,
        )
        return 2
    result = delete_cleanup_targets(targets)
    for target in result.deleted:
        print(f"已删除: {_label(target)}")
    for target, error in result.failed:
        print(f"删除失败 {target}: {error}", file=sys.stderr)
    if result.failed:
        return 1
    make_logger().info(f"清理完成，共删除 {len(result.deleted)} 项")
    return 0




def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voucher-audit", description="凭证审核 skill CLI（规则 + 脚本）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("preview", help="预览将执行的审核事项（不修改文件）")
    sp.add_argument("--workdir", default=".", help="工作目录（可选；仅用于展示路径）")
    sp.add_argument("--show-inputs", action="store_true", help="附带展示 inputs/sheets 配置")
    sp.set_defaults(func=cmd_preview)

    si = sub.add_parser("inspect", help="脚本化检查输入文件/sheet/列（不修改文件）")
    si.add_argument("--workdir", default=".", help="工作目录（包含 数据汇总.xlsx、考核表输出.xlsx）")
    si.set_defaults(func=cmd_inspect)

    sr = sub.add_parser("run", help="执行审核：preview -> 确认 -> 生成报告（可选源文件标注）")
    sr.add_argument("--workdir", default=".", help="工作目录（包含 数据汇总.xlsx、考核表输出.xlsx）")
    sr.add_argument("--yes", action="store_true", help="跳过交互确认（仍建议谨慎使用）")
    sr.add_argument("--month", type=int, default=None, help="目标月份（默认自动从数据最大月推断）")
    sr.add_argument("--include-rule-id", action="append", default=None, help="仅执行指定规则ID（可多次传入）")
    sr.add_argument("--enable-ai", action="store_true", default=None, help="启用 AI 复核（需要 OPENAI_API_KEY）")
    sr.add_argument("--openai-api-key", default=None, help="直接传入 API Key（不推荐；默认读环境变量）")
    sr.add_argument("--openai-base-url", default=None, help="Base URL（可选）")
    sr.add_argument("--openai-model", default=None, help="模型（可选）")
    ann = sr.add_mutually_exclusive_group()
    ann.add_argument("--annotate", dest="annotate", action="store_true", default=None, help="启用源文件标注（会修改源 Excel）")
    ann.add_argument("--no-annotate", dest="annotate", action="store_false", help="禁用源文件标注")
    sr.add_argument("--yes-annotate", action="store_true", help="跳过“修改源Excel”的二次确认")
    sr.set_defaults(func=cmd_run)

    sx = sub.add_parser("repair", help="针对缺文件/缺sheet/缺列等错误，生成修复补丁并版本化（需确认）")
    sx.add_argument("--workdir", default=".", help="工作目录（包含 数据汇总.xlsx、考核表输出.xlsx）")
    sx.add_argument("--yes", action="store_true", help="跳过交互确认（仍建议谨慎使用）")
    sx.set_defaults(func=cmd_repair)

    sc = sub.add_parser("cleanup", help="清理临时目录；报告输出需 --include-reports")
    sc.add_argument("--workdir", default=".", help="只清理该目录内由本工具生成的目录")
    sc.add_argument("--dry-run", action="store_true", help="仅列出要删除的文件，不实际删除")
    sc.add_argument(
        "--include-reports",
        action="store_true",
        help="同时删除 凭证审核输出（审核报告目录，默认不删）",
    )
    sc.add_argument("--yes", action="store_true", help="确认永久删除列出的目录")
    sc.set_defaults(func=cmd_cleanup)

    rules = sub.add_parser("rules", help="规则管理")
    rules_sub = rules.add_subparsers(dest="rules_cmd", required=True)

    rs = rules_sub.add_parser("show-active", help="显示当前 active 规则指针")
    rs.set_defaults(func=cmd_rules_show_active)

    rset = rules_sub.add_parser("set-active", help="手动设置 active_rules.json（危险操作，需确认）")
    rset.add_argument("--yes", action="store_true", help="跳过交互确认（仍建议谨慎使用）")
    rset.add_argument("--app", required=True, help="app_rules 路径")
    rset.add_argument("--audit", required=True, help="audit_rules 路径")
    rset.add_argument("--compiled", required=True, help="compiled_rules 路径")
    rset.set_defaults(func=cmd_rules_set_active)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if hasattr(args, "workdir"):
        args.workdir = str(Path(args.workdir).resolve())
    return int(args.func(args))

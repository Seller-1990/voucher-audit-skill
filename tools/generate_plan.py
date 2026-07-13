#!/usr/bin/env python
"""规则预览工具 - 生成规则执行计划"""

import sys
import argparse
import json
import yaml
from pathlib import Path
from typing import List, Dict, Any


def load_rules(rules_path: Path) -> Dict[str, Any]:
    """加载审核规则"""
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def build_execution_plan(rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """构建执行计划"""
    checks = rules.get("checks", [])
    execution_plan = []

    for check in checks:
        execution_plan.append({
            "rule_id": check.get("id", ""),
            "rule_name": check.get("name", ""),
            "severity": check.get("severity", "未知"),
            "scope": check.get("scope", ""),
            "type": check.get("type", ""),
            "description": check.get("description", ""),
            "fields": check.get("params", {}).get("key_fields", []),
        })

    return execution_plan


def print_plan_table(plan: List[Dict[str, Any]]):
    """格式化输出执行计划"""
    cols = ["规则ID", "规则名称", "严重度", "Scope", "Type", "描述"]
    rows = []

    for item in plan:
        rows.append([
            item["rule_id"],
            item["rule_name"][:40],
            item["severity"],
            item["scope"],
            item["type"],
            item["description"][:60],
        ])

    # 计算列宽
    widths = [len(c) for c in cols]
    for r in rows:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], len(v))

    # 输出表格
    header = " | ".join(v.ljust(widths[i]) for i, v in enumerate(cols))
    separator = "-+-".join("-" * w for w in widths)

    print("\n📋 凭证审核执行计划:")
    print(header)
    print(separator)

    for r in rows:
        print(" | ".join(v.ljust(widths[i]) for i, v in enumerate(r)))


def export_markdown(plan: List[Dict[str, Any]], output_path: Path):
    """导出为 Markdown 格式"""
    lines = [
        "# 凭证审核执行计划",
        "",
        f"生成时间: {Path(__file__).stat().st_mtime}",
        f"规则总数: {len(plan)}",
        "",
        "",
    ]

    # 按严重度分组
    by_severity = {}
    for item in plan:
        severity = item["severity"]
        if severity not in by_severity:
            by_severity[severity] = []
        by_severity[severity].append(item)

    for severity in ["错误", "需确认", "信息"]:
        if severity in by_severity:
            lines.append(f"## {severity}级别 ({len(by_severity[severity])} 条)")

            for item in by_severity[severity]:
                lines.append(f"### {item['rule_id']} - {item['rule_name']}")
                lines.append(f"- **Scope**: `{item['scope']}`")
                lines.append(f"- **Type**: `{item['type']}`")
                lines.append(f"- **描述**: {item['description']}")
                if item['fields']:
                    lines.append(f"- **检查字段**: `{', '.join(item['fields'])}`")
                lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ Markdown 已导出到: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="生成凭证审核执行计划")
    parser.add_argument("--rules", default="rules/compiled_rules.yaml",
                        help="规则文件路径")
    parser.add_argument("--output", "-o", help="输出 Markdown 文件路径（可选）")
    parser.add_argument("--json", "-j", help="输出 JSON 文件路径（可选）")

    args = parser.parse_args()
    rules_path = Path(args.rules).resolve()

    print(f"📄 读取规则文件: {rules_path}")
    rules = load_rules(rules_path)

    plan = build_execution_plan(rules)
    print_plan_table(plan)

    # 统计
    severity_count = {}
    for item in plan:
        severity_count[item["severity"]] = severity_count.get(item["severity"], 0) + 1

    print(f"\n📊 统计:")
    for severity, count in severity_count.items():
        print(f"   {severity}: {count} 条")

    # 导出
    if args.output:
        export_markdown(plan, Path(args.output))

    if args.json:
        import json
        Path(args.json).write_text(
            json.dumps({"plan": plan, "statistics": severity_count}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print(f"✅ JSON 已导出到: {args.json}")


if __name__ == "__main__":
    main()
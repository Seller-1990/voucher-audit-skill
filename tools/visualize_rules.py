#!/usr/bin/env python
"""规则可视化工具 - 生成规则统计图表"""

import sys
import argparse
import json
import yaml
from pathlib import Path
from typing import Dict, List, Any
from collections import Counter


def load_compiled_rules(rules_path: Path) -> Dict[str, Any]:
    """加载编译后的规则"""
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_audit_results(report_path: Path) -> Dict[str, Any]:
    """加载审核结果"""
    try:
        import pandas as pd
        df = pd.read_excel(report_path, sheet_name=None)

        results = {
            "total_rules": 0,
            "error_level": 0,
            "warning_level": 0,
            "info_level": 0,
             "by_type": Counter(),
            "by_scope": Counter(),
            "triggered_rules": set(),
        }

        for sheet_name, sheet_df in df.items():
            rule_count = len(sheet_df)
            results["total_rules"] += rule_count

            # 统计严重度
            severity_col = None
            for col in ["严重度", "severity", "Level"]:
                if col in sheet_df.columns:
                    severity_col = col
                    break

            if severity_col:
                severity_counts = sheet_df[severity_col].value_counts()
                results["error_level"] += int(severity_counts.get("错误", 0))
                results["warning_level"] += int(severity_counts.get("需确认", 0))
                results["info_level"] += int(severity_counts.get("信息", 0))

            # 记录触发的规则
            rule_id_col = "规则ID" if "规则ID" in sheet_df.columns else ("rule_id" if "rule_id" in sheet_df.columns else None)
            if rule_id_col:
                triggered = sheet_df[rule_id_col].dropna().tolist()
                results["triggered_rules"].update(triggered)

        return results

    except Exception as e:
        print(f"错误：无法读取审核报告 {report_path}: {e}")
        return {}


def visualize_rules(stats: Dict[str, Any]) -> str:
    """生成ASCII可视化图表"""
    lines = [
        "\n" + "="*60,
        "📋 凭证审核规则统计可视化",
        "="*60,
    ]

    # 1. 严重度分布
    lines.append("\n【严重度分布】")
    lines.append("-" * 40)
    severity_total = stats["error_level"] + stats["warning_level"] + stats["info_level"]
    if severity_total > 0:
        error_pct = (stats["error_level"] / severity_total) * 100
        warning_pct = (stats["warning_level"] / severity_total) * 100
        info_pct = (stats["info_level"] / severity_total) * 100

        lines.append(f"  错误:  [{'■' * int(error_pct/2)}{'░' * (20-int(error_pct/2))}] {error_pct:.1f}% ({stats['error_level']}条)")
        lines.append(f"  需确认: [{'■' * int(warning_pct/2)}{'░' * (20-int(warning_pct/2))}] {warning_pct:.1f}% ({stats['warning_level']}条)")
        lines.append(f"  信息:  [{'■' * int(info_pct/2)}{'░' * (20-int(info_pct/2))}] {info_pct:.1f}% ({stats['info_level']}条)")
    else:
        lines.append("  （无数据）")

    # 2. 规则类型分布
    if stats.get("by_type"):
        lines.append("\n【规则类型分布】")
        lines.append("-" * 40)
        for rule_type, count in stats["by_type"].most_common():
            bars = "■" * min(count, 10)
            lines.append(f"  {rule_type}: {count:2d}条 {bars}")

    # 3. 触发情况
    if stats["triggered_rules"]:
        lines.append(f"\n【已触发规则 ({len(stats['triggered_rules'])}条)】")
        lines.append("-" * 40)
        for rule_id in sorted(stats["triggered_rules"]):
            lines.append(f"  • {rule_id}")
    else:
        lines.append("\n【已触发规则】")
        lines.append("-" * 40)
        lines.append("  （所有规则均未触发）")

    # 4. 优化建议
    lines.append("\n【💡 优化建议】")
    lines.append("-" * 40)

    # 建议1：未触发规则
    untriggered = 16 - len(stats["triggered_rules"])
    if untriggered > 0:
        lines.append(f"  1. 有 {untriggered} 条规则未触发，建议检查阈值或业务规则")
    else:
        lines.append(f"  1. 所有规则均有触发，覆盖率良好")

    # 建议2：错误级别过高
    if stats["error_level"] > 5:
        lines.append(f"  2. 错误级别问题较多({stats['error_level']}条)，建议降级为'需确认'")
    elif stats["error_level"] == 0:
        lines.append(f"  2. 无错误级别问题，建议保持")

    lines.append("\n" + "="*60)

    return "\n".join(lines)


def export_chart_data(stats: Dict[str, Any], output_path: Path):
    """导出图表数据为JSON"""
    data = {
        "statistics": {
            "total_rules": stats["total_rules"],
            "error_level": stats["error_level"],
            "warning_level": stats["warning_level"],
            "info_level": stats["info_level"],
            "triggered_rules": len(stats["triggered_rules"]),
        },
        "by_severity": {
            "error": stats["error_level"],
            "warning": stats["warning_level"],
            "info": stats["info_level"],
        },
        "triggered_rules": sorted(list(stats["triggered_rules"])),
    }

    output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"✅ 图表数据已导出到: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="凭证审核规则可视化工具")
    parser.add_argument("--rules", default="rules/compiled_rules.yaml",
                        help="规则文件路径")
    parser.add_argument("--report", help="审核报告路径（可选，用于显示触发统计）")
    parser.add_argument("--chart-data", "-c", help="导出图表数据JSON文件（可选）")

    args = parser.parse_args()
    rules_path = Path(args.rules).resolve()

    # 加载规则
    print(f"📄 读取规则文件: {rules_path}")
    rules = load_compiled_rules(rules_path)

    # 统计规则
    stats = {
        "total_rules": 0,
        "error_level": 0,
        "warning_level": 0,
        "info_level": 0,
        "by_type": Counter(),
        "triggered_rules": set(),
    }

    checks = rules.get("checks", [])
    for check in checks:
        stats["total_rules"] += 1
        severity = check.get("severity", "未知")
        rule_type = check.get("type", "unknown")

        if severity in ["错误", "Error", "ERROR"]:
            stats["error_level"] += 1
        elif severity in ["需确认", "Warning", "WARNING"]:
            stats["warning_level"] += 1
        else:
            stats["info_level"] += 1

        stats["by_type"][rule_type] += 1

    # 如果有审核报告，加载统计数据
    if args.report:
        report_path = Path(args.report).resolve()
        if report_path.exists():
            print(f"📊 加载审核报告: {report_path}")
            report_stats = load_audit_results(report_path)
            stats["triggered_rules"] = report_stats.get("triggered_rules", set())

    # 输出可视化
    visualization = visualize_rules(stats)
    print(visualization)

    # 导出图表数据
    if args.chart_data:
        export_chart_data(stats, Path(args.chart_data))


if __name__ == "__main__":
    main()
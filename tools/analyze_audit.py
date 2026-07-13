#!/usr/bin/env python
"""规则优化建议生成器 - 根据审核结果生成规则调整建议"""

import sys
import json
import argparse
from pathlib import Path
from typing import List, Dict, Any


def analyze_audit_report(report_path: Path) -> Dict[str, Any]:
    """分析审核报告，提取关键统计信息"""
    try:
        import pandas as pd
        df = pd.read_excel(report_path, sheet_name=None)

        stats = {
            "total_rules": 0,
            "error_level": 0,
            "warning_level": 0,
            "info_level": 0,
            "rule_breakdown": {},
            "top_issues": [],
        }

        for sheet_name, sheet_df in df.items():
            rule_count = len(sheet_df)
            stats["total_rules"] += rule_count

            # 统计严重度（支持中文和英文列名）
            severity_col = None
            for col in ["严重度", "severity", "Level"]:
                if col in sheet_df.columns:
                    severity_col = col
                    break

            if severity_col:
                severity_counts = sheet_df[severity_col].value_counts()
                stats["error_level"] += int(severity_counts.get("错误", 0))
                stats["warning_level"] += int(severity_counts.get("需确认", 0))
                stats["info_level"] += int(severity_counts.get("信息", 0))

            # 规则分解
            rule_id_col = "规则ID" if "规则ID" in sheet_df.columns else ("rule_id" if "rule_id" in sheet_df.columns else None)
            rule_name_col = "规则名称" if "规则名称" in sheet_df.columns else ("rule_name" if "rule_name" in sheet_df.columns else None)

            if rule_id_col and rule_name_col:
                for _, row in sheet_df.iterrows():
                    rule_id = row.get(rule_id_col, "")
                    severity = row.get(severity_col, "未知")
                    stats["rule_breakdown"][rule_id] = {
                        "rule_name": row.get(rule_name_col, ""),
                        "severity": severity,
                        "triggered": True,
                    }

        return stats

    except Exception as e:
        print(f"错误：无法读取审核报告 {report_path}: {e}")
        return {}


def generate_recommendations(stats: Dict[str, Any]) -> List[Dict[str, str]]:
    """根据统计信息生成优化建议"""
    recommendations = []

    # 规则数量检查
    if stats["total_rules"] > 20:
        recommendations.append({
            "level": "中",
            "category": "规则数量",
            "suggestion": f"当前共有 {stats['total_rules']} 条审核规则，建议考虑合并相似的规则以提升效率",
            "impact": "减少误报率，提高审核聚焦度"
        })

    # 严重度分布检查
    if stats["error_level"] > stats["warning_level"] * 2:
        recommendations.append({
            "level": "高",
            "category": "严重度分布",
            "suggestion": f"错误级别问题过多（{stats['error_level']} 条），建议降级部分非关键规则为\"需确认\"级别",
            "impact": "减少紧急问题数量，优先处理真正重要的问题"
        })

    # 未触发规则检查
    # TODO: 需要在审核系统中记录每个规则的触发情况
    recommendations.append({
        "level": "低",
        "category": "规则覆盖率",
        "suggestion": "定期检查是否有规则未被触发，考虑调整业务规则或阈值",
        "impact": "确保规则设置符合实际业务场景"
    })

    return recommendations


def generate_yaml_patches(recommendations: List[Dict[str, str]], rules_path: Path) -> str:
    """生成 YAML 格式的规则调整建议"""
    patches = []

    for rec in recommendations:
        patches.append(f"# {rec['category']} - {rec['level']} 建议")
        patches.append(f"# {rec['suggestion']}")
        patches.append(f"# 影响: {rec['impact']}")
        patches.append("")
        patches.append("# 修改建议示例:")
        patches.append("# 1. 降级非关键规则的严重度")
        patches.append("# 2. 调整阈值参数")
        patches.append("# 3. 合并相似规则")
        patches.append("")

    return "\n".join(patches)


def main():
    parser = argparse.ArgumentParser(description="分析审核报告并生成优化建议")
    parser.add_argument("report", help="审核报告路径 (.xlsx)")
    parser.add_argument("--output", "-o", help="输出建议文件路径")
    parser.add_argument("--format", "-f", choices=["json", "yaml", "text"], default="text",
                        help="输出格式")

    args = parser.parse_args()
    report_path = Path(args.report).resolve()

    print(f"📖 分析审核报告: {report_path}")
    stats = analyze_audit_report(report_path)

    if not stats:
        sys.exit(1)

    # 统计输出
    print(f"\n📊 统计信息:")
    print(f"   总规则数: {stats['total_rules']}")
    print(f"   错误级别: {stats['error_level']}")
    print(f"   需确认级别: {stats['warning_level']}")
    print(f"   信息级别: {stats['info_level']}")

    # 生成建议
    recommendations = generate_recommendations(stats)

    if recommendations:
        print(f"\n💡 优化建议 ({len(recommendations)} 条):")
        for i, rec in enumerate(recommendations, 1):
            print(f"\n   {i}. [{rec['level']}] {rec['category']}")
            print(f"      {rec['suggestion']}")
            print(f"      ⚠️  影响: {rec['impact']}")
    else:
        print("\n✅ 规则配置良好，无明显优化建议")

    # 输出
    if args.format == "json":
        output = {
            "stats": stats,
            "recommendations": recommendations
        }
        print(f"\n📋 JSON 输出:")
        print(json.dumps(output, ensure_ascii=False, indent=2))
        if args.output:
            Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))
    elif args.format == "yaml":
        patches = generate_yaml_patches(recommendations, report_path)
        print(f"\n📝 YAML 调整建议:")
        print(patches)
    else:
        print(f"\n📝 调整建议 (文本格式):")
        for rec in recommendations:
            print(f"\n### {rec['category']} [{rec['level']}]")
            print(f"{rec['suggestion']}")
            print(f"* 影响: {rec['impact']}")


if __name__ == "__main__":
    main()
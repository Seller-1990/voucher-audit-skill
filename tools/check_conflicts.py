#!/usr/bin/env python
"""规则冲突检测 - 检测重复、重叠的审核规则"""

import sys
import argparse
import yaml
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict


def load_rules(rules_path: Path) -> Dict[str, Any]:
    """加载规则文件"""
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def analyze_rule_similarity(rule1: Dict[str, Any], rule2: Dict[str, Any]) -> float:
    """分析两个规则的相似度"""
    similarity = 0.0

    # 1. Scope 相同
    if rule1.get("scope") == rule2.get("scope"):
        similarity += 0.3

    # 2. Type 相同
    if rule1.get("type") == rule2.get("type"):
        similarity += 0.3

    # 3. 关键字段重叠
    params1 = rule1.get("params", {})
    params2 = rule2.get("params", {})

    fields1 = set(params1.get("key_fields", []))
    fields2 = set(params2.get("key_fields", []))

    if fields1 & fields2:
        overlap = len(fields1 & fields2) / max(len(fields1 | fields2), 1)
        similarity += overlap * 0.4

    return similarity


def detect_conflicts(rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """检测规则冲突"""
    conflicts = []
    checks = rules.get("checks", [])

    # 检查重复规则ID
    rule_ids = [r.get("id") for r in checks if r.get("id")]
    duplicates = [id for id in rule_ids if rule_ids.count(id) > 1]

    for duplicate_id in duplicates:
        matching_rules = [r for r in checks if r.get("id") == duplicate_id]
        conflicts.append({
            "type": "duplicate_id",
            "severity": "严重",
            "message": f"规则ID重复: {duplicate_id}",
            "rules": matching_rules
        })

    # 检查规则冲突（高相似度）
    for i in range(len(checks)):
        for j in range(i + 1, len(checks)):
            rule1 = checks[i]
            rule2 = checks[j]

            # 跳过重复ID的规则
            if rule1.get("id") == rule2.get("id"):
                continue

            # 计算相似度
            similarity = analyze_rule_similarity(rule1, rule2)

            if similarity > 0.7:  # 阈值：70%相似度
                conflicts.append({
                    "type": "overlap",
                    "severity": "警告" if similarity > 0.85 else "注意",
                    "message": f"规则可能重叠（相似度: {similarity:.1%}）",
                    "rule1": rule1,
                    "rule2": rule2,
                    "similarity": similarity
                })

    return conflicts


def generate_conflict_report(conflicts: List[Dict[str, Any]]) -> str:
    """生成冲突报告"""
    lines = [
        "\n" + "="*70,
        "⚠️  规则冲突检测报告",
        "="*70,
    ]

    if not conflicts:
        lines.append("\n✅ 没有检测到规则冲突")
        lines.append("\n" + "="*70)
        return "\n".join(lines)

    lines.append(f"\n共发现 {len(conflicts)} 个冲突问题\n")

    for idx, conflict in enumerate(conflicts, 1):
        severity = conflict["severity"]
        symbol = "🔴" if severity == "严重" else "🟡" if severity == "警告" else "🔵"
        lines.append(f"{symbol} [{idx}] {conflict['type'].upper()} - {severity}")

        if conflict['type'] == "duplicate_id":
            lines.append(f"\n   {conflict['message']}")
            lines.append(f"   规则ID: {conflict['rules'][0].get('id', 'N/A')}")
            lines.append(f"   描述: {conflict['rules'][0].get('name', 'N/A')}")

        elif conflict['type'] == "overlap":
            lines.append(f"\n   {conflict['message']}")
            lines.append(f"\n   规则1:")
            lines.append(f"      ID: {conflict['rule1'].get('id')}")
            lines.append(f"      名称: {conflict['rule1'].get('name')}")
            lines.append(f"      类型: {conflict['rule1'].get('type')} ({conflict['rule1'].get('scope')})")
            lines.append(f"      描述: {conflict['rule1'].get('description')}")

            lines.append(f"\n   规则2:")
            lines.append(f"      ID: {conflict['rule2'].get('id')}")
            lines.append(f"      名称: {conflict['rule2'].get('name')}")
            lines.append(f"      类型: {conflict['rule2'].get('type')} ({conflict['rule2'].get('scope')})")
            lines.append(f"      描述: {conflict['rule2'].get('description')}")

            lines.append(f"\n   相似度: {conflict['similarity']:.1%}")

    lines.append("\n" + "="*70)
    lines.append("\n💡 修复建议:")
    lines.append("   1. 删除重复的规则ID")
    lines.append("   2. 合并相似的规则")
    lines.append("   3. 调整规则类型或参数以区分功能")

    lines.append("\n" + "="*70)

    return "\n".join(lines)


def auto_fix_conflicts(conflicts: List[Dict[str, Any]], rules: Dict[str, Any]) -> int:
    """自动修复简单冲突"""
    fixes = 0
    checks = rules.get("checks", [])
    indices_to_remove = set()

    for conflict in conflicts:
        if conflict['type'] == "duplicate_id":
            # 删除重复的规则（保留第一个，删除后面的）
            rule_id = conflict['rules'][0].get("id")
            for i, rule in enumerate(checks):
                if rule.get("id") == rule_id and i != 0:
                    indices_to_remove.add(i)

        elif conflict['type'] == "overlap":
            similarity = conflict['similarity']
            if similarity > 0.85:  # 高相似度，删除重复的
                lines.append("   检测到高相似度规则，建议删除其中一条")
                # 这里可以根据规则名称、ID等判断保留哪一个
                # 简单策略：删除ID较大的
                rule1_id = conflict['rule1'].get("id", "")
                rule2_id = conflict['rule2'].get("id", "")
                indices_to_remove.add(i for i, r in enumerate(checks) if r.get("id") == rule2_id)

    # 反向排序，从后往前删除
    for idx in sorted(list(indices_to_remove), reverse=True):
        removed_rule = checks.pop(idx)
        lines.append(f"   🔧 已删除重复规则: {removed_rule.get('id')} - {removed_rule.get('name')}")
        fixes += 1

    return fixes


def main():
    parser = argparse.ArgumentParser(
        description="规则冲突检测工具 - 检测重复、重叠的审核规则",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 检查规则冲突
  python -m tools.check_conflicts --rules rules/audit_rules.yaml

  # 查看自动修复建议
  python -m tools.check_conflicts --rules rules/audit_rules.yaml --fix
        """
    )
    parser.add_argument("--rules", default="rules/audit_rules.yaml",
                        help="规则文件路径")
    parser.add_argument("--fix", action="store_true", help="尝试自动修复冲突")
    parser.add_argument("--output", "-o", help="保存修复后的规则文件")

    args = parser.parse_args()
    rules_path = Path(args.rules).resolve()

    print(f"📄 检查规则文件: {rules_path}")

    # 加载规则
    rules = load_rules(rules_path)
    checks = rules.get("checks", [])

    print(f"当前规则数: {len(checks)}")

    # 检测冲突
    print("\n🔍 正在检测规则冲突...")
    conflicts = detect_conflicts(rules)

    # 生成报告
    report = generate_conflict_report(conflicts)
    print(report)

    # 自动修复
    if args.fix and conflicts:
        print("\n🔧 尝试自动修复冲突...")
        fixes = auto_fix_conflicts(conflicts, rules)

        if fixes > 0:
            # 保存修复后的规则
            output_path = Path(args.output or rules_path)
            output_path.write_text(
                yaml.safe_dump(rules, allow_unicode=True, sort_keys=False).replace("\r\n", "\n"),
                encoding="utf-8"
            )
            print(f"\n✅ 已自动修复 {fixes} 个冲突，保存到: {output_path}")
        else:
            print("\n⚠️  没有自动修复的冲突，请手动处理")
    elif args.fix and not conflicts:
        print("\n✅ 没有冲突需要修复")


if __name__ == "__main__":
    main()
#!/usr/bin/env python
"""AI 规则编辑器 - 理解自然语言描述，自动生成/修改审核规则"""

import sys
import argparse
import json
import yaml
import re
from pathlib import Path
from typing import Dict, List, Any, Optional
import os


def load_rules(rules_path: Path) -> Dict[str, Any]:
    """加载规则文件"""
    with open(rules_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def save_rules(rules: Dict[str, Any], rules_path: Path) -> None:
    """保存规则文件"""
    rules_path.write_text(
        yaml.safe_dump(rules, allow_unicode=True, sort_keys=False).replace("\r\n", "\n"),
        encoding="utf-8"
    )


def detect_rule_type(description: str) -> Optional[str]:
    """根据描述自动检测规则类型"""
    keywords = {
        "combo_drift": ["主映射", "漂移", "一致性", "历史", "对比"],
        "rev_cost_zero_mismatch": ["收入成本", "零值", "不匹配", "0", "相等"],
        "forbidden_regex": ["禁止", "不得", "不允许", "不能包含", "严禁"],
        "hard_rule": ["必须", "应为", "应该", "严格", "严格规则"],
        "distinct_count": ["多", "唯一", "重复", "相同", "多个"],
        "neg_profit_ratio": ["负毛利", "利润", "负数"],
        "outsourcing_missing_cost": ["外包", "缺", "没有", "0"],
        "metric_pp_change": ["指标", "同比", "波动", "变化", "增长", "下降"],
        "value_pp_change": ["金额", "返费", "挂靠成本", "数值", "同比"],
        "ratio_pp_change": ["比率", "率", "占比", "比例"],
    }

    description_lower = description.lower()

    for rule_type, keywords_list in keywords.items():
        for keyword in keywords_list:
            if keyword in description_lower:
                return rule_type

    return None


def generate_rule_yaml(description: str, scope: str = "income_cost",
                       severity: str = "需确认") -> Dict[str, Any]:
    """根据描述生成规则YAML模板"""

    rule_type = detect_rule_type(description)
    if not rule_type:
        rule_type = "combo_drift"  # 默认类型

    # 生成规则ID
    import random
    import string
    rule_id = f"INC_{random.choice(string.ascii_uppercase)}{random.randint(100,999)}"

    # 生成规则名称
    rule_name = description[:40] + "..." if len(description) > 40 else description

    rule = {
        "id": rule_id,
        "name": rule_name,
        "type": rule_type,
        "scope": scope,
        "severity": severity,
        "description": description,
        "params": {
            "key_fields": ["主体账簿", "月", "实际客户", "项目"],
            "threshold": 0.1,
        }
    }

    return rule


def explain_rule_suggestion(rule: Dict[str, Any]) -> str:
    """解释规则建议"""
    lines = [
        f"\n💡 规则建议:",
        f"   ID: {rule['id']}",
        f"   名称: {rule['name']}",
        f"   类型: {rule['type']} ({rule['scope']})",
        f"   严重度: {rule['severity']}",
        f"   描述: {rule['description']}",
    ]

    if rule['params']:
        lines.append("   参数:")
        for key, value in rule['params'].items():
            lines.append(f"     - {key}: {value}")

    return "\n".join(lines)


def interactive_rule_editor(description: str, existing_rules: List[Dict[str, Any]],
                           rules_path: Path, enable_ai: bool = True) -> Dict[str, Any]:
    """交互式规则编辑器"""

    # 尝试AI生成（如果启用）
    if enable_ai:
        print(f"🤖 AI 规则生成器: '{description}'")
        new_rule = generate_rule_yaml(description)

        # 检查是否与现有规则重复
        conflicts = check_rule_conflicts(new_rule, existing_rules)
        if conflicts:
            print(f"\n⚠️  检测到可能的规则冲突:")
            for conflict in conflicts:
                print(f"   - {conflict}")
            print("\n正在调整规则...")

            # 调整参数
            new_rule['params'] = {
                "key_fields": ["主体账簿", "月", "实际客户"],
                "threshold": 0.15,
            }

        print(explain_rule_suggestion(new_rule))
        print("\n是否应用此规则？[y/N] ", end="", flush=True)
        response = input().strip().lower()

        if response in {"y", "yes"}:
            return new_rule
        else:
            print("\n📝 请手动调整规则参数:")
            print("   1. 修改 description（规则描述）")
            print("   2. 修改 severity（严重度: 错误/需确认/信息）")
            print("   3. 修改 params（参数）")
            print("   4. 修改 type（规则类型）")
            print("\n请输入完整的规则YAML配置（输入空行结束）:")

            yaml_config = []
            while True:
                line = input()
                if not line.strip():
                    break
                yaml_config.append(line)

            yaml_text = "\n".join(yaml_config)
            custom_rule = yaml.safe_load(yaml_text)

            if not custom_rule or "description" not in custom_rule:
                print("❌ 配置无效，已取消")
                return {}

            return custom_rule

    else:
        # 无AI模式
        print(f"📝 请输入规则配置:")
        print("   需要的字段: id, name, type, scope, severity, description")
        print("\n输入完整YAML配置（空行结束）:")

        yaml_config = []
        while True:
            line = input()
            if not line.strip():
                break
            yaml_config.append(line)

        yaml_text = "\n".join(yaml_config)
        custom_rule = yaml.safe_load(yaml_text)

        if not custom_rule:
            print("❌ 配置无效")
            return {}

        return custom_rule


def check_rule_conflicts(new_rule: Dict[str, Any], existing_rules: List[Dict[str, Any]]) -> List[str]:
    """检查新规则与现有规则的冲突"""
    conflicts = []

    new_id = new_rule.get("id", "")
    new_scope = new_rule.get("scope", "")
    new_type = new_rule.get("type", "")

    for rule in existing_rules:
        existing_id = rule.get("id", "")
        existing_scope = rule.get("scope", "")
        existing_type = rule.get("type", "")

        # 同ID冲突
        if new_id and new_id == existing_id:
            conflicts.append(f"规则ID重复: {new_id}")

        # 同scope同type冲突
        if new_scope == existing_scope and new_type == existing_type:
            conflicts.append(f"规则类型重复: {new_scope}/{new_type}")

    return conflicts


def main():
    parser = argparse.ArgumentParser(
        description="AI 规则编辑器 - 理解自然语言描述，自动生成审核规则",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # AI生成规则
  python -m tools.edit_rules "检查收入成本表中实际客户是否必须对应账载客户"

  # 无AI模式手动编辑
  python -m tools.edit_rules "检查空行" --no-ai

  # 批量添加规则
  python -m tools.edit_rules --batch rules_new.yaml
        """
    )
    parser.add_argument("description", nargs="?", help="规则描述")
    parser.add_argument("--rules", default="rules/audit_rules.yaml",
                        help="规则文件路径")
    parser.add_argument("--no-ai", action="store_true", help="禁用AI生成，手动输入")
    parser.add_argument("--batch", help="批量添加规则模式（读取文件）")
    parser.add_argument("--output", "-o", help="输出文件路径（默认覆盖原文件）")

    args = parser.parse_args()

    # 批量模式
    if args.batch:
        print("📦 批量添加规则模式")

        descriptions = []  # 初始化
        batch_file = Path(args.batch)
        with open(batch_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.rstrip('\n\r')  # 去除所有换行符
                if line.strip():
                    descriptions.append(line)

        print(f"检测到 {len(descriptions)} 条规则描述\n")

        # 确定输出路径
        if args.output:
            output_path = Path(args.output)
            # 确保父目录存在
            output_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            # 默认保存到 rules/audit_rules.yaml
            output_path = rules_path.parent / "audit_rules.yaml"

        # 如果是纯描述文件，保存为新规则文件
        if batch_file.suffix in [".txt", ".md"]:
            rules_path = output_path
            rules = {"checks": []} if not rules_path.exists() else load_rules(rules_path)
            existing_checks = rules.get("checks", [])
        else:
            rules = load_rules(rules_path)
            existing_checks = rules.get("checks", [])

        added_count = 0
        for desc in descriptions:
            print(f"\n{'='*50}")
            new_rule = interactive_rule_editor(desc, existing_checks, rules_path, not args.no_ai)
            if new_rule:
                existing_checks.append(new_rule)
                added_count += 1

        # 保存规则
        rules["checks"] = existing_checks
        save_rules(rules, output_path)
        print(f"\n{'='*50}")
        print(f"✅ 成功添加 {added_count} 条规则到: {output_path}")

    # 单条模式
    elif args.description:
        rules_path = Path(args.rules).resolve()
        rules = load_rules(rules_path)
        existing_checks = rules.get("checks", [])

        print(f"📄 规则文件: {rules_path}")
        print(f"当前规则数: {len(existing_checks)}")

        new_rule = interactive_rule_editor(args.description, existing_checks, rules_path, not args.no_ai)

        if new_rule:
            existing_checks.append(new_rule)
            rules["checks"] = existing_checks

            output_path = Path(args.output or rules_path)
            save_rules(rules, output_path)
            print(f"\n✅ 规则已保存到: {output_path}")
        else:
            print("\n❌ 规则未被添加")

    # 无参数模式
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
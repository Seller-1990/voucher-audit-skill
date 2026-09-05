"""规则执行前预检（对抗性复核修复1/F2）：配置错误不再静默返回空结果。

每条规则在 run_checks 执行前，先用本模块校验其 YAML 所需列在数据表中是否存在、
必需参数是否齐全。校验失败 → 规则不执行，记入 skipped 列表，由 runner 在
命中统计中大声报告（而不是产出一份"看起来干净"的报告）。

设计取舍：不在 33 个检查分支内部逐个改造（侵入大、易漏），统一在调度层预检——
检查函数内部原有的 early-return 仍保留（对"数据为空"场景是正确语义）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd


class RuleConfigError(ValueError):
    """规则 YAML 配置无法执行（缺列/缺参数/参数非法）。"""


@dataclass
class RuleSkip:
    rule_id: str
    rule_name: str
    reason: str
    missing_columns: list[str] = field(default_factory=list)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        base = f"{self.rule_id}（{self.rule_name}）：{self.reason}"
        if self.missing_columns:
            base += f"；缺列: {'、'.join(self.missing_columns)}"
        return base


# 每种规则类型需要的数据列（"月"由调度层统一检查，不在此列）
# 组合式规则（嵌套 items/ratios/fields 引用的列）在 _extra_required_columns 动态收集
_TYPE_REQUIRED_COLUMNS: dict[str, dict[str, tuple[str, ...]]] = {
    "customer_consistency_check": {
        "income": ("主体账簿", "三级科目", "账载客户", "实际客户", "部门", "项目",
                   "净额收入", "全额收入", "成本合计", "项目毛利润", "月"),
        "mapping": ("主体账簿", "月", "业务类型", "账载客户", "部门", "项目", "实际客户"),
    },
    "rev_cost_zero_mismatch": {
        "income": ("主体账簿", "三级科目", "实际客户", "部门", "项目", "净额收入", "成本合计", "月"),
    },
    "headcount_data_check": {
        "aux": ("摘要", "凭证号", "月"),
    },
    "neg_profit_ratio": {
        "income": ("主体账簿", "三级科目", "实际客户", "月", "全额收入", "净额收入", "项目毛利润", "部门"),
    },
    "outsourcing_missing_cost": {
        "income": ("主体账簿", "三级科目", "账载客户", "实际客户", "部门", "项目", "月",
                   "全额收入", "成本合计", "工资", "第三方挂靠成本"),
    },
    "pp_change": {
        "income": ("主体账簿", "三级科目", "月", "净额收入", "项目毛利润", "结算人次",
                   "项目返费", "第三方挂靠成本", "全额收入"),
    },
    "mom_change": {
        "income": ("主体账簿", "三级科目", "实际客户", "部门", "月", "全额收入", "成本合计", "项目毛利润"),
    },
    "gm_high_ratio": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "全额收入", "项目毛利润"),
    },
    "rev_cost_inversion": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "部门", "项目", "全额收入", "成本合计"),
    },
    "headcount_rev_mismatch": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "部门", "净额收入", "结算人次", "成本合计"),
    },
    "social_headcount_mismatch": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "部门", "净额收入", "社保人数", "社保"),
    },
    "cost_ratio_high": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "部门", "全额收入", "项目返费", "第三方挂靠成本"),
    },
    "expense_ratio": {
        "income": ("主体账簿", "月", "三级科目", "实际客户", "部门", "全额收入", "项目福利费", "项目其他费用"),
    },
    "cost_sudden_appearance": {
        "income": ("主体账簿", "三级科目", "实际客户", "部门", "项目", "月",
                   "项目福利费", "项目其他费用", "第三方挂靠成本"),
    },
    "duplicate_row": {
        "income": ("主体账簿", "月", "内外", "三级科目", "账载客户", "实际客户", "部门", "项目",
                   "全额收入", "成本合计", "净额收入", "项目毛利润"),
    },
    "group_hq_unsettled": {
        "income": ("主体账簿", "月", "部门", "三级科目", "实际客户", "成本合计"),
    },
    "similar_customer_rename": {
        "income": ("实际客户", "月", "全额收入"),
    },
    "aux_wage_wrong_customer": {
        "aux": ("主体账簿", "月", "账载客户", "部门", "实际客户", "摘要", "三级科目", "本币"),
        "income": ("主体账簿", "月", "三级科目", "账载客户", "部门", "实际客户"),
    },
    "mixed_biz_type": {
        "income": ("主体账簿", "月", "账载客户", "三级科目", "全额收入"),
    },
    "rev_cost_biz_type_mismatch": {
        "income": ("主体账簿", "实际客户", "三级科目", "全额收入", "成本合计", "月"),
    },
    "same_amount_adjacent_months": {
        "income": ("主体账簿", "三级科目", "账载客户", "实际客户", "部门", "项目", "月",
                   "全额收入", "成本合计"),
    },
    "small_amount_wrong_dept": {
        "income": ("主体账簿", "实际客户", "部门", "月", "成本合计", "全额收入"),
    },
    "entity_switch_mapping_drift": {
        "income": ("主体账簿", "账载客户", "实际客户", "月"),
        "mapping": ("主体账簿", "月", "账载客户", "实际客户"),
    },
    "rebate_external_cost_reconcile": {
        "income": ("主体账簿", "账载客户", "实际客户", "部门", "三级科目", "月",
                   "项目返费", "第三方挂靠成本", "成本合计"),
        "aux": ("主体账簿", "账载客户", "一级科目", "二级科目", "三级科目", "月", "本币"),
    },
    # 旧类型（rare_combo/sealed_hint/drift_check/hard_rule/allowed_values/required_fields/
    # mapping_check/combo_drift/distinct_count/dept_multi_distinct_trigger 等）：
    # 无内置规则使用；预检按"不校验"处理，行为与修复前一致。
}


def _extra_required_columns(rule: dict[str, Any]) -> set[str]:
    """从 params 里动态引用的列名（items/ratios/fields 等）。"""
    params = rule.get("params") or {}
    out: set[str] = set()
    for it in params.get("items") or []:
        if isinstance(it, dict):
            for k in ("field", "numerator", "denominator", "guard_field"):
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    out.add(v.strip())
    for r in params.get("ratios") or []:
        if isinstance(r, dict):
            v = r.get("field")
            if isinstance(v, str) and v.strip():
                out.add(v.strip())
    for v in params.get("fields") or []:
        if isinstance(v, str) and v.strip():
            out.add(v.strip())
    return out


def check_rule_preconditions(
    rule: dict[str, Any],
    df_income: Optional[pd.DataFrame],
    df_aux: Optional[pd.DataFrame],
    df_mapping: Optional[pd.DataFrame],
) -> Optional[RuleSkip]:
    """执行前校验一条规则。返回 None=可以执行；返回 RuleSkip=跳过并报告。"""
    rule_id = str(rule.get("id") or "（无ID）")
    rule_name = str(rule.get("name") or rule_id)
    rtype = str(rule.get("type") or "")
    spec = _TYPE_REQUIRED_COLUMNS.get(rtype)
    if not spec:
        return None  # 未登记的类型不做预检（保持旧行为）

    def _missing(df: Optional[pd.DataFrame], cols: tuple[str, ...], which: str) -> list[str]:
        if df is None or df.empty:
            return []  # 空数据帧由执行层的"无数据"路径处理，不算配置错误
        have = set(str(c) for c in df.columns)
        return [c for c in cols if c not in have]

    missing: list[str] = []
    parts: list[str] = []
    if "income" in spec and df_income is not None:
        cols = tuple(spec["income"]) + tuple(_extra_required_columns(rule))
        m = _missing(df_income, cols, "收入成本表")
        if m:
            missing.extend(m)
            parts.append("收入成本表")
    if "aux" in spec and df_aux is not None:
        m = _missing(df_aux, tuple(spec["aux"]), "调整后序时账")
        if m:
            missing.extend(m)
            parts.append("调整后序时账")
    if "mapping" in spec:
        if df_mapping is None or df_mapping.empty:
            # 映射缺失对 customer_consistency 是数据缺失而非配置错误（子检查自身会处理）
            pass
        else:
            m = _missing(df_mapping, tuple(spec["mapping"]), "客户调整校验")
            if m:
                missing.extend(m)
                parts.append("客户调整校验")

    if missing:
        return RuleSkip(
            rule_id=rule_id,
            rule_name=rule_name,
            reason=f"{'、'.join(dict.fromkeys(parts))} 缺少规则所需列",
            missing_columns=missing,
        )
    return None

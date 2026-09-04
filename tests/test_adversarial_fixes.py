"""对抗性复核修复的回归测试：F1 双目标月、F2 预检跳过、F8 指针缺失报错。"""
from __future__ import annotations

import pandas as pd
import pytest

from voucher_audit.checks import run_checks
from voucher_audit.config import RuleConfig
from voucher_audit.rule_precheck import check_rule_preconditions
from voucher_audit.runner import pick_target_month, pick_target_months


def _rule(rid, rtype, scope="income_cost", **params):
    return {"id": rid, "name": rid, "description": "", "source": {}, "type": rtype, "scope": scope, "params": params}


def _income():
    return pd.DataFrame({
        "主体账簿": ["甲"], "月": [8], "三级科目": ["外包"], "账载客户": ["C1"],
        "实际客户": ["C1"], "部门": ["A部"], "项目": ["0"], "内外": ["外部"],
        "全额收入": [100.0], "净额收入": [100.0], "成本合计": [50.0], "项目毛利润": [50.0],
        "结算人次": [5], "社保人数": [0], "社保": [0.0],
    })


def _config(rules):
    return RuleConfig(raw={}, inputs=None, thresholds=None, ai=None, report_format={}, checks=rules)


def test_f1_pick_target_months_returns_both():
    aux = pd.DataFrame({"月": [1, 2, 8]})
    inc = pd.DataFrame({"月": [1, 2, 3, 7]})
    a, i = pick_target_months(aux, inc, "月", "月")
    assert a == 8 and i == 7
    # 旧接口兼容：max
    assert pick_target_month(aux, inc, "月", "月") == 8


def test_f1_run_checks_uses_separate_target_months():
    # aux 月=8 有数据、income 月=8 无数据（只有7月）：
    # 修复前 income 规则以月=8 跑空集静默"干净"；修复后 income 规则用自己的目标月 7
    aux = pd.DataFrame({
        "主体账簿": ["甲"], "月": [8], "凭证号": ["V1"], "摘要": ["Z5S20"],
    })
    inc = pd.DataFrame({
        "主体账簿": ["甲"], "月": [7], "三级科目": ["外包"], "账载客户": ["C1"],
        "实际客户": ["C1"], "部门": ["A部"], "项目": ["0"],
        "全额收入": [0.0], "净额收入": [0.0], "成本合计": [500.0], "项目毛利润": [-500.0],
    })
    rules = _config([
        _rule("T1", "rev_cost_zero_mismatch",
              key_fields=["主体账簿", "月", "三级科目", "实际客户", "部门", "项目"],
              revenue_field="净额收入", cost_field="成本合计",
              biz_type_field="三级科目", biz_type_keywords=["外包"], biz_type_strip_percent_suffix=True),
    ])
    _, _, income_dim, _, skipped = run_checks(
        rules, df_aux=aux, df_income=inc, df_mapping=None,
        target_month=7, target_month_aux=8,
    )
    assert not skipped
    # income 规则用月=7 的数据 → 命中（修复前用月=8 会静默为空）
    assert not income_dim.empty


def test_f2_precheck_skips_rule_with_missing_columns():
    rule = _rule("BAD", "rev_cost_zero_mismatch")
    inc = _income().drop(columns=["成本合计"])  # 缺关键列
    skip = check_rule_preconditions(rule, inc, None, None)
    assert skip is not None
    assert "成本合计" in skip.missing_columns


def test_f2_run_checks_reports_skipped_rules():
    rule = _rule("BAD", "neg_profit_ratio")
    inc = _income().drop(columns=["项目毛利润"])
    rules = _config([rule, _rule("OK", "rev_cost_zero_mismatch")])
    _, _, _, _, skipped = run_checks(rules, df_aux=pd.DataFrame(), df_income=inc, df_mapping=None, target_month=8)
    assert len(skipped) == 1
    assert skipped[0].rule_id == "BAD"


def test_f2_empty_dataframe_is_not_a_config_error():
    skip = check_rule_preconditions(_rule("R", "neg_profit_ratio"), pd.DataFrame(), None, None)
    assert skip is None  # 空数据走"无数据"路径，不算配置错误

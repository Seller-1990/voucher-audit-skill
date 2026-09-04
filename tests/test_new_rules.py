import pandas as pd

from voucher_audit.checks import (
    _cost_sudden_appearance_income,
    _gm_high_ratio_income,
    _headcount_rev_mismatch_income,
    _mom_change_income,
    _rev_cost_inversion_income,
    _social_headcount_mismatch_income,
)


def _df():
    return pd.DataFrame(
        {
            "主体账簿": ["甲", "甲", "甲", "甲", "甲", "乙"],
            "月": [7, 7, 8, 8, 8, 8],
            "部门": ["A部", "A部", "A部", "B部", "A部", "集团本部"],
            "三级科目": ["外包", "外包", "外包", "外包", "外包", "外包"],
            "实际客户": ["C1", "C1", "C1", "C2", "C3", "C4"],
            "项目": ["0", "0", "0", "0", "0", "0"],
            "全额收入": [100.0, 0.0, 300.0, 500.0, 50.0, 90.0],
            "净额收入": [100.0, 0.0, 300.0, 500.0, 50.0, 90.0],
            "成本合计": [80.0, 0.0, 100.0, 600.0, 0.0, 80.0],
            "项目毛利润": [20.0, 0.0, 200.0, -100.0, 50.0, 10.0],
            "结算人次": [10, 0, 0, 5, 5, 1],
            "社保人数": [0, 0, 0, 3, 0, 1],
            "社保": [0.0, 0.0, 0.0, 0.0, 0.0, 1.0],
            "项目返费": [0.0, 0.0, 60.0, 0.0, 0.0, 0.0],
            "第三方挂靠成本": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "项目福利费": [0.0, 0.0, 0.0, 20.0, 0.0, 0.0],
            "项目其他费用": [0.0, 0.0, 0.0, 0.0, 30.0, 0.0],
            "账载客户": ["C1", "C1", "C1", "C2", "C3", "C4"],
        }
    )


def _rule(rid, rtype, **params):
    return {"id": rid, "name": rid, "description": "", "source": {}, "type": rtype, "params": params}


def test_gm_high_ratio():
    # C1 8月 毛利200/全额300 = 66.7% > 50%；C2 毛利为负不命中
    out = _gm_high_ratio_income(_df(), 8, _rule("R", "gm_high_ratio", threshold=0.5, min_revenue=100, revenue_field="全额收入", profit_field="项目毛利润"))
    assert "C1" in out["实际客户"].values
    assert "C2" not in out["实际客户"].values


def test_rev_cost_inversion():
    # C2 8月 收入500 < 成本600 → 倒挂；C1 300>100 不倒挂
    out = _rev_cost_inversion_income(_df(), 8, _rule("R", "rev_cost_inversion", revenue_field="全额收入", cost_field="成本合计"))
    assert not out.empty
    assert (out["实际客户"] == "C2").any()


def test_headcount_rev_mismatch():
    # C1 8月 人次=0 收入=300 → 人次缺失；C2 人次=5 收入=500 正常
    out = _headcount_rev_mismatch_income(_df(), 8, _rule("R", "headcount_rev_mismatch", revenue_field="净额收入", headcount_field="结算人次", income_min=100))
    assert not out.empty
    assert (out["实际客户"] == "C1").any()


def test_social_headcount_mismatch():
    # C2 8月 社保人数=3 社保费=0 → 漏计社保；集团本部 C4 被排除
    out = _social_headcount_mismatch_income(_df(), 8, _rule("R", "social_headcount_mismatch"))
    assert not out.empty
    assert (out["实际客户"] == "C2").any()
    assert "C4" not in out["实际客户"].values


def test_mom_change():
    # C1 7月收入100 → 8月300，变动200% > 100%
    out = _mom_change_income(_df(), 8, _rule("R", "mom_change", revenue_threshold=1.0, cost_threshold=1.0, gm_threshold=0.3))
    assert not out.empty


def test_cost_sudden_appearance():
    # C2 8月福利费20，历史为0 → 突然出现
    out = _cost_sudden_appearance_income(_df(), 8, _rule("R", "cost_sudden_appearance", fields=["项目福利费", "项目其他费用"], threshold=10))
    assert not out.empty
    assert (out["实际客户"] == "C2").any()

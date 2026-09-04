import pandas as pd

from voucher_audit.checks_customer_subchecks import _sub_mapping_check
from voucher_audit.deep_analysis import (
    build_customer_profile_sheet,
    build_rule_correlation_sheet,
)


def _income_df():
    return pd.DataFrame(
        {
            "主体账簿": ["甲", "甲", "甲", "甲", "甲", "乙"],
            "月": [7, 7, 8, 8, 8, 8],
            "部门": ["A部", "A部", "A部", "A部", "A部", "集团本部"],
            "三级科目": ["外包", "外包", "外包", "外包", "外包", "外包"],
            "账载客户": ["C1", "C1", "C1", "C2", "C9", "C3"],
            "实际客户": ["C1", "C1", "C1", "C2", "C9wrong", "C3"],
            "项目": ["0", "0", "0", "0", "0", "0"],
            "全额收入": [100.0, 0.0, 0.0, 0.0, 30.0, 50.0],
            "净额收入": [100.0, 0.0, 0.0, -300.0, 30.0, 50.0],
            "成本合计": [80.0, 300.0, 150.0, 0.0, 5.0, 10.0],
            "项目毛利润": [20.0, -300.0, 50.0, 0.0, 25.0, 40.0],
            "结算人次": [10, 0, 20, 0, 1, 1],
            "_src_row": [2, 3, 4, 5, 6, 7],
        }
    )


def test_sub_mapping_check_distinguishes_mapping_change_from_error():
    df_map = pd.DataFrame(
        {
            "主体账簿": ["甲", "甲", "甲", "甲"],
            "月": [7, 8, 7, 8],
            "业务类型": ["外包", "外包", "外包", "外包"],
            "账载客户": ["C2", "C2", "C9", "C9"],
            "部门": ["A部", "A部", "A部", "A部"],
            "项目": ["0", "0", "0", "0"],
            "实际客户": ["C2old", "C2", "C9", "C9"],
        }
    )
    rule = {"id": "R", "name": "r", "description": "", "source": {}, "params": {}}
    out = _sub_mapping_check(_income_df(), df_map, 8, rule)
    # C2：历史(7月)映射=C2old，8月映射=C2 且与账面一致 → 需确认（映射变更）
    c2 = out[out["账载客户"] == "C2"]
    assert not c2.empty
    assert c2.iloc[0]["严重度"] == "需确认"
    assert "与当月映射一致" in c2.iloc[0]["命中原因"]
    # C9：历史映射=C9、当月映射=C9，账面=C9wrong → 与两者均不一致 → 错误
    c9 = out[out["账载客户"] == "C9"]
    assert not c9.empty
    assert c9.iloc[0]["严重度"] == "错误"
    assert "当月映射" in c9.iloc[0]["命中原因"]


def test_correlation_links_multiple_rules_for_same_group():
    income_dim = pd.DataFrame(
        {
            "严重度": ["错误", "需确认"],
            "规则ID": ["INC_REV_COST_ZERO_MISMATCH", "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO"],
            "主体账簿": ["甲", "甲"],
            "三级科目": ["外包", "外包"],
            "实际客户": ["C2", "C2"],
            "命中原因": ["净额收入=0且成本合计≠0", "外包业务类型下，工资=0 且 第三方挂靠成本=0"],
        }
    )
    out = build_rule_correlation_sheet(
        df_income=_income_df(),
        income_dim_anomalies=income_dim,
        income_gm_anomalies=pd.DataFrame(),
        aux_rule_violations=pd.DataFrame(),
        target_month=8,
    )
    assert not out.empty
    row = out[(out["实际客户"] == "C2") & (out["三级科目"] == "外包")]
    assert not row.empty
    r = row.iloc[0]
    assert r["命中规则数"] == 2
    assert "疑似成本计提错期" in r["模式标签"]
    assert r["综合风险"] in ("高", "中")


def test_customer_profile_no_contradictory_labels_for_inverted_month():
    df = _income_df()
    out = build_customer_profile_sheet(df, target_month=8)
    assert not out.empty
    # C1：7月净额100，8月净额0且成本150（倒挂）→ 不应出现"骤增/骤降/背离"等不可比标签
    c1 = out[out["实际客户"] == "C1"].iloc[0]
    for banned in ("收入骤增", "收入骤降", "收入与人次背离"):
        assert banned not in str(c1["风险标签"])
    assert "本月净额收入为0" in str(c1["风险标签"])

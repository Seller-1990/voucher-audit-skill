from __future__ import annotations

import pandas as pd

from voucher_audit.checks import _customer_consistency_check_income, _pp_change_income
from voucher_audit.report import build_customer_consistency_sheet, build_pp_change_sheet


def _pp_rule() -> dict:
    return {
        "id": "INC_PP_CHANGE",
        "name": "同比波动",
        "severity": "需确认",
        "description": "检查同比波动",
        "source": {"doc": "测试制度", "clause": "同比"},
        "params": {
            "key_fields": ["主体账簿", "三级科目"],
            "month_field": "月",
            "items": [
                {
                    "name": "毛利率",
                    "kind": "ratio",
                    "numerator": "项目毛利润",
                    "denominator": "净额收入",
                    "tolerance_ratio": 0.3,
                    "guard_field": "净额收入",
                    "min_guard": 1,
                },
                {
                    "name": "项目返费",
                    "kind": "value",
                    "field": "项目返费",
                    "tolerance_ratio": 0.3,
                    "min_abs": 5_000,
                    "eps": 1,
                },
            ],
        },
    }


def _pp_income() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"主体账簿": "A", "三级科目": "外包", "月": 1, "净额收入": 100, "项目毛利润": 50, "项目返费": 1_000},
            {"主体账簿": "A", "三级科目": "外包", "月": 2, "净额收入": 100, "项目毛利润": 50, "项目返费": 3_000},
            {"主体账簿": "A", "三级科目": "外包", "月": 3, "净额收入": 100, "项目毛利润": 90, "项目返费": 10_000},
        ]
    )


def test_pp_change_uses_historical_average_and_cumulative_ratio() -> None:
    out = _pp_change_income(_pp_income(), target_month=3, rule=_pp_rule())

    assert set(out["指标"]) == {"毛利率", "项目返费"}
    by_metric = out.set_index("指标")
    assert by_metric.loc["毛利率", "前期值"] == 0.5
    assert by_metric.loc["项目返费", "前期值"] == 2_000
    assert by_metric.loc["项目返费", "前期月份"] == "1，2"


def test_pp_change_report_builds_auditable_rows() -> None:
    income = _pp_income()
    rule = _pp_rule()
    hits = _pp_change_income(income, target_month=3, rule=rule)

    out = build_pp_change_sheet(income, hits, [rule], target_month=3)

    assert not out.empty
    assert {"标注", "命中原因", "严重度"}.issubset(out.columns)
    assert "问题" in set(out["标注"])


def _customer_rule() -> dict:
    return {
        "id": "INC_CUSTOMER_CONSISTENCY",
        "name": "客户一致性",
        "description": "检查客户归属",
        "source": {"doc": "测试制度", "clause": "客户"},
        "params": {
            "mapping_check_enabled": True,
            "book_customer_multi_actual_enabled": False,
            "combo_drift_enabled": False,
            "actual_customer_multi_entity_enabled": False,
            "pm_center_multi_dept_enabled": False,
        },
    }


def test_customer_consistency_and_report_preserve_problem_row() -> None:
    income = pd.DataFrame(
        [
            {
                "主体账簿": "A",
                "月": 3,
                "三级科目": "外包",
                "账载客户": "账载A",
                "实际客户": "错误客户",
                "部门": "部门A",
                "项目": "项目A",
                "净额收入": 10_000,
                "全额收入": 10_000,
            }
        ]
    )
    mapping = pd.DataFrame(
        [
            {
                "主体账簿": "A",
                "月": 2,
                "业务类型": "外包",
                "账载客户": "账载A",
                "实际客户": "正确客户",
                "部门": "部门A",
                "项目": "项目A",
            }
        ]
    )
    rule = _customer_rule()

    hits = _customer_consistency_check_income(income, mapping, 3, rule, dominance_ratio=0.7)
    report = build_customer_consistency_sheet(income, hits, mapping, target_month=3)

    assert len(hits) == 1
    assert hits.iloc[0]["问题分类"] == "实际客户与映射表不一致"
    assert len(report) == 1
    assert report.iloc[0]["标注"] == "错误"
    assert report.iloc[0]["实际客户"] == "错误客户"

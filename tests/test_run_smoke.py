from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from voucher_audit.rules_io import dump_yaml
from voucher_audit.runner import run_audit


def _write_xlsx(path: Path, *, sheet: str, header: list[str], rows: list[list[object]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(header)
    for r in rows:
        ws.append(list(r))
    wb.save(path)


def test_run_audit_smoke_generates_report(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True, exist_ok=True)

    # 数据汇总.xlsx -> 调整后序时账 (aux_ledger)
    aux_header = [
        "主体账簿",
        "月",
        "日",
        "凭证号",
        "摘要",
        "一级科目",
        "二级科目",
        "三级科目",
        "账载客户",
        "实际客户",
        "收支项目",
        "部门",
        "项目",
        "本币",
        "是否封存",
    ]
    aux_rows = [
        ["A", 1, 1, "V001", "Z5S0", "科目1", "科目2", "科目3", "客户A", "客户A", "项目A", "部门A", "项目A", 100.0, "否"],
        ["A", 2, 1, "V002", "Z5S0-", "科目1", "科目2", "科目3", "客户A", "客户A", "项目A", "部门A", "项目A", 200.0, "否"],
    ]
    _write_xlsx(workdir / "数据汇总.xlsx", sheet="调整后序时账", header=aux_header, rows=aux_rows)

    # 考核表输出.xlsx -> 收入成本表 (income_cost)
    inc_header = [
        "主体账簿",
        "月",
        "三级科目",
        "账载客户",
        "实际客户",
        "部门",
        "项目",
        "净额收入",
        "全额收入",
        "成本合计",
        "项目毛利润",
        "结算人次",
        "项目返费",
        "第三方挂靠成本",
    ]
    inc_rows = [
        ["A", 1, "科目3", "客户A", "客户A", "部门A", "项目A", 100000.0, 100000.0, 60000.0, 40000.0, 10, 0.0, 0.0],
        ["A", 2, "科目3", "客户A", "客户A", "部门A", "项目A", 120000.0, 120000.0, 70000.0, 50000.0, 12, 0.0, 0.0],
    ]
    _write_xlsx(workdir / "考核表输出.xlsx", sheet="收入成本表", header=inc_header, rows=inc_rows)

    # Minimal rules for smoke: correct inputs + empty checks (runner should still produce report).
    rules = {
        "inputs": {
            "files": {"data_summary": "数据汇总.xlsx", "income_cost": "考核表输出.xlsx"},
            "sheets": {
                "aux_ledger": {"preferred": ["调整后序时账"], "fuzzy_contains_any": ["序时账", "辅助帐"]},
                "income_cost": {"preferred": ["收入成本表"], "fuzzy_contains_any": ["收入成本"]},
            },
            "columns": {
                "aux_ledger": {
                    "entity": ["主体账簿"],
                    "month": ["月", "月份"],
                    "day": ["日"],
                    "voucher_no": ["凭证号"],
                    "summary": ["摘要"],
                    "acct1": ["一级科目"],
                    "acct2": ["二级科目"],
                    "acct3": ["三级科目"],
                    "customer_book": ["账载客户"],
                    "customer_actual": ["实际客户"],
                    "cashflow_item": ["收支项目"],
                    "dept": ["部门"],
                    "project": ["项目"],
                    "amount": ["本币"],
                    "sealed": ["是否封存"],
                },
                "income_cost": {
                    "entity": ["主体账簿"],
                    "month": ["月", "月份"],
                    "biz_type": ["三级科目"],
                    "customer_book": ["账载客户"],
                    "customer_actual": ["实际客户"],
                    "dept": ["部门"],
                    "project": ["项目"],
                    "revenue_net": ["净额收入"],
                    "revenue_gross": ["全额收入"],
                    "cost_total": ["成本合计"],
                    "profit": ["项目毛利润"],
                    "settlement_cnt": ["结算人次"],
                    "rebate": ["项目返费"],
                    "third_party_cost": ["第三方挂靠成本"],
                },
            },
        },
        "thresholds": {"drift_dominance_ratio": 0.7, "gross_margin": {}},
        "ai": {"enabled_default": False, "model": "gpt-5.4", "base_url": "", "api_key_env": "OPENAI_API_KEY"},
        "report_format": {},
        "checks": [],
    }
    rules_path = workdir / "rules.yaml"
    rules_path.write_text(dump_yaml(rules).replace("\r\n", "\n"), encoding="utf-8", newline="\n")

    res = run_audit(workdir=workdir, rules_path=rules_path, annotate_source=False, enable_ai=False)
    assert res.ok
    assert res.report_path is not None
    assert res.report_path.exists()


def test_rev_cost_zero_mismatch_strips_percent_suffix_when_enabled() -> None:
    import pandas as pd

    from voucher_audit.checks import _rev_cost_zero_mismatch_income

    # Same logical business type split into two labels: '劳务派遣3%' vs '劳务派遣'.
    # Without normalization, income and cost fall into different groups -> false mismatch.
    df_inc = pd.DataFrame(
        [
            ["A", 3, "劳务派遣3%", "C1", "C1", "D1", "P1", 0.0, 0.0, 100.0],
            ["A", 3, "劳务派遣", "C1", "C1", "D1", "P1", 0.0, 200.0, 0.0],
        ],
        columns=["主体账簿", "月", "三级科目", "账载客户", "实际客户", "部门", "项目", "净额收入", "全额收入", "成本合计"],
    )

    base_rule = {
        "id": "INC_REV_COST_ZERO_MISMATCH",
        "name": "收入/成本零值不匹配（限定业务类型）",
        "type": "rev_cost_zero_mismatch",
        "scope": "income_cost",
        "severity": "错误",
        "description": "",
        "source": {"doc": "", "clause": ""},
        "params": {
            "key_fields": ["主体账簿", "三级科目", "实际客户", "部门", "项目"],
            "revenue_field": "全额收入",
            "cost_field": "成本合计",
            "eps": 1e-6,
            "biz_type_field": "三级科目",
            "biz_type_keywords": ["劳务派遣"],
        },
    }

    out_no_norm = _rev_cost_zero_mismatch_income(df_inc, target_month=3, rule=base_rule)
    assert len(out_no_norm) == 2

    rule_norm = dict(base_rule)
    rule_norm["params"] = dict(base_rule["params"])
    rule_norm["params"]["biz_type_strip_percent_suffix"] = True
    out_norm = _rev_cost_zero_mismatch_income(df_inc, target_month=3, rule=rule_norm)
    assert out_norm.empty

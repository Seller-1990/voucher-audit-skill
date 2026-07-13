from __future__ import annotations


TEMPLATE_YAML = """\
# 凭证审核规则包（YAML 驱动）
# - 本文件必须放在“运行目录”，文件名固定为 voucher_audit_rules.yaml

inputs:
  files:
    data_summary: "数据汇总.xlsx"
    income_cost: "考核表输出.xlsx"
  sheets:
    aux_ledger:
      preferred: ["调整后序时账"]
      fuzzy_contains_any: ["序时账", "辅助帐"]
    income_cost:
      preferred: ["收入成本表"]
      fuzzy_contains_any: ["收入成本"]
  columns:
    aux_ledger:
      entity: ["主体账簿"]
      month: ["月", "月份"]
      day: ["日"]
      voucher_no: ["凭证号"]
      summary: ["摘要"]
      acct1: ["一级科目"]
      acct2: ["二级科目"]
      acct3: ["三级科目"]
      customer_book: ["账载客户"]
      customer_actual: ["实际客户"]
      cashflow_item: ["收支项目"]
      dept: ["部门"]
      project: ["项目"]
      amount: ["本币"]
      sealed: ["是否封存"]
    income_cost:
      entity: ["主体账簿"]
      month: ["月", "月份"]
      biz_type: ["三级科目"]
      customer_book: ["账载客户"]
      customer_actual: ["实际客户"]
      dept: ["部门"]
      project: ["项目"]
      revenue_net: ["净额收入"]
      revenue_gross: ["全额收入"]
      cost_total: ["成本合计"]
      profit: ["项目毛利润"]
      settlement_cnt: ["结算人次"]
      rebate: ["项目返费"]
      third_party_cost: ["第三方挂靠成本", "第三方挂靠"]

period:
  pick: "max_month"

thresholds:
  drift_dominance_ratio: 0.7

ai:
  enabled_default: false
  model: "gpt-5.4"
  # 可选：私有化/代理网关地址；留空则使用环境变量 OPENAI_BASE_URL（若也为空则直连官方）
  base_url: ""
  # API Key 从环境变量读取（建议用系统环境变量设置），GUI 里也可临时粘贴（不落盘）
  api_key_env: "OPENAI_API_KEY"
  max_items_per_section: 40
  max_output_tokens: 1200

report_format:
  sheet_names:
    overview: "概览"
    aux_rule_violations: "入账规则违规_辅助帐"
    aux_suspect_wrong_account: "疑似错科目_辅助帐"
    income_dim_anomalies: "维度异常_收入成本"
    combo_drift_friendly: "主映射异常_友好视图"
    income_gm_anomalies: "毛利异常_收入成本"
    ai_review: "AI复核意见"
  column_layouts: {}

checks:
  - id: INC_FULL_COMBO_DRIFT
    type: combo_drift
    scope: income_cost
    severity: "错误"
    description: "按（主体账簿、账载客户）为主键，检查（三级科目、实际客户、部门、项目）是否与前置月份的历史主映射不一致。"
    source:
      doc: "（统计异常）"
      clause: "完整组合稳定性"
    params:
      key_fields: ["主体账簿", "账载客户"]
      value_fields: ["三级科目", "实际客户", "部门", "项目"]
      amount_field: "净额收入"
      min_amount_abs: 50000

  - id: INC_REV_COST_ZERO_MISMATCH
    type: rev_cost_zero_mismatch
    scope: income_cost
    severity: "错误"
    description: "检查组合（主体账簿+三级科目+实际客户+部门+项目）下是否出现：全额收入=0但成本合计≠0，或成本合计=0但全额收入≠0。"
    source:
      doc: "（一致性校验）"
      clause: "收入/成本同向性"
    params:
      key_fields: ["主体账簿", "三级科目", "实际客户", "部门", "项目"]
      revenue_field: "全额收入"
      cost_field: "成本合计"
      eps: 1e-6

  - id: AUX_SUMMARY_ZS_SUFFIX
    type: summary_zs_suffix
    scope: aux_ledger
    severity: "错误"
    description: '检查调整后序时账摘要中 Z\\d+S\\d+（如 Z5S0 / z5s1）后是否紧跟其它字符（含文字、- 等）。'
    source:
      doc: "（格式校验）"
      clause: "摘要 Z*S* 码规范"
    params:
      summary_field: "摘要"
      voucher_field: "凭证号"
      month_field: "月"
      pattern: '(?i)Z\\d+S\\d+'
      # 允许 Z代码后立刻结束，或以空白/标点分隔；其余一律视为异常
      allowed_next_chars: ["", " ", "\\t", "，", ",", "。", ".", "；", ";", "：", ":", "、", "/", "\\\\", "|", ")", "）", "]", "】", "}", "）"]

  - id: INC_METRIC_PP_CHANGE
    type: metric_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查毛利率与单人毛利相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "指标稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      revenue_guard_field: "净额收入"
      min_revenue: 0
      metrics:
        - name: "毛利率"
          numerator: "项目毛利润"
          denominator: "净额收入"
        - name: "单人毛利"
          numerator: "项目毛利润"
          denominator: "结算人次"

  - id: INC_VALUE_PP_CHANGE
    type: value_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查项目返费、第三方挂靠成本相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "费用稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      value_fields: ["项目返费", "第三方挂靠成本"]
      min_abs: 0

  - id: INC_RATIO_PP_CHANGE
    type: ratio_pp_change
    scope: income_cost
    severity: "需确认"
    description: "以（主体账簿+实际客户+部门）为主键，检查返费率与挂靠占比相对前期是否波动超过±20%。"
    source:
      doc: "（同比波动）"
      clause: "比率稳定性"
    params:
      key_fields: ["主体账簿", "实际客户", "部门"]
      month_field: "月"
      tolerance_ratio: 0.2
      ratios:
        - name: "项目返费/净额收入"
          numerator: "项目返费"
          denominator: "净额收入"
        - name: "第三方挂靠成本/全额收入"
          numerator: "第三方挂靠成本"
          denominator: "全额收入"
"""

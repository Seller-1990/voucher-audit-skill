from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from .ai_review import build_ai_payload, run_ai_review
from .checks import run_checks
from .config import RuleConfig, load_rules
from .constants import DEFAULT_REPORT_PREFIX, DEFAULT_RULES_FILENAME
from .excel_io import match_sheet_name, open_workbook, read_sheet, require_column, resolve_column
from .logging_util import Logger, make_logger
from .report import make_report_paths, write_report
from .rules_template import TEMPLATE_YAML
from .source_annotation import build_source_annotation_bundle


@dataclass(frozen=True)
class AuditResult:
    ok: bool
    message: str
    report_path: Optional[Path]
    annotation_requested: bool = False
    annotation_ok: bool = True
    annotation_message: str = ""


@dataclass(frozen=True)
class LoadedAuditContext:
    workdir: Path
    rules_path: Path
    rules: RuleConfig
    data_summary_path: Path
    income_cost_path: Path
    target_month: int
    df_aux: pd.DataFrame
    df_income: pd.DataFrame
    df_mapping: Optional[pd.DataFrame]


def generate_template_rules(path: Path) -> None:
    path.write_text(TEMPLATE_YAML.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def _normalize_month(df: pd.DataFrame, month_col: str) -> pd.Series:
    s = pd.to_numeric(df[month_col], errors="coerce")
    return s.dropna().astype(int)


def pick_target_month(
    df_aux: pd.DataFrame,
    df_income: pd.DataFrame,
    aux_month_col: str,
    inc_month_col: str,
    aux_scope_suffix: Optional[str] = None
) -> int:
    """
    选择目标月份，按 scope 分别选择而不是取全局最大值。

    修复 bug: 原实现取两张表的最大月，导致辅助帐表中的月=1数据
    被跳过（因为收入成本表可能只有月=3的数据）。

    参数:
        df_aux: 辅助帐 DataFrame
        df_income: 收入成本 DataFrame
        aux_month_col: 辅助帐月份列名
        inc_month_col: 收入成本月份列名
        aux_scope_suffix: 辅助帐规则 scope 的后缀（如'_aux'），用于判断规则适用范围

    返回:
        目标月份（整数）
    """
    aux_m = _normalize_month(df_aux, aux_month_col)
    inc_m = _normalize_month(df_income, inc_month_col)

    if aux_m.empty and inc_m.empty:
        raise ValueError("两张表均无法解析\"月\"列")

    m = 0

    # 根据 scope_suffix 决定使用哪个表的月份
    if aux_scope_suffix and ("_aux" in aux_scope_suffix or "ledger" in aux_scope_suffix):
        # 辅助帐范围的规则，使用辅助帐的最大月
        if not aux_m.empty:
            m = int(aux_m.max())
    else:
        # 收入成本范围或其他规则，使用收入成本的最大月
        if not inc_m.empty:
            m = int(inc_m.max())

    # 如果都无法解析，回退到取两个表的最大月
    if m == 0:
        if not aux_m.empty:
            m = max(m, int(aux_m.max()))
        if not inc_m.empty:
            m = max(m, int(inc_m.max()))

    return m


def detect_year_from_workdir(workdir: Path) -> int:
    # Try to infer from folder name like 202602...
    name = workdir.name
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])", name)
    if m:
        return int(m.group(1))
    return datetime.now().year


def _build_colmap(df: pd.DataFrame, cfg: dict[str, list[str]], required_keys: list[str], context: str) -> dict[str, str]:
    m: dict[str, str] = {}
    for k in required_keys:
        m[k] = require_column(df, cfg.get(k, [k]), friendly=k, context=context)
    for k, cands in cfg.items():
        if k in m:
            continue
        found = resolve_column(df, cands)
        if found:
            m[k] = found
    return m


def load_audit_context(
    workdir: Path,
    rules_path: Optional[Path] = None,
    target_month: Optional[int] = None,
    logger: Optional[Logger] = None,
) -> LoadedAuditContext:
    log = logger or make_logger()
    workdir = workdir.resolve()

    rules_path = rules_path or (workdir / DEFAULT_RULES_FILENAME)
    if not rules_path.exists():
        raise FileNotFoundError(f"缺少规则文件：{rules_path.name}")
    rules: RuleConfig = load_rules(rules_path)

    data_summary_path = workdir / rules.inputs.data_summary_file
    income_cost_path = workdir / rules.inputs.income_cost_file
    if not data_summary_path.exists():
        raise FileNotFoundError(f"缺少文件：{data_summary_path.name}")
    if not income_cost_path.exists():
        raise FileNotFoundError(f"缺少文件：{income_cost_path.name}")

    wb_sum = open_workbook(data_summary_path)
    wb_inc = open_workbook(income_cost_path)

    aux_sheet = match_sheet_name(wb_sum.xls, rules.inputs.sheets["aux_ledger"])
    inc_sheet = match_sheet_name(wb_inc.xls, rules.inputs.sheets["income_cost"])
    map_sheet = match_sheet_name(wb_sum.xls, rules.inputs.sheets["customer_mapping"]) if "customer_mapping" in rules.inputs.sheets else None

    if not aux_sheet:
        raise ValueError("无法匹配 数据汇总.xlsx 的辅助帐sheet（调整后序时账）")
    if not inc_sheet:
        raise ValueError("无法匹配 考核表输出.xlsx 的收入成本表sheet")

    log.info(f"读取辅助帐sheet：{aux_sheet}")
    df_aux = read_sheet(wb_sum.xls, aux_sheet)
    log.info(f"读取收入成本sheet：{inc_sheet}")
    df_inc = read_sheet(wb_inc.xls, inc_sheet)

    df_map: Optional[pd.DataFrame] = None
    if map_sheet:
        log.info(f"读取映射sheet：{map_sheet}")
        df_map = read_sheet(wb_sum.xls, map_sheet)

    aux_cols_cfg = rules.inputs.columns.get("aux_ledger", {})
    inc_cols_cfg = rules.inputs.columns.get("income_cost", {})
    map_cols_cfg = rules.inputs.columns.get("customer_mapping", {})

    aux_cols = _build_colmap(
        df_aux,
        aux_cols_cfg,
        required_keys=["month", "voucher_no", "acct1", "acct2", "acct3", "customer_actual", "dept", "cashflow_item", "amount"],
        context="辅助帐",
    )
    inc_cols = _build_colmap(
        df_inc,
        inc_cols_cfg,
        required_keys=["month", "biz_type", "customer_book", "customer_actual", "dept", "project", "revenue_net", "cost_total", "profit"],
        context="收入成本表",
    )

    aux_rename = {
        aux_cols.get("entity", ""): "主体账簿",
        aux_cols["month"]: "月",
        aux_cols.get("day", ""): "日",
        aux_cols["voucher_no"]: "凭证号",
        aux_cols.get("summary", ""): "摘要",
        aux_cols["acct1"]: "一级科目",
        aux_cols["acct2"]: "二级科目",
        aux_cols["acct3"]: "三级科目",
        aux_cols.get("customer_book", ""): "账载客户",
        aux_cols["customer_actual"]: "实际客户",
        aux_cols["cashflow_item"]: "收支项目",
        aux_cols["dept"]: "部门",
        aux_cols.get("project", ""): "项目",
        aux_cols["amount"]: "本币",
        aux_cols.get("sealed", ""): "是否封存",
    }
    df_aux = df_aux.rename(columns={k: v for k, v in aux_rename.items() if k})

    inc_rename = {
        inc_cols.get("entity", ""): "主体账簿",
        inc_cols["month"]: "月",
        inc_cols["biz_type"]: "三级科目",
        inc_cols["customer_book"]: "账载客户",
        inc_cols["customer_actual"]: "实际客户",
        inc_cols["dept"]: "部门",
        inc_cols["project"]: "项目",
        inc_cols["revenue_net"]: "净额收入",
        inc_cols.get("revenue_gross", ""): "全额收入",
        inc_cols["cost_total"]: "成本合计",
        inc_cols["profit"]: "项目毛利润",
        inc_cols.get("settlement_cnt", ""): "结算人次",
        inc_cols.get("rebate", ""): "项目返费",
        inc_cols.get("third_party_cost", ""): "第三方挂靠成本",
    }
    df_inc = df_inc.rename(columns={k: v for k, v in inc_rename.items() if k})

    if df_map is not None and not df_map.empty:
        map_cols = _build_colmap(
            df_map,
            map_cols_cfg,
            required_keys=["entity", "month", "biz_type", "customer_book", "dept", "project", "customer_actual"],
            context="客户调整校验",
        )
        map_rename = {
            map_cols["entity"]: "主体账簿",
            map_cols["month"]: "月",
            map_cols["biz_type"]: "业务类型",
            map_cols["customer_book"]: "账载客户",
            map_cols["dept"]: "部门",
            map_cols["project"]: "项目",
            map_cols["customer_actual"]: "实际客户",
        }
        df_map = df_map.rename(columns=map_rename)

    if target_month is None:
        # 按 scope 分别选择月份：
        # - 辅助帐规则 (aux_ledger) 使用辅助帐的最大月
        # - 收入成本规则使用收入成本的最大月
        target_month = pick_target_month(df_aux, df_inc, "月", "月", aux_scope_suffix="ledger")

    # 月列防御性归一（数值化），避免源文件为 '08' 等文本月份时 == target_month 判断失效
    for _df in (df_aux, df_inc, df_map):
        if _df is not None and not _df.empty and "月" in _df.columns:
            _df["月"] = pd.to_numeric(_df["月"], errors="coerce")

    # 源行号（Excel 实际行号 = DataFrame 位置 + 表头 1 行），供报告回溯源表
    if "_src_row" not in df_aux.columns:
        df_aux = df_aux.assign(_src_row=pd.RangeIndex(len(df_aux)) + 2)
    if "_src_row" not in df_inc.columns:
        df_inc = df_inc.assign(_src_row=pd.RangeIndex(len(df_inc)) + 2)
    if df_map is not None and not df_map.empty and "_src_row" not in df_map.columns:
        df_map = df_map.assign(_src_row=pd.RangeIndex(len(df_map)) + 2)

    return LoadedAuditContext(
        workdir=workdir,
        rules_path=rules_path,
        rules=rules,
        data_summary_path=data_summary_path,
        income_cost_path=income_cost_path,
        target_month=int(target_month),
        df_aux=df_aux,
        df_income=df_inc,
        df_mapping=df_map,
    )


def run_audit(
    workdir: Path,
    rules_path: Optional[Path] = None,
    target_month: Optional[int] = None,
    include_rule_ids: Optional[list[str]] = None,
    enable_ai: Optional[bool] = None,
    openai_api_key: str = "",
    openai_base_url: str = "",
    openai_model: str = "",
    annotate_source: bool = False,
    logger: Optional[Logger] = None,
) -> AuditResult:
    log = logger or make_logger()
    workdir = workdir.resolve()
    log.info(f"工作目录：{workdir}")
    try:
        ctx = load_audit_context(workdir=workdir, rules_path=rules_path, target_month=target_month, logger=log)
    except Exception as e:
        return AuditResult(ok=False, message=str(e), report_path=None, annotation_requested=annotate_source)

    rules = ctx.rules
    rules_path = ctx.rules_path
    data_summary_path = ctx.data_summary_path
    income_cost_path = ctx.income_cost_path
    df_aux = ctx.df_aux
    df_inc = ctx.df_income
    df_map = ctx.df_mapping
    target_month = int(ctx.target_month)
    log.info(f"目标月份：{target_month}")

    filtered_checks = rules.checks
    if include_rule_ids is not None:
        include_ids = [str(x).strip() for x in include_rule_ids if str(x).strip()]
        include_set = set(include_ids)
        filtered_checks = [c for c in rules.checks if str((c or {}).get("id", "")).strip() in include_set]
        if include_ids:
            matched_ids = {str((c or {}).get("id", "")).strip() for c in filtered_checks}
            missing = [x for x in include_ids if x not in matched_ids]
            if missing:
                log.warn(f"以下规则ID未匹配到，已忽略：{missing}")
        else:
            log.warn("未选择任何规则，本次将不执行审核规则，仅输出概览与空结果页。")

    active_rules = RuleConfig(
        raw=rules.raw,
        inputs=rules.inputs,
        thresholds=rules.thresholds,
        ai=rules.ai,
        report_format=rules.report_format,
        checks=filtered_checks,
    )
    executed_rule_ids = [str((c or {}).get("id", "")).strip() for c in active_rules.checks if str((c or {}).get("id", "")).strip()]
    log.info(f"本次执行规则数：{len(executed_rule_ids)}")

    aux_rule_violations, aux_suspect_wrong, income_dim, income_gm = run_checks(
        rules=active_rules,
        df_aux=df_aux,
        df_income=df_inc,
        df_mapping=df_map,
        target_month=int(target_month),
    )

    # 命中统计（stdout/日志一眼可见，无需打开报告逐 sheet 数）
    def _stat_line(df: pd.DataFrame) -> str:
        if df is None or df.empty or "规则ID" not in df.columns:
            return ""
        parts: list[str] = []
        for rid, grp in df.groupby("规则ID", dropna=False):
            rid_s = str(rid).strip() or "（空）"
            rname = rule_name_map.get(rid_s, rid_s)
            if "严重度" in grp.columns:
                sev = grp["严重度"].value_counts().to_dict()
                sev_str = "、" + "，".join(f"{k}{v}" for k, v in sev.items()) if sev else ""
            else:
                sev_str = ""
            parts.append(f"{rname}={len(grp)}{sev_str}")
        return "；".join(parts)

    rule_name_map = {
        str((c or {}).get("id", "")).strip(): (str((c or {}).get("name", "")).strip() or str((c or {}).get("id", "")))
        for c in active_rules.checks
    }
    stat_parts = [
        s for s in [
            _stat_line(aux_rule_violations),
            _stat_line(aux_suspect_wrong),
            _stat_line(income_dim),
            _stat_line(income_gm),
        ] if s
    ]
    log.info("命中统计：" + ("；".join(stat_parts) if stat_parts else "全部规则无命中"))

    ai_df: Optional[pd.DataFrame] = None
    ai_enabled = bool(active_rules.ai.enabled_default) if enable_ai is None else bool(enable_ai)
    ai_message = "AI未启用。"
    if ai_enabled:
        model = openai_model.strip() or active_rules.ai.model
        base_url = openai_base_url.strip() or active_rules.ai.base_url
        payload = build_ai_payload(
            target_month=int(target_month),
            aux_rule_violations=aux_rule_violations,
            aux_suspect_wrong=aux_suspect_wrong,
            income_dim=income_dim,
            income_gm=income_gm,
            max_items=active_rules.ai.max_items_per_section,
        )
        r = run_ai_review(
            model=model,
            max_output_tokens=active_rules.ai.max_output_tokens,
            payload=payload,
            api_key_env=active_rules.ai.api_key_env,
            base_url=base_url,
            api_key_override=openai_api_key.strip(),
        )
        ai_df = r.df
        ai_message = r.message
        log.info(ai_message)

    year = detect_year_from_workdir(ctx.workdir)
    yyyymm = f"{year}{int(target_month):02d}"

    # Build per-rule breakdown for overview
    rule_name_map = {}
    for c in active_rules.checks:
        rid = str((c or {}).get("id", "")).strip()
        rname = str((c or {}).get("name", "")).strip() or rid
        rule_name_map[rid] = rname

    def _per_rule_counts(df: pd.DataFrame) -> list[dict[str, object]]:
        if df is None or df.empty or "规则ID" not in df.columns:
            return []
        rows = []
        for rid, grp in df.groupby("规则ID", dropna=False):
            rid_s = str(rid).strip()
            rname = rule_name_map.get(rid_s, rid_s)
            sev_col = "严重度" if "严重度" in grp.columns else None
            if sev_col:
                sev_counts = grp[sev_col].value_counts().to_dict()
                sev_str = "、".join(f"{k}{v}条" for k, v in sev_counts.items())
            else:
                sev_str = ""
            rows.append({"规则": rname, "命中数": len(grp), "严重度分布": sev_str})
        return rows

    all_per_rule = []
    for label, df in [
        ("辅助帐-违规", aux_rule_violations),
        ("辅助帐-疑似", aux_suspect_wrong),
        ("收入成本-维度", income_dim),
        ("收入成本-毛利", income_gm),
    ]:
        for item in _per_rule_counts(df):
            item["来源"] = label
            all_per_rule.append(item)

    overview_rows = [
        {"项目": "工作目录", "值": str(workdir)},
        {"项目": "数据汇总文件", "值": str(data_summary_path.name)},
        {"项目": "考核表输出文件", "值": str(income_cost_path.name)},
        {"项目": "目标月份", "值": int(target_month)},
        {"项目": "规则文件", "值": str(rules_path.name)},
        {"项目": "规则文件（实际生效）", "值": str(rules_path)},
        {"项目": "执行规则数", "值": int(len(executed_rule_ids))},
        {"项目": "执行规则列表", "值": "、".join(rule_name_map.get(rid, rid) for rid in executed_rule_ids) if executed_rule_ids else "（空）"},
        {"项目": "辅助帐违规条数", "值": int(0 if aux_rule_violations.empty else len(aux_rule_violations))},
        {"项目": "辅助帐疑似错科目条数", "值": int(0 if aux_suspect_wrong.empty else len(aux_suspect_wrong))},
        {"项目": "维度异常条数", "值": int(0 if income_dim.empty else len(income_dim))},
        {"项目": "毛利异常条数", "值": int(0 if income_gm.empty else len(income_gm))},
        {"项目": "AI复核", "值": "启用" if ai_enabled else "关闭"},
        {"项目": "AI状态", "值": ai_message},
        {"项目": "需线下核对项", "值": "暂估事项表提报/发票交接/收入确认条件等条款需结合线下流程核对（本工具不直接判错）"},
    ]
    overview = pd.DataFrame(overview_rows)
    overview_rule_breakdown = pd.DataFrame(all_per_rule) if all_per_rule else pd.DataFrame(columns=["规则", "来源", "命中数", "严重度分布"])

    report_paths = make_report_paths(ctx.workdir, DEFAULT_REPORT_PREFIX, yyyymm)
    write_report(
        path=report_paths.report_path,
        overview=overview,
        overview_rule_breakdown=overview_rule_breakdown,
        aux_rule_violations=aux_rule_violations,
        aux_suspect_wrong_account=aux_suspect_wrong,
        income_dim_anomalies=income_dim,
        income_gm_anomalies=income_gm,
        ai_review=ai_df if ai_enabled else None,
        report_format=active_rules.report_format,
        # 新增参数，用于生成规则说明和客户归属一致性专门报告
        checks=active_rules.checks,
        df_aux=df_aux,
        df_income=df_inc,
        df_mapping=df_map,
        target_month=int(target_month),
        workdir=ctx.workdir,
        yyyymm=yyyymm,
    )
    log.info(f"报告已生成：{report_paths.report_path}")

    annotation_ok = True
    annotation_message = ""
    if annotate_source:
        log.info("开始回写源文件标注（PQ安全模式：右侧留空1列后写异常项）。")
        try:
            from .excel_annotation_com import write_source_annotations

            bundle = build_source_annotation_bundle(
                data_summary_path=data_summary_path,
                income_cost_path=income_cost_path,
                df_aux=df_aux,
                df_income=df_inc,
                aux_rule_violations=aux_rule_violations,
                aux_suspect_wrong_account=aux_suspect_wrong,
                income_dim_anomalies=income_dim,
                income_gm_anomalies=income_gm,
                target_month=int(target_month),
                gap_columns=1,
            )
            annotation_result = write_source_annotations(bundle, logger=log)
            annotation_ok = bool(annotation_result.ok)
            annotation_message = annotation_result.message
            if annotation_ok:
                log.info(annotation_message)
            else:
                log.warn(annotation_message)
        except Exception as e:
            annotation_ok = False
            annotation_message = f"源文件标注失败：{type(e).__name__}: {e}"
            log.error(annotation_message)

    return AuditResult(
        ok=True,
        message="审核完成。",
        report_path=report_paths.report_path,
        annotation_requested=annotate_source,
        annotation_ok=annotation_ok,
        annotation_message=annotation_message,
    )

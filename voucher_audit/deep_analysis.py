"""深度分析：规则关联影响分析 + 客户级综合分析（历史 vs 本月）。

两个产出：
1. build_rule_correlation_sheet —— 按客户组合键聚合多规则命中，识别跨规则叠加/矛盾模式，
   输出综合风险分级，回答"同一个客户/组合被几类问题同时命中"。
2. build_customer_profile_sheet —— 对每个实际客户构建历史(1..N-1月)与本月对照画像：
   收入/成本/毛利/人次趋势、生命周期、背离检测，输出风险标签与处理建议。

口径约定：
- 组合键：主体账簿 + 三级科目(去百分比后缀) + 实际客户；
- INC_PP_CHANGE 的主键是（主体账簿+三级科目），不含客户，故以"键级命中"方式参与关联；
- AUX 人次规则命中按"实际客户"弱关联，仅作参考，不计入风险分；
- 指标聚合默认剔除 部门=集团本部（与各审核规则口径一致）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import pandas as pd

from .check_utils import _src_rows_text, _strip_percent_suffix

# 参与客户组合键关联的收入成本规则
_CORRELATION_RULES = {
    "INC_CUSTOMER_CONSISTENCY": "客户归属一致性",
    "INC_REV_COST_ZERO_MISMATCH": "收入/成本零值不匹配",
    "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO": "外包缺工资或挂靠",
    "INC_NEG_GM_HIGH_RATIO": "负毛利且占比过高",
    "INC_GM_HIGH_RATIO": "毛利偏高，可能漏记了成本",
    "INC_REV_COST_INVERSION": "花的钱比挣的多",
    "INC_HEADCOUNT_REV_MISMATCH": "结算人数和收入对不上",
    "INC_SOCIAL_HEADCOUNT_MISMATCH": "报了社保人数却没交社保费",
    "INC_COST_RATIO_HIGH": "返费或挂靠费占收入比例过高",
    "INC_EXPENSE_RATIO": "福利费或其他费用占比偏高",
    "INC_COST_SUDDEN_APPEARANCE": "以前没有的费用本月突然出现",
    "INC_MOM_CHANGE": "和上个月比波动较大",
    "INC_DUPLICATE_ROW": "收入成本表里有重复行",
    "INC_GROUP_HQ_UNSETTLED": "集团本部还挂着没调整的成本",
    "INC_SIMILAR_CUSTOMER_RENAME": "疑似客商改名",
    "AUX_WAGE_WRONG_CUSTOMER": "工资好像挂错了客户（序时账）",
    "INC_MIXED_BIZ_TYPE": "同一个客户混着做多种业务",
    "INC_REV_COST_BIZ_TYPE_MISMATCH": "收入和成本记的业务类型对不上",
    "INC_SAME_AMOUNT_ADJACENT_MONTHS": "相邻两个月金额一模一样",
    "INC_SMALL_AMOUNT_WRONG_DEPT": "零头成本挂在别的部门",
    "INC_ENTITY_SWITCH_MAPPING_DRIFT": "客户换了主体，映射要跟着改",
    "INC_REBATE_EXTERNAL_COST_RECONCILE": "收入成本表和账上的外部成本对不上",
}
_PP_RULE_ID = "INC_PP_CHANGE"
_AUX_RULE_IDS = {"AUX_HEADCOUNT_DATA_CHECK": "人次数据检查"}

_ZERO_REASON_REV = "成本合计=0且净额收入≠0"
_ZERO_REASON_COST = "净额收入=0且成本合计≠0"

# 收入/人次背离阈值：变动率差异超过该值视为背离
_DIVERGENCE_THRESHOLD = 0.5
# 收入骤增/骤降阈值（相对历史月均）
_SURGE_THRESHOLD = 0.5


def _sev_rank(s: Any) -> int:
    s = str(s)
    if s == "错误":
        return 0
    if s == "需确认":
        return 1
    return 2


def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _norm_biz(v: Any) -> str:
    return _strip_percent_suffix(_norm_text(v))


def _fmt_num(v: Any, nd: int = 2) -> Any:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if pd.isna(f):
        return ""
    return round(f, nd)


def _fmt_pct(v: Any) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    if pd.isna(f):
        return ""
    return f"{f * 100:+.1f}%"


def _group_hits_by_customer_key(
    hits: pd.DataFrame,
    rule_id: str,
    rule_display: str,
) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], set[tuple[str, str]]]:
    """返回 (客户组合键 -> 命中列表, 键级(主体+三级科目)命中集合)。"""
    out: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    key_level: set[tuple[str, str]] = set()
    if hits is None or hits.empty:
        return out, key_level
    for _, h in hits.iterrows():
        book = _norm_text(h.get("主体账簿"))
        biz = _norm_biz(h.get("三级科目"))
        cust = _norm_text(h.get("实际客户"))
        if not cust:
            # 归属一致性/主体切换/混做等规则按"账载客户"分组，命中行没有实际客户——
            # 回退到账载客户做关联键，否则这些命中全部落到空键，客户级根因归并失效。
            cust = _norm_text(h.get("账载客户")) or _norm_text(h.get("旧客户名")) or "(客户未知)"
        sev = _norm_text(h.get("严重度")) or "需确认"
        reason = _norm_text(h.get("命中原因"))
        if rule_id == _PP_RULE_ID:
            key_level.add((book, biz))
            continue
        out[(book, biz, cust)].append(
            {"rule_id": rule_id, "rule": rule_display, "severity": sev, "reason": reason}
        )
    return out, key_level


def build_correlation_index(
    income_dim_anomalies: Optional[pd.DataFrame],
    income_gm_anomalies: Optional[pd.DataFrame],
) -> tuple[dict[tuple[str, str, str], list[dict[str, str]]], set[tuple[str, str]]]:
    """供关联表与客户画像共用的关联索引。"""
    index: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    pp_keys: set[tuple[str, str]] = set()
    for df, rid in [
        (income_dim_anomalies, None),
        (income_gm_anomalies, "INC_NEG_GM_HIGH_RATIO"),
    ]:
        if df is None or df.empty or "规则ID" not in df.columns:
            continue
        for rule_id in df["规则ID"].astype(str).unique():
            display = _CORRELATION_RULES.get(rule_id)
            if display is None:
                continue
            part, keys = _group_hits_by_customer_key(
                df[df["规则ID"].astype(str) == rule_id], rule_id, display
            )
            for k, v in part.items():
                index[k].extend(v)
            pp_keys |= keys
    return index, pp_keys


def _detect_pattern(rules_hit: set[str], zero_reasons: set[str]) -> str:
    """基于规则组合的跨规则模式识别。"""
    has_cc = "INC_CUSTOMER_CONSISTENCY" in rules_hit
    has_zero = "INC_REV_COST_ZERO_MISMATCH" in rules_hit
    has_out = "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO" in rules_hit
    has_gm = "INC_NEG_GM_HIGH_RATIO" in rules_hit
    has_pp = _PP_RULE_ID in rules_hit
    tags: list[str] = []
    if has_zero and _ZERO_REASON_REV in zero_reasons and has_out:
        tags.append("疑似整月成本漏记（收入在而成本/工资/挂靠全缺）")
    if has_zero and _ZERO_REASON_COST in zero_reasons and has_out:
        tags.append("疑似成本计提错期（成本在而收入倒挂，且外包成本结构缺失）")
    if has_cc and has_pp:
        tags.append("归属调整叠加波动（应先确认归属，再判断波动是否真实）")
    if has_cc and has_gm:
        tags.append("归属调整叠加负毛利（核对归属变更前后成本口径）")
    if has_cc and has_zero:
        tags.append("归属调整叠加收入成本倒挂")
    if has_zero and has_gm:
        tags.append("收入成本倒挂叠加负毛利")
    if has_gm and has_out:
        tags.append("负毛利叠加外包成本结构缺失（重点核挂靠/工资完整性）")
    if not tags and (has_zero or has_out or has_gm or has_cc or has_pp):
        tags.append("单规则命中")
    return "；".join(tags)


def _detect_root_cause(rules_hit: set[str], zero_reasons: set[str]) -> tuple[str, str]:
    """多规则命中时推断底层根因：同一个错误（如实际客户没修正）会连锁触发多条规则。

    返回 (根因推断, 建议动作)。修掉根因后，同组合的同源命中大概率一起消失——
    先修根因、再重跑，能避免对失真命中的无效人工核对。无多规则命中返回空串。
    """
    if len(rules_hit) < 2:
        return "", ""
    has_cc = "INC_CUSTOMER_CONSISTENCY" in rules_hit
    has_entity = "INC_ENTITY_SWITCH_MAPPING_DRIFT" in rules_hit
    has_rename = "INC_SIMILAR_CUSTOMER_RENAME" in rules_hit
    has_mom = "INC_MOM_CHANGE" in rules_hit
    has_pp = _PP_RULE_ID in rules_hit
    has_gm = "INC_GM_HIGH_RATIO" in rules_hit
    has_neg_gm = "INC_NEG_GM_HIGH_RATIO" in rules_hit
    has_inv = "INC_REV_COST_INVERSION" in rules_hit
    has_zero = "INC_REV_COST_ZERO_MISMATCH" in rules_hit
    has_hc = "INC_HEADCOUNT_REV_MISMATCH" in rules_hit
    has_hq = "INC_GROUP_HQ_UNSETTLED" in rules_hit
    has_same = "INC_SAME_AMOUNT_ADJACENT_MONTHS" in rules_hit
    has_out = "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO" in rules_hit
    has_mixed = "INC_MIXED_BIZ_TYPE" in rules_hit
    has_biz_mm = "INC_REV_COST_BIZ_TYPE_MISMATCH" in rules_hit
    has_aux_wage = "AUX_WAGE_WRONG_CUSTOMER" in rules_hit
    any_data = has_mom or has_pp or has_gm or has_neg_gm or has_inv or has_zero or has_hc

    # ① 归属/映射未修正：数据被拆挂到不同实际客户/主体名下，历史基线与当月口径都对不上，
    #    波动/毛利率/人数/零值等数据型命中连锁出现——先修映射再重跑。
    if (has_cc or has_entity) and (any_data or has_mixed):
        return ("疑似实际客户/映射未修正引发的连锁命中",
                "先核对客户调整校验映射并修正实际客户归属，修正后重跑——本组合的波动/毛利率/人数/零值等命中大概率随之消失，不要逐条核对失真命中")
    # ② 客商改名未处理：新旧客商并存，历史基线断开。
    if has_rename and (any_data or has_entity):
        return ("疑似客商改名未处理引发的连锁命中",
                "确认更名后直接修改客商名称（不要新增客商），改名后历史基线接上，波动/毛利命中大概率消失")
    # ③ 业务类型记错：类型错位+混做+类型级毛利异常同源。
    if has_biz_mm and (has_mixed or has_gm or has_zero):
        return ("疑似业务类型记错引发的连锁命中",
                "先统一该客户收入/成本侧的业务类型（三级科目），更正后混做/毛利偏高/零值命中大概率消失")
    # ④ 暂估未冲销：跨月同金额+集团本部挂账（或有成本无收入）是重复暂估特征。
    if has_hq and (has_same or (has_zero and _ZERO_REASON_COST in zero_reasons) or has_mom):
        return ("疑似暂估/预提未冲销引发的连锁命中",
                "先核暂估成本是否重复计提并冲销（跨月同金额+集团本部挂账为特征），冲销后同金额/零值/挂账命中大概率消失")
    # ⑤ 外包成本漏记：成本结构缺失导致毛利率/倒挂异常。
    if has_out and (has_gm or has_inv or has_neg_gm):
        return ("疑似外包成本漏记引发的连锁命中",
                "先核对外包合同工资/挂靠成本完整性并补记，补记后毛利率/倒挂命中大概率消失")
    # ⑥ 序时账工资挂错客户：客户成本口径失真。
    if has_aux_wage and (has_cc or has_hc or has_gm):
        return ("疑似工资挂错客户引发的连锁命中",
                "先按映射表修正序时账工资/社保行的实际客户，修正后该客户成本与毛利口径恢复正常")
    return ("", "")


def build_rule_correlation_sheet(
    df_income: Optional[pd.DataFrame],
    income_dim_anomalies: Optional[pd.DataFrame],
    income_gm_anomalies: Optional[pd.DataFrame],
    aux_rule_violations: Optional[pd.DataFrame] = None,
    target_month: Optional[int] = None,
) -> pd.DataFrame:
    index, pp_keys = build_correlation_index(income_dim_anomalies, income_gm_anomalies)
    if not index and not pp_keys:
        return pd.DataFrame()

    # 本月关键指标（剔除集团本部，与规则口径一致）
    metrics: dict[tuple[str, str, str], dict[str, float]] = {}
    if df_income is not None and not df_income.empty and target_month is not None:
        cur = df_income[pd.to_numeric(df_income["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)].copy()
        if "部门" in cur.columns:
            cur = cur[cur["部门"].astype(str).str.strip() != "集团本部"]
        if not cur.empty:
            num_cols = [c for c in ["全额收入", "净额收入", "成本合计", "项目毛利润", "结算人次"] if c in cur.columns]
            cur["_biz_norm"] = cur["三级科目"].map(_norm_biz) if "三级科目" in cur.columns else ""
            g = cur.groupby(["主体账簿", "_biz_norm", "实际客户"], dropna=False)[num_cols].sum().reset_index()
            for _, r in g.iterrows():
                metrics[(str(r["主体账簿"]), str(r["_biz_norm"]), str(r["实际客户"]))] = {
                    c: float(r.get(c, 0.0) or 0.0) for c in num_cols
                }

    # AUX 人次命中按实际客户弱关联
    aux_by_cust: dict[str, list[str]] = defaultdict(list)
    if aux_rule_violations is not None and not aux_rule_violations.empty:
        for _, h in aux_rule_violations.iterrows():
            rid = _norm_text(h.get("规则ID"))
            if rid in _AUX_RULE_IDS:
                cust = _norm_text(h.get("实际客户"))
                if cust:
                    aux_by_cust[cust].append(_AUX_RULE_IDS[rid])

    rows: list[dict[str, Any]] = []
    for (book, biz, cust), hits in index.items():
        rules_hit = {h["rule_id"] for h in hits}
        if (book, biz) in pp_keys:
            rules_hit.add(_PP_RULE_ID)
        zero_reasons = {h["reason"] for h in hits if h["rule_id"] == "INC_REV_COST_ZERO_MISMATCH"}
        err_cnt = sum(1 for h in hits if h["severity"] == "错误")
        confirm_cnt = sum(1 for h in hits if h["severity"] == "需确认")
        rule_cnt = len(rules_hit)
        if err_cnt > 0 and rule_cnt >= 2:
            risk = "高"
        elif rule_cnt >= 2 or err_cnt > 0:
            risk = "中"
        else:
            risk = "低"

        m = metrics.get((book, biz, cust), {})
        aux_refs = aux_by_cust.get(cust, [])
        rule_texts = []
        for rid in sorted(rules_hit):
            display = _CORRELATION_RULES.get(rid) or ("同比波动(键级)" if rid == _PP_RULE_ID else rid)
            rule_texts.append(display)
        if aux_refs:
            rule_texts.append(f"{'、'.join(sorted(set(aux_refs)))}(辅助帐参考)")

        reason_texts: list[str] = []
        seen: set[str] = set()
        for h in sorted(hits, key=lambda x: _sev_rank(x["severity"])):
            t = f"[{h['rule']}] {h['reason']}"
            if t not in seen:
                seen.add(t)
                reason_texts.append(t)
        if (book, biz) in pp_keys:
            reason_texts.append("[同比波动(键级)] 该（主体账簿+三级科目）键命中同比波动，需结合客户级数据核查")

        root_cause, root_action = _detect_root_cause(rules_hit, zero_reasons)

        rows.append({
            "主体账簿": book,
            "三级科目": biz,
            "实际客户": cust,
            "综合风险": risk,
            "命中规则数": rule_cnt,
            "命中规则": "；".join(rule_texts),
            "模式标签": _detect_pattern(rules_hit, zero_reasons),
            "根因推断（可能同源）": root_cause,
            "建议处理顺序": ("P0 先修根因" if root_cause else ("P1 单独核对" if risk == "高" else "P2 常规核对")),
            "根因修复建议": root_action,
            "错误数": err_cnt,
            "需确认数": confirm_cnt,
            "本月全额收入": _fmt_num(m.get("全额收入")),
            "本月净额收入": _fmt_num(m.get("净额收入")),
            "本月成本合计": _fmt_num(m.get("成本合计")),
            "本月项目毛利润": _fmt_num(m.get("项目毛利润")),
            "本月结算人次": _fmt_num(m.get("结算人次"), 0),
            "命中明细": " ｜ ".join(reason_texts),
        })

    # 补充：仅有键级 PP 命中、但该键下无客户级命中的组合不展开（避免噪音），仅在客户级组合存在时叠加。
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    # 有根因推断的组合排最前：修一个根因可批量消除同源命中，人工核对收益最大。
    out["_root_rank"] = (out["根因推断（可能同源）"] != "").map({True: 0, False: 1})
    risk_rank = {"高": 0, "中": 1, "低": 2}
    out = out.sort_values(
        by=["_root_rank", "综合风险", "命中规则数", "本月净额收入"],
        key=lambda s: (s.map(risk_rank).fillna(3) if s.name == "综合风险" else s),
        ascending=[True, True, False, False],
    )
    out = out.drop(columns=["_root_rank"])
    return out.reset_index(drop=True)


def _rate(cur: float, base: float) -> float:
    if base == 0:
        return float("nan")
    return (cur - base) / abs(base)


def _gmr(profit: float, rev: float) -> float:
    if rev == 0:
        return float("nan")
    return profit / rev


# ---------------------------------------------------------------------------
# 疑似数据错误清单：面向"当期修正"的统一输出
# 优先级：
#   P1 疑似错误（建议当期修正）——零值倒挂、外包双零/挂靠断档、人次填写错误
#   P2 需确认（确认后修正或签认）——映射真实不一致、归属漂移、多实际客户、负毛利
#   P3 波动参考（不直接修正，先核业务口径）——同比波动、映射变更（需确认依据）
# ---------------------------------------------------------------------------

def _fix_action(rule_id: str, reason: str, row: dict[str, Any]) -> str:
    """按规则给出建议修正动作（大白话，避免行话）。"""
    if rule_id == "INC_REV_COST_ZERO_MISMATCH":
        if "成本合计=0" in reason:
            return "补记成本，或确认成本该记到哪个月"
        return "补记收入（先核对结算单），或把多记的成本冲掉"
    if rule_id == "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO":
        if "工资=0" in reason:
            return "补记外包工资/挂靠费，或确认业务类型是否填错"
        return "补记本月挂靠费（先核对挂靠协议是否到期）"
    if rule_id == "INC_CUSTOMER_CONSISTENCY":
        if "映射变更" in reason or "与当月映射一致" in reason:
            return "确认归属变更的依据，没问题就签认"
        if "映射表不一致" in reason:
            return "把实际客户改对，或把映射表补上"
        if "组合漂移" in reason or "与上月不一致" in reason:
            return "核对归属是否填错，必要时改正部门/项目/客户"
        if "实际客户 不同值数" in reason or "多实际客户" in reason:
            return "拆开核对每个实际客户各自的收入成本"
        return "核对客户归属字段"
    if rule_id == "INC_HEADCOUNT_DATA_CHECK" or rule_id.startswith("AUX_"):
        if "符号" in reason:
            return "确认业务场景后，改对人次码的正负号"
        return "改对摘要里人次码的写法"
    if rule_id == "INC_NEG_GM_HIGH_RATIO":
        return "看工资/挂靠/返费有没有漏记；若是逾期扣款导致，先核对扣款单"
    if rule_id == "INC_GM_HIGH_RATIO":
        return "看工资、社保、挂靠、返费是不是漏记了"
    if rule_id == "INC_REV_COST_INVERSION":
        return "核对结算单，看收入或成本是不是记错月、记错对象"
    if rule_id == "INC_HEADCOUNT_REV_MISMATCH":
        return "核对结算单，补记漏掉的人次或收入"
    if rule_id == "INC_SOCIAL_HEADCOUNT_MISMATCH":
        return "补记社保费，或核对社保人数是否报错"
    if rule_id == "INC_COST_RATIO_HIGH":
        return "核对返费/挂靠是不是记错科目或金额"
    if rule_id == "INC_EXPENSE_RATIO":
        return "核对福利费/其他费用是不是记错科目"
    if rule_id == "INC_COST_SUDDEN_APPEARANCE":
        return "查这笔突然增加的费用是啥，是不是记错科目"
    if rule_id == "INC_DUPLICATE_ROW":
        return "核对是不是重复录入，重复的行删掉或冲掉"
    if rule_id == "INC_GROUP_HQ_UNSETTLED":
        return "把集团本部挂的暂估成本调整到实际主体/部门，或补做账外调整"
    if rule_id == "INC_SIMILAR_CUSTOMER_RENAME":
        return "确认是不是改名：是的话账上直接改客商名称，别新增客商"
    if rule_id == "AUX_WAGE_WRONG_CUSTOMER":
        return "核对这笔工资/社保实际发给谁，把客户挂对"
    if rule_id == "INC_MIXED_BIZ_TYPE":
        return "核对是不是合同换签了，账上业务类型同步改过来"
    if rule_id == "INC_REV_COST_BIZ_TYPE_MISMATCH":
        return "看收入和成本哪一边的业务类型记错了，改过来"
    if rule_id == "INC_SAME_AMOUNT_ADJACENT_MONTHS":
        return "核对是不是重复暂估/重复确认了，是的话冲掉一笔"
    if rule_id == "INC_SMALL_AMOUNT_WRONG_DEPT":
        return "看这笔零头是不是挂错部门，挂错就调整到正确部门"
    if rule_id == "INC_ENTITY_SWITCH_MAPPING_DRIFT":
        return "确认客户换主体的依据，把映射表更新到新主体"
    if rule_id == "INC_REBATE_EXTERNAL_COST_RECONCILE":
        return "核对返费/挂靠在账上（外部成本）有没有记全，差额补记或更正"
    if rule_id == _PP_RULE_ID or rule_id == "INC_MOM_CHANGE":
        return "先确认业务是不是真有这么大变化，再决定是否调账"
    return "人工核对"


def _fix_priority(rule_id: str, severity: str, reason: str) -> tuple[int, str]:
    if rule_id in (_PP_RULE_ID, "INC_MOM_CHANGE"):
        return (3, "P3 波动参考")
    if rule_id == "INC_CUSTOMER_CONSISTENCY" and ("与当月映射一致" in reason):
        return (3, "P3 波动参考")
    # 可直接当期修正的数据错误 → P1（含从 收入成本表1.py 整合的规则，2026-09-04 重划）
    if rule_id in {
        "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO",
        "INC_GM_HIGH_RATIO",
        "INC_REV_COST_INVERSION",
        "INC_HEADCOUNT_REV_MISMATCH",
        "INC_SOCIAL_HEADCOUNT_MISMATCH",
        "INC_COST_RATIO_HIGH",
        "INC_EXPENSE_RATIO",
        "INC_COST_SUDDEN_APPEARANCE",
        "INC_DUPLICATE_ROW",
        "INC_GROUP_HQ_UNSETTLED",
        "AUX_WAGE_WRONG_CUSTOMER",
        "INC_REV_COST_BIZ_TYPE_MISMATCH",
        "INC_SMALL_AMOUNT_WRONG_DEPT",
        "INC_REBATE_EXTERNAL_COST_RECONCILE",
    }:
        return (1, "P1 疑似错误")
    # 登记表来源的确认类
    if rule_id in {"INC_SIMILAR_CUSTOMER_RENAME", "INC_MIXED_BIZ_TYPE", "INC_ENTITY_SWITCH_MAPPING_DRIFT", "INC_SAME_AMOUNT_ADJACENT_MONTHS"}:
        return (2, "P2 需确认")
    if str(severity) == "错误":
        return (1, "P1 疑似错误")
    return (2, "P2 需确认")


def build_overdue_map(
    aux_df: Optional[pd.DataFrame],
    target_month: Optional[int],
) -> dict[tuple[str, str, str], float]:
    """从调整后序时账识别"应收账款逾期考核扣款"金额。

    口径（与收入成本表1.py一致）：摘要同时含"考核"与"逾期/应收账款"、部门≠集团本部、
    当月记录；按（主体账簿,实际客户,项目,三级科目,账载客户）汇总本币。
    返回按 (主体账簿, 实际客户, 三级科目) 二次聚合的金额（跨项目/账载客户合计），
    供负毛利项目归因判断：亏损是否由逾期考核扣款导致。
    """
    if aux_df is None or aux_df.empty or target_month is None:
        return {}
    if "摘要" not in aux_df.columns or "本币" not in aux_df.columns:
        return {}
    df = aux_df.copy()
    if "月" in df.columns:
        df["_m"] = pd.to_numeric(df["月"], errors="coerce").fillna(-1).astype(int)
        df = df[df["_m"] == int(target_month)]
    if df.empty:
        return {}
    m = df["摘要"].astype(str)
    df = df[m.str.contains("考核", na=False) & m.str.contains("逾期|应收账款", na=False)]
    if df.empty:
        return {}
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"]
    if "三级科目" in df.columns:
        df["三级科目"] = df["三级科目"].map(_norm_biz)
    df["本币"] = pd.to_numeric(df["本币"], errors="coerce").fillna(0.0)
    if df.empty:
        return {}
    agg: dict[tuple[str, str, str], float] = defaultdict(float)
    for _, r in df.iterrows():
        key = (_norm_text(r.get("主体账簿")), _norm_text(r.get("实际客户")), _norm_text(r.get("三级科目")))
        agg[key] += float(r.get("本币", 0.0) or 0.0)
    return {k: v for k, v in agg.items() if abs(v) > 1e-9}


def build_fix_list_sheet(
    df_income: Optional[pd.DataFrame],
    income_dim_anomalies: Optional[pd.DataFrame],
    income_gm_anomalies: Optional[pd.DataFrame],
    aux_rule_violations: Optional[pd.DataFrame] = None,
    aux_df: Optional[pd.DataFrame] = None,
    target_month: Optional[int] = None,
) -> pd.DataFrame:
    """疑似数据错误清单：一行 = 一个待修正/待确认事项，按优先级与金额排序。

    与明细页的区别：明细页按规则展开（同一组合可能多行），本页按"修正事项"聚合——
    同一客户组合的多个规则命中合并为一行，给出唯一的主修正动作。
    """
    index, _pp_keys = build_correlation_index(income_dim_anomalies, income_gm_anomalies)

    # 本月关键指标 + 源行号（与规则关联分析同一套聚合口径）
    metrics: dict[tuple[str, str, str], dict[str, float]] = {}
    src_rows_map: dict[tuple[str, str, str], str] = {}
    hit_amount_map: dict[tuple[str, str, str], float] = {}

    def _hit_amount(rule_id: str, reason: str, m: dict[str, float]) -> float:
        """取该命中直接对应的金额（与规则口径一致，而非客户全景聚合）。"""
        if rule_id == "INC_REV_COST_ZERO_MISMATCH":
            return abs(m.get("净额收入", 0.0)) if "成本合计=0" in reason else abs(m.get("成本合计", 0.0))
        if rule_id == "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO":
            return max(abs(m.get("全额收入", 0.0)), abs(m.get("成本合计", 0.0)))
        if rule_id == "INC_NEG_GM_HIGH_RATIO":
            return abs(m.get("项目毛利润", 0.0))
        return max(abs(m.get("净额收入", 0.0)), abs(m.get("成本合计", 0.0)))

    if df_income is not None and not df_income.empty and target_month is not None:
        cur = df_income[
            pd.to_numeric(df_income["月"], errors="coerce").fillna(-1).astype(int) == int(target_month)
        ].copy()
        if "部门" in cur.columns:
            cur = cur[cur["部门"].astype(str).str.strip() != "集团本部"]
        if not cur.empty:
            num_cols = [c for c in ["全额收入", "净额收入", "成本合计", "项目毛利润", "结算人次"] if c in cur.columns]
            cur["_biz_norm"] = cur["三级科目"].map(_norm_biz) if "三级科目" in cur.columns else ""
            g = cur.groupby(["主体账簿", "_biz_norm", "实际客户"], dropna=False)[num_cols].sum().reset_index()
            for _, r in g.iterrows():
                metrics[(str(r["主体账簿"]), str(r["_biz_norm"]), str(r["实际客户"]))] = {
                    c: float(r.get(c, 0.0) or 0.0) for c in num_cols
                }
            if "_src_row" in cur.columns:
                src_g = cur.groupby(["主体账簿", "_biz_norm", "实际客户"], dropna=False)["_src_row"].apply(_src_rows_text)
                for (book, biz, cust), txt in src_g.items():
                    src_rows_map[(str(book), str(biz), str(cust))] = str(txt)

    rows: list[dict[str, Any]] = []

    # 应收账款逾期考核归因：负毛利可能由逾期考核扣款导致，非漏记成本
    overdue_map = build_overdue_map(aux_df, target_month)

    for (book, biz, cust), hits in index.items():
        if not hits:
            continue
        # 修正清单只收"疑似错误/需确认"，剔除纯波动信号（同比/环比）——它们留在明细页作参考
        hits_fix = [h for h in hits if h["rule_id"] not in (_PP_RULE_ID, "INC_MOM_CHANGE")]
        if not hits_fix:
            continue
        m = metrics.get((book, biz, cust), {})
        # 主规则：优先取零值倒挂（错误级、金额可直接定位），否则取严重度最高的命中
        primary = None
        for h in sorted(hits_fix, key=lambda x: _sev_rank(x["severity"])):
            if h["rule_id"] == "INC_REV_COST_ZERO_MISMATCH":
                primary = h
                break
            if primary is None:
                primary = h
        best = min(
            (_fix_priority(h["rule_id"], h["severity"], h["reason"]) for h in hits_fix),
            key=lambda x: x[0],
        )
        reasons: list[str] = []
        for h in sorted(hits_fix, key=lambda x: _sev_rank(x["severity"])):
            t = f"[{h['rule']}] {h['reason']}"
            if t not in reasons:
                reasons.append(t)

        # 命中组合金额展示：主规则口径金额放在首要位置，其余为全景参考
        hit_amt = _hit_amount(primary["rule_id"], primary["reason"], m)
        if primary["rule_id"] == "INC_REV_COST_ZERO_MISMATCH":
            show_rev = _fmt_num(m.get("净额收入")) if "成本合计=0" in primary["reason"] else _fmt_num(0.0)
            show_cost = _fmt_num(m.get("成本合计")) if "成本合计=0" not in primary["reason"] else _fmt_num(0.0)
        else:
            show_rev = _fmt_num(m.get("净额收入"))
            show_cost = _fmt_num(m.get("成本合计"))
        hit_amount_map[(book, biz, cust)] = hit_amt

        issue_text = "；".join(reasons)[:500]
        action_text = _fix_action(primary["rule_id"], primary["reason"], {})
        # 负毛利归因：若应收账款逾期考核扣款可解释亏损，追加参考说明（避免误当漏记成本去修）
        profit_val = m.get("项目毛利润", 0.0)
        if profit_val is not None and float(profit_val) < 0:
            ov = overdue_map.get((book, biz, cust), 0.0)
            if abs(ov) > 1e-9:
                issue_text = (
                    f"{issue_text}｜参考：应收账款逾期考核扣款 {ov:,.2f} 元"
                    f"（序时账摘要含'考核+逾期/应收账款'；若|毛利|≈{abs(ov):,.0f}，亏损由逾期考核导致，非漏记成本）"
                )[:500]
                action_text = f"{action_text}；优先核对逾期考核扣款单"

        rows.append({
            "优先级": best[1],
            "主体账簿": book,
            "业务类型": biz,
            "实际客户": cust,
            "本月净额收入": show_rev,
            "本月成本合计": show_cost,
            "本月项目毛利润": _fmt_num(m.get("项目毛利润")),
            "疑似问题": issue_text,
            "命中规则": "、".join(sorted({h["rule"] for h in hits_fix})),
            "建议修正动作": action_text,
            "源行号": src_rows_map.get((book, biz, cust), ""),
        })

    # AUX 人次命中（逐条：有凭证号可直接定位修正）
    if aux_rule_violations is not None and not aux_rule_violations.empty:
        for _, h in aux_rule_violations.iterrows():
            rid = _norm_text(h.get("规则ID"))
            if rid not in _AUX_RULE_IDS:
                continue
            sev = _norm_text(h.get("严重度")) or "需确认"
            reason = _norm_text(h.get("命中原因"))
            pri = _fix_priority(rid, sev, reason)
            rows.append({
                "优先级": pri[1],
                "主体账簿": _norm_text(h.get("主体账簿")),
                "业务类型": "",
                "实际客户": _norm_text(h.get("实际客户")),
                "本月净额收入": "",
                "本月成本合计": "",
                "本月项目毛利润": "",
                "疑似问题": f"[人次数据检查] {reason}（摘要：{_norm_text(h.get('摘要'))[:60]}，凭证号：{_norm_text(h.get('凭证号'))}）",
                "命中规则": _AUX_RULE_IDS[rid],
                "建议修正动作": _fix_action(rid, reason, {}),
                "源行号": _norm_text(h.get("_src_row")),
            })

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    pri_rank = {"P1 疑似错误": 0, "P2 需确认": 1, "P3 波动参考": 2}

    def _amount_of(r: pd.Series) -> float:
        for col in ("本月净额收入", "本月成本合计", "本月项目毛利润"):
            v = pd.to_numeric(pd.Series([r.get(col)]), errors="coerce").iloc[0]
            if not pd.isna(v) and float(v) != 0.0:
                return abs(float(v))
        return 0.0

    out = out.sort_values(
        by=["优先级"],
        key=lambda s: s.map(pri_rank).fillna(9),
        ascending=True,
        kind="stable",
    )
    # 组内按影响金额降序
    out["__amt"] = out.apply(_amount_of, axis=1)
    out = out.sort_values(by=["优先级", "__amt"], ascending=[True, False], kind="stable").drop(columns=["__amt"])
    return out.reset_index(drop=True)


def build_customer_profile_sheet(
    df_income: Optional[pd.DataFrame],
    target_month: Optional[int],
    correlation_index: Optional[dict[tuple[str, str, str], list[dict[str, str]]]] = None,
    pp_keys: Optional[set[tuple[str, str]]] = None,
) -> pd.DataFrame:
    if df_income is None or df_income.empty or target_month is None:
        return pd.DataFrame()

    df = df_income.copy()
    df["月"] = pd.to_numeric(df["月"], errors="coerce")
    df = df[df["月"].notna()]
    df["月"] = df["月"].astype(int)
    if "部门" in df.columns:
        df = df[df["部门"].astype(str).str.strip() != "集团本部"]
    if df.empty:
        return pd.DataFrame()
    if "三级科目" in df.columns:
        df["_biz_norm"] = df["三级科目"].map(_norm_biz)

    num_cols = [c for c in ["全额收入", "净额收入", "成本合计", "项目毛利润", "结算人次"] if c in df.columns]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)

    g = df.groupby(["主体账簿", "实际客户", "月"], dropna=False)[num_cols].sum().reset_index()
    if g.empty:
        return pd.DataFrame()

    tgt = int(target_month)
    index = correlation_index or {}
    pp_set = pp_keys or set()

    rows: list[dict[str, Any]] = []
    for (book, cust), grp in g.groupby(["主体账簿", "实际客户"], dropna=False):
        cur_row = grp[grp["月"] == tgt]
        hist = grp[grp["月"] < tgt]
        if cur_row.empty:
            continue
        cur = {c: float(cur_row.iloc[0].get(c, 0.0) or 0.0) for c in num_cols}
        months_hist = sorted(int(m) for m in hist["月"].tolist())
        hist_mean = {c: float(hist[c].mean()) if not hist.empty else 0.0 for c in num_cols}
        has_hist = bool(months_hist)

        cur_gmr = _gmr(cur.get("项目毛利润", 0.0), cur.get("净额收入", 0.0))
        hist_gmr = _gmr(
            float(hist["项目毛利润"].sum()) if not hist.empty else 0.0,
            float(hist["净额收入"].sum()) if not hist.empty else 0.0,
        )
        rev_rate = _rate(cur.get("净额收入", 0.0), hist_mean.get("净额收入", 0.0)) if has_hist else float("nan")
        cnt_rate = _rate(cur.get("结算人次", 0.0), hist_mean.get("结算人次", 0.0)) if has_hist else float("nan")

        labels: list[str] = []
        advice: list[str] = []

        # 生命周期
        no_rev_cur = abs(cur.get("净额收入", 0.0)) <= 1e-9
        if not has_hist:
            life = "新客户(无历史)"
            labels.append("新客户")
            advice.append("核对首月计价口径、成本计提与开票安排")
        elif no_rev_cur and float(hist["净额收入"].abs().sum()) > 1e-9:
            life = f"本月无收入(历史{len(months_hist)}个月有发生)"
            labels.append("本月净额收入为0")
            advice.append("核对是否停业务、结转或漏记收入")
        else:
            life = f"存续({len(months_hist)}个月历史)"

        # 倒挂（收入0成本>0）时，变动率/背离标签失去可比意义，跳过
        inverted = no_rev_cur and abs(cur.get("成本合计", 0.0)) > 1e-9

        # 收入变动（基线月均<=0 时比率不可比，不输出标签）
        if (
            has_hist
            and not inverted
            and pd.notna(rev_rate)
            and hist_mean.get("净额收入", 0.0) > 0
            and abs(rev_rate) >= _SURGE_THRESHOLD
        ):
            tag = "收入骤增" if rev_rate > 0 else "收入骤降"
            labels.append(tag)
            advice.append("核对结算单/确认口径" if rev_rate > 0 else "核对业务量与结算单")

        # 毛利
        if pd.notna(cur_gmr) and cur_gmr < 0:
            labels.append("本月负毛利")
            advice.append("核对成本计提完整性（工资/挂靠/返费）")
        elif has_hist and pd.notna(cur_gmr) and pd.notna(hist_gmr) and hist_gmr > 0 and cur_gmr < hist_gmr - 0.1:
            labels.append("毛利率显著下滑")
            advice.append("对比成本结构（工资/挂靠/返费）与历史月")

        # 人次背离（停业/倒挂月份无从谈配比，跳过）
        if (
            has_hist
            and not inverted
            and not no_rev_cur
            and pd.notna(rev_rate)
            and pd.notna(cnt_rate)
            and hist_mean.get("净额收入", 0.0) > 0
            and abs(rev_rate) >= _SURGE_THRESHOLD
            and abs(cnt_rate - rev_rate) > _DIVERGENCE_THRESHOLD
        ):
            labels.append("收入与人次背离")
            advice.append("核对结算人次与收入的配比关系")

        # 挂靠断档
        if has_hist and "第三方挂靠成本" in df.columns:
            hp = float(hist["第三方挂靠成本"].abs().max()) if "第三方挂靠成本" in hist.columns else 0.0
            cp = cur.get("第三方挂靠成本", 0.0)
            if hp > 1e-9 and abs(cp) <= 1e-9:
                labels.append("挂靠成本断档")
                advice.append("核对挂靠协议是否到期或漏计")

        # 收入成本倒挂
        if abs(cur.get("净额收入", 0.0)) <= 1e-9 and abs(cur.get("成本合计", 0.0)) > 1e-9:
            labels.append("本月收入0成本>0")
            advice.append("核对暂估/红冲/错期")

        # 规则关联
        rules_hit: set[str] = set()
        for (b, _b, c) in index.keys():
            if b == book and c == cust:
                for h in index[(b, _b, c)]:
                    rules_hit.add(h["rule_id"])
        if any(b == book for (b, _b) in pp_set):
            # 键级波动命中：仅当该客户同键也有命中或指标波动时提示，避免全量误挂
            pass
        linked = sorted((_CORRELATION_RULES.get(r, r) for r in rules_hit))
        if linked:
            labels.append(f"规则命中:{len(linked)}项")
            advice.append("见\"规则关联分析\"页")

        risk_score = len(labels)
        rows.append({
            "主体账簿": book,
            "实际客户": cust,
            "生命周期": life,
            "首次/历史月份": "、".join(str(m) for m in months_hist[:12]) + ("…" if len(months_hist) > 12 else ""),
            "本月净额收入": _fmt_num(cur.get("净额收入")),
            "本月全额收入": _fmt_num(cur.get("全额收入")),
            "本月成本合计": _fmt_num(cur.get("成本合计")),
            "本月项目毛利润": _fmt_num(cur.get("项目毛利润")),
            "本月结算人次": _fmt_num(cur.get("结算人次"), 0),
            "历史月均净额收入": _fmt_num(hist_mean.get("净额收入")),
            "历史月均成本合计": _fmt_num(hist_mean.get("成本合计")),
            "收入vs月均变动": _fmt_pct(rev_rate) if (has_hist and hist_mean.get("净额收入", 0.0) > 0) else "",
            "人次vs月均变动": _fmt_pct(cnt_rate) if (has_hist and hist_mean.get("结算人次", 0.0) > 0) else "",
            "本月毛利率": _fmt_pct(cur_gmr) if pd.notna(cur_gmr) else "",
            "历史毛利率": _fmt_pct(hist_gmr) if pd.notna(hist_gmr) else "",
            "风险标签数": risk_score,
            "风险标签": "；".join(labels) if labels else "无异常标签",
            "处理建议": "；".join(dict.fromkeys(advice)) if advice else "—",
            "本月规则命中": "、".join(linked) if linked else "—",
        })

    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    out = out.sort_values(by=["风险标签数", "本月净额收入"], ascending=[False, False])
    return out.reset_index(drop=True)

"""月度项目利润异常登记表打通：读取登记表 + 已知问题去重 + 导出登记格式。

登记表结构（13 列）：序号/账务月份/主体账簿/账载客户/问题描述/附件/金额/问题发现人/
发现日期/问题类型/制单人/是否修改/备注。

两个用途：
1. deduplicate：审核命中与历史登记比对——客户+月份已登记的命中标"已登记"，
   登记过"已修改"但本月又命中的标"疑似重复出现"。
2. export：把修正清单转换成登记表格式（待登记行），直接粘进登记表走指派闭环。
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .check_utils import _strip_percent_suffix

# 登记表标准列（导出时按此顺序生成）
REGISTRATION_COLUMNS = [
    "序号", "账务月份", "主体账簿", "账载客户", "问题描述", "附件", "金额",
    "问题发现人", "发现日期", "问题类型", "制单人", "是否修改", "备注",
]

# 修正清单规则 → 登记表"问题类型"下拉值的映射
_RULE_TO_REG_TYPE = {
    "INC_CUSTOMER_CONSISTENCY": "账载客户",
    "AUX_WAGE_WRONG_CUSTOMER": "账载客户",
    "INC_REV_COST_ZERO_MISMATCH": "金额",
    "INC_REV_COST_INVERSION": "金额",
    "INC_OUTSOURCING_NO_WAGE_OR_HANGKAO": "金额",
    "INC_GM_HIGH_RATIO": "金额",
    "INC_NEG_GM_HIGH_RATIO": "金额",
    "INC_COST_RATIO_HIGH": "收支项目",
    "INC_EXPENSE_RATIO": "科目",
    "INC_COST_SUDDEN_APPEARANCE": "科目",
    "INC_DUPLICATE_ROW": "金额",
    "INC_GROUP_HQ_UNSETTLED": "科目",
    "AUX_HEADCOUNT_DATA_CHECK": "摘要",
    "INC_MIXED_BIZ_TYPE": "科目",
    "INC_SIMILAR_CUSTOMER_RENAME": "账载客户",
    "INC_MOM_CHANGE": "金额",
    "INC_HEADCOUNT_REV_MISMATCH": "摘要",
    "INC_SOCIAL_HEADCOUNT_MISMATCH": "金额",
}

_DEFAULT_REG_TYPE = "金额"


def find_registration_file(workdir: Path) -> Optional[Path]:
    """在 workdir 内查找登记表（名称含"异常登记"的 xlsx）。"""
    if not workdir.exists():
        return None
    for p in sorted(workdir.glob("*.xlsx")):
        name = p.name
        if name.startswith("~$"):
            continue
        if ("异常登记" in name) or ("登记表" in name and "异常" in name):
            return p
    return None


def load_registration_table(path: Optional[Path]) -> pd.DataFrame:
    """读取登记表数据行（问题描述非空）。缺文件返回空表。

    兼容两种布局：① 首个 sheet 即数据表（表头在第 1 行）；② "登记说明" sheet 在前，
    "异常登记表" 数据 sheet 在后（表头可能不在第 1 行）——扫描所有 sheet 定位含"问题描述"列的表头。
    """
    empty = pd.DataFrame(columns=["账务月份", "主体账簿", "账载客户", "问题描述", "金额", "问题类型", "是否修改"])
    if path is None or not path.exists():
        return empty
    try:
        xls = pd.ExcelFile(path)
    except Exception:
        return empty
    try:
        return _load_registration_from_xls(xls)
    finally:
        try:
            xls.close()
        except Exception:
            pass


def _load_registration_from_xls(xls: pd.ExcelFile) -> pd.DataFrame:
    for sheet in xls.sheet_names:
        try:
            raw = pd.read_excel(xls, sheet_name=sheet, header=None)
        except Exception:
            continue
        if raw.empty:
            continue
        # 定位表头行：须同时含"问题描述"与"主体账簿"（排除"登记说明"里的列说明表）
        header_idx = None
        for i, row in raw.iterrows():
            cells = {str(v).strip() for v in row if pd.notna(v)}
            if "问题描述" in cells and "主体账簿" in cells:
                header_idx = i
                break
        if header_idx is None:
            continue
        headers = [str(v).strip() if pd.notna(v) else f"_c{j}" for j, v in enumerate(raw.iloc[header_idx])]
        data = raw.iloc[header_idx + 1:].copy()
        data.columns = headers
        if "问题描述" not in data.columns:
            continue
        df = data[data["问题描述"].notna() & (data["问题描述"].astype(str).str.strip() != "")].copy()
        if df.empty:
            continue
        for c in ["账务月份", "主体账簿", "账载客户", "问题描述", "金额", "问题类型", "是否修改"]:
            if c not in df.columns:
                df[c] = ""
        return df.reset_index(drop=True)
    return empty


def _norm_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def _month_key(v: Any) -> str:
    """归一账务月份到 YYYY-MM。"""
    s = _norm_text(v)
    if not s:
        return ""
    m = re.search(r"(20\d{2})[/\-年.]?(\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return s


def _customer_key(v: Any) -> str:
    """客户名归一：去括号/空格/常见后缀差异，便于模糊匹配。"""
    s = _norm_text(v)
    s = re.sub(r"[（(].*?[)）]", "", s)
    for w in ("有限公司", "有限责任公司", "股份有限公司", "分公司"):
        s = s.replace(w, "")
    return re.sub(r"\s+", "", s)


def attach_registration_status(
    fix_list: pd.DataFrame,
    reg_df: pd.DataFrame,
    yyyymm: str,
) -> pd.DataFrame:
    """给修正清单附加"登记状态"列。

    - 已登记（历史登记过同客户同月的问题）：该客户+账务月份在登记表出现过
    - 疑似重复出现：该客户+月份登记过且"是否修改=已修改"，但本月审核又命中
    - 未登记：默认
    """
    if fix_list is None or fix_list.empty:
        return fix_list
    out = fix_list.copy()
    if reg_df is None or reg_df.empty:
        out["登记状态"] = "未登记"
        return out

    reg_df = reg_df.copy()
    reg_df["_mk"] = reg_df["账务月份"].map(_month_key)
    reg_df["_ck"] = reg_df["账载客户"].map(_customer_key)
    reg_df["_bk"] = reg_df["主体账簿"].map(_customer_key)

    # 登记过的 (账簿key, 客户key) → 是否有"已修改"
    reg_map: dict[tuple[str, str], bool] = {}
    for _, r in reg_df.iterrows():
        k = (r["_bk"], r["_ck"])
        fixed = str(r.get("是否修改") or "").strip() == "已修改"
        reg_map[k] = reg_map.get(k, False) or fixed

    def _status(r: pd.Series) -> str:
        ck = _customer_key(r.get("实际客户"))
        bk = _customer_key(r.get("主体账簿"))
        if not ck:
            return "未登记"
        # 匹配顺序：账簿+客户 → 仅客户
        if (bk, ck) in reg_map:
            return "疑似重复出现（曾已修改）" if reg_map[(bk, ck)] else "已登记"
        # 仅客户匹配：任意账簿下登记过
        for (b, c), fixed in reg_map.items():
            if c == ck:
                return "疑似重复出现（曾已修改）" if fixed else "已登记"
        return "未登记"

    out["登记状态"] = out.apply(_status, axis=1)
    return out


def export_registration_rows(
    fix_list: pd.DataFrame,
    yyyymm: str,
    finder: str = "凭证审核工具",
) -> pd.DataFrame:
    """把修正清单转换成登记表格式（待登记行），可直接粘贴到登记表走指派闭环。

    只导出 未登记 的行（已登记的跳过，避免重复）。
    """
    if fix_list is None or fix_list.empty:
        return pd.DataFrame(columns=REGISTRATION_COLUMNS)
    out_rows: list[dict[str, Any]] = []
    seq = 1
    today = datetime.now().strftime("%Y-%m-%d")
    for _, r in fix_list.iterrows():
        if str(r.get("登记状态", "未登记")) != "未登记":
            continue
        rules = _norm_text(r.get("命中规则"))
        reg_type = _DEFAULT_REG_TYPE
        for rid, t in _RULE_TO_REG_TYPE.items():
            if rid in rules:
                reg_type = t
                break
        amount = ""
        for col in ("本月净额收入", "本月成本合计", "本月项目毛利润"):
            v = pd.to_numeric(pd.Series([r.get(col)]), errors="coerce").iloc[0]
            if not pd.isna(v) and float(v) != 0:
                amount = round(float(v), 2)
                break
        biz = _norm_text(r.get("业务类型"))
        desc = f"[{r.get('优先级')}] {_norm_text(r.get('疑似问题'))}（命中规则：{rules}；源行号：{_norm_text(r.get('源行号'))}）"
        out_rows.append({
            "序号": seq,
            "账务月份": f"{yyyymm[:4]}/{int(yyyymm[4:]):02d}",
            "主体账簿": _norm_text(r.get("主体账簿")),
            "账载客户": f"{_norm_text(r.get('实际客户'))}" + (f"（{biz}）" if biz else ""),
            "问题描述": desc[:500],
            "附件": "",
            "金额": amount,
            "问题发现人": finder,
            "发现日期": today,
            "问题类型": reg_type,
            "制单人": "",
            "是否修改": "",
            "备注": "",
        })
        seq += 1
    return pd.DataFrame(out_rows, columns=REGISTRATION_COLUMNS)

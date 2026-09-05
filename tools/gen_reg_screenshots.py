# -*- coding: utf-8 -*-
"""为月度项目利润异常登记表的明细行生成"收入成本表区域截图"并锚定到附件列。

参照登记表既有附件截图的样式（筛选到目标客户的收入成本表区域，
带 Excel 列标/筛选按钮/行号，命中单元格红底高亮），用 PIL 自绘同风格
图片，无需打开 Excel，可批量生成。

用法：
    python tools/gen_reg_screenshots.py \
        --reg "月度项目利润异常登记表-1.xlsx" \
        --report "<报告目录>/凭证审核报告_xxx.xlsx" \
        --workdir "D:\\分析Work\\...\\202608收入成本表xxx" \
        [--out-dir 附件截图] [--max-rows 20] [--only-matching]

流程：
  1. 从报告「疑似数据错误清单」建立 (主体,客户,规则,金额)->源行号 映射；
  2. 从登记表找出本轮工具追加的明细行（发现人=凭证审核工具 且描述含"明细"）；
  3. 每条明细：取该账载客户在收入成本表中的行（时间升序，默认最多 20 行），
     命中源行红底高亮，渲染 PNG；
  4. 图片锚定到该行"附件"列，保存登记表。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.drawing.image import Image as XLImage
from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------- 源表读取

def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def load_income_cost(workdir: Path):
    """读取 考核表输出.xlsx 的收入成本表，返回 (header, rows)，rows 带源表行号。"""
    path = workdir / "考核表输出.xlsx"
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["收入成本表"]
    data = list(ws.iter_rows(values_only=True))
    header = [str(x) if x is not None else "" for x in data[0]]
    rows = []
    for i, r in enumerate(data[1:], start=2):  # 源表行号：表头占 1 行
        if r[0] is None and r[1] is None:
            continue
        rows.append({"_src_row": i, "cells": r})
    return header, rows


# ------------------------------------------------------------ 命中映射构建

def parse_src_rows(txt: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", str(txt))]


def strip_biz_suffix(cust: str) -> str:
    """去掉待登记异常客户列的 (业务类型) 后缀。"""
    return re.sub(r"（[^）]*）\s*$", "", str(cust or "")).strip()


def build_hit_map(report: Path):
    """(主体, 实际客户, 规则名, 金额) -> 源行号集合。

    疑似数据错误清单中按账载客户分组的规则（归属一致性/主体切换）没有实际客户值，
    额外从「新增规则明细」按账载客户注册宽松键，保证登记表明细能匹配到源行。
    """
    wb = openpyxl.load_workbook(report, read_only=True, data_only=True)
    ws = wb["疑似数据错误清单"]
    rows = list(ws.iter_rows(values_only=True))
    h = [str(x) for x in rows[0]]
    j = {c: i for i, c in enumerate(h)}
    hit = defaultdict(set)
    for r in rows[1:]:
        if not any(v is not None for v in r):
            continue
        cust = strip_biz_suffix(r[j["实际客户"]])
        entity = str(r[j["主体账簿"]] or "")
        rules = str(r[j["命中规则"]] or "")
        amount = r[j["本月净额收入"]]
        srcs = parse_src_rows(r[j["源行号"]])
        for rule in rules.split("、"):
            rule = rule.strip()
            if not rule:
                continue
            hit[(entity, cust, rule, round(_num(amount), 2))] |= set(srcs)
        # 兜底键：不带金额
        for rule in rules.split("、"):
            rule = rule.strip()
            if rule:
                hit[(entity, cust, rule, None)] |= set(srcs)

    # 宽松键：无实际客户的规则改按账载客户注册（"客户换了主体，映射要跟着改"等跨主体规则主体为空）。
    # 新增规则明细无源行号列，命中后高亮该客户目标月全部行（主流程处理）。
    ws2 = wb["新增规则明细"]
    rows2 = list(ws2.iter_rows(values_only=True))
    h2 = [str(x) for x in rows2[0]]
    j2 = {c: i for i, c in enumerate(h2)}
    loose_rules = {"客户归属一致性", "客户归属一致性检查", "客户换了主体，映射要跟着改"}
    for r in rows2[1:]:
        if not any(v is not None for v in r):
            continue
        rule = str(r[j2.get("规则名称")] or "").strip()
        if rule not in loose_rules:
            continue
        cust = str(r[j2.get("账载客户")] or "").strip()
        if not cust:
            continue
        entity = str(r[j2.get("主体账簿")] or "").strip()
        hit[(entity, cust, rule, None)] |= set()
        hit[("", cust, rule, None)] |= set()
    return hit


# ---------------------------------------------------------------- 截图渲染

# 配色（参照 Excel/登记表截图样例）
HDR_BG = (68, 84, 106)       # 表头深蓝灰底
HDR_FG = (255, 255, 255)     # 表头白字
COLBAR_BG = (240, 240, 240)  # 列标行浅灰
ROWNO_BG = (232, 232, 232)   # 行号列灰
GRID = (208, 208, 208)       # 网格线
HIT_BG = (255, 80, 80)       # 命中红底（样例为纯红，稍柔和不刺眼）
ALT_BG = (246, 248, 250)     # 斑马纹

# 归属/映射类问题高亮"实际客户"列；金额类问题高亮整行
HIGHLIGHT_COL_RULES = {
    "客户归属一致性", "客户换了主体，映射要跟着改", "疑似客商改名",
    "工资好像挂错了客户（序时账）", "零头成本挂在别的部门",
}

# 截图列：基础列（与样例一致 A-H）+ 金额类问题追加的关键列
BASE_COLS = ["主体账簿", "月", "内外", "三级科目", "账载客户", "实际客户", "部门", "项目"]
MONEY_COLS = ["净额收入", "全额收入", "成本合计", "项目毛利润", "结算人次"]


def _load_font(size: int = 11):
    for name in ("msyh.ttc", "msyh.ttf", "simhei.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _col_letter(idx: int) -> str:
    s = ""
    while idx >= 0:
        s = chr(65 + idx % 26) + s
        idx = idx // 26 - 1
    return s


def render_table_png(header: list[str], rows: list[dict], cols: list[str],
                     hit_rows: set[int], hit_col: str | None,
                     out_path: Path, row_h: int = 22) -> None:
    """渲染模拟 Excel 的区域截图。rows: [{_src_row, cells}]; cells 按源表列索引取值。"""
    font = _load_font(11)
    colbar_font = _load_font(10)
    idx = {c: i for i, c in enumerate(header)}
    use_cols = [c for c in cols if c in idx]

    # 列宽自适应（像素），中文字符按 2 倍宽估算，再夹取范围
    def text_w(t: str) -> int:
        w = 0
        for ch in str(t):
            w += 18 if ord(ch) > 0x2E7F else 9
        return w + 12

    widths = [max(46, min(text_w(str(r["cells"][idx[c]]) for r in rows) if rows else 60,
                          text_w(c), 260)) for c in use_cols]
    W = sum(widths) + 46  # +行号列
    H = (len(rows) + 2) * row_h

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    # 列标行（A/B/C + 筛选按钮感）
    x = 46
    for k, c in enumerate(use_cols):
        d.rectangle([x, 0, x + widths[k], row_h], fill=COLBAR_BG, outline=GRID)
        d.text((x + widths[k] // 2 - 5, 4), _col_letter(k), fill=(80, 80, 80), font=colbar_font)
        x += widths[k]

    # 表头行
    x = 46
    for k, c in enumerate(use_cols):
        d.rectangle([x, row_h, x + widths[k], row_h * 2], fill=HDR_BG, outline=(40, 50, 66))
        d.text((x + 4, row_h + 4), c, fill=HDR_FG, font=font)
        d.text((x + widths[k] - 12, row_h + 4), "▼", fill=HDR_FG, font=colbar_font)
        x += widths[k]

    # 数据行
    y = row_h * 2
    for n, r in enumerate(rows):
        is_hit = r["_src_row"] in hit_rows
        bg = ALT_BG if n % 2 else (255, 255, 255)
        d.rectangle([0, y, W, y + row_h], fill=bg, outline=GRID)
        # 行号
        d.rectangle([0, y, 46, y + row_h], fill=ROWNO_BG, outline=GRID)
        d.text((40 - len(str(r["_src_row"])) * 7, y + 4), str(r["_src_row"]), fill=(90, 90, 90), font=colbar_font)
        x = 46
        for k, c in enumerate(use_cols):
            v = r["cells"][idx[c]]
            text = "" if v is None else str(v)
            cell_bg = None
            if is_hit and (hit_col is None or c == hit_col):
                cell_bg = HIT_BG
            if cell_bg:
                d.rectangle([x, y, x + widths[k], y + row_h], fill=cell_bg, outline=GRID)
            # 按像素宽度裁剪文本，避免溢出到相邻单元格
            avail = widths[k] - 8
            shown, acc = "", 0
            for ch in text:
                acc += 18 if ord(ch) > 0x2E7F else 9
                if acc > avail:
                    shown += "…"
                    break
                shown += ch
            d.text((x + 4, y + 4), shown, fill=(20, 20, 20), font=font)
            x += widths[k]
        y += row_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path)


# ---------------------------------------------------------------- 主流程

def pick_rows_for_customer(header: list[str], rows: list[dict], booked: str, max_rows: int) -> list[dict]:
    """按客户取行：优先账载客户精确匹配，回退实际客户，再回退客户名前缀模糊。"""
    def rows_where(col: int, pred) -> list[dict]:
        return [r for r in rows if pred(str(r["cells"][col] or "").strip())]
    booked_col = header.index("账载客户") if "账载客户" in header else 4
    actual_col = header.index("实际客户") if "实际客户" in header else 5
    got = rows_where(booked_col, lambda v: v == booked)
    if not got:
        got = rows_where(actual_col, lambda v: v == booked)
    if not got:
        key = booked[:8]
        got = [r for r in rows
               if str(r["cells"][booked_col] or "").startswith(key)
               or str(r["cells"][actual_col] or "").startswith(key)]
    return got[-max_rows:] if len(got) > max_rows else got


def main() -> int:
    ap = argparse.ArgumentParser(description="为登记表明细行生成收入成本表区域截图并锚定到附件列")
    ap.add_argument("--reg", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out-dir", default=None, help="截图输出目录（默认：登记表同目录/附件截图）")
    ap.add_argument("--max-rows", type=int, default=20)
    ap.add_argument("--month", type=int, default=8, help="目标月（宽松命中时高亮该月行）")
    ap.add_argument("--dry-run", action="store_true", help="只生成图片，不写回登记表")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条明细（调试用）")
    args = ap.parse_args()

    reg_path = Path(args.reg)
    workdir = Path(args.workdir)
    out_dir = Path(args.out_dir) if args.out_dir else reg_path.parent / "附件截图"

    header, src_rows = load_income_cost(workdir)
    hit_map = build_hit_map(Path(args.report))

    wb = openpyxl.load_workbook(reg_path)
    ws = wb["异常登记表"]

    # 找本轮工具追加的明细行
    targets = []
    for i in range(2, ws.max_row + 1):
        who = ws.cell(row=i, column=8).value
        desc = str(ws.cell(row=i, column=5).value or "")
        if str(who or "") == "凭证审核工具" and "明细：" in desc:
            targets.append(i)
    if args.limit:
        targets = targets[: args.limit]
    print(f"待附图明细行: {len(targets)}")

    ok = miss = 0
    for i in targets:
        entity = str(ws.cell(row=i, column=3).value or "")
        booked = strip_biz_suffix(ws.cell(row=i, column=4).value)
        rule = str(ws.cell(row=i, column=10).value or "")
        amount = ws.cell(row=i, column=7).value
        desc = str(ws.cell(row=i, column=5).value or "")

        srcs = hit_map.get((entity, booked, rule, round(_num(amount), 2))) or set()
        if not srcs:
            srcs = hit_map.get((entity, booked, rule, None)) or set()
        if not srcs:
            # 宽松兜底：同客户 + 同规则（规则名去"检查"归一），命中多条时合并源行。
            def _norm(s: str) -> str:
                return str(s).replace("检查", "").strip()
            cands = [v for k, v in hit_map.items()
                     if k[1] == booked and _norm(k[2]) == _norm(rule)]
            if cands:
                srcs = set().union(*cands)
        if not srcs and not any(k[1] == booked and _norm(k[2]) == _norm(rule) for k in hit_map):
            miss += 1
            print(f"  [miss] 行{i}: 键=({entity[:12]}.., {booked[:12]}.., {rule[:12]}.., {round(_num(amount),2)})")
            continue

        cust_rows = pick_rows_for_customer(header, src_rows, booked, args.max_rows)
        if not cust_rows:
            miss += 1
            print(f"  [miss] 行{i}: 源表无该账载客户行 {booked[:20]}")
            continue
        if not srcs:
            # 归属/主体切换类按账载客户命中的规则无行级定位：高亮该客户目标月全部行。
            mcol = header.index("月") if "月" in header else 1
            srcs = {r["_src_row"] for r in cust_rows
                    if int(_num(r["cells"][mcol])) == int(args.month)}

        # 截图列：归属类只看基础列；金额类追加金额列
        cols = list(BASE_COLS)
        if not (rule in HIGHLIGHT_COL_RULES):
            cols += [c for c in MONEY_COLS if c in header]
        hit_col = "实际客户" if rule in HIGHLIGHT_COL_RULES else None

        fname = f"row{i:03d}_{re.sub(r'[\\\\/:*?\"<>|（）() ]', '_', booked)[:24]}_{rule[:14]}.png"
        out_png = out_dir / fname
        render_table_png(header, cust_rows, cols, set(srcs), hit_col, out_png)

        if not args.dry_run:
            img = XLImage(str(out_png))
            # 限制显示宽度，避免遮挡相邻单元格（附件列宽约 10 字符）
            scale = min(1.0, 480 / img.width)
            img.width = int(img.width * scale)
            img.height = int(img.height * scale)
            ws.add_image(img, f"F{i}")
        ok += 1

    print(f"生成截图 {ok}，未匹配源行 {miss}")
    if not args.dry_run:
        wb.save(reg_path)
        print(f"已写回 {reg_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

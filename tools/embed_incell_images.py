# -*- coding: utf-8 -*-
"""把附件截图以"嵌入单元格"（richData / Place-in-Cell）方式注入登记表 xlsx。

原理：Excel 365/WPS 的"嵌入单元格图片"用 rich value 存储链——
  单元格 <c t="e" vm="N"> → metadata.xml → richData(rdrichvalue/structure/rel) → media 图片。
本脚本对 openpyxl 保存好的登记表做 zip 层手术：
  1. 注入/合并 xl/metadata.xml、xl/richData/*、xl/media/imageN.png；
  2. 改"异常登记表" sheet XML：附件列单元格替换为 t="e" vm=... 引用；
  3. 加大明细行行高与附件列列宽，使嵌入图有可读尺寸；
  4. 删除 drawing 中旧的浮动截图锚点（保留原有历史图片）；
  5. 更新 [Content_Types].xml 与 workbook.xml.rels。

用法：
    python tools/embed_incell_images.py --reg 登记表.xlsx --png-dir 附件截图 \
        --rows 48-218 [--backup]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RT_METADATA = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata"
RT_RVR = "http://schemas.microsoft.com/office/2022/10/relationships/richValueRel"
RT_RDRICHVALUE = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValue"
RT_RDRICHVALUESTRUCTURE = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueStructure"
RT_RDRICHVALUETYPES = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueTypes"

RICH_TYPES = {  # 部件名 -> ContentType Override
    "/xl/metadata.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml",
    "/xl/richData/rdRichValueTypes.xml": "application/vnd.ms-excel.rdrichvaluetypes+xml",
    "/xl/richData/rdrichvalue.xml": "application/vnd.ms-excel.rdrichvalue+xml",
    "/xl/richData/rdrichvaluestructure.xml": "application/vnd.ms-excel.rdrichvaluestructure+xml",
    "/xl/richData/richValueRel.xml": "application/vnd.ms-excel.richvaluerel+xml",
}


def find_sheet_target(zf: zipfile.ZipFile, sheet_name: str) -> str:
    wb = zf.read("xl/workbook.xml").decode("utf-8")
    m = re.search(rf'<sheet[^>]*name="{sheet_name}"[^>]*r:id="(rId\d+)"', wb)
    if not m:
        m = re.search(rf'<sheet[^>]*r:id="(rId\d+)"[^>]*name="{sheet_name}"', wb)
    rid = m.group(1)
    rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8")
    m2 = re.search(rf'<Relationship[^>]*Id="{rid}"[^>]*Target="([^"]+)"', rels)
    if not m2:
        m2 = re.search(rf'<Relationship[^>]*Target="([^"]+)"[^>]*Id="{rid}"', rels)
    target = m2.group(1).lstrip("/")
    if not target.startswith("xl/"):
        target = "xl/" + target
    return target


def parse_rows(spec: str) -> list[int]:
    lo, hi = spec.split("-")
    return list(range(int(lo), int(hi) + 1))


def build_rich_parts(images: list[Path]) -> tuple[dict[str, bytes], list[str], list[str]]:
    """构造 richData 部件。返回 (parts, content_type_overrides, workbook_rels_entries)。"""
    n = len(images)
    media_names = []
    rvb_blocks = []
    vm_blocks = []
    rv_blocks = []
    rel_blocks = []
    rel_rels = []
    for k in range(n):
        media_names.append(f"image{204 + k}.png")  # 登记表 media 已占用 image1..203
        rvb_blocks.append(f'<xlrd:rvb i="{k}"/>')
        vm_blocks.append(f'<bk><rc t="1" v="{k}"/></bk>')
        rv_blocks.append('<rv s="0"><v>0</v><v>5</v></rv>')
        rel_blocks.append(f'<rel r:id="rId{k + 1}"/>')
        rel_rels.append(
            f'<Relationship Id="rId{k + 1}" Type="{NS_R}/image" '
            f'Target="../media/{media_names[k]}"/>')

    metadata = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">'
        '<metadataTypes count="1"><metadataType name="XLRICHVALUE" minSupportedVersion="120000" '
        'copy="1" pasteAll="1" pasteValues="1" merge="1" splitFirst="1" rowColShift="1" '
        'clearFormats="1" clearComments="1" assign="1" coerce="1"/></metadataTypes>'
        f'<futureMetadata name="XLRICHVALUE" count="{n}">'
        + '<bk><extLst>'
        + ''.join('<ext uri="{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}">' + r + '</ext>' for r in rvb_blocks)
        + '</extLst></bk></futureMetadata>'
        f'<valueMetadata count="{n}">{"".join(vm_blocks)}</valueMetadata></metadata>')

    rdrichvalue = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<rvData xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" '
        f'count="{n}">{"".join(rv_blocks)}</rvData>')

    rdrichvaluestructure = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<rvStructures xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata" '
        'count="1"><s t="_localImage"><k n="_rvRel:LocalImageIdentifier" t="i"/>'
        '<k n="CalcOrigin" t="i"/></s></rvStructures>')

    richValueRel = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<richValueRels xmlns="http://schemas.microsoft.com/office/2022/richvaluerel" '
        f'xmlns:r="{NS_R}">{"".join(rel_blocks)}</richValueRels>')

    richValueRel_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(rel_rels)}</Relationships>')

    rdrichvaluetypes = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<rvTypesInfo xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
        'mc:Ignorable="x" xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<global><keyFlags><key name="_Self"><flag name="ExcludeFromFile" value="1"/>'
        '<flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_DisplayString"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_Flags"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_Format"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_SubLabel"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_Attribution"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_Icon"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_Display"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_CanonicalPropertyNames"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '<key name="_ClassificationId"><flag name="ExcludeFromCalcComparison" value="1"/></key>'
        '</keyFlags></global></rvTypesInfo>')

    parts = {
        "xl/metadata.xml": metadata.encode("utf-8"),
        "xl/richData/rdrichvalue.xml": rdrichvalue.encode("utf-8"),
        "xl/richData/rdrichvaluestructure.xml": rdrichvaluestructure.encode("utf-8"),
        "xl/richData/richValueRel.xml": richValueRel.encode("utf-8"),
        "xl/richData/_rels/richValueRel.xml.rels": richValueRel_rels.encode("utf-8"),
        "xl/richData/rdRichValueTypes.xml": rdrichvaluetypes.encode("utf-8"),
    }
    for k, img in enumerate(images):
        parts[f"xl/media/{media_names[k]}"] = Path(img).read_bytes()

    overrides = list(RICH_TYPES.items())
    rels_entries = [
        (RT_METADATA, "metadata.xml"),
        (RT_RVR, "richData/richValueRel.xml"),
        (RT_RDRICHVALUE, "richData/rdrichvalue.xml"),
        (RT_RDRICHVALUESTRUCTURE, "richData/rdrichvaluestructure.xml"),
        (RT_RDRICHVALUETYPES, "richData/rdRichValueTypes.xml"),
    ]
    return parts, overrides, rels_entries


def patch_sheet_xml(xml: str, row_to_vm: dict[int, int], col_letter: str = "F",
                    row_h: int = 60, col_width: float = 60.0) -> str:
    # 1) 附件单元格 -> 嵌入图片引用（移除原有 t 属性，避免重复属性产生非法 XML）
    col_idx = ord(col_letter) - 64
    for row, vm in row_to_vm.items():
        ref = f"{col_letter}{row}"
        pat = re.compile(r'<c r="' + ref + r'"([^/>]*?)(/?)>')
        m = pat.search(xml)
        if m:
            attrs = re.sub(r'\s*t="[^"]*"', "", m.group(1))
            repl = f'<c r="{ref}"{attrs} t="e" vm="{vm}"><v>#VALUE!</v></c>'
            xml = xml[:m.start()] + repl + xml[m.end():]
        else:
            row_pat = re.compile(r'(<row r="' + str(row) + r'"[^>]*>)(.*?)(</row>)', re.S)
            xml = row_pat.sub(
                lambda mm: mm.group(1) + mm.group(2) +
                           f'<c r="{ref}" t="e" vm="{vm}"><v>#VALUE!</v></c>' + mm.group(3),
                xml, count=1)

    # 2) 明细行行高（覆盖已有 ht）
    for row, vm in row_to_vm.items():
        pat1 = re.compile(r'(<row r="' + str(row) + r'" )ht="[^"]*" customHeight="1"')
        if pat1.search(xml):
            xml = pat1.sub(r'\1ht="' + str(row_h) + r'" customHeight="1"', xml, count=1)
            continue
        pat2 = re.compile(r'<row r="' + str(row) + r'"(?!/)( )')
        if pat2.search(xml):
            xml = pat2.sub(r'<row r="' + str(row) + r'" ht="' + str(row_h) + r'" customHeight="1" ', xml, count=1)
    # 3) 附件列列宽（替换或追加 col 定义）
    col_pat = re.compile(r'<col min="' + str(col_idx) + r'" max="' + str(col_idx) + r'"[^/]*/>')
    col_def = f'<col min="{col_idx}" max="{col_idx}" width="{col_width}" customWidth="1"/>'
    if col_pat.search(xml):
        xml = col_pat.sub(col_def, xml, count=1)
    elif "<cols>" in xml:
        xml = xml.replace("<cols>", "<cols>" + col_def, 1)
    else:
        xml = xml.replace("<sheetData>", "<cols>" + col_def + "</cols><sheetData>", 1)
    return xml


def strip_floating_anchors(drawing_xml: str, keep_rows_below: int) -> str:
    """删除 from.row >= keep_rows_below 的浮动图锚点（0-based）。"""
    pat = re.compile(r'<(?:xdr:)?oneCellAnchor>.*?</(?:xdr:)?oneCellAnchor>|<(?:xdr:)?twoCellAnchor>.*?</(?:xdr:)?twoCellAnchor>', re.S)

    def repl(m):
        block = m.group(0)
        rm = re.search(r'<(?:xdr:)?from>\s*<(?:xdr:)?col>\d+</(?:xdr:)?col>\s*<(?:xdr:)?colOff>\d+</(?:xdr:)?colOff>\s*<(?:xdr:)?row>(\d+)</(?:xdr:)?row>', block)
        if rm and int(rm.group(1)) >= keep_rows_below:
            return ""
        return block

    return pat.sub(repl, drawing_xml)


def main() -> int:
    """把登记表中全部浮动图片（原有 + 本轮工具截图）统一转为嵌入单元格图片。

    关键约束：WPS/Excel 对含 drawing（浮动图）的工作簿不渲染 richData 嵌图，
    因此必须删除全部浮动图并转为嵌入方式。
    """
    import _incell_rich

    ap = argparse.ArgumentParser(description="把登记表浮动图片统一转为嵌入单元格图片（richData）")
    ap.add_argument("--reg", required=True)
    ap.add_argument("--month", type=int, default=8)
    ap.add_argument("--row-height", type=int, default=60)
    ap.add_argument("--detail-col-width", type=float, default=60.0, help="附件列宽（新截图行）")
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--floating-source", default=None,
                    help="浮动图提取源 xlsx（默认 --reg 自身；注入过一次后 drawing rels 被删，"
                         "应改用注入前的备份文件作为提取源）")
    args = ap.parse_args()

    reg = Path(args.reg)
    if args.backup:
        bak = reg.with_name(reg.stem + ".before-incell.xlsx")
        shutil.copy2(reg, bak)
        print(f"备份: {bak}")

    # 1) 提取全部浮动图
    float_src = Path(args.floating_source) if args.floating_source else reg
    items = _incell_rich.extract_floating_images(float_src)
    detail_rows = [it["row"] for it in items if it["row"] >= 48]
    print(f"浮动图共 {len(items)} 张（本轮工具明细 {len(detail_rows)} 张 + 历史 {len(items) - len(detail_rows)} 张）")
    if not items:
        return 0

    # 2) 构造 richData 部件（media 从 image204 起编号，避开 openpyxl 已用名）
    parts, overrides, rels_entries, cell_vm = _incell_rich.build_rich_parts(items, first_media_index=204)

    src_zip = zipfile.ZipFile(reg)
    sheet_target = find_sheet_target(src_zip, "异常登记表")
    out_entries: dict[str, bytes] = {n: src_zip.read(n) for n in src_zip.namelist()}
    src_zip.close()

    # 3) 删除 drawing 部件（浮动图全部转为嵌图）与 sheet 的 drawing 引用
    removed = [n for n in list(out_entries) if re.match(r"xl/drawings/", n)]
    for n in removed:
        del out_entries[n]
    for n in list(out_entries):
        if re.match(r"xl/worksheets/_rels/sheet\d+\.xml\.rels$", n):
            rx = out_entries[n].decode("utf-8")
            rx = re.sub(r'<Relationship[^>]*relationships/drawing[^>]*/>', "", rx)
            out_entries[n] = rx.encode("utf-8")
    print(f"drawing 部件删除: {len(removed)}")

    # 4) sheet XML：单元格 vm 引用 + 明细行行高 + 附件列宽
    sheet_xml = out_entries[sheet_target].decode("utf-8")
    col_letter = "F"
    for (row, col), vm in sorted(cell_vm.items()):
        col_l = chr(ord("A") + col - 1)
        ref = f"{col_l}{row}"
        pat = re.compile(r"<c r=\"" + ref + r'"([^>]*?)(/?)>')
        m = pat.search(sheet_xml)
        if m:
            attrs = re.sub(r'\s*t="[^"]*"', "", m.group(1))
            new = f'<c r="{ref}"{attrs} t="e" vm="{vm}"><v>#VALUE!</v></c>'
            sheet_xml = sheet_xml[:m.start()] + new + sheet_xml[m.end():]
        else:
            row_pat = re.compile(r'(<row r="' + str(row) + r'"[^>]*>)(.*?)(</row>)', re.S)
            sheet_xml = row_pat.sub(
                lambda mm: mm.group(1) + mm.group(2) + f'<c r="{ref}" t="e" vm="{vm}"><v>#VALUE!</v></c>' + mm.group(3),
                sheet_xml, count=1)
    for row in detail_rows:
        pat = re.compile(r'(<row r="' + str(row) + r'"[^>]*?)((?:ht="[^"]*")?[^>]*?)(/?>)')
        m = pat.search(sheet_xml)
        if m:
            attrs = re.sub(r'\s*(?:ht|customHeight)="[^"]*"', "", m.group(1) + m.group(2))
            sheet_xml = sheet_xml[:m.start()] + f'<row r="{row}"{attrs} ht="{args.row_height}" customHeight="1">' + sheet_xml[m.end():]
    # 删除 sheet XML 里的悬空 drawing 引用（rels 关系已删，悬空引用会导致 WPS 不渲染该 sheet 的嵌图）
    sheet_xml = re.sub(r"<drawing[^>]*/>", "", sheet_xml)
    out_entries[sheet_target] = sheet_xml.encode("utf-8")

    # 5) workbook.xml.rels 追加关系
    rels_path = "xl/_rels/workbook.xml.rels"
    rels_xml = out_entries[rels_path].decode("utf-8")
    max_rid = max(int(m) for m in re.findall(r'Id="rId(\d+)"', rels_xml))
    add_rels = []
    for idx, (rtype, target) in enumerate(rels_entries):
        add_rels.append(f'<Relationship Id="rId{max_rid + 1 + idx}" Type="{rtype}" Target="{target}"/>')
    rels_xml = rels_xml.replace("</Relationships>", "".join(add_rels) + "</Relationships>")
    out_entries[rels_path] = rels_xml.encode("utf-8")

    # 6) [Content_Types].xml
    ct_path = "[Content_Types].xml"
    ct_xml = out_entries[ct_path].decode("utf-8")
    add_ct = []
    for part, ctype in overrides:
        if f'PartName="{part}"' not in ct_xml:
            add_ct.append(f'<Override PartName="{part}" ContentType="{ctype}"/>')
    if 'Extension="png"' not in ct_xml:
        add_ct.insert(0, '<Default Extension="png" ContentType="image/png"/>')
    ct_xml = ct_xml.replace("</Types>", "".join(add_ct) + "</Types>")
    out_entries[ct_path] = ct_xml.encode("utf-8")

    # 7) 删除旧孤儿 media（drawing 已删除，原 imageN 不再被引用；新 media 从 image1 重新编号）
    used_media = {n for n in parts if n.startswith("xl/media/")}
    for n in list(out_entries):
        if re.match(r"xl/media/image\d+\.png$", n) and n not in used_media:
            del out_entries[n]

    # 8) 写出
    out_entries.update(parts)
    tmp = reg.with_suffix(".tmp.xlsx")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for n, d in out_entries.items():
            zout.writestr(n, d)
    tmp.replace(reg)
    print(f"完成：{len(cell_vm)} 张图以嵌入单元格方式写入 {reg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

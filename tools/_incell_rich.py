# -*- coding: utf-8 -*-
"""richData（嵌入单元格图片）部件构造与浮动图提取工具。

被 tools/embed_incell_images.py 使用。
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
RT_METADATA = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sheetMetadata"
RT_RVR = "http://schemas.microsoft.com/office/2022/10/relationships/richValueRel"
RT_RDRICHVALUE = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValue"
RT_RDRICHVALUESTRUCTURE = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueStructure"
RT_RDRICHVALUETYPES = "http://schemas.microsoft.com/office/2017/06/relationships/rdRichValueTypes"

RICH_TYPES = {
    "/xl/metadata.xml": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheetMetadata+xml",
    "/xl/richData/rdRichValueTypes.xml": "application/vnd.ms-excel.rdrichvaluetypes+xml",
    "/xl/richData/rdrichvalue.xml": "application/vnd.ms-excel.rdrichvalue+xml",
    "/xl/richData/rdrichvaluestructure.xml": "application/vnd.ms-excel.rdrichvaluestructure+xml",
    "/xl/richData/richValueRel.xml": "application/vnd.ms-excel.richvaluerel+xml",
}

_RICHDATA_TYPES_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<rvTypesInfo xmlns="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'mc:Ignorable="x" xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    '<global><keyFlags>'
    '<key name="_Self"><flag name="ExcludeFromFile" value="1"/><flag name="ExcludeFromCalcComparison" value="1"/></key>'
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


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def extract_floating_images(reg_path: Path) -> list[dict]:
    """从登记表 zip 提取全部浮动图片锚点。

    返回 [{row(1-based), col(1-based), png(bytes)}]，同格多图垂直拼接为一张。
    """
    from PIL import Image
    zf = zipfile.ZipFile(reg_path)
    drawing_parts = [n for n in zf.namelist() if re.match(r"xl/drawings/drawing\d+\.xml$", n)]
    flat: list[dict] = []
    for dpart in drawing_parts:
        dxml = zf.read(dpart).decode("utf-8")
        rels_part = dpart.replace("xl/drawings/", "xl/drawings/_rels/") + ".rels"
        rid_to_target: dict[str, str] = {}
        if rels_part in zf.namelist():
            rels_xml = zf.read(rels_part).decode("utf-8")
            for rel in re.findall(r"<Relationship[^>]*/>", rels_xml):
                rid_m = re.search(r'Id="(rId\d+)"', rel)
                tgt_m = re.search(r'Target="([^"]+)"', rel)
                if rid_m and tgt_m:
                    tgt = tgt_m.group(1)
                    # openpyxl 可能写包内绝对路径 /xl/media/...；WPS/Excel 写相对 ../media/...
                    tgt = tgt.lstrip("/")
                    if not tgt.startswith("xl/"):
                        tgt = "xl/" + tgt
                    rid_to_target[rid_m.group(1)] = tgt
        for m in re.finditer(
                r"<oneCellAnchor>.*?</oneCellAnchor>|<twoCellAnchor>.*?</twoCellAnchor>", dxml, re.S):
            blk = m.group(0)
            fr = re.search(
                r"<from>\s*<col>(\d+)</col>\s*<colOff>\d+</colOff>\s*<row>(\d+)</row>", blk)
            rid = re.search(r'r:embed="(rId\d+)"', blk)
            if not fr or not rid or rid.group(1) not in rid_to_target:
                continue
            col, row = int(fr.group(1)) + 1, int(fr.group(2)) + 1
            target = rid_to_target[rid.group(1)].replace("../", "xl/")
            png = zf.read(target)
            flat.append({"row": row, "col": col, "png": png})
    zf.close()

    # 同格多图垂直拼接
    merged: dict[tuple[int, int], list[bytes]] = {}
    order: list[tuple[int, int]] = []
    for it in flat:
        key = (it["row"], it["col"])
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].append(it["png"])

    result = []
    for key in order:
        blobs = merged[key]
        if len(blobs) == 1:
            png = blobs[0]
        else:
            imgs = [Image.open(io.BytesIO(b)).convert("RGB") for b in blobs]
            w = max(im.width for im in imgs)
            h = sum(im.height for im in imgs) + 6 * (len(imgs) - 1)
            canvas = Image.new("RGB", (w, h), (255, 255, 255))
            y = 0
            for im in imgs:
                canvas.paste(im, (0, y))
                y += im.height + 6
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            png = buf.getvalue()
        result.append({"row": key[0], "col": key[1], "png": png})
    return result


def build_rich_parts(items: list[dict], first_media_index: int = 204) -> tuple:
    """items: [{row, col, png(bytes)}] -> (parts, overrides, rels_entries, cell_vm)。

    cell_vm: {(row, col) -> vm 索引(1-based)}。
    """
    n = len(items)
    rvb_blocks, vm_blocks, rv_blocks, rel_blocks, rel_rels = [], [], [], [], []
    media_parts: dict[str, bytes] = {}
    cell_vm: dict[tuple[int, int], int] = {}
    for k, it in enumerate(items):
        media_name = f"image{first_media_index + k}.png"
        media_parts[f"xl/media/{media_name}"] = it["png"]
        rvb_blocks.append(f'<xlrd:rvb i="{k}"/>')
        vm_blocks.append(f'<bk><rc t="1" v="{k}"/></bk>')
        rv_blocks.append('<rv s="0"><v>0</v><v>5</v></rv>')
        rel_blocks.append(f'<rel r:id="rId{k + 1}"/>')
        rel_rels.append(
            f'<Relationship Id="rId{k + 1}" Type="{NS_R}/image" Target="../media/{media_name}"/>')
        cell_vm[(it["row"], it["col"])] = k + 1

    metadata = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<metadata xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:xlrd="http://schemas.microsoft.com/office/spreadsheetml/2017/richdata">'
        '<metadataTypes count="1"><metadataType name="XLRICHVALUE" minSupportedVersion="120000" '
        'copy="1" pasteAll="1" pasteValues="1" merge="1" splitFirst="1" rowColShift="1" '
        'clearFormats="1" clearComments="1" assign="1" coerce="1"/></metadataTypes>'
        f'<futureMetadata name="XLRICHVALUE" count="{n}">'
        + "".join(f'<bk><extLst><ext uri="{{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}}">'
                  f'{r}</ext></extLst></bk>' for r in rvb_blocks)
        + f'</futureMetadata><valueMetadata count="{n}">{"".join(vm_blocks)}</valueMetadata></metadata>')
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
        '<richValueRels xmlns="http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel" '
        f'xmlns:r="{NS_R}">{"".join(rel_blocks)}</richValueRels>')
    richValueRel_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'{"".join(rel_rels)}</Relationships>')

    parts = {
        "xl/metadata.xml": metadata.encode("utf-8"),
        "xl/richData/rdrichvalue.xml": rdrichvalue.encode("utf-8"),
        "xl/richData/rdrichvaluestructure.xml": rdrichvaluestructure.encode("utf-8"),
        "xl/richData/richValueRel.xml": richValueRel.encode("utf-8"),
        "xl/richData/_rels/richValueRel.xml.rels": richValueRel_rels.encode("utf-8"),
        "xl/richData/rdRichValueTypes.xml": _RICHDATA_TYPES_XML.encode("utf-8"),
    }
    parts.update(media_parts)
    overrides = list(RICH_TYPES.items())
    rels_entries = [
        (RT_METADATA, "metadata.xml"),
        (RT_RVR, "richData/richValueRel.xml"),
        (RT_RDRICHVALUE, "richData/rdrichvalue.xml"),
        (RT_RDRICHVALUESTRUCTURE, "richData/rdrichvaluestructure.xml"),
        (RT_RDRICHVALUETYPES, "richData/rdRichValueTypes.xml"),
    ]
    return parts, overrides, rels_entries, cell_vm

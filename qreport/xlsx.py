"""把「管线号 / 焊口号」写成一个 .xlsx。

项目里没有 openpyxl，也**不想**为了两列数字塞一个依赖进来，所以这里手写最小 xlsx：
一个 sheet、两列、表头合并两行（和用户给的 ``示例.xlsx`` 一模一样），
中文用「宋体」，表头居中带细边框，列宽按示例来。
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

TAG_HEADER = "管线号"
WELD_HEADER = "焊口号"
PAGE_HEADER = "页码"
PERCENT_HEADER = "探伤比例"
HEADERS = (TAG_HEADER, WELD_HEADER, PAGE_HEADER, PERCENT_HEADER)
#: 前两列和 示例.xlsx 一致；页码列给够宽度（一格里可能放好几页），比例列窄一点
COLUMN_WIDTHS = (27.78, 25.89, 22.0, 13.0)

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>
"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>
"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>
"""

# cellXfs[0]：正文（宋体 12）；cellXfs[1]：表头（宋体 11、居中、细边框、自动换行）
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2">
<font><sz val="12"/><name val="宋体"/><charset val="134"/></font>
<font><sz val="11"/><name val="宋体"/><charset val="134"/></font>
</fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"/><right style="thin"/><top style="thin"/><bottom style="thin"/><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">
<alignment horizontal="center" vertical="center" wrapText="1"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="常规" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>
"""

_CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:creator>QReport</dc:creator>
</cp:coreProperties>
"""

_APP = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">
<Application>QReport</Application>
</Properties>
"""


def _col_letter(index: int) -> str:
    """1 → A，26 → Z，27 → AA。"""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _inline_str(ref: str, style: int, text: str) -> str:
    return (
        f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">'
        f"{escape(text)}</t></is></c>"
    )


def _merge_cells(count: int) -> str:
    cells = "".join(
        f'<mergeCell ref="{_col_letter(index)}1:{_col_letter(index)}2"/>'
        for index in range(1, count + 1)
    )
    return f'<mergeCells count="{count}">{cells}</mergeCells>'


def build_sheet_xml(
    rows: list[tuple[str, ...]],
    headers: tuple[str, ...] = HEADERS,
    widths: tuple[float, ...] | None = None,
) -> str:
    """通用工作表：表头合并两行（每列都合）+ 数据行。

    ``widths`` 不给就沿用焊口表那套列宽；材料表这类列多的自己传一组。
    """
    width = len(headers)
    column_widths = widths if widths is not None else COLUMN_WIDTHS
    last_col = _col_letter(width)
    parts: list[str] = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
        '<sheetPr><pageSetUpPr fitToPage="0"/></sheetPr>',
        f'<dimension ref="A1:{last_col}{len(rows) + 2}"/>',
        '<sheetViews><sheetView workbookViewId="0"/></sheetViews>',
        '<sheetFormatPr defaultRowHeight="15"/>',
        "<cols>",
    ]
    for index in range(width):
        width_value = column_widths[index] if index < len(column_widths) else 20.0
        parts.append(
            f'<col min="{index + 1}" max="{index + 1}" width="{width_value}" customWidth="1"/>'
        )
    parts.append("</cols>")
    parts.append("<sheetData>")
    parts.append('<row r="1" ht="15.6" customHeight="1">')
    for index, title in enumerate(headers, start=1):
        parts.append(_inline_str(f"{_col_letter(index)}1", 1, title))
    parts.append("</row>")
    parts.append('<row r="2" ht="15.6" customHeight="1">')
    for index in range(1, width + 1):
        parts.append(_inline_str(f"{_col_letter(index)}2", 1, ""))
    parts.append("</row>")

    for offset, row in enumerate(rows):
        line = offset + 3
        parts.append(f'<row r="{line}" ht="15.6" customHeight="1">')
        for index, text in enumerate(row, start=1):
            parts.append(_inline_str(f"{_col_letter(index)}{line}", 0, text))
        parts.append("</row>")

    parts.extend(
        [
            "</sheetData>",
            _merge_cells(width),
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>',
            "</worksheet>",
        ]
    )
    return "".join(parts)


def write_xlsx(path: str | Path, rows: list[tuple[str, ...]]) -> int:
    """写盘，返回数据行数。失败的异常交给调用方（导出失败要让人看见）。"""
    return write_table_xlsx(path, HEADERS, rows)


def write_table_xlsx(
    path: str | Path,
    headers: tuple[str, ...],
    rows: list[tuple[str, ...]],
    widths: tuple[float, ...] | None = None,
) -> int:
    """按任意表头写一个 sheet；表头仍合并两行，和既有导出样式一致。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".qreport-tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as book:
            book.writestr("[Content_Types].xml", _CONTENT_TYPES)
            book.writestr("_rels/.rels", _ROOT_RELS)
            book.writestr("xl/workbook.xml", _WORKBOOK)
            book.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
            book.writestr("xl/styles.xml", _STYLES)
            book.writestr(
                "xl/worksheets/sheet1.xml",
                build_sheet_xml(rows, headers=headers, widths=widths),
            )
            book.writestr("docProps/core.xml", _CORE)
            book.writestr("docProps/app.xml", _APP)
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return len(rows)


def weld_table_xlsx(path: str | Path, rows) -> int:
    """把 ``welds.WeldRow`` 列表直接写出去：管线号 / 焊口号 / 页码 / 探伤比例。"""
    return write_xlsx(
        path,
        [(row.tag, row.weld_no, row.pages_text, row.percent) for row in rows],
    )

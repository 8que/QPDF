"""CSV / Excel 表格读取 + 管线号匹配（数据匹配标注第 1、3 步）。

为什么不用 openpyxl / pandas：

- 打包时 ``QPDF.spec`` 的 EXCLUDES 里排掉了 numpy，pandas 进来就是一条死路；
- 为了读一张十几列的表拖进几十 MB 依赖，绿色包会明显变大，不划算。

所以 xlsx 自己读：它就是一个 zip，``xl/sharedStrings.xml`` 放字符串、
``xl/worksheets/sheetN.xml`` 放单元格、``xl/styles.xml`` 放数字格式，
标准库的 ``zipfile`` + ``xml.etree`` 够用。

匹配的三条规矩（都是用户点名要的）：

1. **匹配不到就是匹配不到**，绝不拿"最像的"顶上，只给候选让人挑；
2. **同一个键有多条记录 = 冲突**，必须由人指定用哪条；
3. OCR 修正过（``repair_tag`` 改过字）的编号，如果正好命中表里的键，
   算强证据，但要在预览里标出来，不能悄悄当普通匹配。
"""

from __future__ import annotations

import csv
import difflib
import hashlib
import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

from core import textscan

#: xlsx 里的主命名空间
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
#: workbook.xml 里 r:id 的命名空间
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: 单元格数字格式：百分比 / 日期（xlsx 内置格式号，没法从 formatCode 拿到的那些）
_PERCENT_FORMATS = {9, 10}
_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}

#: 匹配状态
MATCHED = "matched"        # 唯一命中
CONFLICT = "conflict"      # 命中多条，必须选
UNMATCHED = "unmatched"    # 表里没有
NOVALUE = "novalue"        # 命中了，但要写的字段是空的

#: 表里拿这些写法表示"没有"：当成空字段处理，别往图上写个 "/"
EMPTY_MARKS = ("/", "//", "-", "—", "–", "无", "n/a", "na", "none", "")

#: 看起来像管线号：有字母、有数字、带一段 '-'
_TAGISH = re.compile(r"[A-Za-z].*\d|\d.*[A-Za-z]")


class DataError(RuntimeError):
    """表格读不了 / 格式不对。"""


# --------------------------------------------------------------------- 文件指纹


def file_fingerprint(path: str) -> str:
    """导入版本用：大小 + 头尾各 64 KB 的 sha1（表文件一般都不大，够快）。"""
    try:
        size = os.path.getsize(path)
    except OSError:
        return ""
    digest = hashlib.sha1()
    digest.update(str(size).encode("ascii"))
    with open(path, "rb") as handle:
        digest.update(handle.read(65536))
        if size > 131072:
            handle.seek(-65536, os.SEEK_END)
            digest.update(handle.read(65536))
    return digest.hexdigest()


# --------------------------------------------------------------------- 表


@dataclass
class Table:
    """一张读进来的表：表头 + 每行每列都是字符串。"""

    path: str = ""
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)
    sheet: str = ""

    @property
    def column_count(self) -> int:
        return len(self.headers)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def name(self) -> str:
        return os.path.basename(self.path) if self.path else "（未导入）"

    def index_of(self, name: str) -> int:
        try:
            return self.headers.index(name)
        except ValueError:
            return -1

    def values_in(self, name: str) -> list[str]:
        index = self.index_of(name)
        if index < 0:
            return []
        return [row[index] if index < len(row) else "" for row in self.rows]

    def describe(self) -> str:
        sheet = f"「{self.sheet}」" if self.sheet else ""
        return f"{self.name}{sheet} · {self.row_count} 行 × {self.column_count} 列"


def read_table(path: str, sheet: str = "") -> Table:
    """按扩展名选读取器；``sheet`` 只对 xlsx 有效（空 = 第一张有数据的表）。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet)
    if ext == ".xls":
        raise DataError(
            "这是老版 .xls 文件，程序不装 Excel 组件读不了。\n"
            "用 Excel 打开 → 另存为 → 选「.xlsx」或「.csv」，再导进来。"
        )
    if ext in (".csv", ".txt", ".tsv", ".tab", ".dat"):
        return _read_csv(path)
    raise DataError(f"不认识这种文件：{ext or '（没有扩展名）'}\n支持 .csv / .xlsx")


# ---------------------------------------------------------------- CSV


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
    except csv.Error:
        head = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {sep: head.count(sep) for sep in (",", "\t", ";", "|")}
        best = max(counts, key=counts.get)
        return best if counts[best] else ","


def _read_csv(path: str) -> Table:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise DataError(f"打不开文件：{exc}") from exc
    text = _decode(raw)
    if not text.strip():
        raise DataError("文件是空的")
    delimiter = _sniff_delimiter(text[:8192])
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    grid = [[(cell or "").strip() for cell in row] for row in reader]
    grid = [row for row in grid if any(cell for cell in row)]
    if not grid:
        raise DataError("文件里没有数据行")
    headers = _clean_headers(grid[0])
    rows = [_pad(row, len(headers)) for row in grid[1:]]
    return _trim(Table(path=path, headers=headers, rows=rows, sheet="CSV"))


# ---------------------------------------------------------------- XLSX


def _col_index(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + (ord(ch) - 64)
    return max(0, value - 1)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out: list[str] = []
    for si in root.findall(_NS + "si"):
        # 富文本会被拆成多个 <r><t>；直接拼所有 <t> 就是完整字符串
        out.append(_text_of(si))
    return out


def _text_of(node) -> str:
    parts = [child.text or "" for child in node.iter(_NS + "t")]
    return "".join(parts)


def _number_formats(zf: zipfile.ZipFile) -> list[str]:
    """返回 ``cellXfs`` 每个样式对应的格式代码（按 xf 下标）。"""
    custom: dict[int, str] = {}
    xf_fmt: list[int] = []
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except KeyError:
        return []
    formats = root.find(_NS + "numFmts")
    if formats is not None:
        for item in formats.findall(_NS + "numFmt"):
            try:
                custom[int(item.get("numFmtId", "0"))] = item.get("formatCode", "")
            except (TypeError, ValueError):
                continue
    cell_xfs = root.find(_NS + "cellXfs")
    if cell_xfs is not None:
        for xf in cell_xfs.findall(_NS + "xf"):
            try:
                xf_fmt.append(int(xf.get("numFmtId", "0")))
            except (TypeError, ValueError):
                xf_fmt.append(0)
    out: list[str] = []
    for fmt_id in xf_fmt:
        if fmt_id in custom:
            out.append(custom[fmt_id])
        elif fmt_id in _PERCENT_FORMATS:
            out.append("0%" if fmt_id == 9 else "0.00%")
        elif fmt_id in _DATE_FORMATS:
            out.append("date")
        else:
            out.append("")
    return out


def _format_cell(text: str, fmt: str) -> str:
    if not text:
        return ""
    try:
        value = float(text)
    except ValueError:
        return text
    if fmt == "date":
        return _excel_date(value)
    if "%" in fmt:
        decimals = _decimals_of(fmt.split("%", 1)[0])
        return f"{value * 100:.{decimals}f}%"
    if fmt and re.search(r"[0#](?:\.[0#]+)?", fmt) and "/" in fmt:
        return text  # 分数、货币之类不猜，原样给
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.10g}"


def _decimals_of(pattern: str) -> int:
    if "." not in pattern:
        return 0
    tail = pattern.split(".", 1)[1]
    return sum(1 for ch in tail if ch in "0#")


def _excel_date(value: float) -> str:
    # Excel 的 1900 年基准有著名的闰年 bug，1900-03-01 之后用 1899-12-30 才准
    moment = datetime(1899, 12, 30) + timedelta(days=value)
    if abs(value - round(value)) < 1e-9:
        return moment.strftime("%Y-%m-%d")
    return moment.strftime("%Y-%m-%d %H:%M")


def _sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        raise DataError("这个 xlsx 里没有工作表信息，可能文件损坏了") from None
    rid_map = {rel.get("Id"): rel.get("Target", "") for rel in rels}
    out: list[tuple[str, str]] = []
    for sheet in workbook.iter(_NS + "sheet"):
        target = rid_map.get(sheet.get(_REL_NS + "id"), "")
        if not target:
            continue
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target.lstrip("./")
        out.append((sheet.get("name") or f"Sheet{len(out) + 1}", path))
    return out


def _looks_like_key_value(text: str) -> bool:
    """像不像一个"编号"（不是表头文字）。

    ``AC-E690101`` / ``PA-1023`` 算；``管道号`` / ``PIPING NO.`` 不算。
    要求：有字母、有数字、没有中文。真实的管线号几乎都满足这条。
    """
    text = (text or "").strip()
    if len(text) < 4:
        return False
    if any(ord(ch) > 0x2E80 for ch in text):
        return False
    return any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text)


def _find_key_column(rows: list[list[str]], width: int) -> int | None:
    """先粗猜哪一列是编号列：前面几百行里"像编号"的格子最多的那列。"""
    best_col: int | None = None
    best_hits = 0
    for col in range(width):
        hits = sum(1 for row in rows[:300] if col < len(row) and _looks_like_key_value(row[col]))
        if hits > best_hits:
            best_hits, best_col = hits, col
    # 至少两行像编号才敢认，否则交给下面的兜底
    return best_col if best_hits >= 2 else None


def _split_header(grid: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """把网格拆成「表头 + 数据」。

    真实工程表的花样比测试表多得多：标题横幅占一行、表头跨两三层
    （「管道号」在第一行、「公称通径 DN」在第二行），还有整列合并单元格。
    做法：先用"像不像编号"找到第一行数据，它上面的全算表头，
    每一列把表头的几层文字竖着拼起来（``焊缝 检测 比例 TESTING REQ.``）。
    """
    rows = [row for row in grid if any(value.strip() for value in row)]
    if not rows:
        raise _EmptySheet("")
    width = max(len(row) for row in rows)

    data_start: int | None = None
    key_col = _find_key_column(rows, width)
    if key_col is not None:
        for index, row in enumerate(rows):
            value = row[key_col] if key_col < len(row) else ""
            if _looks_like_key_value(value):
                data_start = index
                break

    if data_start is None:
        # 兜底：没有看起来像编号的列（比如纯数字编号表），就按"第一行填满的行"切
        counts = [sum(1 for value in row if value.strip()) for row in rows]
        widest = max(counts[:40] or [0])
        if widest <= 0:
            raise _EmptySheet("")
        threshold = max(3, int(widest * 0.6))
        data_start = next((index for index, count in enumerate(counts) if count >= threshold), 0)
    if data_start <= 0:
        data_start = 1
    header_rows = rows[:data_start]

    headers: list[str] = []
    for col in range(width):
        parts: list[str] = []
        for row in header_rows:
            text = (row[col] if col < len(row) else "").replace("\n", "").strip()
            if text and (not parts or parts[-1] != text):
                parts.append(text)
        headers.append(" ".join(parts))
    return _clean_headers(headers), rows[data_start:]


class _EmptySheet(Exception):
    """内部用：这张表没数据，换下一张。"""


def _parse_sheet(
    zf: zipfile.ZipFile,
    path: str,
    target: tuple[str, str],
    shared: list[str],
    formats: dict[int, str],
    names: list[str],
) -> Table:
    name, member = target
    try:
        root = ET.fromstring(zf.read(member))
    except KeyError:
        raise DataError(f"找不到工作表「{name}」") from None

    cells: dict[tuple[int, int], str] = {}
    max_row = 0
    max_col = 0
    for row in root.iter(_NS + "row"):
        try:
            row_index = int(row.get("r", "0")) - 1
        except ValueError:
            continue
        if row_index < 0:
            continue
        for cell in row.findall(_NS + "c"):
            ref = cell.get("r") or ""
            col_index = _col_index(ref)
            text = _cell_value(cell, shared, formats)
            if text == "":
                continue
            cells[(row_index, col_index)] = text
            max_row = max(max_row, row_index)
            max_col = max(max_col, col_index)

    if not cells:
        raise _EmptySheet(name)

    # 从第一行有字的地方开始当表头（有的表前面空几行，标题横幅之类）
    first_row = min(index[0] for index in cells)
    grid = [
        [cells.get((row_index, col), "") for col in range(max_col + 1)]
        for row_index in range(first_row, max_row + 1)
    ]

    try:
        headers, body = _split_header(grid)
    except _EmptySheet:
        raise _EmptySheet(name) from None
    rows = [_pad(row, len(headers)) for row in body]
    return _trim(Table(path=path, headers=headers, rows=rows, sheets=names, sheet=name))


def _read_xlsx(path: str, sheet: str = "") -> Table:
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise DataError(f"打不开这个 Excel 文件：{exc}") from exc
    with zf:
        targets = _sheet_targets(zf)
        if not targets:
            raise DataError("这个 xlsx 里没有工作表")
        names = [name for name, _ in targets]
        shared = _shared_strings(zf)
        formats = _number_formats(zf)
        if sheet:
            chosen = next((item for item in targets if item[0] == sheet), None)
            if chosen is None:
                raise DataError(f"这个文件里没有工作表「{sheet}」")
            try:
                return _parse_sheet(zf, path, chosen, shared, formats, names)
            except _EmptySheet:
                raise DataError(f"工作表「{sheet}」是空的") from None

        # 不指定就挑一张最像数据库的：真实表里第一张常是 Macro1 空表，
        # 第二张可能只是两行表头的模板——按「有管线号表头 + 行数多」打分。
        best_score: tuple[int, int] | None = None
        best_table: Table | None = None
        for target in targets:
            try:
                table = _parse_sheet(zf, path, target, shared, formats, names)
            except _EmptySheet:
                continue
            score = (1 if _has_key_hint(table) else 0, table.row_count)
            if best_score is None or score > best_score:
                best_score, best_table = score, table
        if best_table is None:
            raise DataError("这个 Excel 里所有工作表都是空的")
        return best_table


def _cell_value(cell, shared: list[str], formats: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        node = cell.find(_NS + "is")
        return _text_of(node) if node is not None else ""
    value_node = cell.find(_NS + "v")
    raw = "" if value_node is None or value_node.text is None else value_node.text
    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind in ("str", "b"):
        return "TRUE" if (kind == "b" and raw == "1") else raw
    if kind == "e":
        return ""
    try:
        style = int(cell.get("s", "0"))
    except (TypeError, ValueError):
        style = 0
    fmt = formats[style] if 0 <= style < len(formats) else ""
    return _format_cell(raw, fmt)


# ---------------------------------------------------------------- 收尾


def _clean_headers(row: list[str]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(row):
        name = (cell or "").strip().lstrip("\ufeff") or f"列{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name} ({seen[name]})"
        else:
            seen[name] = 0
        headers.append(name)
    return headers


def _pad(row: list[str], width: int) -> list[str]:
    if len(row) >= width:
        return list(row[:width])
    return list(row) + [""] * (width - len(row))


def _trim(table: Table) -> Table:
    """去掉尾部"整列都是空、表头还是自动生成"的列（Excel 常带一堆空列）。"""
    while table.headers:
        index = len(table.headers) - 1
        if not table.headers[index].startswith("列"):
            break
        if any(row[index] for row in table.rows if index < len(row)):
            break
        table.headers.pop()
        for row in table.rows:
            if index < len(row):
                row.pop(index)
    if not table.headers:
        raise DataError("表头是空的：第一行应该是列名")
    return table


# --------------------------------------------------------------------- 匹配


def normalize_key(text: str) -> str:
    """两边用同一把尺子：OCR 修正规则 + 全角/破折号归一 + 大写。

    不做"去掉数字里 O/0 混淆"那种模糊归一——那等于替用户猜，猜错了没人知道。
    """
    return textscan.normalize_tag(text or "").upper()


def _looks_like_tag_values(values: list[str]) -> int:
    score = 0
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        if "-" in text and _TAGISH.search(text):
            score += 2
        elif textscan.looks_like_tag(text):
            score += 1
    return score


#: 图上那串编号怎么变成查表用的键
KEY_WHOLE = "whole"    # 整串：100-BU-V110501-N1B-C50
KEY_MIDDLE = "middle"  # 中间两段：BU-V110501
KEY_AUTO = "auto"      # 先整串，整串查不到再取中间两段

#: 界面上的顺序和文字
KEY_RULES = (
    (KEY_AUTO, "自动（先整串，再中间两段）"),
    (KEY_WHOLE, "整串"),
    (KEY_MIDDLE, "中间两段"),
)


def extract_key(text: str, rule: str = KEY_WHOLE) -> str:
    """按规则从图上那串编号里取出查表用的键。

    工程图的管线号常写成「口径-单元-线号-材料等级-保温」
    （``100-BU-V110501-N1B-C50``），而管道特性表里记的是「单元-线号」
    （``BU-V110501``）。取中间两段就是给这种情况用的：仍然要求整键相等，
    不拿"看起来像"的去凑。
    """
    whole = normalize_key(text)
    if rule != KEY_MIDDLE:
        return whole
    parts = [part for part in whole.split("-") if part]
    if len(parts) < 3:
        return ""
    return f"{parts[1]}-{parts[2]}"


@dataclass
class MatchResult:
    """一个识别结果在表里的下场。"""

    status: str
    key: str = ""
    rows: list[dict] = field(default_factory=list)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    #: 这个键是怎么来的：``whole`` 整串 / ``middle`` 中间两段
    via: str = ""

    @property
    def ok(self) -> bool:
        return self.status in (MATCHED, CONFLICT)


class RecordTable:
    """按某个字段建索引的表格，负责"查得到 / 查不到 / 查到几条"。"""

    def __init__(
        self,
        table: Table,
        key_column: str,
        display_columns: list[str] | None = None,
        template: str = "",
        key_rule: str = KEY_WHOLE,
    ) -> None:
        self.table = table
        self.key_column = key_column
        self.display_columns = list(display_columns or [])
        self.template = template or self._default_template()
        self.key_rule = key_rule if key_rule in (KEY_WHOLE, KEY_MIDDLE, KEY_AUTO) else KEY_WHOLE
        self._index: dict[str, list[dict]] = {}
        self._rebuild()

    def _default_template(self) -> str:
        columns = self.display_columns
        if not columns and self.table.headers:
            # 没选内容字段时默认用匹配字段自己（先跑通流程，再谈多字段）
            columns = [self.key_column]
        return " ".join("{" + name + "}" for name in columns)

    @property
    def keys_of(self) -> list[str]:
        return list(self._index)

    @property
    def record_count(self) -> int:
        return sum(len(items) for items in self._index.values())

    def _rebuild(self) -> None:
        self._index.clear()
        index = self.table.index_of(self.key_column)
        if index < 0:
            return
        for row in self.table.rows:
            record = {
                name: (row[i] if i < len(row) else "")
                for i, name in enumerate(self.table.headers)
            }
            key = normalize_key(record.get(self.key_column, ""))
            if not key:
                continue
            self._index.setdefault(key, []).append(record)

    # -- 查询 -------------------------------------------------------------
    def all_records(self) -> list[dict]:
        """整张表按行变成字典（给"手动挑记录"的搜索列表用）。"""
        out: list[dict] = []
        for row in self.table.rows:
            out.append(
                {
                    name: (row[i] if i < len(row) else "")
                    for i, name in enumerate(self.table.headers)
                }
            )
        return out

    def lookup(self, tag: str) -> MatchResult:
        """按「编号取法」查表：整串查不到时看要不要退到中间两段。"""
        whole = normalize_key(tag)
        if not whole:
            return MatchResult(UNMATCHED, key="")
        if self.key_rule == KEY_MIDDLE:
            return self._lookup_key(extract_key(tag, KEY_MIDDLE), via=KEY_MIDDLE)
        result = self._lookup_key(whole)
        if result.ok or self.key_rule != KEY_AUTO:
            return result
        middle = extract_key(tag, KEY_MIDDLE)
        if not middle or middle == whole:
            return result
        alt = self._lookup_key(middle, via=KEY_MIDDLE)
        # 整串查不到、中间两段查到了，就用中间两段；两段也没查到就留整串的候选
        if alt.ok:
            return alt
        if alt.candidates and not result.candidates:
            return alt
        return result

    def _lookup_key(self, key: str, via: str = KEY_WHOLE) -> MatchResult:
        if not key:
            return MatchResult(UNMATCHED, key="", via=via)
        rows = self._index.get(key, [])
        if len(rows) == 1:
            return MatchResult(MATCHED, key=key, rows=rows, via=via)
        if len(rows) > 1:
            return MatchResult(CONFLICT, key=key, rows=rows, via=via)
        candidates = [
            (text, round(difflib.SequenceMatcher(None, key, text).ratio(), 3))
            for text in difflib.get_close_matches(key, list(self._index), n=5, cutoff=0.5)
        ]
        return MatchResult(UNMATCHED, key=key, candidates=candidates, via=via)

    def record_for_key(self, key: str) -> dict | None:
        rows = self._index.get(normalize_key(key), [])
        return rows[0] if rows else None

    def render(self, record: dict) -> str:
        """按模板把一条记录变成要写到图上的文字。"""
        text = self.template
        for name in self.table.headers:
            text = text.replace("{" + name + "}", (record.get(name) or "").strip())
        return " ".join(text.split())

    def content_for(self, record: dict) -> tuple[str, str]:
        """返回 ``(内容, 状态)``：字段空着就是 NOVALUE，不写字也不假装匹配成功。

        真表里"没有"常常写成 ``/``、``-``、``无``，这些一律当空的——
        用户可以在对话框里设「字段为空写」给它们一个统一的字。
        """
        text = self.render(record)
        if not text or text.strip().lower() in EMPTY_MARKS:
            return "", NOVALUE
        # 模板里写了固定前缀（比如"RT 10%"）也允许
        return text, MATCHED


# --------------------------------------------------------------------- 选列


_KEY_HINTS = ("管线号", "管号", "管线", "管道号", "pipe", "line no", "lineno", "tag", "编号", "位号")


def _has_key_hint(table: Table) -> bool:
    """表头里有没有明确写着"这是管线号"的列（不靠打分兜底）。"""
    return any(any(hint in name.lower() for hint in _KEY_HINTS) for name in table.headers)


def guess_key_column(table: Table) -> str:
    """猜哪一列是管线号：先看名字，再看长得像不像。"""
    if not table.headers:
        return ""
    for name in table.headers:
        low = name.lower()
        if any(hint in low for hint in _KEY_HINTS):
            return name
    scored = [(name, _looks_like_tag_values(table.values_in(name))) for name in table.headers]
    best = max(scored, key=lambda item: item[1])
    return best[0] if best[1] > 0 else table.headers[0]


def guess_display_columns(table: Table, key_column: str) -> list[str]:
    """猜要写进图里的列：探伤 / 比例 / RT 优先，否则取第一个非键列。"""
    # 按优先级一档一档找，不是"谁先出现算谁"——真表里「操作条件OPERATION」
    # 就含 "ratio"（opeRATIO n），按列顺序找会被它抢在前面。
    hints = ("探伤", "比例", "rt", "radiograph", "percent", "ratio", "检测", "验收", "备注")
    for hint in hints:
        for name in table.headers:
            if name == key_column:
                continue
            low = name.lower()
            if hint.isascii():
                # 英文词按整词比，免得 OPERATION 里蹦出个 ratio
                if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", low):
                    return [name]
            elif hint in low:
                return [name]
    for name in table.headers:
        if name != key_column:
            return [name]
    return []

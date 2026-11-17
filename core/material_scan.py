"""材料表 OCR：从图纸右上角的材料表里拆出「序号 / 材料描述 / 规格 / 数量」。

图纸里的材料表通常是一张**栅格图片**，不是矢量线，所以这里不猜测表格线，
只拿 ``textscan.ocr_page`` 的 line box：按 y 聚行、按 x 分列。
管线号来自 PDF 书签（``doc.get_toc``），页码就是 PDF 的 1-based 页码。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QThread, Signal

from core import textscan
from core.region import Region

#: 材料表字号小，6 倍已经够 Windows OCR 认；再大只是变慢
MATERIAL_ZOOM = 8.0

#: 表格列（顺序固定，界面和 Excel 都按这个来）；数量不导出
COLUMNS = ("管线号", "页码", "序号", "材质", "材料", "标准", "Φ规格", "DN")
COLUMN_WIDTHS = (26.0, 8.0, 7.0, 16.0, 14.0, 14.0, 16.0, 11.0)

_STANDARD_RE = re.compile(r"(?:GB|HG|JB|NB)/T\d+(?:\.\d+)?")
_ASTM_RE = re.compile(r"ASTM\s+[A-Z]\d+")
#: 国标牌号：S30408 是 GB/T14976 的不锈钢牌号，不能截成 304；20 是碳钢牌号
_GRADE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])("
    r"S\s*\d{5}"
    r"|20#?"
    r"|Q\d{3}[A-Z]?"
    r"|L\d{3}[A-Z]?"
    r"|16Mn"
    r"|304L?|316L?|321"
    r"|WCB|A105"
    r"|35CrMo/30CrMo"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_PHI_RE = re.compile(r"Φ\s*([^;,/]+)")
_DN_RE = re.compile(r"^\d+(?:x\d+)*$")
#: 只给了 Φ 外径、规格列又没认出来时的常用 DN 对照（GB/T 8163 系列）
_DN_BY_OD = {
    18: 15,
    25: 20,
    32: 25,
    38: 32,
    45: 40,
    57: 50,
    76: 65,
    89: 80,
    108: 100,
    133: 125,
    159: 150,
    219: 200,
    273: 250,
    325: 300,
}


@dataclass
class MaterialRow:
    """一行材料表；字段对应 Excel 的九列。"""

    pipeline: str = ""
    page: int = 0
    seq: int = 0
    category: str = ""
    grade: str = ""
    standard: str = ""
    phi: str = ""
    dn: str = ""
    qty: str = ""
    raw: str = ""

    def as_tuple(self) -> tuple[str, ...]:
        return (
            self.pipeline,
            str(self.page) if self.page else "",
            str(self.seq) if self.seq else "",
            self.category,
            self.grade,
            self.standard,
            self.phi,
            self.dn,
        )


def normalize_text(text: str) -> str:
    """收拾 OCR 的空格/全角符号，但保留标准号和等级之间的空格。

    ``GB/T8163 20`` 不能变成 ``GB/T816320``，所以这里不全局删空格，只删中文之间的、
    标点两侧的、以及斜杠两侧的空格。
    """
    s = (text or "").strip()
    s = s.replace("巾", "Φ").replace("φ", "Φ").replace("Φ", "Φ")
    s = s.replace("SpiraI", "Spiral").replace("FIexible", "Flexible")
    s = s.replace("0R", "OR")
    s = s.replace("ASTM M05", "ASTM A105")
    # 中文之间被 OCR 拆出来的空格
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    # 标点两侧
    s = re.sub(r"\s*([;；,，])\s*", lambda m: ";" if m.group(1) in ";；" else ",", s)
    s = re.sub(r"\s*/\s*", "/", s)
    # 数字之间被当成小数点的逗号/中点
    s = re.sub(r"(?<=\d)[,，·•](?=\d)", ".", s)
    s = re.sub(r"(?<=\d)\s*[·•]\s*(?=\d)", ".", s)
    s = s.replace("一 ll 系列", "-II系列").replace("一ll系列", "-II系列")
    s = s.replace("一 ll", "-II").replace("一ll", "-II")
    s = s.replace("一系列", "-II系列")
    s = re.sub(r"\bll\b", "II", s)
    # 表格里常见的 OCR 拆字/误认
    s = s.replace("二一通", "三通").replace("二 通", "三通")
    s = re.sub(r"(?<=\d)\s*[。．]\s*", "°", s)
    s = re.sub(r"°(?=[\u4e00-\u9fff])", "° ", s)
    # 「Z41Y 一 25C」里的连接号
    s = re.sub(r"(?<=[A-Za-z0-9])\s*一\s*(?=[A-Za-z0-9])", "-", s)
    # 「8 字盲板」「2 型」这类数字与汉字之间的空格
    s = re.sub(r"(?<=\d)\s+(?=[\u4e00-\u9fff])", "", s)
    # 中文括号内两侧的空格
    s = re.sub(r"\s*([（）()])\s*", r"\1", s)
    # Φ 和数字之间的空格、尺寸里的 X
    s = re.sub(r"Φ\s+", "Φ", s)
    s = re.sub(r"\s*[xX×*]\s*(?=\d)", "x", s)
    # 数量里的「9 · 3 米」
    s = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s*([;,])\s*", r"\1", s)
    return s


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _is_header_hit(hit: textscan.ScanHit) -> bool:
    text = _compact(hit.text)
    return "材料描述" in text or text in {"序号", "规格", "数量"}


def _split_columns(hits: list[textscan.ScanHit]) -> tuple[float, float, float] | None:
    """用 OCR 的 x0 聚列；返回 (序号右界, 规格左界, 数量左界)。

    数量列可以不框：只有「描述 + 规格」两列时，规格仍按右列处理，
    数量左界设成无穷大，后面没有命中就自然为空。
    """
    clusters: list[list[textscan.ScanHit]] = []
    for hit in sorted(hits, key=lambda h: h.box[0]):
        if not hit.text.strip() or _is_header_hit(hit):
            continue
        x = round(hit.box[0], 1)
        if not clusters or x - round(clusters[-1][-1].box[0], 1) > 8.0:
            clusters.append([hit])
        else:
            clusters[-1].append(hit)
    if len(clusters) < 2:
        return None
    groups = [
        (len(c), min(h.box[0] for h in c), max(h.box[2] for h in c))
        for c in clusters
    ]
    # 材料描述那一列横向最宽；序号/规格/数量都窄
    content_x = max(groups, key=lambda item: item[2] - item[1])[1]
    right = sorted(
        (item for item in groups if item[1] > content_x + 40.0),
        key=lambda item: item[1],
    )
    if right:
        spec_left = right[0][1] - 4.0
        qty_left = right[1][1] - 4.0 if len(right) > 1 else float("inf")
    else:
        spec_left = float("inf")
        qty_left = float("inf")
    left = [item[1] for item in groups if item[1] < content_x - 8.0]
    serial_right = left[-1] + 6.0 if left else content_x - 10.0
    return serial_right, spec_left, qty_left


def _group_rows(hits: list[textscan.ScanHit]) -> list[list[textscan.ScanHit]]:
    """按 y 中心聚行；同一行的序号/描述/规格/数量会落在一起。"""
    ordered = sorted(hits, key=lambda h: (h.box[1], h.box[0]))
    groups: list[list[textscan.ScanHit]] = []
    for hit in ordered:
        yc = (hit.box[1] + hit.box[3]) / 2.0
        if not groups:
            groups.append([hit])
            continue
        prev = groups[-1]
        prev_yc = sum((h.box[1] + h.box[3]) / 2.0 for h in prev) / len(prev)
        if yc - prev_yc <= 5.0:
            prev.append(hit)
        else:
            groups.append([hit])
    return groups


def _pick(group: list[textscan.ScanHit], bounds: tuple[float, float, float]):
    serial: list[str] = []
    content: list[str] = []
    spec: list[str] = []
    qty: list[str] = []
    serial_right, spec_left, qty_left = bounds
    for hit in sorted(group, key=lambda h: h.box[0]):
        text = hit.text.strip()
        if not text:
            continue
        x = hit.box[0]
        if x < serial_right:
            serial.append(text)
        elif x < spec_left:
            content.append(text)
        elif x < qty_left:
            spec.append(text)
        else:
            qty.append(text)
    return (
        normalize_text(" ".join(serial)),
        normalize_text(" ".join(content)),
        normalize_text(" ".join(spec)),
        normalize_text(" ".join(qty)),
    )


def _category(text: str) -> str:
    first = re.split(r"[;,]", text, maxsplit=1)[0].strip()
    # OCR 有时把「无缝钢管」和后面的 SMLS 拆成两个 hit，拼起来中间会多一个空格
    match = re.match(r"^(.+?)(?=[A-Za-z]{2,})", first)
    if match:
        first = match.group(1).strip()
    return first.strip(" ;,，。.·•：:")


def _fix_category(category: str, standard: str) -> str:
    """补 OCR 只剩一半的过滤器名称。"""
    if category == "T" and standard == "GB/T21385":
        return "T型过滤器"
    if category.endswith("型过滤器") and not category.startswith("Y"):
        return "Y" + category
    return category


def _standard(text: str) -> str:
    match = _STANDARD_RE.search(text)
    if match:
        return match.group(0).upper()
    match = _ASTM_RE.search(text)
    return match.group(0).upper().replace("  ", " ") if match else ""


def _canonical_grade(value: str) -> str:
    """把 OCR 常见的 S 30408 / 20# / 304l 统一成国标写法。"""
    compact = re.sub(r"\s+", "", value or "")
    if not compact:
        return ""
    if re.fullmatch(r"[sS]\d{5}", compact):
        return compact.upper()
    if compact.startswith("20"):
        return "20"
    if re.fullmatch(r"(?i)(304L?|316L?|321|WCB|A105)", compact):
        return compact.upper()
    return compact


def _grade(text: str) -> str:
    # 优先看标准号后面的牌号：GB/T14976 S30408、GB/T8163 20、
    # GB/T12459 S30408、ASTM A216 WCB 都走这里。
    for pattern in (_STANDARD_RE, _ASTM_RE):
        for match in pattern.finditer(text):
            tail = text[match.end() :]
            cut = tail.find(";")
            if cut >= 0:
                tail = tail[:cut]
            token = _GRADE_TOKEN_RE.search(tail)
            if token:
                return _canonical_grade(token.group(1))
    token = _GRADE_TOKEN_RE.search(text)
    return _canonical_grade(token.group(1)) if token else ""


def _phi(text: str) -> str:
    match = _PHI_RE.search(text)
    if not match:
        return ""
    value = match.group(1).strip()
    value = re.sub(r"(?<=\d)\s+(?=\d)", ".", value)
    value = re.sub(r"\s*[xX×*]\s*", "x", value)
    value = value.replace(",", ".").replace(" ", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?(?:x\d+(?:\.\d+)?)?", value):
        return ""
    return f"Φ{value}"


def _dn_from_phi(phi: str) -> str:
    match = re.match(r"Φ(\d+)", phi or "")
    if not match:
        return ""
    return str(_DN_BY_OD.get(int(match.group(1)), ""))


def _dn(spec: str) -> str:
    value = re.sub(r"\s+", "", spec or "").replace("×", "x").replace("*", "x").replace("X", "x")
    return value if _DN_RE.fullmatch(value) else ""


def rows_from_hits(
    hits: list[textscan.ScanHit],
    pipeline: str = "",
    page: int = 0,
) -> list[MaterialRow]:
    """把一页 OCR 命中拆成材料表行；不是材料表就返回空。"""
    if not hits:
        return []
    header = "".join(_compact(h.text) for h in hits)
    has_header = "材料描述" in header or (
        "规格" in header and "材料" in header
    )
    bounds = _split_columns(hits)
    if bounds is None:
        return []

    rows: list[MaterialRow] = []
    expected = 1
    first = True
    for group in _group_rows(hits):
        joined = "".join(_compact(h.text) for h in group)
        if "材料描述" in joined or (
            ("规格" in joined or "数量" in joined)
            and len(joined) <= 12
            and not re.search(r"\d", joined)
        ):
            continue
        serial_text, content, spec, qty = _pick(group, bounds)
        if not content:
            continue
        # OCR 常把数量的 1 认成 l/I，例如「l. 4米」。
        qty = re.sub(r"(?i)^\s*[lI][.．]\s*(?=\d)", "1.", qty)
        digits = re.sub(r"\D", "", serial_text)
        serial = int(digits) if digits and len(digits) <= 3 else None
        if first and serial is not None:
            expected = serial
        if serial is not None and abs(serial - expected) <= 1 and (
            not rows or serial > rows[-1].seq
        ):
            seq = serial
        else:
            seq = expected
        # 描述换行：没有规格/数量、且像标准号续行，并回上一行
        if not spec and not qty and rows and (
            content.upper().startswith(("HG/", "GB/", "JB/", "NB/")) or len(content) <= 12
        ):
            rows[-1].raw = f"{rows[-1].raw} {content}".strip()
            rows[-1].category = _fix_category(
                _category(rows[-1].raw), _standard(rows[-1].raw)
            )
            rows[-1].grade = _grade(rows[-1].raw)
            rows[-1].standard = _standard(rows[-1].raw)
            rows[-1].phi = _phi(rows[-1].raw)
            rows[-1].dn = rows[-1].dn or _dn_from_phi(rows[-1].phi)
            continue
        raw = content
        phi = _phi(raw)
        dn = _dn(spec) or _dn_from_phi(phi)
        standard = _standard(raw)
        category = _fix_category(_category(raw), standard)
        if rows and (
            not category
            or category.startswith(("：", ":"))
            or "图号" in category
        ):
            # 仪表位号/图号行没有重复写材料名，沿用上一行的材料名。
            category = rows[-1].category
        rows.append(
            MaterialRow(
                pipeline=pipeline,
                page=page,
                seq=seq,
                category=category,
                grade=_grade(raw),
                standard=standard,
                phi=phi,
                dn=dn,
                qty=qty,
                raw=raw,
            )
        )
        expected = seq + 1
        first = False
    if not has_header:
        # 没框到表头时，至少得有一行像材料名，避免把图纸上的其它字当成表格。
        rows = [row for row in rows if re.search(r"[\u4e00-\u9fff]", row.category)]
    return rows


def page_pipelines(doc: pymupdf.Document) -> dict[int, str]:
    """页码（1-based）→ 该页书签标题。相同页取第一条。"""
    out: dict[int, str] = {}
    try:
        entries = doc.get_toc(simple=True)
    except Exception:  # noqa: BLE001
        return out
    for entry in entries:
        if len(entry) < 3:
            continue
        _level, title, page = entry[0], entry[1], entry[2]
        try:
            number = int(page)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= doc.page_count and number not in out:
            out[number] = str(title or "").strip()
    return out


def scan_material_page(
    page: pymupdf.Page,
    region: Region,
    page_index: int,
    pipeline: str = "",
    zoom: float = MATERIAL_ZOOM,
) -> list[MaterialRow]:
    """识别一页；不是材料表、或表头没认出来，返回空列表。"""
    rect = textscan.region_rect(page, region)
    if rect.is_empty or rect.is_infinite:
        return []
    hits = textscan.ocr_page(page, rect, zoom=zoom)
    return rows_from_hits(hits, pipeline=pipeline, page=page_index + 1)


class MaterialScanWorker(QThread):
    """后台遍历所有页；每页识别完发一小批行，主线程只负责往表里加。"""

    progress = Signal(int, int)
    rows = Signal(object)  # list[MaterialRow]
    failed = Signal(str)
    finished_ok = Signal(int, int, float)  # 已处理页数、行数、耗时秒

    def __init__(
        self,
        path: str,
        region: Region,
        zoom: float = MATERIAL_ZOOM,
        first_page: int = 1,
        last_page: int | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._region = region
        self._zoom = zoom
        self._first_page = max(1, int(first_page))
        self._last_page = int(last_page) if last_page else 0
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: D401 - QThread 入口
        if not textscan.ocr_available():
            self.failed.emit(textscan.ocr_error() or "系统 OCR 不可用")
            return
        started = time.monotonic()
        try:
            doc = pymupdf.open(self._path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"打开 PDF 失败：{exc}")
            return
        total = doc.page_count
        pipelines = page_pipelines(doc)
        first = min(self._first_page, total)
        last = min(self._last_page or total, total)
        if last < first:
            first, last = last, first
        span = max(0, last - first + 1)
        done = 0
        count = 0
        try:
            for index in range(first - 1, last):
                if self._stop:
                    break
                page = doc[index]
                try:
                    found = scan_material_page(
                        page,
                        self._region,
                        index,
                        pipeline=pipelines.get(index + 1, ""),
                        zoom=self._zoom,
                    )
                except Exception:  # noqa: BLE001 - 单页失败不拖垮整批
                    found = []
                if found:
                    count += len(found)
                    self.rows.emit(found)
                done += 1
                self.progress.emit(done, span)
        finally:
            doc.close()
        self.finished_ok.emit(done, count, time.monotonic() - started)


def write_material_xlsx(path: str, rows: list[MaterialRow]) -> int:
    """写材料表 Excel；返回数据行数。"""
    from qreport.xlsx import write_table_xlsx

    return write_table_xlsx(
        path,
        COLUMNS,
        [row.as_tuple() for row in rows],
        widths=COLUMN_WIDTHS,
    )

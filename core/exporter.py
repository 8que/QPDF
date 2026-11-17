"""第 11 章：把项目里的标注导出成 PDF。

两级写入策略（第 11.4 节要求"增量保存与全量重写方案对比测试"）：

1. **复制 + 增量追加** —— 把原件复制到临时文件，再 `save(incremental=True)` 追加注释。
   实测 82.7 MB / 2700 页样本：复制 0.02 s + 追加 0.16 s；全量重写 0.94 s。
   页数越多、原始内容越重，差距越大，因为增量只写注释对象，不重写页面。
2. **全量重写** —— 增量不可用时（加密 / 已签名 / 结构异常 / MuPDF 拒绝）自动退回。

不管走哪条路，写的都是**临时文件**；重新打开验证过页数和注释数之后才 `os.replace`
到目标位置。所以第 11.5 节的"导出失败不得破坏原始 PDF"是结构性保证：
源文件全程只读打开，一次都没有被写。

所有标注都写成 **FreeText 注释**（第 11.2 节）：文字、数字都是标准注释对象，
Adobe / Foxit / PDF-XChange / Bluebeam / WPS 都能选中、改内容、改样式。
扁平化模式（第 11.3 节）把它们画进页面内容流，别的软件只能看不能改。
"""
from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from core.annotation import ALIGN_CENTER, ALIGN_RIGHT, KIND_NUMBER
from core.fonts import font_file_for

#: MuPDF 自带的简体中文字体（Droid Sans Fallback 子集），无需系统里有中文字体
CJK_FONTNAME = "china-s"
#: 纯英文用 base14，体积最小、兼容性最好；粗斜体有现成的变体
LATIN_FONTS = {
    (False, False): "helv",
    (True, False): "hebo",
    (False, True): "heit",
    (True, True): "hebi",
}
#: 界面里选了衬线/等宽字体时，英文走对应的 base14 家族——
#: 这样属性面板里的字体选择至少在拉丁文上「说到做到」。
#: 中文没法跟着走：内置 CJK 字体只有一个（Droid Sans Fallback），
#: 真按用户选的字体导出就得嵌入系统字体文件（体积/授权/字体缺失都要处理），
#: 那是后面单独做的一件事，这里先把界面说明白。
LATIN_FAMILY_FONTS = {
    "sans": LATIN_FONTS,
    "serif": {
        (False, False): "tiro",
        (True, False): "tibo",
        (False, True): "tiit",
        (True, True): "tibi",
    },
    "mono": {
        (False, False): "cour",
        (True, False): "cobo",
        (False, True): "coit",
        (True, True): "cobi",
    },
}
_SERIF_HINTS = (
    "serif", "times", "song", "宋", "simsun", "nsimsun", "songti", "stsong",
    "楷", "kai", "fangsong", "仿宋", "georgia", "cambria", "book", "roman", "mincho",
)
_MONO_HINTS = ("mono", "consolas", "courier", "等宽", "fixed", "menlo", "monaco")
#: 行高/字号的比值，跟界面里 QFontMetricsF 的效果对齐（略宽松，宁可有留白也别裁字）
LINE_RATIO = 1.45
#: 注释框比文字实际宽度多留一点：FreeText 内部还有一圈 inset，
#: 而且不同阅读器字体度量有差异，宁可框大一点也别让字被裁掉（边框宽度是 0，看不出来）。
#: 但留太多会让"从 rect 左上角开始排版"的阅读器（WPS 就是）把字画得偏左上，所以只留 2pt。
#: 字形顶端相对注释框顶部的冲出比例（实测 = 4.4 pt / 16 pt）。
#: 补上它，"墨迹顶端"才真的落在用户点的那条线上。
_ASCENT_RATIO = 0.275
#: 框宽在墨迹之外多留的量（按字号折算），纯粹为了别让 FreeText 自动换行
_WIDTH_ROOM = 2.5
#: 框高在墨迹之外多留的量
_HEIGHT_ROOM = 0.6

WIDTH_PAD = 2.0
#: 宽度系数：MuPDF 量的墨迹宽 × 这个系数。
#: 为什么留这么多：WPS 用的是它自己的字体度量，往往比 MuPDF 量出来更宽，
#: 系数小了它就自作主张换行（预览里明明是一行）。框向右放宽不影响字的落点
#: （左边缘不动、边框宽度 0），所以宁可宽一点。
WIDTH_SLACK = 1.30
#: 高度方向的余量（同样为了不掉字，也要照顾 WPS 的排版差异）
HEIGHT_PAD = 2.0
#: 导出前要求的额外可用磁盘（第 11.5 节：检查目标磁盘剩余空间）
DISK_HEADROOM = 16 * 1024 * 1024


class ExportError(RuntimeError):
    """导出过程中可以直接告诉用户看的错误。"""


class ExportCancelled(Exception):
    """用户点了取消。"""


@dataclass(frozen=True)
class ExportItem:
    """一个待写进 PDF 的标注（已经在主线程里算好文本，导出线程不再碰界面对象）。"""

    page: int
    x: float
    y: float
    text: str
    font_size: float
    color: str
    align: str = "left"
    bold: bool = False
    italic: bool = False
    opacity: int = 100
    kind: str = "text"
    #: 界面里选的字体族（只用来判断英文该走衬线还是等宽；中文固定用内置字体）
    font_family: str = ""
    #: 需要真正嵌入用户字体时，本机字体文件绝对路径；空 = 用内置字体
    font_file: str = ""


@dataclass
class ExportPlan:
    #: 注意：这里**不能**是 frozen=True——主窗口会在建好计划之后补上 toc（书签），
    #: 冻结的 dataclass 一赋值就抛 FrozenInstanceError，表现成"导出按钮点了没反应/导出失败"。
    source: str
    target: str
    page_count: int
    items: list[ExportItem] = field(default_factory=list)
    flatten: bool = False
    title: str = ""
    #: 书签：``[[1, "50-S0-P1302B01-N1B-H60", 501], ...]``（页号 1 起）
    toc: list[list] = field(default_factory=list)
    #: 页码装饰规格（core.page_number.PageNumberSpec）；与 annotations 完全分开，
    #: 不写进 .qpdf，也不会被 qreport 当成焊口数字/文字。
    page_numbers: object | None = None


@dataclass(frozen=True)
class ExportResult:
    target: str
    page_count: int
    annot_count: int
    flatten: bool
    mode: str
    elapsed: float
    size: int


_FONT_CACHE: dict[str, pymupdf.Font] = {}


def _font(fontname: str) -> pymupdf.Font:
    font = _FONT_CACHE.get(fontname)
    if font is None:
        font = pymupdf.Font(fontname)
        _FONT_CACHE[fontname] = font
    return font


def _file_font(path: str) -> pymupdf.Font:
    key = f"file:{path}"
    font = _FONT_CACHE.get(key)
    if font is None:
        font = pymupdf.Font(fontfile=path)
        _FONT_CACHE[key] = font
    return font


def _item_font(item: ExportItem) -> pymupdf.Font:
    """有指定字体文件就用它，否则按原来的内置字体/base14 规则选。"""
    if item.font_file:
        try:
            return _file_font(item.font_file)
        except Exception:  # noqa: BLE001 - 字体文件坏了就退回内置字体，别让导出失败
            pass
    return _font(fontname_for(item))


def has_cjk(text: str) -> bool:
    for ch in text:
        code = ord(ch)
        if (
            0x2E80 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF
            or 0x3000 <= code <= 0x303F
        ):
            return True
    return False


def latin_family(family: str) -> str:
    """把界面字体名归成「衬线 / 等宽 / 无衬线」三档，用来挑 base14 变体。"""
    name = (family or "").strip().lower()
    if any(hint in name for hint in _MONO_HINTS):
        return "mono"
    if any(hint in name for hint in _SERIF_HINTS):
        return "serif"
    return "sans"


def fontname_for(item: ExportItem) -> str:
    """选字体：正文里有中文就用 CJK 字体，否则按界面选的字族挑 base14。"""
    if has_cjk(item.text):
        return CJK_FONTNAME
    table = LATIN_FAMILY_FONTS[latin_family(item.font_family)]
    return table[(item.bold, item.italic)]


def parse_color(value: str) -> tuple[float, float, float]:
    """`#rrggbb` -> (0..1, 0..1, 0..1)，非法的颜色一律当黑色，别让导出因为一个颜色失败。"""
    text = (value or "").strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        return (0.0, 0.0, 0.0)
    try:
        return (
            int(text[0:2], 16) / 255.0,
            int(text[2:4], 16) / 255.0,
            int(text[4:6], 16) / 255.0,
        )
    except ValueError:
        return (0.0, 0.0, 0.0)


def _lines(item: ExportItem) -> list[str]:
    return item.text.splitlines() or [""]


def text_extent(item: ExportItem) -> tuple[float, float]:
    """按 MuPDF 自己的字体度量算文字宽高，保证注释框装得下。"""
    font = _item_font(item)
    size = item.font_size
    width = 0.0
    for line in _lines(item):
        width = max(width, font.text_length(line, fontsize=size))
    height = len(_lines(item)) * size * LINE_RATIO
    return width, height


def annot_rect(item: ExportItem) -> pymupdf.Rect:
    """标注位置 -> PDF 注释框。

    - 数字：点击位置是**中心**（第 5.1 节），框也以它为中心。
    - 文字：位置是左上角，按对齐方式改成左/中/右锚点，纵向仍以顶部对齐。

    这里要按**字形的实际墨迹**来摆框，不能直接拿 item 的坐标当框边：
    MuPDF 生成外观流时，字形顶端会跑到框顶**上方**约 0.275 个字高
    （基线 + ascender 的关系），框宽还会留一圈余量。
    以前直接拿 item.y 当框顶，导出去就整体"往上偏几个像素"——
    在 WPS 里尤其明显。现在把这两项都算进去，墨迹才真的落在点击位置。
    """
    ink_w, ink_h = text_extent(item)
    size = float(item.font_size or 12.0)
    # 墨迹顶端相对框顶的偏移（负值 = 字形冲出框顶）
    overshoot = _ASCENT_RATIO * size
    # 框高度按墨迹给足，宽度也留一点，免得 FreeText 自动换行
    width = ink_w * WIDTH_SLACK + _WIDTH_ROOM * size
    height = ink_h + _HEIGHT_ROOM * size

    if item.kind == KIND_NUMBER:
        ink_top = item.y - ink_h / 2.0
        return pymupdf.Rect(
            item.x - ink_w / 2.0,
            ink_top + overshoot,
            item.x + ink_w / 2.0,
            ink_top + overshoot + height,
        )

    if item.align == ALIGN_CENTER:
        left = item.x - ink_w / 2.0
    elif item.align == ALIGN_RIGHT:
        left = item.x - ink_w
    else:
        left = item.x
    return pymupdf.Rect(left, item.y + overshoot, left + width, item.y + overshoot + height)


def _align_code(item: ExportItem) -> int:
    if item.align == ALIGN_CENTER:
        return 1
    if item.align == ALIGN_RIGHT:
        return 2
    return 0


def page_rotation(page: pymupdf.Page) -> int:
    """页面的 /Rotate 值（0/90/180/270）。工程图纸经常整页转了 90° 存放。"""
    return int(page.rotation or 0) % 360


def seen_to_pdf(page: pymupdf.Page, rect: pymupdf.Rect) -> pymupdf.Rect:
    """把"屏幕上看到的那一页"上的矩形换算到 PDF 内部坐标。

    这件事必须做：我们显示、标注、点选用的都是**旋转之后**的那页（``page.rect``
    的坐标系），而注释和文字写进去用的是**没旋转**的内部坐标（``page.mediabox``）。
    页面上有 ``/Rotate 90`` 时两者差一个旋转矩阵——不做换算，导出的标注就会整体
    跑到另一个角上去，看起来像"整个结果逆时针转了 90 度"。
    """
    if page_rotation(page) == 0:
        return rect
    return rect * page.derotation_matrix


def _add_freetext(page: pymupdf.Page, item: ExportItem) -> None:
    rotation = page_rotation(page)
    annot = page.add_freetext_annot(
        seen_to_pdf(page, annot_rect(item)),
        item.text,
        fontsize=item.font_size,
        fontname=fontname_for(item),
        text_color=parse_color(item.color),
        align=_align_code(item),
        border_width=0,
        rotate=rotation,
        opacity=max(0.0, min(1.0, item.opacity / 100.0)),
    )
    _pin_default_appearance(page.parent, annot, item)


#: 外观流里出现的字体名（`/Song 14 Tf`）
_AP_FONT_RE = re.compile(r"/([A-Za-z0-9_.+-]+)\s+[\d.]+\s+Tf")
#: 资源字典里的字体引用（`/Song 1537 0 R`）
_REF_RE = re.compile(r"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+0\s+R")


def _pin_default_appearance(doc: pymupdf.Document, annot, item: ExportItem) -> None:
    """把 /DA 和 /DR 指到真正用的那个字体上。

    MuPDF 生成外观流时会正确用上中文字体，但 /DA 里写的是 `/Helv`。别的阅读器
    （Acrobat 之类）一旦让你改这条注释，就会按 /DA 重新排版——中文会变成乱码。
    这里把 /DA 改成外观流里实际使用的字体，并把字体挂到注释自己的 /DR 上，
    这样"改文字"在别的软件里也能正常显示（实测 a.update() 重绘后中文正常）。
    """
    try:
        ap = doc.xref_get_key(annot.xref, "AP")
        if ap[0] != "dict":
            return
        match = re.search(r"(\d+)\s+0\s+R", ap[1] or "")
        if not match:
            return
        ap_xref = int(match.group(1))
        stream = doc.xref_stream(ap_xref) or b""
        fonts = doc.xref_get_key(ap_xref, "Resources/Font")
        if fonts[0] != "dict":
            return
        refs = {name: int(xref) for name, xref in _REF_RE.findall(fonts[1])}
        used = _AP_FONT_RE.findall(stream.decode("latin-1"))
        if not used:
            return

        def is_cjk_font(name: str) -> bool:
            xref = refs.get(name)
            if xref is None:
                return False
            return doc.xref_get_key(xref, "Subtype")[1] == "Type0"

        chosen = None
        if has_cjk(item.text):
            chosen = next((name for name in used if is_cjk_font(name)), None)
        chosen = chosen or next((name for name in used if name in refs), None)
        if not chosen:
            return
        xref = refs[chosen]
        rgb = parse_color(item.color)
        da = f"{rgb[0]:.6g} {rgb[1]:.6g} {rgb[2]:.6g} rg /{chosen} {item.font_size:g} Tf"
        doc.xref_set_key(annot.xref, "DA", f"({da})")
        doc.xref_set_key(annot.xref, "DR", f"<</Font <</{chosen} {xref} 0 R>>>>")
    except Exception:  # noqa: BLE001 - 这一步只是让别家软件更好改，失败也不该让导出挂掉
        pass


def _flatten(page: pymupdf.Page, item: ExportItem) -> None:
    """扁平化：把文字用 TextWriter 写进页面内容流，字体会被内嵌子集化。

    文字先在"看到的那页"坐标系里排好（左/中/右对齐、行距都是按这个空间算的），
    再把每一行的起点换算进 PDF 内部坐标，并按页面旋转角度把字形转回去。
    逐行各写一个 writer（各以本行起点为轴心），多行才不会绕着一个点甩开。
    """
    seen = annot_rect(item)
    rotation = page_rotation(page)
    font = _item_font(item)
    size = item.font_size
    line_height = size * LINE_RATIO
    morph = pymupdf.Matrix(rotation) if rotation else None
    for index, line in enumerate(_lines(item)):
        width = font.text_length(line, fontsize=size)
        if item.align == ALIGN_CENTER:
            left = seen.x0 + (seen.width - width) / 2.0
        elif item.align == ALIGN_RIGHT:
            left = seen.x1 - width
        else:
            left = seen.x0
        baseline = seen.y0 + index * line_height + size * 1.05
        if item.font_file and " " in line:
            # 微软雅黑等字体的空格在 ToUnicode 里映射成 NBSP（\xa0），复制/搜索
            # “第 1 页”会匹配不到。空格单独用内置中文字体画，汉字仍用用户字体。
            x = left
            for ch in line:
                width = font.text_length(ch, fontsize=size)
                origin = pymupdf.Point(x, baseline)
                if rotation:
                    origin = origin * page.derotation_matrix
                writer = pymupdf.TextWriter(page.rect)
                writer.append(
                    origin,
                    ch,
                    font=_font("china-s") if ch == " " else font,
                    fontsize=size,
                )
                writer.write_text(
                    page,
                    color=parse_color(item.color),
                    opacity=item.opacity / 100.0,
                    morph=(origin, morph) if morph else None,
                )
                x += width
            continue
        origin = pymupdf.Point(left, baseline)
        if rotation:
            origin = origin * page.derotation_matrix
        writer = pymupdf.TextWriter(page.rect)
        writer.append(origin, line, font=font, fontsize=size)
        writer.write_text(
            page,
            color=parse_color(item.color),
            opacity=item.opacity / 100.0,
            morph=(origin, morph) if morph else None,
        )


def annot_counts(doc: pymupdf.Document, pages) -> dict[int, int]:
    """记下这些页上**原本就有**的注释条数。

    工程图纸经常是前后手交接的，页面上可能已经有别人画的批注。写后校验只能比
    "本次多出来的那几条"，拿整页注释总数去跟 ``len(plan.items)`` 对齐的话，
    只要源文件里本来有一条批注，导出就会被误判成写漏而整次放弃。
    """
    return {int(page): sum(1 for _ in doc[int(page)].annots()) for page in pages}


def _apply(doc: pymupdf.Document, plan: ExportPlan, report, is_cancelled) -> dict[int, int]:
    """把标注写进 ``doc``，返回**写入前**每页原有的注释条数（给 ``_verify`` 当基线）。"""
    by_page: dict[int, list[ExportItem]] = {}
    for item in plan.items:
        by_page.setdefault(item.page, []).append(item)

    baseline = annot_counts(doc, by_page)
    total = len(by_page)
    for done, (page_index, items) in enumerate(sorted(by_page.items()), start=1):
        if is_cancelled():
            raise ExportCancelled
        if page_index >= doc.page_count:
            raise ExportError(f"标注落在第 {page_index + 1} 页，但这份 PDF 只有 {doc.page_count} 页")
        page = doc[page_index]
        for item in items:
            if plan.flatten:
                _flatten(page, item)
            else:
                _add_freetext(page, item)
        report(done, total, f"正在写入第 {page_index + 1} 页")

    spec = plan.page_numbers
    if spec is not None and getattr(spec, "enabled", True):
        style = spec.style
        total_pages = doc.page_count
        for page_index in range(total_pages):
            if is_cancelled():
                raise ExportCancelled
            page = doc[page_index]
            text = spec.text_for(page_index + 1, total_pages)
            if not text:
                continue
            x0, y0, _x1, _y1 = spec.region.to_pdf(page.rect.width, page.rect.height)
            item = ExportItem(
                page=page_index,
                x=float(x0),
                y=float(y0),
                text=text,
                font_size=float(style.font_size),
                color=style.color,
                align=style.align,
                bold=bool(style.bold),
                italic=bool(style.italic),
                opacity=int(style.opacity),
                kind="page_number",
                font_family=style.font_family,
                font_file=font_file_for(style.font_family, bool(style.bold))
                if has_cjk(text)
                else "",
            )
            # 页码必须真的写进 PDF 内容流：不管是注释模式还是扁平化模式，
            # 都用 TextWriter 画成页面文字，打印/别家阅读器都看得到。
            _flatten(page, item)
            report(page_index + 1, total_pages, f"正在写第 {page_index + 1} 页页码")

    if plan.toc:
        # 书签（第 Phase 10 章）：管线号当标题，点一下直接跳到那页
        try:
            doc.set_toc(plan.toc)
        except Exception as exc:  # noqa: BLE001 - 书签写不进去不该毁掉整次导出
            print(f"[导出] 书签写入失败：{exc}")
    return baseline


def _verify(path: str, plan: ExportPlan, baseline: dict[int, int]) -> None:
    """写完之后必须重新打开确认一遍，通过才允许改名到正式位置（第 11.5 节）。

    注释数按"每页原有条数 + 本次写入条数"核对，源文件自带的批注不算在内。
    """
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        raise ExportError(f"导出的文件打不开：{exc}") from exc
    try:
        if doc.page_count != plan.page_count:
            raise ExportError(
                f"导出的文件页数不对（{doc.page_count} 页，应为 {plan.page_count} 页），已放弃这次导出"
            )
        if not plan.flatten:
            expected_new: dict[int, int] = {}
            for item in plan.items:
                expected_new[item.page] = expected_new.get(item.page, 0) + 1
            for page_index in sorted(expected_new):
                existing = baseline.get(page_index, 0)
                found = sum(1 for _ in doc[page_index].annots())
                if found - existing != expected_new[page_index]:
                    raise ExportError(
                        f"第 {page_index + 1} 页的注释数量不对（本次应新增 {expected_new[page_index]} 条，"
                        f"实际新增 {found - existing} 条），已放弃这次导出"
                    )
        if plan.toc:
            written = doc.get_toc(simple=True)
            if len(written) < len(plan.toc):
                raise ExportError(
                    f"书签没写全（{len(written)} 条，应为 {len(plan.toc)} 条），已放弃这次导出"
                )
    finally:
        doc.close()


def _disk_check(target_dir: str, source: str) -> None:
    free = shutil.disk_usage(target_dir).free
    need = os.path.getsize(source) + DISK_HEADROOM
    if free < need:
        raise ExportError(
            f"磁盘空间不够：还需要约 {need / 1024 / 1024:.0f} MB，"
            f"当前只剩 {free / 1024 / 1024:.0f} MB"
        )


def _subset_fonts(doc: pymupdf.Document) -> None:
    """把内嵌字体裁掉没用到的字模。

    扁平化为了几个汉字会把整份中文字体嵌进去（实测 3.5 KB 的源文件 → 3.6 MB 的成品）；
    子集化之后同样内容约 0.4 MB，文字照样能抽、渲染结果一个像素都不差。
    失败也不影响导出（字体嵌全了只是文件大），所以只提示不抛。
    """
    try:
        doc.subset_fonts()
    except Exception as exc:  # noqa: BLE001 - 字体异常不该让整次导出失败
        print(f"[导出] 字体子集化跳过：{exc}")


def _same_file(a: str, b: str) -> bool:
    """两个路径是不是同一个文件（比字符串靠谱：大小写、短路径、硬链接都算）。"""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _write(doc_target: str, plan: ExportPlan, source_path: str, incremental: bool, report, is_cancelled) -> dict[int, int]:
    """写临时文件，返回写入前每页原有的注释条数（给 ``_verify`` 当基线）。"""
    if incremental:
        shutil.copy2(source_path, doc_target)
        doc = pymupdf.open(doc_target)
    else:
        doc = pymupdf.open(source_path)
    try:
        baseline = _apply(doc, plan, report, is_cancelled)
        if plan.flatten or plan.page_numbers is not None:
            # 扁平化/页码都会往页面内容流里写字模；只写注释的模式不动源文件字体
            _subset_fonts(doc)
        if incremental:
            doc.save(doc_target, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
        else:
            doc.save(doc_target, garbage=0, deflate=False, clean=False)
    finally:
        doc.close()
    return baseline


def export_pdf(plan: ExportPlan, progress=None, cancelled=None) -> ExportResult:
    """同步执行一次导出（在后台线程里跑，界面线程只收信号）。

    `progress(done, total, message)` 用来刷新进度条；
    `cancelled()` 返回 True 就中止并删掉临时文件。
    """
    started = time.perf_counter()

    def report(done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)

    def is_cancelled() -> bool:
        return bool(cancelled and cancelled())

    source = os.path.abspath(plan.source)
    target = os.path.abspath(plan.target)
    if source == target or _same_file(source, target):
        # samefile 兜住大小写、8.3 短路径、硬链接这些"字符串不同但其实是同一个文件"的情况
        raise ExportError("导出目标不能是原始 PDF，请换一个文件名（原始文件永远不改动）")
    if not os.path.exists(source):
        raise ExportError(f"找不到原始 PDF：{source}")

    target_dir = os.path.dirname(target) or "."
    os.makedirs(target_dir, exist_ok=True)
    _disk_check(target_dir, source)

    tmp = target + ".qpdf-tmp"
    if os.path.exists(tmp):
        os.remove(tmp)

    report(0, max(1, len({item.page for item in plan.items})), "准备导出")
    try:
        try:
            baseline = _write(tmp, plan, source, True, report, is_cancelled)
            mode = "增量追加"
        except ExportCancelled:
            raise
        except ExportError:
            raise
        except Exception:  # noqa: BLE001 —— 加密/签名/结构特殊，退全量重写
            if os.path.exists(tmp):
                os.remove(tmp)
            if is_cancelled():
                raise ExportCancelled
            baseline = _write(tmp, plan, source, False, report, is_cancelled)
            mode = "全量重写"

        if is_cancelled():
            raise ExportCancelled
        report(1, 1, "检查导出结果")
        _verify(tmp, plan, baseline)
        os.replace(tmp, target)
    except ExportCancelled:
        _cleanup(tmp)
        raise
    except ExportError:
        _cleanup(tmp)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup(tmp)
        raise ExportError(f"导出失败：{exc}") from exc

    return ExportResult(
        target=target,
        page_count=plan.page_count,
        annot_count=len(plan.items),
        flatten=plan.flatten,
        mode=mode,
        elapsed=time.perf_counter() - started,
        size=os.path.getsize(target),
    )


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def build_plan(
    source: str,
    target: str,
    page_count: int,
    store,
    sequence=None,
    flatten: bool = False,
) -> ExportPlan:
    """把内存里的标注整理成导出任务（主线程调用；数字的显示值在这里定下来）。"""
    items: list[ExportItem] = []
    for ann in store.all():
        if ann.hidden:
            continue
        text = ann.content
        if ann.is_number and sequence is not None:
            text = sequence.text_for(ann)
        if not text:
            continue
        style = ann.style
        items.append(
            ExportItem(
                page=int(ann.page),
                x=float(ann.x),
                y=float(ann.y),
                text=text,
                font_size=float(style.font_size),
                color=style.color,
                align=style.align,
                bold=style.bold,
                italic=style.italic,
                opacity=int(style.opacity),
                kind=ann.kind,
                font_family=style.font_family,
            )
        )
    items.sort(key=lambda item: (item.page, item.y, item.x))
    return ExportPlan(
        source=os.path.abspath(source),
        target=os.path.abspath(target),
        page_count=int(page_count),
        items=items,
        flatten=flatten,
    )


class _ExportSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class _ExportWorker(QRunnable):
    def __init__(self, plan: ExportPlan) -> None:
        super().__init__()
        self.plan = plan
        self.signals = _ExportSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D401
        try:
            result = export_pdf(
                self.plan,
                progress=lambda done, total, msg: self.signals.progress.emit(done, total, msg),
                cancelled=lambda: self._cancelled,
            )
        except ExportCancelled:
            self.signals.cancelled.emit()
        except ExportError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(f"导出时出错：{exc}")
        else:
            self.signals.finished.emit(result)


class ExportManager(QObject):
    """把导出放到后台线程，界面只收信号（第 11.4 节：导出阶段采用后台处理）。"""

    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()
    started = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._worker: _ExportWorker | None = None

    @property
    def is_busy(self) -> bool:
        return self._worker is not None

    def start(self, plan: ExportPlan) -> bool:
        if self.is_busy:
            return False
        worker = _ExportWorker(plan)
        worker.signals.progress.connect(self.progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        self._worker = worker
        self._pool.start(worker)
        self.started.emit()
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def shutdown(self) -> None:
        self.cancel()
        self._pool.waitForDone(5000)
        self._worker = None

    def _on_finished(self, result) -> None:
        self._worker = None
        self.finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self.failed.emit(message)

    def _on_cancelled(self) -> None:
        self._worker = None
        self.cancelled.emit()

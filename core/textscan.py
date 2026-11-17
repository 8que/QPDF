"""从图纸的一块区域里读字。

两条路，先便宜后昂贵：

1. **原生文字层**：如果这页的 PDF 里还留着文字对象，``page.get_text`` 直接就有，
   零成本、100% 准确。CAD 导出的图纸经常已经把字转成线条了（我们那份 1.2 GB 样本
   每页只剩 0~29 个词），所以这条路经常空手而归，但必须先试。
2. **本地 OCR**：用 Windows 自带的 OCR 引擎（``Windows.Media.Ocr``），不打包模型、
   不联网。这台机器上实测一小块标签 0.01~0.02 秒，比带模型的方案快两个数量级。

OCR 只在后台线程里调用（见 ``core/ocr_job.py``）：一次几十毫秒乘以两千页，
放在主线程就是灾难。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

import pymupdf

from core.region import Region

#: OCR 时把区域放大多少倍。图纸上的字常常只有 5~6 pt，8 倍之后约 45 px 高，
#: 是 Windows OCR 比较舒服的尺寸；再大只是白白变慢（实测 6/8/12/16 倍结果一模一样）。
DEFAULT_ZOOM = 8.0

#: 各种"长得像横线"的字符。中文 OCR 特别爱把 ``-`` 输出成 ``一``。
_DASHES = "一—–－‐‑_−﹣━·•・"
#: 全角括号
_BRACKETS = {"（": "(", "）": ")", "【": "(", "】": ")", "〔": "(", "〕": ")", "［": "[", "］": "]"}

#: 管线号长这样：``100-BU-V110501-N1B-C50``。至少三段、有数字有字母。
_TAG_RE = re.compile(r"^\d{1,4}(?:-[A-Z0-9()\[\]]{1,10}){2,6}$")


def normalize_tag(text: str) -> str:
    """把 OCR 的脏输出收拾成能当书签的管线号。

    只做**确定性**的修正（换字符、去空白、全角转半角），不猜内容——
    认错的字母（``N1B`` 读成 ``NIB``）留给校对那一步，软件不擅自改写。
    """
    s = (text or "").strip()
    for ch in _DASHES:
        s = s.replace(ch, "-")
    for src, dst in _BRACKETS.items():
        s = s.replace(src, dst)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = s.replace("——", "-").replace("--", "-")
    s = re.sub(r"-{2,}", "-", s)
    return s.strip("-")


def looks_like_tag(text: str) -> bool:
    """这段字像不像管线号（有分隔、有字母有数字）。"""
    tag = normalize_tag(text)
    if len(tag) < 6:
        return False
    if not re.search(r"[A-Z]", tag) or not re.search(r"\d", tag):
        return False
    return bool(_TAG_RE.match(tag))


#: 中文 OCR 模型认英数字符串时的常见混淆。只在**明确的位置**上替换，不全局乱改。
#: 依据：这份图纸的管线号里，夹在两个字母中间的那一位一定是数字（``N1B``、``M1B``），
#: 而 OCR 十次有十次把 ``1`` 读成 ``I``。
_MID_DIGIT = re.compile(r"^([A-Z])[Il]([A-Z])$")
_TAIL_DIGIT = re.compile(r"^([A-Z])[Il]$")
#: 段末的 ``5I``：数字后面跟了个被读成 I 的 1（``C5I`` → ``C51``）
_TAIL_AFTER_DIGIT = re.compile(r"^(.*\d)[Il]$")
_LEAD_L = re.compile(r"^([A-Z])L(\d{3,})$")
#: 线号段 ``P1107D01`` 被读成 ``PL107D01``：首字母后面那个 L 一定是 1。
#: 加长度门槛，免得把 ``CL150`` 这种规格段改成 ``C1150``。
_INNER_L = re.compile(r"^([A-Z])L(\d{3,}[A-Z0-9]*)$")


def repair_tag(tag: str) -> str:
    """猜测性修正（``NIB`` → ``N1B``），结果仍然可编辑。

    只处理两条这个文档里被反复验证过的规则：
    * 三段的中间位 ``[A-Z]I[A-Z]`` 一定是数字 1；
    * 段末的 ``[A-Z]I`` 同理（``C5I`` → ``C51``）；
    * 括号前面缺横线的补回来（``MIB(09)`` → ``M1B-(09)``）。
    """
    parts = [p for p in tag.split("-") if p]
    fixed: list[str] = []
    for part in parts:
        suffix = ""
        m = re.match(r"^(.*?)(\([^)]*\))$", part)
        if m:
            part, suffix = m.group(1), m.group(2)
        part = part.upper()
        m = _MID_DIGIT.match(part)
        if m:
            part = f"{m.group(1)}1{m.group(2)}"
        else:
            m = _TAIL_DIGIT.match(part)
            if m:
                part = f"{m.group(1)}1"
            else:
                m = _TAIL_AFTER_DIGIT.match(part)
                if m and len(part) >= 3:
                    part = f"{m.group(1)}1"
                else:
                    m = _LEAD_L.match(part)
                    if m and len(part) >= 6:
                        # EL10701 → E110701（OCR 把 1 读成了小写 l，大写后就成了 L）
                        part = f"{m.group(1)}1{m.group(2)}"
                    else:
                        m = _INNER_L.match(part)
                        if m and len(part) >= 7:
                            # PL107D01 → P1107D01（线号段里的 L 也是被读错的 1）
                            part = f"{m.group(1)}1{m.group(2)}"
        fixed.append(part + suffix)
    joined = "-".join(fixed)
    # 括号组前面缺横线的补回来：M1B(09) → M1B-(09)
    joined = re.sub(r"(?<=[A-Z0-9])\(", "‑(", joined).replace("‑", "-")
    return joined



@dataclass
class ScanHit:
    """一条识别结果。"""

    text: str
    source: str  # "text" | "ocr"
    score: float = 0.0  # OCR 才有，0~1
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # 页面坐标

    @property
    def compact(self) -> str:
        """去掉所有空白。管线号常被 OCR 拆成 ``F7 G11 B17`` 这样。"""
        return re.sub(r"\s+", "", self.text)


@dataclass
class ScanResult:
    page: int
    hits: list[ScanHit] = field(default_factory=list)
    error: str = ""

    @property
    def text(self) -> str:
        return " ".join(h.text for h in self.hits[:1]).strip()

    @property
    def compact(self) -> str:
        return "".join(h.compact for h in self.hits[:1]).strip()

    @property
    def source(self) -> str:
        return self.hits[0].source if self.hits else ""

    @property
    def tag(self) -> str:
        """收拾干净的识别结果（书签就用这个）。"""
        return normalize_tag(self.compact)

    @property
    def is_tag(self) -> bool:
        return looks_like_tag(self.compact)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


def region_rect(page: pymupdf.Page, region: Region) -> pymupdf.Rect:
    """区域 → 这一页**可见坐标**下的矩形（就是用户在屏幕上框的那块）。"""
    width, height = page.rect.width, page.rect.height
    x0, y0, x1, y1 = region.to_pdf(width, height)
    return pymupdf.Rect(x0, y0, x1, y1)


def native_text(page: pymupdf.Page, rect: pymupdf.Rect) -> list[ScanHit]:
    """只看这一小块里的原生文字。旋转页要先换回未旋转坐标。"""
    rotation = int(page.rotation or 0) % 360
    clip = rect if rotation == 0 else (pymupdf.Rect(rect) * page.derotation_matrix)
    try:
        words = page.get_text("words", clip=clip)
    except Exception:  # noqa: BLE001
        return []
    hits: list[ScanHit] = []
    for x0, y0, x1, y1, word, *_rest in words:
        text = (word or "").strip()
        if not text:
            continue
        box = (x0, y0, x1, y1)
        if rotation:
            r = pymupdf.Rect(box) * page.rotation_matrix
            box = (r.x0, r.y0, r.x1, r.y1)
        hits.append(ScanHit(text=text, source="text", score=1.0, box=box))
    # 阅读顺序：先上后下、先左后右
    hits.sort(key=lambda h: (round(h.box[1], 1), h.box[0]))
    return hits


# --------------------------------------------------------------------------- OCR

_engine = None
_engine_error = ""
#: 中文优先（标题栏有汉字），退到英文——管线号本身是字母数字，英文引擎够用。
_LANG_CANDIDATES = ("zh-Hans-CN", "zh-Hans", "zh-CN", "en-US", "en-GB", "en")


def _load_engine():
    """惰性加载 Windows OCR 引擎；失败时把原因记下来，界面直接显示。"""
    global _engine, _engine_error
    if _engine is not None or _engine_error:
        return _engine
    try:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine
    except Exception as exc:  # noqa: BLE001 - 不是 Windows 或没装 winrt
        _engine_error = f"系统 OCR 不可用（{type(exc).__name__}）"
        return None
    try:
        languages = [str(l.language_tag) for l in OcrEngine.available_recognizer_languages]
    except Exception as exc:  # noqa: BLE001
        _engine_error = f"读取系统 OCR 语言失败（{type(exc).__name__}）"
        return None
    for tag in _LANG_CANDIDATES:
        if tag not in languages:
            continue
        engine = OcrEngine.try_create_from_language(Language(tag))
        if engine is not None:
            _engine = engine
            return engine
    for tag in languages:
        try:
            engine = OcrEngine.try_create_from_language(Language(tag))
        except Exception:  # noqa: BLE001
            continue
        if engine is not None:
            _engine = engine
            return engine
    _engine_error = (
        "系统装不了 OCR 引擎（可选语言：" + (", ".join(languages) or "无") + "）"
    )
    return None


def ocr_available() -> bool:
    return _load_engine() is not None


def ocr_error() -> str:
    _load_engine()
    return _engine_error


def _to_software_bitmap(image=None, rgb=None):
    """把一张图变成 WinRT 的 SoftwareBitmap。

    优先吃 QImage（Qt 在 C++ 里转成 BGRA8888，快且不需要 numpy）；
    打包时 numpy 是被排除掉的，所以这里**绝不能**依赖 numpy。
    """
    from winrt.windows.graphics.imaging import (
        BitmapAlphaMode,
        BitmapPixelFormat,
        SoftwareBitmap,
    )
    from winrt.windows.storage.streams import DataWriter

    if image is not None:
        from PySide6.QtGui import QImage

        converted = image.convertToFormat(QImage.Format.Format_ARGB32)
        width = converted.width()
        height = converted.height()
        stride = converted.bytesPerLine()
        raw = bytes(converted.constBits())
        if stride != width * 4:
            rows = bytearray()
            for y in range(height):
                rows += raw[y * stride : y * stride + width * 4]
            raw = bytes(rows)
        payload = raw
    else:
        if rgb is None:
            raise ValueError("没有图像数据")
        height, width, _channels = rgb.shape
        payload = bytearray(height * width * 4)
        src = rgb.tobytes()
        for index in range(height * width):
            s = index * 3
            d = index * 4
            payload[d] = src[s + 2]
            payload[d + 1] = src[s + 1]
            payload[d + 2] = src[s]
            payload[d + 3] = 255
        payload = bytes(payload)

    writer = DataWriter()
    writer.write_bytes(payload)
    return SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(),
        BitmapPixelFormat.BGRA8,
        width,
        height,
        BitmapAlphaMode.PREMULTIPLIED,
    )


def ocr_image(image) -> str:
    """对一张 QImage 直接做 OCR，返回拼起来的文本（自检/调试用）。"""
    engine = _load_engine()
    if engine is None:
        return ""
    try:
        bitmap = _to_software_bitmap(image)

        async def _run():
            return await engine.recognize_async(bitmap)

        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        global _engine_error
        _engine_error = f"识别出错（{type(exc).__name__}: {exc}）"
        return ""
    return " ".join((line.text or "").strip() for line in result.lines).strip()


def ocr_page(
    page: pymupdf.Page,
    rect: pymupdf.Rect,
    zoom: float = DEFAULT_ZOOM,
) -> list[ScanHit]:
    """对一块区域做 OCR。返回页面坐标下的结果。"""
    engine = _load_engine()
    if engine is None:
        return []
    try:
        pix = page.get_pixmap(
            clip=pymupdf.Rect(rect),
            matrix=pymupdf.Matrix(zoom, zoom),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        from PySide6.QtGui import QImage

        # 必须 copy：pix 一释放 samples 就悬空了
        image = QImage(
            bytes(pix.samples), pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
        ).copy()
        bitmap = _to_software_bitmap(image)

        async def _run():
            return await engine.recognize_async(bitmap)

        result = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 - OCR 失败不该炸掉批量任务
        global _engine_error
        _engine_error = f"识别出错（{type(exc).__name__}: {exc}）"
        return []

    hits: list[ScanHit] = []
    for line in result.lines:
        text = (line.text or "").strip()
        if not text:
            continue
        words = list(line.words)
        if not words:
            continue
        left = min(w.bounding_rect.x for w in words) / zoom + rect.x0
        top = min(w.bounding_rect.y for w in words) / zoom + rect.y0
        right = max(w.bounding_rect.x + w.bounding_rect.width for w in words) / zoom + rect.x0
        bottom = max(w.bounding_rect.y + w.bounding_rect.height for w in words) / zoom + rect.y0
        hits.append(
            ScanHit(text=text, source="ocr", score=0.9, box=(left, top, right, bottom))
        )
    hits.sort(key=lambda h: (round(h.box[1], 1), h.box[0]))
    return hits


def scan_page(
    page: pymupdf.Page,
    region: Region,
    page_index: int = 0,
    zoom: float = DEFAULT_ZOOM,
    allow_ocr: bool = True,
) -> ScanResult:
    """读一页：先原生文字，没有再 OCR。

    注意原生文字**不一定是我们想要的那段**：这份图纸被编辑过，前 300 页在同一块
    地方盖了红色的「20%」，文字层里只有它，管线号本身是线条画的。所以只有原生文字
    **长得像管线号**时才采信，否则照样走 OCR。
    """
    rect = region_rect(page, region)
    if rect.is_empty or rect.is_infinite:
        return ScanResult(page=page_index, error="识别区域为空")
    hits = native_text(page, rect)
    if hits and looks_like_tag(" ".join(h.text for h in hits)):
        return ScanResult(page=page_index, hits=hits)
    if not allow_ocr:
        if hits:
            return ScanResult(page=page_index, hits=hits)
        return ScanResult(page=page_index, error="没有文字层，且未启用 OCR")
    if not ocr_available():
        return ScanResult(page=page_index, error=ocr_error())
    ocr_hits = ocr_page(page, rect, zoom=zoom)
    if ocr_hits:
        return ScanResult(page=page_index, hits=ocr_hits)
    if hits:
        # OCR 一无所获，但原生文字至少有点东西，总比空着强
        return ScanResult(page=page_index, hits=hits)
    return ScanResult(page=page_index, error="区域内没有识别到文字")

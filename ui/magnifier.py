"""放大悬浮窗：框住一块区域，开一个置顶小窗显示它的放大图。

这一版支持**多个放大窗**：每个窗口有自己的取景区域、位置大小、锁定、
全局置顶和内容倍率。窗口集合存在程序设置里（所有项目共用一套），
切标签只换内容不藏窗口。

放大窗默认只浮在雀雀 PDF 之上（``Qt.Tool``）；右键可以勾选
「始终显示在所有窗口之上」，加上 ``WindowStaysOnTopHint`` 后压到别的软件上面。

滚轮默认改**窗口大小**（框里的内容跟着铺满）；按住 Ctrl 再滚轮，
改的是**内容倍率**（窗口不动，取景框缩小），和主画布缩放 PDF 一个感觉。

鼠标侧键在放大窗上直接翻页：靠后那颗上一页、靠前那颗下一页。

渲染仍然单独开线程：MuPDF 全程握着 GIL，翻页时同步渲小图会让画面一顿。
"""

from __future__ import annotations

import json

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QMenu, QWidget

from core.region import Region
from ui import theme

try:  # PyMuPDF 1.24 之后推荐 pymupdf 包名，旧的 fitz 仍在
    import pymupdf
except ImportError:  # pragma: no cover
    import fitz as pymupdf

TITLE_H = 26
GRIP = 14

#: 内容倍率：1× 就是「框住什么显示什么」，只放大不缩小（不会看到框外的东西）
MIN_ZOOM = 1.0
MAX_ZOOM = 16.0
ZOOM_STEP = 1.12

#: 多个窗口的集合存在这里（QSettings，全局共用，不跟项目走）
SETTINGS_KEY = "magnifier/windows"

#: 标记颜色：(显示名, 颜色值)。存十六进制，空串表示默认（跟随主题）
WINDOW_COLORS: tuple[tuple[str, str], ...] = (
    ("红", "#E5484D"),
    ("橙", "#F76B15"),
    ("黄", "#F5B400"),
    ("绿", "#30A46C"),
    ("蓝", "#0091FF"),
    ("紫", "#8E4EC6"),
    ("灰", "#6B7280"),
)


def _color_icon(value: str) -> QIcon:
    """菜单里的颜色小圆点。"""
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(theme.BORDER), 1))
    painter.setBrush(QColor(value))
    painter.drawEllipse(1, 1, 12, 12)
    painter.end()
    return QIcon(pixmap)


def _base_flags():
    """所有放大窗共用的窗口标志。

    ``WindowDoesNotAcceptFocus`` 很关键：放大窗是主窗口的从属小窗，
    默认点它会让 Windows 把「属主」主窗口激活（用户的 CAD 就被切回 QPDF 了）。
    """
    return (
        Qt.WindowType.Tool
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowDoesNotAcceptFocus
    )


def content_clip(base, zoom: float, anchor=(0.5, 0.5)):
    """把「框住的那块」按内容倍率收小，返回真正要渲染的取景框。

    ``base`` 是页面坐标 ``(x0, y0, x1, y1)``，``anchor`` 是锚点在框内的
    相对位置（0~1，鼠标指哪就以哪为中心）。倍率 ≥ 1 时结果一定落在
    ``base`` 里面，所以「框住什么看什么」的语义不会被内容缩放破坏。
    """
    if not base:
        return None
    x0, y0, x1, y1 = (float(v) for v in base)
    span_w = max(0.5, x1 - x0)
    span_h = max(0.5, y1 - y0)
    z = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom or MIN_ZOOM)))
    if z <= 1.0001:
        return (x0, y0, x1, y1)
    try:
        ax = float(anchor[0])
        ay = float(anchor[1])
    except (TypeError, ValueError, IndexError):
        ax = ay = 0.5
    # 锚点再夹一道：太靠边会让取景框越出原框，所以最多只能到 (0.5/z, 1-0.5/z)
    half = 0.5 / z
    ax = max(half, min(1.0 - half, ax))
    ay = max(half, min(1.0 - half, ay))
    cw = span_w / z
    ch = span_h / z
    cx = x0 + ax * span_w
    cy = y0 + ay * span_h
    return (cx - cw / 2.0, cy - ch / 2.0, cx + cw / 2.0, cy + ch / 2.0)


class _RenderWorker(QThread):
    """渲一页里的一小块，渲完把 QImage 递回去。"""

    done = Signal(int, object)
    failed = Signal(int)

    def __init__(
        self,
        path: str,
        page: int,
        rect: tuple,
        scale_x: float,
        scale_y: float,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._page = int(page)
        self._rect = tuple(rect)
        self._scale_x = max(0.02, float(scale_x))
        self._scale_y = max(0.02, float(scale_y))

    def run(self) -> None:  # noqa: D401 - Qt 线程入口
        image = None
        try:
            doc = pymupdf.open(self._path)
            try:
                if 0 <= self._page < doc.page_count:
                    page = doc[self._page]
                    clip = pymupdf.Rect(*self._rect)
                    # 横竖两个方向分开缩放：框是什么形状，窗口就是什么形状，图不变形也不留边
                    matrix = pymupdf.Matrix(self._scale_x, self._scale_y)
                    pix = page.get_pixmap(clip=clip, matrix=matrix, alpha=False)
                    image = QImage(
                        pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888
                    ).copy()
            finally:
                doc.close()
        except Exception:  # noqa: BLE001 - 放大窗坏了不该影响看图和标注
            image = None
        # 成功和失败都**必须**回一声：以前出错直接 return，外面永远在等这张图，
        # 窗口就一直停在「放大区域准备中…」。
        if image is None or image.isNull():
            self.failed.emit(self._page)
        else:
            self.done.emit(self._page, image)


class _MinimizeChip(QWidget):
    """放大窗最小化后的浮条：点一下恢复，右键可以关掉这个放大窗。"""

    def __init__(self, window: "MagnifierWindow") -> None:
        super().__init__(
            None,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self._window = window
        self._hover = False
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(164, 30)

    def place(self, slot: int) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        area = screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)
        x = area.right() - self.width() - 12
        y = area.bottom() - self.height() - 12 - max(0, int(slot)) * (self.height() + 6)
        self.move(x, y)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor(self._window.accent or theme.ACCENT)
        card = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        painter.setPen(QPen(accent, 1.3))
        painter.setBrush(QColor("#ffffff") if not self._hover else QColor("#f3f7ff"))
        painter.drawRoundedRect(card, 14.0, 14.0)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QRectF(13.0, 11.0, 8.0, 8.0))

        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 0.5))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT)))
        number = str(self._window.win_id or "").lstrip("m") or "1"
        painter.drawText(
            QRect(28, 0, self.width() - 38, self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"放大窗 {number}",
        )
        # 右侧一个小「展开」箭头，告诉用户点一下会回去
        painter.setPen(QPen(accent, 1.4))
        cx = self.width() - 18
        cy = self.height() // 2
        painter.drawLine(cx - 3, cy - 3, cx, cy)
        painter.drawLine(cx, cy, cx + 3, cy - 3)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._window.restore_from_minimize()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            menu = QMenu(self)
            menu.addAction("恢复放大窗", self._window.restore_from_minimize)
            menu.addSeparator()
            menu.addAction("关闭这个放大窗", self._window._close_from_menu)
            menu.exec(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)


class MagnifierWindow(QWidget):
    """一个放大窗：拖动移动、四边/右下角拉大小、靠边自动贴合、可缩放内容。"""

    moved = Signal(object)  # QRect，拖完/拉完通知外面存起来
    closed = Signal()
    #: 锁定 / 置顶 / 内容倍率这类不该丢的状态变了（外面防抖存盘）
    state_changed = Signal()
    #: 右键点了「设置本窗放大区域…」/「新建一个放大窗」/「关闭全部」
    pick_region_requested = Signal(object)
    new_window_requested = Signal(object)
    close_all_requested = Signal()
    #: 窗口被点了一下（外面用它记住「当前放大窗」）
    activated = Signal(object)
    #: 鼠标侧键：-1 = 上一页，+1 = 下一页（由主窗口去翻页）
    page_step_requested = Signal(int)

    MIN_W = 40   # 小到只剩个点也行，上限交给屏幕（能铺满半屏、整屏）
    MIN_H = 34
    SNAP = 14

    def __init__(self, parent=None, window_id: str = "m1") -> None:
        # 默认只做 Qt.Tool + 无边框：作为主窗口的子窗口，它只会浮在雀雀 PDF 之上；
        # 右键勾了「始终显示在所有窗口之上」才加 WindowStaysOnTopHint。
        # WindowDoesNotAcceptFocus + WA_ShowWithoutActivating：在别的软件里点它、拖它、
        # 滚它，都不会把雀雀 PDF 主窗口激活/带到前台。
        super().__init__(parent, _base_flags())
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.win_id = window_id
        self.setObjectName("magnifierWindow")
        self.setMinimumSize(self.MIN_W, self.MIN_H)
        self.resize(320, 240)
        self.setMouseTracking(True)
        self._path = ""
        self._page = 0
        self._rect: tuple | None = None
        #: 页面比例区域（存盘用；真正渲染用按当前页换算出来的 _rect）
        self.region: Region | None = None
        self._image: QImage | None = None
        self._worker: _RenderWorker | None = None
        self._pending = False
        #: 拖动内容时用的「整块预览图」：拖动期间只平移它，不反复让 MuPDF 渲图
        self._overview: QImage | None = None
        self._overview_key: tuple | None = None
        self._overview_worker: _RenderWorker | None = None
        self._overview_pending = False
        #: 上一次渲染是不是直接失败了（窗口上要显示一句人话，而不是一直「准备中」）
        self._failed = False
        self._drag_from: QPoint | None = None
        self._drag_origin: QPoint | None = None
        self._resize_edge = ""
        self._press_pos: QPoint | None = None
        self._targets: list[QRect] = []
        self._hover_close = False
        self._hover_shrink = False
        # 窗口拉大拉小 = 看更多/更少页面（取景框语义）；滚轮改窗口大小，侧键+滚轮改内容倍率
        self._locked = False
        self._topmost = False
        self._content_zoom = MIN_ZOOM
        self._anchor = (0.5, 0.5)
        #: 窗口标记色（None = 跟随主题）
        self._accent: str | None = None
        #: 按住 Ctrl 左键拖动内容：拖动期间只移动缓存画面，松手才重渲一次
        self._pan_from: QPoint | None = None
        self._pan_anchor = (0.5, 0.5)
        self._pan_preview_anchor = (0.5, 0.5)
        #: 用户想不想看见它（关标签/程序里临时藏起来不算，重启后按这个恢复）
        self._want_visible = False
        #: 最小化到屏幕右下角的小浮条；不是把窗口改小
        self._minimized = False
        self._minimize_chip: _MinimizeChip | None = None
        #: 当前文档的全部标注（按页过滤后画在页面图上）；resolver 负责把数字序号变成文字
        self._annotations: tuple = ()
        self._annotation_text = None
        self._annotations_visible = True
        #: 当前页标注的排版缓存：拖动时每帧都要重画，不能每帧现算字体度量
        self._annotation_cache: list = []
        self._annotation_cache_page = -2
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(90)
        self._refresh_timer.timeout.connect(lambda: self.refresh(self._page))

    # ------------------------------------------------------------------ 对外

    def set_document(self, path: str) -> None:
        if path != self._path:
            self._path = path or ""
            self._image = None
            self._failed = False
            self._invalidate_overview()
            self.update()

    def set_region(self, rect: tuple | None) -> None:
        """``rect`` 是页面可见坐标（pt）的 (x0, y0, x1, y1)。

        框里是什么就显示什么：整块刚好铺满窗口（横竖各自缩放，不变形、不留白边）。
        """
        self._rect = tuple(rect) if rect else None
        self._image = None
        self._failed = False
        self._invalidate_overview()
        self.refresh(self._page)
        self.update()

    def reset_content_zoom(self) -> None:
        """回到 1×：框住的那块整块显示，不出框。"""
        if self._content_zoom <= MIN_ZOOM + 1e-6:
            return
        self._content_zoom = MIN_ZOOM
        self._anchor = (0.5, 0.5)
        self.refresh(self._page)
        self.state_changed.emit()
        self.update()

    def _body_size(self) -> tuple[int, int]:
        return max(8, self.width() - 2), max(8, self.height() - TITLE_H - 1)

    def _body_rect(self) -> QRect:
        return QRect(1, TITLE_H, self.width() - 2, self.height() - TITLE_H - 1)

    def _sample_rect(self) -> tuple | None:
        """当前该渲的取景框：框住的那块按内容倍率收小。"""
        return content_clip(self._rect, self._content_zoom, self._anchor)

    def _scales(self) -> tuple[float, float]:
        """把取景框铺满窗口需要的横竖缩放（现算，不缓存——窗口大小随时会变）。"""
        rect = self._sample_rect()
        if rect is None:
            return 4.0, 4.0
        x0, y0, x1, y1 = rect
        body_w, body_h = self._body_size()
        span_w = max(0.5, x1 - x0)
        span_h = max(0.5, y1 - y0)
        return max(0.02, body_w / span_w), max(0.02, body_h / span_h)

    def _clip(self) -> tuple | None:
        return self._sample_rect()

    def has_region(self) -> bool:
        return self._rect is not None

    @property
    def page(self) -> int:
        """当前显示的是第几页（0 基）。"""
        return int(self._page)

    @property
    def document_path(self) -> str:
        return self._path

    @property
    def content_zoom(self) -> float:
        return float(self._content_zoom)

    @property
    def accent(self) -> str:
        return self._accent or ""

    @property
    def panning(self) -> bool:
        return self._pan_from is not None

    def set_accent(self, value: str) -> None:
        """窗口标记色；空串/无效值回到默认主题色。"""
        value = (value or "").strip()
        if value and not QColor(value).isValid():
            value = ""
        if value == (self._accent or ""):
            return
        self._accent = value or None
        self.state_changed.emit()
        self.update()

    def set_annotations(self, annotations, resolver=None, visible: bool = True) -> None:
        """接入当前文档的标注。

        放大窗拿到的只是页面坐标里的标注（pt），绘制时按当前取景框换算成窗口
        像素——和主画布一样，标注跟着页面一起缩放/平移，而不是贴在窗口上。
        """
        self._annotations = tuple(annotations or ())
        self._annotation_text = resolver
        self._annotations_visible = bool(visible)
        self._annotation_cache = []
        self._annotation_cache_page = -2
        self.update()

    def _annotation_font(self, style) -> QFont:
        font = QFont(style.font_family)
        font.setPointSizeF(max(1.0, float(style.font_size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        return font

    def _annotation_layout(self, ann, text: str):
        """复刻主画布 AnnotationItem 的排版：数字以锚点为中心，文字按对齐方式摆放。"""
        metrics = QFontMetricsF(self._annotation_font(ann.style))
        lines = text.split("\n") or [""]
        width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        width = max(width, metrics.horizontalAdvance("M") * 0.6)
        height = metrics.lineSpacing() * len(lines)
        align = getattr(ann.style, "align", "left")
        if ann.is_number:
            rect = QRectF(-width / 2.0, -height / 2.0, width, height)
        elif align == "center":
            rect = QRectF(-width / 2.0, 0.0, width, height)
        elif align == "right":
            rect = QRectF(-width, 0.0, width, height)
        else:
            rect = QRectF(0.0, 0.0, width, height)
        if ann.is_number:
            flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        else:
            flags = Qt.AlignmentFlag.AlignTop
            if align == "center":
                flags |= Qt.AlignmentFlag.AlignHCenter
            elif align == "right":
                flags |= Qt.AlignmentFlag.AlignRight
            else:
                flags |= Qt.AlignmentFlag.AlignLeft
        return rect, flags

    def _build_annotation_cache(self) -> None:
        """把当前页标注的文字、字体和页面坐标预排好，拖动时只做平移裁切。"""
        items = []
        for ann in self._annotations:
            if getattr(ann, "hidden", False) or int(getattr(ann, "page", -1)) != self._page:
                continue
            text = str(getattr(ann, "content", "") or "")
            if ann.is_number and self._annotation_text is not None:
                try:
                    text = str(self._annotation_text(ann))
                except Exception:  # noqa: BLE001 - 序号解析失败不该让整个放大窗画不出来
                    text = str(getattr(ann, "content", "") or "")
            if not text:
                continue
            local, flags = self._annotation_layout(ann, text)
            rect = local.translated(float(ann.x), float(ann.y))
            color = QColor(getattr(ann.style, "color", "#000000"))
            if not color.isValid():
                color = QColor("#000000")
            color.setAlphaF(
                max(0.0, min(1.0, float(getattr(ann.style, "opacity", 100)) / 100.0))
            )
            items.append((rect, flags, text, color, self._annotation_font(ann.style)))
        self._annotation_cache = items
        self._annotation_cache_page = self._page

    def _paint_annotations(self, painter: QPainter, body: QRect, use_overview: bool) -> None:
        """把当前页标注叠在页面图上；拖动预览时用预览锚点，标注和底图一起动。"""
        if (
            not self._annotations_visible
            or not self._annotations
            or self._rect is None
        ):
            return
        clip = content_clip(self._rect, self._content_zoom, self._anchor)
        if use_overview and self._pan_from is not None:
            clip = content_clip(self._rect, self._content_zoom, self._pan_preview_anchor)
        if clip is None:
            return
        if self._annotation_cache_page != self._page:
            self._build_annotation_cache()
        x0, y0, x1, y1 = (float(v) for v in clip)
        span_w = max(0.5, x1 - x0)
        span_h = max(0.5, y1 - y0)
        scale_x = body.width() / span_w
        scale_y = body.height() / span_h
        view = QRectF(x0, y0, span_w, span_h)

        painter.save()
        painter.setClipRect(body)
        painter.translate(body.left(), body.top())
        painter.scale(scale_x, scale_y)
        painter.translate(-x0, -y0)
        for rect, flags, text, color, font in self._annotation_cache:
            if not view.intersects(rect):
                continue
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(rect, flags, text)
        painter.restore()

    def _pan_by(self, dx: int, dy: int) -> bool:
        """按屏幕像素位移更新拖动预览（不动锚点、不触发渲染，松手才落定）。"""
        if self._rect is None or self._content_zoom <= MIN_ZOOM + 1e-6:
            return False
        body = self._body_rect()
        zoom = self._content_zoom
        half = 0.5 / zoom
        ax = self._pan_anchor[0] - float(dx) / (max(1.0, body.width()) * zoom)
        ay = self._pan_anchor[1] - float(dy) / (max(1.0, body.height()) * zoom)
        ax = max(half, min(1.0 - half, ax))
        ay = max(half, min(1.0 - half, ay))
        if (
            abs(ax - self._pan_preview_anchor[0]) < 1e-6
            and abs(ay - self._pan_preview_anchor[1]) < 1e-6
        ):
            return False
        self._pan_preview_anchor = (ax, ay)
        self.update()
        return True

    # ------------------------------------------------------------------ 拖动预览

    def _overview_key_now(self) -> tuple | None:
        if self._rect is None:
            return None
        body_w, body_h = self._body_size()
        return (
            self._path,
            self._page,
            tuple(round(float(v), 3) for v in self._rect),
            body_w,
            body_h,
        )

    def _invalidate_overview(self) -> None:
        self._overview = None
        self._overview_key = None

    def _start_overview(self) -> None:
        """渲一张「整块区域铺满窗口」的预览图；拖动期间只平移/裁它，不再让 MuPDF 出图。"""
        key = self._overview_key_now()
        if key is None or not self._path:
            return
        if self._overview is not None and self._overview_key == key:
            return
        # 正图还在渲时不另开线程抢同一份 PDF；这一下拖动先用现有画面，松手再补
        if self._worker is not None or self._overview_worker is not None:
            self._overview_pending = True
            return
        rect = self._rect
        body_w, body_h = self._body_size()
        span_w = max(0.5, rect[2] - rect[0])
        span_h = max(0.5, rect[3] - rect[1])
        worker = _RenderWorker(
            self._path,
            self._page,
            rect,
            body_w / span_w,
            body_h / span_h,
            self,
        )
        worker.done.connect(lambda _page, image, k=key: self._on_overview_ready(k, image))
        worker.failed.connect(lambda _page: self._on_overview_failed())
        worker.finished.connect(lambda w=worker: self._forget_overview_worker(w))
        worker.finished.connect(worker.deleteLater)
        self._overview_worker = worker
        worker.start()

    def _forget_overview_worker(self, worker) -> None:
        if self._overview_worker is worker:
            self._overview_worker = None

    def _on_overview_ready(self, key, image) -> None:
        if isinstance(image, QImage) and not image.isNull() and key == self._overview_key_now():
            self._overview = image
            self._overview_key = key
            self.update()
        if self._overview_pending:
            self._overview_pending = False
            self._start_overview()

    def _on_overview_failed(self) -> None:
        self._overview_pending = False

    @property
    def topmost(self) -> bool:
        return bool(self._topmost)

    def set_snap_targets(self, rects) -> None:
        """可以贴上去的几块地方（全局坐标）：属性栏、左侧栏、画布……"""
        self._targets = [QRect(r) for r in rects if r is not None and not QRect(r).isNull()]

    def place_default(self) -> None:
        """没存过位置时的落点：父窗口右上角往里一点。"""
        host = self.parentWidget()
        if host is not None:
            geo = host.frameGeometry()
            self.move(geo.right() - self.width() - 28, geo.top() + 72)

    def place_cascade(self, index: int) -> None:
        """多个放大窗默认阶梯错开，不至于完全叠在一起。"""
        area = self._screen_rect()
        width = min(420, max(240, area.width() // 4))
        height = min(320, max(180, area.height() // 4))
        step = 28 * max(0, int(index))
        x = area.right() - width - 28 - step
        y = area.top() + 72 + step
        x = max(area.left(), x)
        y = max(area.top(), y)
        self.setGeometry(x, y, width, height)

    # ------------------------------------------------------------------ 置顶 / 可见

    def _flags(self):
        flags = _base_flags()
        if self._topmost:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        return flags

    def set_topmost(self, on: bool) -> None:
        """勾上就压在所有程序的窗口之上；取消就只浮在雀雀 PDF 上。"""
        on = bool(on)
        if on == self._topmost:
            return
        self._topmost = on
        # 改 windowFlags 会先隐藏窗口，所以位置、图片、可见性都得自己接住
        geometry = self.geometry()
        was_visible = self.isVisible()
        self.setWindowFlags(self._flags())
        self.setGeometry(geometry)
        if was_visible:
            self.show()
            self.raise_()
        self.state_changed.emit()
        self.update()

    def set_wanted_visible(self, on: bool) -> None:
        """按「用户想不想看见」来显示；没有区域时不弹空窗。"""
        self._want_visible = bool(on)
        if on:
            if self._minimized:
                self.restore_from_minimize()
                return
            # 有区域就弹（页面坐标可能还没换算好，等下马上会被 set_region 补上）
            if self.region is not None:
                self.show()
                self.raise_()
        else:
            self.hide()
            if self._minimize_chip is not None:
                self._minimize_chip.hide()

    @property
    def want_visible(self) -> bool:
        return bool(self._want_visible)

    @property
    def is_minimized(self) -> bool:
        return bool(self._minimized)

    def shutdown(self) -> None:
        """窗口关掉前叫一下：把还在渲的线程（正图和拖动预览）都等回来。"""
        self._pending = False
        self._overview_pending = False
        self._destroy_minimize_chip()
        workers = [self._worker, self._overview_worker]
        self._worker = None
        self._overview_worker = None
        for worker in workers:
            if worker is None:
                continue
            try:
                if worker.isRunning():
                    worker.wait(3000)
            except RuntimeError:  # 线程对象已经被 deleteLater 收掉了，没什么可等的
                pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        # 放大窗是主窗口的子窗口，程序退出时它会跟主窗口一起被销毁。
        # 那会儿要是还有渲染线程在跑，Qt 直接 "QThread: Destroyed while
        # thread is still running" 把进程带走——所以关窗前先把它收回来。
        self._destroy_minimize_chip()
        self.shutdown()
        super().closeEvent(event)

    def refresh(self, page: int) -> None:
        self._page = int(page)
        if not self._path or self._rect is None or not self.isVisible():
            return
        # 判「_worker 还在」而不是「线程还在跑」：线程可能已经跑完，但 finished
        # 信号还在事件队列里没轮到。这时候起新线程，就会有两份线程同时 open 这个
        # 1.2 GB 的文件。
        if self._worker is not None:
            self._pending = True
            return
        self._start(page)

    # ------------------------------------------------------------------ 渲染

    def _forget_worker(self, worker) -> None:
        if self._worker is worker:
            self._worker = None
            # 拖动时正图占着线程，预览图排了队：等它空出来再补
            if self._overview_pending and self._overview_worker is None:
                self._overview_pending = False
                self._start_overview()

    def _start(self, page: int) -> None:
        self._pending = False
        self._failed = False
        scale_x, scale_y = self._scales()
        worker = _RenderWorker(self._path, page, self._clip(), scale_x, scale_y, self)
        worker.done.connect(self._on_rendered)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda w=worker: self._forget_worker(w))
        worker.finished.connect(worker.deleteLater)
        self._worker = worker
        worker.start()

    def _on_failed(self, page: int) -> None:
        """这一块渲不出来（页号越界、文件被换掉、图太大内存不够…）。

        关键是**别把 _pending 留在 True**：那之后每次 refresh 都以为「正在渲」，
        窗口就永远停在「放大区域准备中…」，翻页也不会重试。
        """
        self._pending = False
        if page == self._page:
            self._image = None
            self._failed = True
            self.update()

    def _on_rendered(self, page: int, image) -> None:
        if page == self._page:
            self._image = image if isinstance(image, QImage) and not image.isNull() else None
            self.update()
        # 渲染期间又换了区域、改了倍率或页码（同一页重框最常见）：用最新的一次再渲一遍。
        # 不补这一步的话，重框同一块区域时画面会一直停在上一次的样子。
        if self._pending:
            self._start(self._page)

    # ------------------------------------------------------------------ 画

    def _close_rect(self) -> QRect:
        return QRect(self.width() - TITLE_H, 0, TITLE_H - 2, TITLE_H)

    def _shrink_rect(self) -> QRect:
        return QRect(self.width() - 2 * TITLE_H + 2, 0, TITLE_H - 2, TITLE_H)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 一整块白底 + 一道细边框，里面不再套别的框；标了颜色就用标记色描边、标题条淡淡带色
        accent = QColor(self._accent) if self._accent else None
        outer = self.rect().adjusted(0, 0, -1, -1)
        painter.setPen(QPen(accent or QColor(theme.BORDER), 1.2 if accent else 1))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(outer)
        if accent is not None:
            tint = QColor(accent)
            tint.setAlpha(30)
            painter.fillRect(QRect(1, 1, self.width() - 2, TITLE_H - 1), tint)

        # 顶部一行很淡的说明，字号比正文小一号
        font = QFont(self.font())
        font.setPointSizeF(max(8.0, font.pointSizeF() - 1.0))
        painter.setFont(font)
        text_left = 8
        if accent is not None:
            # 左边一个小圆点，远看也能分辨是哪个窗口
            painter.setPen(QPen(accent.darker(110), 1))
            painter.setBrush(accent)
            painter.drawEllipse(QRect(8, TITLE_H // 2 - 3, 6, 6))
            painter.setPen(QPen(accent.darker(135)))
            text_left = 18
        else:
            painter.setPen(QPen(QColor(theme.FAINT)))
        label = f"第 {self._page + 1} 页 · 内容 {self._content_zoom:.1f}×"
        if self._topmost:
            label += " · 置顶"
        if self._locked:
            label += " · 已锁定"
        painter.drawText(
            QRect(text_left, 0, self.width() - TITLE_H - text_left, TITLE_H),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            label,
        )

        # 右下角画三道小斜线，明示「这里能拉大小」（灰色，不抢眼）
        painter.setPen(QPen(QColor(theme.FAINT), 1))
        right = self.width() - 4
        bottom = self.height() - 4
        for offset in (0, 4, 8):
            painter.drawLine(right - offset - 6, bottom, right, bottom - offset - 6)

        # 最小化按钮：在关闭叉左边，点一下收起到屏幕右下角的小浮条
        shrink = self._shrink_rect()
        if self._hover_shrink:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#eceef2"))
            painter.drawRoundedRect(shrink.adjusted(0, 3, -3, -3), 3, 3)
        painter.setPen(QPen(QColor(theme.MUTED), 1.2))
        sy = shrink.center().y()
        painter.drawLine(shrink.center().x() - 3, sy, shrink.center().x() + 3, sy)

        # 关闭叉：就在窗口里，鼠标移上去才显出来
        close = self._close_rect()
        if self._hover_close:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#eceef2"))
            painter.drawRoundedRect(close.adjusted(0, 3, -3, -3), 3, 3)
        painter.setPen(QPen(QColor(theme.MUTED), 1.2))
        cx, cy, r = close.center().x(), close.center().y(), 3.5
        painter.drawLine(int(cx - r), int(cy - r), int(cx + r), int(cy + r))
        painter.drawLine(int(cx - r), int(cy + r), int(cx + r), int(cy - r))

        # 图：正常情况是清晰正图；拖动内容时改画「整块预览图」的一块，跟着手走不卡
        body = self._body_rect()
        overview = self._overview
        use_overview = (
            overview is not None
            and self._overview_key == self._overview_key_now()
            and (self._pan_from is not None or self._image is None)
        )
        if use_overview:
            ax, ay = (
                self._pan_preview_anchor if self._pan_from is not None else self._anchor
            )
            zoom = max(MIN_ZOOM, self._content_zoom)
            ow = max(1, overview.width())
            oh = max(1, overview.height())
            src_w = ow / zoom
            src_h = oh / zoom
            src = QRectF(
                ax * ow - src_w / 2.0,
                ay * oh - src_h / 2.0,
                src_w,
                src_h,
            )
            painter.drawImage(
                QRectF(body.x(), body.y(), body.width(), body.height()),
                overview,
                src,
            )
        elif self._image is not None and not self._image.isNull():
            painter.drawPixmap(body, QPixmap.fromImage(self._image))
        else:
            painter.setPen(QPen(QColor(theme.FAINT)))
            painter.drawText(
                body,
                Qt.AlignmentFlag.AlignCenter,
                "这块渲不出来，换个区域或翻页试试" if self._failed else "放大区域准备中…",
            )

        # 标注叠在页面图上：底图怎么缩放/平移，标注就怎么跟着
        if use_overview or (self._image is not None and not self._image.isNull()):
            self._paint_annotations(painter, body, use_overview)

    # ------------------------------------------------------------------ 鼠标

    def _edge_at(self, pos: QPoint) -> str:
        # 10 px 的抓取边：无边框窗口只有 6 px 的时候，125% 缩放下几乎抓不住
        edge = 10
        edges = []
        if pos.y() >= self.height() - edge:
            edges.append("b")
        if pos.x() >= self.width() - edge:
            edges.append("r")
        if pos.y() <= edge - 4:
            edges.append("t")
        if pos.x() <= edge - 4:
            edges.append("l")
        return "".join(edges)

    def _side_button_step(self, button) -> int:
        """侧键映射（按用户鼠标的物理位置）：靠后那颗下一页，靠前那颗上一页。"""
        if button == Qt.MouseButton.ExtraButton1:
            return 1
        if button == Qt.MouseButton.ExtraButton2:
            return -1
        return 0

    def _can_pan(self, event, pos: QPoint) -> bool:
        """Ctrl+左键拖内容：锁不锁定都认（锁的是窗口，不是看的那块内容）。"""
        return bool(
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and self._content_zoom > MIN_ZOOM + 1e-6
            and self._body_rect().contains(pos)
        )

    def _begin_pan(self, pos: QPoint) -> None:
        self._pan_from = pos
        self._pan_anchor = self._anchor
        self._pan_preview_anchor = self._anchor
        # 先要一张「整块区域」的预览图；拖动期间只平移它，不再让 MuPDF 出图
        self._start_overview()
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 鼠标侧键：在放大窗上直接翻页（上一页 / 下一页），主窗口不会被激活
        step = self._side_button_step(event.button())
        if step:
            self.activated.emit(self)
            self.page_step_requested.emit(step)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        self.activated.emit(self)
        pos = event.position().toPoint()
        if self._close_rect().contains(pos):
            self._close_from_menu()
            return
        if self._shrink_rect().contains(pos):
            self._shrink_window()
            return
        if self._locked:
            # 锁定后窗口不动，但 Ctrl+拖动仍要能移内容（这里必须排在 locked 判断前）
            if self._can_pan(event, pos):
                self._begin_pan(pos)
                event.accept()
                return
            return super().mousePressEvent(event)
        self._press_pos = pos
        edge = self._edge_at(pos)
        if edge and not self._close_rect().contains(pos):
            self._resize_edge = edge
            return
        # 按住 Ctrl 拖动内容：锁不锁定都行（锁定后窗口不动，正好用它移看的那块）
        if self._can_pan(event, pos):
            self._begin_pan(pos)
            event.accept()
            return
        if pos.y() <= TITLE_H:
            self._drag_from = event.globalPosition().toPoint()
            self._drag_origin = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position().toPoint()
        hover = self._close_rect().contains(pos)
        hover_shrink = self._shrink_rect().contains(pos)
        if hover != self._hover_close or hover_shrink != self._hover_shrink:
            self._hover_close = hover
            self._hover_shrink = hover_shrink
            self.update()

        # 内容拖动优先：锁定状态下也要能拖（锁的是窗口，不是看的那块内容）
        if self._pan_from is not None:
            delta = pos - self._pan_from
            self._pan_by(delta.x(), delta.y())
            return

        if self._locked:
            # 锁定时只让关闭叉亮起来 / Ctrl 可拖内容时给手型，不动位置也不改大小
            can_pan = (
                bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                and self._content_zoom > MIN_ZOOM + 1e-6
                and self._body_rect().contains(pos)
            )
            self.setCursor(
                Qt.CursorShape.OpenHandCursor if can_pan else Qt.CursorShape.ArrowCursor
            )
            super().mouseMoveEvent(event)
            return

        if self._resize_edge:
            self._resize(event.globalPosition().toPoint())
            return
        if self._drag_from is not None and self._drag_origin is not None:
            delta = event.globalPosition().toPoint() - self._drag_from
            self._move_snapped(self._drag_origin + delta)
            return

        edge = self._edge_at(pos)
        if (
            not edge
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and self._content_zoom > MIN_ZOOM + 1e-6
            and self._body_rect().contains(pos)
        ):
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            cursor = {
                "r": Qt.CursorShape.SizeHorCursor,
                "l": Qt.CursorShape.SizeHorCursor,
                "b": Qt.CursorShape.SizeVerCursor,
                "t": Qt.CursorShape.SizeVerCursor,
                "rb": Qt.CursorShape.SizeFDiagCursor,
                "lb": Qt.CursorShape.SizeBDiagCursor,
                "rt": Qt.CursorShape.SizeBDiagCursor,
                "lt": Qt.CursorShape.SizeFDiagCursor,
            }.get(
                edge,
                Qt.CursorShape.SizeAllCursor
                if pos.y() <= TITLE_H
                else Qt.CursorShape.ArrowCursor,
            )
            self.setCursor(QCursor(cursor))
        super().mouseMoveEvent(event)

    def _move_snapped(self, top_left: QPoint) -> None:
        rect = QRect(top_left, self.size())
        rect = self._apply_snap(rect, resize=False)
        self.move(rect.topLeft())

    def _resize(self, global_pos: QPoint) -> None:
        rect = QRect(self.pos(), self.size())
        if "r" in self._resize_edge:
            rect.setRight(max(rect.left() + self.MIN_W, global_pos.x()))
        if "b" in self._resize_edge:
            rect.setBottom(max(rect.top() + self.MIN_H, global_pos.y()))
        if "l" in self._resize_edge:
            rect.setLeft(min(rect.right() - self.MIN_W, global_pos.x()))
        if "t" in self._resize_edge:
            rect.setTop(min(rect.bottom() - self.MIN_H, global_pos.y()))
        self.setGeometry(rect)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._commit_pan():
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        changed = self._drag_from is not None or bool(self._resize_edge)
        self._drag_from = None
        self._drag_origin = None
        self._resize_edge = ""
        if changed:
            self.moved.emit(self.geometry())
        super().mouseReleaseEvent(event)

    def _commit_pan(self) -> bool:
        """松手：把拖动预览的锚点落定，并按新位置重渲一张清晰图。"""
        if self._pan_from is None:
            return False
        self._anchor = self._pan_preview_anchor
        self._pan_from = None
        # 拖动期间一直是预览图；先清掉旧正图，画出预览，等清晰图回来再替换
        self._image = None
        self.refresh(self._page)
        self.state_changed.emit()
        return True

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self._locked:
            return super().mouseDoubleClickEvent(event)
        if event.position().y() <= TITLE_H:
            # 双击标题条：大/小之间切，但「小」是能用的尺寸，不是最小尺寸
            if self.width() < 480:
                self.resize(640, 480)
            else:
                self.resize(240, 180)
            self.moved.emit(self.geometry())
            return
        super().mouseDoubleClickEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        # 开窗/重新显示时补渲一次：窗口不可见的那段时间 refresh() 是空转的
        if self._rect is not None:
            self.refresh(self._page)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        # 窗口大小变了，取景框里的内容得重新铺满；等手停下来再重渲（拖边时会连着来几十个）
        self._invalidate_overview()
        if self._rect is not None and self.isVisible():
            self._refresh_timer.start()

    def wheelEvent(self, event) -> None:  # noqa: N802
        mods = event.modifiers()
        # Ctrl+滚轮：缩放内容，窗口大小不动。
        # 放在 _locked 判断前面：锁的是位置和大小，内容倍率照样可以调。
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._zoom_content(event)
            event.accept()
            return
        if self._locked:
            return super().wheelEvent(event)
        step = 1.08 if event.angleDelta().y() > 0 else 1 / 1.08
        if mods & Qt.KeyboardModifier.ShiftModifier:
            # Shift+滚轮：只改宽度
            self.resize(max(self.MIN_W, int(self.width() * step)), self.height())
        else:
            # 普通滚轮：整体放大/缩小窗口（框里的内容跟着变大）
            self.resize(
                max(self.MIN_W, int(self.width() * step)),
                max(self.MIN_H, int(self.height() * step)),
            )
        self.moved.emit(self.geometry())

    def _zoom_content(self, event) -> None:
        """按住侧键滚轮：窗口不动，改取景框大小（1×–16×，不出框）。"""
        delta = event.angleDelta().y() or event.angleDelta().x()
        steps = delta / 120.0
        if not steps:
            return
        zoom = self._content_zoom * (ZOOM_STEP ** steps)
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._content_zoom) < 1e-4:
            return
        body = self._body_rect()
        pos = event.position()
        # 鼠标指着哪儿，就以哪儿为中心放大（和主画布 Ctrl+滚轮的手感一致）
        ax = (pos.x() - body.left()) / max(1.0, float(body.width()))
        ay = (pos.y() - body.top()) / max(1.0, float(body.height()))
        self._content_zoom = zoom
        self._anchor = (max(0.0, min(1.0, ax)), max(0.0, min(1.0, ay)))
        self.refresh(self._page)
        self.state_changed.emit()
        self.update()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction(
            f"内容 {self._content_zoom:.1f}× · 框住的那块铺满窗口", lambda: None
        ).setEnabled(False)
        hint = menu.addAction(
            "侧键上一页/下一页 · 普通滚轮改窗口 · Ctrl+滚轮缩放内容 · Ctrl+拖动移内容"
        )
        hint.setEnabled(False)
        menu.addSeparator()
        pick = menu.addAction("设置本窗放大区域…")
        pick.triggered.connect(lambda: self.pick_region_requested.emit(self))
        more = menu.addAction("新建一个放大窗（复制这个区域）")
        more.triggered.connect(lambda: self.new_window_requested.emit(self))
        colors = menu.addMenu("标记窗口颜色")
        default = colors.addAction("默认")
        default.setCheckable(True)
        default.setChecked(self._accent is None)
        default.triggered.connect(self._clear_accent)
        for label, value in WINDOW_COLORS:
            action = colors.addAction(_color_icon(value), label)
            action.setCheckable(True)
            action.setChecked(self._accent == value)
            action.triggered.connect(lambda _checked=False, v=value: self.set_accent(v))
        menu.addSeparator()
        topmost = menu.addAction("始终显示在所有窗口之上")
        topmost.setCheckable(True)
        topmost.setChecked(self._topmost)
        topmost.triggered.connect(lambda checked: self.set_topmost(bool(checked)))
        lock = menu.addAction("锁定位置和大小")
        lock.setCheckable(True)
        lock.setChecked(self._locked)
        lock.triggered.connect(lambda checked: self._set_locked(bool(checked)))
        reset = menu.addAction("重置内容缩放（1.0×）")
        reset.setEnabled(self._content_zoom > MIN_ZOOM + 1e-6)
        reset.triggered.connect(self.reset_content_zoom)
        menu.addAction("回到右上角", self._back_to_corner)
        menu.addAction("默认大小（320×240）", lambda: self.resize(320, 240))
        menu.addAction("半屏大小", self._half_screen)
        menu.addAction("整屏大小", self._full_screen)
        menu.addSeparator()
        menu.addAction("关闭这个放大窗", self._close_from_menu)
        menu.addAction("关闭全部放大窗", lambda: self.close_all_requested.emit())
        menu.exec(event.globalPos())

    def _clear_accent(self, *_args) -> None:
        self.set_accent("")

    def _set_locked(self, locked: bool) -> None:
        """锁住位置和大小；换页照样刷新内容，只是不再跟着手走。"""
        locked = bool(locked)
        if locked == self._locked:
            return
        self._locked = locked
        self._drag_from = None
        self._drag_origin = None
        self._resize_edge = ""
        self._pan_from = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.state_changed.emit()
        self.update()

    def _shrink_window(self) -> None:
        """兼容旧调用：标题栏的「−」现在是最小化，不再改窗口尺寸。"""
        self.minimize()

    def minimize(self) -> None:
        """收起放大窗：隐藏它，在屏幕右下角留一个小浮条，点浮条恢复。"""
        if self._minimized:
            return
        self._minimized = True
        self._want_visible = False
        self._drag_from = None
        self._drag_origin = None
        self._resize_edge = ""
        self._pan_from = None
        self.hide()
        self._ensure_minimize_chip()
        self.state_changed.emit()

    def restore_from_minimize(self) -> None:
        """从右下角小浮条恢复原来的位置、大小和内容。"""
        if not self._minimized:
            return
        self._minimized = False
        self._want_visible = True
        self._destroy_minimize_chip()
        if self.region is not None:
            self.show()
            self.raise_()
            self.refresh(self._page)
        self.state_changed.emit()

    def _ensure_minimize_chip(self) -> None:
        if self._minimize_chip is None:
            self._minimize_chip = _MinimizeChip(self)
        self._minimize_chip.place(0)
        self._minimize_chip.show()
        self._minimize_chip.raise_()

    def place_minimize_chip(self, slot: int) -> None:
        if self._minimize_chip is not None:
            self._minimize_chip.place(slot)

    def _destroy_minimize_chip(self) -> None:
        chip, self._minimize_chip = self._minimize_chip, None
        if chip is not None:
            try:
                chip.hide()
                chip.deleteLater()
            except RuntimeError:
                pass

    def _close_from_menu(self) -> None:
        self._want_visible = False
        self._destroy_minimize_chip()
        self.hide()
        self.closed.emit()

    def _back_to_corner(self) -> None:
        self.place_default()
        self.moved.emit(self.geometry())

    def _screen_rect(self) -> QRect:
        screen = self.screen()
        if screen is not None:
            return screen.availableGeometry()
        host = self.parentWidget()
        return host.rect() if host is not None else QRect(0, 0, 1280, 800)

    def _resize_to(self, width: int, height: int) -> None:
        self.resize(max(self.MIN_W, int(width)), max(self.MIN_H, int(height)))
        self.moved.emit(self.geometry())

    def _half_screen(self) -> None:
        """半屏：屏幕右边一半（宽够看图纸，又不会把整块盖住）。"""
        area = self._screen_rect()
        self._resize_to(area.width() // 2, area.height())
        self.move(area.left() + area.width() // 2, area.top())
        self.moved.emit(self.geometry())

    def _full_screen(self) -> None:
        area = self._screen_rect()
        self.setGeometry(area)
        self.moved.emit(self.geometry())

    # ------------------------------------------------------------------ 贴合

    def _apply_snap(self, rect: QRect, resize: bool = False) -> QRect:
        """靠近时贴边；拖进属性栏这类「容器」里就贴满它的宽度（像 PS 的面板吸附）。"""
        if not self._targets:
            return rect
        tol = self.SNAP
        for target in self._targets:
            if not resize:
                if abs(rect.left() - target.left()) <= tol:
                    rect.moveLeft(target.left())
                if abs(rect.right() - target.right()) <= tol:
                    rect.moveRight(target.right())
                if abs(rect.top() - target.top()) <= tol:
                    rect.moveTop(target.top())
                if abs(rect.bottom() - target.bottom()) <= tol:
                    rect.moveBottom(target.bottom())
                # 拖进容器：宽度差不多够得着这一栏时才贴满（只放宽、不缩窄），
                # 想要一个小窗口时就不管它，尺寸完全由用户定
                if target.contains(rect.center()):
                    span = target.width() - 12
                    slack = max(60, int(target.width() * 0.4))
                    if rect.width() < span and span - rect.width() <= slack:
                        rect.setLeft(target.left() + 6)
                        rect.setRight(target.right() - 6)
                        if target.height() >= rect.height() + 12:
                            top = min(
                                max(rect.top(), target.top() + 6),
                                target.bottom() - rect.height() - 6,
                            )
                            rect.moveTop(top)
            else:
                # 拉伸时只允许「贴到边」，不把高度压回容器里——自由变形就是自由变形
                if abs(rect.right() - target.right()) <= tol:
                    rect.setRight(target.right())
                if abs(rect.bottom() - target.bottom()) <= tol:
                    rect.setBottom(target.bottom())
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            # 只在「装得下」的方向上夹一下，别把刚贴好的宽度又挤出去
            if rect.width() <= avail.width():
                if rect.left() < avail.left():
                    rect.moveLeft(avail.left())
                if rect.right() > avail.right():
                    rect.moveRight(avail.right())
            if rect.height() <= avail.height():
                if rect.top() < avail.top():
                    rect.moveTop(avail.top())
                if rect.bottom() > avail.bottom():
                    rect.moveBottom(avail.bottom())
        return rect


class MagnifierController(QObject):
    """放大窗集合：新建、显示/隐藏、按页刷新、存盘。

    窗口集合是**全局**的（QSettings，所有项目共用一套），所以切标签、
    换项目都不用重新建窗；旧项目里那份 ``magnifier_region`` 只做一次性迁移。
    """

    pick_region_requested = Signal(object)
    new_window_requested = Signal(object)
    close_all_requested = Signal()
    closed = Signal(object)
    changed = Signal()
    #: 放大窗侧键翻页：-1 上一页 / +1 下一页
    page_step_requested = Signal(int)

    def __init__(self, host: QWidget, settings=None, parent=None) -> None:
        super().__init__(parent or host)
        self._host = host
        self._settings = settings
        self.windows: list[MagnifierWindow] = []
        self.active: MagnifierWindow | None = None
        #: 当前文档：新建窗口时立刻喂给它，否则新窗「有区域但渲不出来」
        self._document_path = ""
        #: 标注跟着当前文档走；新建窗口时也要补上，不然新窗没有标注层
        self._annotations: tuple = ()
        self._annotation_resolver = None
        self._annotations_visible = True
        self._next_id = 1
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(300)
        self._save_timer.timeout.connect(self.save)

    # ------------------------------------------------------------------ 建窗

    def create(
        self,
        region: Region | None = None,
        geometry=None,
        visible: bool = False,
        zoom: float = MIN_ZOOM,
        anchor=(0.5, 0.5),
        locked: bool = False,
        topmost: bool = False,
        accent: str = "",
        win_id: str = "",
    ) -> MagnifierWindow:
        wid = win_id or f"m{self._next_id}"
        self._next_id += 1
        try:
            number = int(str(wid).lstrip("m"))
            self._next_id = max(self._next_id, number + 1)
        except (TypeError, ValueError):
            pass
        window = MagnifierWindow(self._host, window_id=wid)
        # 先接上当前文档，再设区域：create() 之后马上 set_region/refresh 才渲得出来
        window.set_document(self._document_path)
        window.set_annotations(
            self._annotations, self._annotation_resolver, self._annotations_visible
        )
        window.region = region
        window._content_zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom or MIN_ZOOM)))
        window._anchor = (float(anchor[0]), float(anchor[1]))
        window._locked = bool(locked)
        window._topmost = bool(topmost)
        window._accent = accent if accent and QColor(accent).isValid() else None
        window.setWindowFlags(window._flags())
        window._want_visible = bool(visible)
        if isinstance(geometry, QRect):
            window.setGeometry(geometry)
        elif isinstance(geometry, dict) and all(k in geometry for k in ("x", "y", "w", "h")):
            try:
                window.setGeometry(
                    int(geometry["x"]),
                    int(geometry["y"]),
                    max(MagnifierWindow.MIN_W, int(geometry["w"])),
                    max(MagnifierWindow.MIN_H, int(geometry["h"])),
                )
            except (TypeError, ValueError):
                window.place_cascade(len(self.windows))
        else:
            window.place_cascade(len(self.windows))
        self._wire(window)
        self.windows.append(window)
        self.active = window
        self.changed.emit()
        return window

    def create_copy(self, source: MagnifierWindow | None = None) -> MagnifierWindow:
        """在当前窗口基础上错开位置再开一个（区域、倍率跟着复制）。"""
        region = source.region if source is not None else None
        window = self.create(region=region)
        if source is not None:
            window._content_zoom = source.content_zoom
            window._anchor = source._anchor
            window._accent = source._accent
        window.place_cascade(len(self.windows) - 1)
        window._want_visible = True
        if window.region is not None:
            window.show()
            window.raise_()
        self.active = window
        self.save()
        return window

    def _wire(self, window: MagnifierWindow) -> None:
        window.moved.connect(lambda _rect, w=window: self._touch(w))
        window.state_changed.connect(lambda w=window: self._touch(w))
        window.activated.connect(self._on_activated)
        window.closed.connect(lambda w=window: self._on_closed(w))
        window.pick_region_requested.connect(self.pick_region_requested)
        window.new_window_requested.connect(self.new_window_requested)
        window.close_all_requested.connect(self.close_all_requested)
        window.page_step_requested.connect(self.page_step_requested)

    def _on_activated(self, window) -> None:
        self.active = window

    def _on_closed(self, window) -> None:
        # 关闭是「真的不要这个窗了」：从集合里移除，别让它下次启动又复活。
        # 之前只清 active、不删列表，所有用过一次的窗口都会留在设置里，重开程序
        # 一按「显示全部」就把历史窗口全弹出来——这就是「多个放大窗」的来源。
        if window in self.windows:
            self.windows.remove(window)
        if self.active is window:
            self.active = self.windows[-1] if self.windows else None
        try:
            window.shutdown()
            window.deleteLater()
        except RuntimeError:
            pass
        self._reposition_chips()
        self.closed.emit(window)
        self._touch(window)

    def _reposition_chips(self) -> None:
        """最小化后的浮条统一贴在屏幕右下角，多个窗口从下往上排。"""
        slot = 0
        for window in self.windows:
            try:
                if window.is_minimized:
                    window.place_minimize_chip(slot)
                    slot += 1
            except RuntimeError:
                continue

    def _touch(self, *_args) -> None:
        self._reposition_chips()
        self.changed.emit()
        self._save_timer.start()

    # ------------------------------------------------------------------ 对外

    def any_visible(self) -> bool:
        return any(w.isVisible() or w.is_minimized for w in self.windows)

    def set_all_visible(self, on: bool) -> None:
        for window in self.windows:
            window.set_wanted_visible(bool(on))
        self._touch()

    def hide_all(self) -> None:
        """临时收起来（关标签/退出时用），不动「用户想不想看见」的记录。"""
        for window in self.windows:
            window.hide()
            if window._minimize_chip is not None:
                window._minimize_chip.hide()

    def raise_all(self) -> None:
        for window in self.windows:
            if window.isVisible():
                window.raise_()
            # 收起成小浮条的那些也要抬上来，否则「打开放大窗」看着像没反应
            if window.is_minimized and window._minimize_chip is not None:
                window._minimize_chip.show()
                window._minimize_chip.raise_()

    def show_saved(self) -> None:
        """启动/第一次打开图纸时，把上次开着的窗口恢复出来。"""
        for window in self.windows:
            if window.want_visible and window.region is not None:
                window.show()
                window.raise_()
            elif window.is_minimized and window._minimize_chip is not None:
                window._minimize_chip.show()
                window._minimize_chip.raise_()
        self.changed.emit()

    def set_document(self, path: str) -> None:
        self._document_path = path or ""
        for window in self.windows:
            window.set_document(self._document_path)

    def set_snap_targets(self, targets) -> None:
        for window in self.windows:
            window.set_snap_targets(targets)

    def set_annotations(self, annotations, resolver=None, visible: bool | None = None) -> None:
        """把当前文档的标注广播给所有窗口（含之后新建的窗口）。"""
        self._annotations = tuple(annotations or ())
        self._annotation_resolver = resolver
        if visible is not None:
            self._annotations_visible = bool(visible)
        for window in self.windows:
            window.set_annotations(
                self._annotations, self._annotation_resolver, self._annotations_visible
            )

    def apply_page(self, page: int, size_fn) -> None:
        """所有窗口按各自的区域换算到这一页，再各自去渲。"""
        for window in self.windows:
            if window.region is None:
                window.set_region(None)
                window.refresh(int(page))
                continue
            size = size_fn(int(page))
            if not size:
                continue
            window.set_region(window.region.to_pdf(size[0], size[1]))
            window.refresh(int(page))

    def refresh_visible(self, page: int) -> None:
        for window in self.windows:
            if window.isVisible() and window.has_region():
                window.refresh(int(page))

    def shutdown(self) -> None:
        for window in self.windows:
            try:
                window.shutdown()
            except RuntimeError:
                pass
        self.save()

    # ------------------------------------------------------------------ 存盘

    def save(self) -> None:
        if self._settings is None:
            return
        items = []
        for window in self.windows:
            try:
                geometry = window.geometry()
                items.append(
                    {
                        "id": window.win_id,
                        "region": window.region.to_dict() if window.region is not None else None,
                        "x": int(geometry.x()),
                        "y": int(geometry.y()),
                        "w": int(geometry.width()),
                        "h": int(geometry.height()),
                        "locked": bool(window._locked),
                        "topmost": bool(window.topmost),
                        "zoom": round(float(window.content_zoom), 3),
                        "anchor": [round(float(window._anchor[0]), 3), round(float(window._anchor[1]), 3)],
                        "color": window.accent,
                        "visible": bool(window.want_visible),
                        "minimized": bool(window.is_minimized),
                    }
                )
            except (RuntimeError, TypeError, ValueError):
                continue
        try:
            self._settings.setValue(SETTINGS_KEY, json.dumps(items, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 存设置失败不该影响看图和标注
            pass

    def load(self) -> bool:
        """从程序设置里恢复窗口集合；没有就返回 False（交给旧项目迁移）。"""
        if self._settings is None:
            return False
        try:
            raw = self._settings.value(SETTINGS_KEY, "", type=str)
        except TypeError:
            raw = self._settings.value(SETTINGS_KEY, "")
        if not raw or not isinstance(raw, str):
            return False
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            return False
        if not isinstance(items, list) or not items:
            return False
        for item in items:
            if not isinstance(item, dict):
                continue
            # 只恢复「上次退出时还开着」的窗口。旧配置里可能攒着已经关掉、
            # 只是 hidden 的历史窗，全部 create 会在下次「显示全部」时一起冒出来。
            # 最小化的窗口也算“还在用”：恢复成右下角的小浮条，而不是弹回桌面。
            if not item.get("visible") and not item.get("minimized"):
                continue
            payload = item.get("region")
            region = Region.from_dict(payload) if isinstance(payload, dict) else None
            geometry = {
                key: item.get(key)
                for key in ("x", "y", "w", "h")
                if item.get(key) is not None
            }
            anchor = item.get("anchor") if isinstance(item.get("anchor"), (list, tuple)) else (0.5, 0.5)
            window = self.create(
                region=region,
                geometry=geometry or None,
                visible=bool(item.get("visible")),
                zoom=item.get("zoom", MIN_ZOOM),
                anchor=anchor,
                locked=bool(item.get("locked")),
                topmost=bool(item.get("topmost")),
                accent=str(item.get("color") or ""),
                win_id=str(item.get("id") or ""),
            )
            if item.get("minimized"):
                window.minimize()
        return bool(self.windows)

"""PDF 页面显示区 + 标注交互层。

坐标体系约定
-----------
场景坐标 = **PDF 页面坐标（pt）**。这样标注位置的存储、渲染、命中测试三者
共用一套数值，不需要在缩放/平移时做任何换算——这是第 8.1 节"不得使用屏幕
像素坐标作为永久标注位置"的落地方式。

一屏的像素映射由 ``QGraphicsView`` 的变换矩阵负责：``scene → view`` 的缩放
因子就是 ``self._zoom``。瓦片则按 ``zoom × devicePixelRatio`` 出图，保证在
高 DPI 屏上也是 1:1 物理像素，不会糊。

标注对象挂在 ``PdfPageItem`` 下面，所以拖拽、命中测试、坐标换算全部在页面
坐标系里完成，与滚动位置无关。
"""

from __future__ import annotations

import bisect
import math
from enum import Enum

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen, QTransform
from PySide6.QtWidgets import QGraphicsItem, QGraphicsScene, QGraphicsView, QMenu

from ui import theme

from core.annotation import (
    ALIGN_CENTER,
    KIND_TEXT,
    Annotation,
    AnnotationStore,
    AnnotationStyle,
    new_id,
)
from core.history import (
    AddAnnotationCommand,
    BatchCommand,
    ModifyAnnotationCommand,
    RemoveAnnotationsCommand,
)
from core.pdf_engine import PageSize
from core.render_manager import PREVIEW_WIDTH, TILE_PX, page_key
from core.sequence import SCOPE_PAGE
from ui.annotation_items import AnnotationItem
from ui.inline_editor import InlineTextEditor

MIN_ZOOM = 0.05
MAX_ZOOM = 16.0
PAGE_GAP = 16.0
SCENE_MARGIN = 8.0
PASTE_OFFSET = 12.0

#: 覆盖模式的碰撞检测半径（第 6.1.2 节），单位 pt，小/中/大三档 + 自定义。
RADIUS_PRESETS: dict[str, float] = {"小": 8.0, "中": 20.0, "大": 40.0}
DEFAULT_DETECT_RADIUS = RADIUS_PRESETS["中"]

#: 数字标注的出厂样式。工程图纸上要能一眼找到，所以默认红色 + 粗体，
#: 字号比正文大一档；用户在属性面板里改一次就记住（Phase 6 会存进 .qpdf）。
DEFAULT_NUMBER_STYLE = AnnotationStyle(
    font_size=16.0, color="#d0342c", bold=True, align=ALIGN_CENTER
)


class ToolMode(str, Enum):
    """第 12.2 节的六种互斥模式。Phase 2 先落地浏览/文字，其余占位。"""

    BROWSE = "browse"
    TEXT = "text"
    NUMBER = "number"
    OVERWRITE = "overwrite"
    INSERT = "insert"
    SELECT = "select"
    REGION = "region"  # 框选识别区域（第 10 章：从图纸里读管线号）


class PdfPageItem(QGraphicsItem):
    """单页图元。只负责把缓存的瓦片画到屏幕上，自己不持有 PDF 数据。"""

    def __init__(self, viewer: "PdfViewer", index: int, size: PageSize) -> None:
        super().__init__()
        self._viewer = viewer
        self.index = index
        self.page_w = float(size.width)
        self.page_h = float(size.height)
        self._rect = QRectF(0.0, 0.0, self.page_w, self.page_h)
        #: 识别区域（页面坐标），以及正在拖拽时的临时框
        self.scan_rect: QRectF | None = None
        #: 数据匹配标注的落字区域（跟识别区域分开：一个读、一个写）
        self.annot_rect: QRectF | None = None
        #: 材料表识别区域（橙色；和管线号识别区域分开）
        self.material_rect: QRectF | None = None
        self.drag_rect: QRectF | None = None
        # 整页预览图用的缩放：翻页/缩放时先拿它顶上，不用干等瓦片
        self._preview_scale = max(0.02, PREVIEW_WIDTH / max(self.page_w, 1.0))

        # exposedRect 只有在开启 ItemUsesExtendedStyleOption 后才有效，
        # 不开的话 paint() 拿到的永远是整个 boundingRect，等于整页重绘。
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemUsesExtendedStyleOption, True)
        self.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.setCacheMode(QGraphicsItem.CacheMode.NoCache)

    # QGraphicsItem 接口 ---------------------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        exposed = option.exposedRect
        if exposed.isEmpty():
            return

        painter.fillRect(exposed, QColor("#ffffff"))

        manager = self._viewer.render_manager
        if manager is None:
            self._draw_border(painter)
            self._draw_page_number(painter)
            return

        # 按真实缩放渲染，不再量化到 1.25 档位——量化是"不够清晰"的根源
        # （缓存键与几何用同一个 scale，画出来的瓦片不会被拉伸）
        scale = self._viewer.tile_scale()
        if scale <= 0.0:
            self._draw_border(painter)
            self._draw_page_number(painter)
            return

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 1) 先铺整页预览图：换页或缩放时立刻有画面，瓦片随后补上
        preview = manager.lookup(page_key(self.index, self._preview_scale))
        if preview is not None:
            painter.drawPixmap(self._rect, preview, QRectF(preview.rect()))
        # 预览图没就绪时这里**不发请求**：paint() 是高频路径，而 request_page
        # 会同步 load_page（解析大图要几十毫秒），在绘制里解析 PDF 就是卡顿的来源。
        # 请求统一由 PdfViewer._sync_requests() 在滚动停/缩放停/换页之后发。

        # 2) 正在缩放或刚翻过页时只画预览图，等手停下来再按新倍率渲染，
        #    避免一串马上就过期的请求把主线程拖住
        if not self._viewer.settled:
            self._draw_border(painter)
            self._draw_page_number(painter)
            return

        span = TILE_PX / scale  # 一个瓦片覆盖的页面坐标边长
        cols = max(1, int(math.ceil(self.page_w / span - 1e-6)))
        rows = max(1, int(math.ceil(self.page_h / span - 1e-6)))

        tx0 = max(0, int(exposed.left() // span))
        tx1 = min(cols - 1, int(exposed.right() // span))
        ty0 = max(0, int(exposed.top() // span))
        ty1 = min(rows - 1, int(exposed.bottom() // span))

        for ty in range(ty0, ty1 + 1):
            y = ty * span
            h = min(span, self.page_h - y)
            if h <= 0.0:
                continue
            for tx in range(tx0, tx1 + 1):
                x = tx * span
                w = min(span, self.page_w - x)
                if w <= 0.0:
                    continue
                key = (self.index, scale, tx, ty)
                pixmap = manager.lookup(key)
                if pixmap is not None:
                    painter.drawPixmap(QRectF(x, y, w, h), pixmap, QRectF(pixmap.rect()))

        self._draw_border(painter)
        self._draw_regions(painter)
        self._draw_page_number(painter)

    def request_visible(self, exposed: QRectF) -> None:
        """把 ``exposed``（本页局部坐标）盖住的预览图和瓦片排进渲染队列。

        只在"用户停下来了"的时候调用（滚动停、缩放停、换页、切文档），
        **绝不能**从 ``paint()`` 里调：``request_page`` 会同步 ``load_page``，
        在绘制路径里解析 PDF 就是"翻页一顿一顿"的根因。
        """
        manager = self._viewer.render_manager
        if manager is None:
            return
        if manager.lookup(page_key(self.index, self._preview_scale)) is None:
            # 预览图要插队：它一出来用户就能看懂这一页，比瓦片重要得多
            manager.request_page(self.index, self._preview_scale, priority=100)
        if not self._viewer.settled:
            return
        scale = self._viewer.tile_scale()
        if scale <= 0.0:
            return
        span = TILE_PX / scale  # 一个瓦片覆盖的页面坐标边长
        cols = max(1, int(math.ceil(self.page_w / span - 1e-6)))
        rows = max(1, int(math.ceil(self.page_h / span - 1e-6)))
        tx0 = max(0, int(exposed.left() // span))
        tx1 = min(cols - 1, int(exposed.right() // span))
        ty0 = max(0, int(exposed.top() // span))
        ty1 = min(rows - 1, int(exposed.bottom() // span))
        for ty in range(ty0, ty1 + 1):
            y = ty * span
            h = min(span, self.page_h - y)
            if h <= 0.0:
                continue
            for tx in range(tx0, tx1 + 1):
                x = tx * span
                w = min(span, self.page_w - x)
                if w <= 0.0:
                    continue
                key = (self.index, scale, tx, ty)
                if manager.contains(key):
                    continue
                manager.request(key, QRectF(x, y, w, h), scale)

    def _draw_border(self, painter: QPainter) -> None:
        # 1 物理像素的发丝边：白纸上没有这条线，纸和画布就糊在一起
        painter.setPen(QPen(QColor(theme.PAPER_EDGE), 1.0 / max(self._viewer.zoom, 1e-6)))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(self._rect)

    def _draw_regions(self, painter: QPainter) -> None:
        """把两个区域画在页面上：虚线框 + 淡底，一眼看出"从这里读、往这里写"。

        识别区域是蓝色（读），标注区域是绿色（写），拖动中的框是实线、
        比常驻的框粗一点。两套颜色不混用，用户不会把框错当成对方的。
        """
        zoom = max(self._viewer.zoom, 1e-6)
        plans = (
            (self.scan_rect, theme.ACCENT, False),
            (self.annot_rect, "#1a9c5b", False),
            (self.material_rect, "#f59e0b", False),
            (self.drag_rect, theme.ACCENT, True),
        )
        for rect, color, strong in plans:
            if rect is None or rect.isEmpty():
                continue
            pen = QPen(QColor(color), (1.6 if strong else 1.2) / zoom)
            pen.setStyle(Qt.PenStyle.DashLine if not strong else Qt.PenStyle.SolidLine)
            painter.setPen(pen)
            tint = QColor(color)
            tint.setAlpha(28 if not strong else 46)
            painter.setBrush(tint)
            painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_page_number(self, painter: QPainter) -> None:
        """页码是「每页同一个位置的一行字」，不建标注对象，也不进 .qpdf。"""
        spec = self._viewer.page_number_spec
        if spec is None or not spec.enabled or spec.region is None:
            return
        text = spec.text_for(self.index + 1, max(1, len(self._viewer._sizes)))
        if not text:
            return
        x0, y0, x1, y1 = spec.region.to_pdf(self.page_w, self.page_h)
        rect = QRectF(x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0))
        style = spec.style
        font = QFont(style.font_family or "Microsoft YaHei UI")
        font.setPointSizeF(max(1.0, float(style.font_size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        painter.setFont(font)
        color = QColor(style.color)
        color.setAlpha(int(max(0, min(100, style.opacity)) * 255 / 100))
        painter.setPen(QPen(color))
        flags = Qt.AlignmentFlag.AlignTop
        if style.align == "center":
            flags |= Qt.AlignmentFlag.AlignHCenter
        elif style.align == "right":
            flags |= Qt.AlignmentFlag.AlignRight
        else:
            flags |= Qt.AlignmentFlag.AlignLeft
        painter.drawText(rect, flags, text)


class PdfViewer(QGraphicsView):
    """连续滚动 / 单页两种浏览模式共用的显示区。"""

    page_changed = Signal(int)
    #: 数字标注模式下右键：要打开「起始值与步长」设置
    number_settings_requested = Signal()
    zoom_changed = Signal(float)
    document_cleared = Signal()
    mode_changed = Signal(object)  # ToolMode
    selection_changed = Signal(list)  # list[Annotation]
    #: 需要给用户看的一句话（覆盖模式没找到数字、插队成功等）
    message = Signal(str)
    #: 插队模式点下位置后请求确认（页码, 页面 x, 页面 y, 插入序号, 编号组）
    insert_requested = Signal(int, float, float, int, str)
    #: 框选完识别区域（页码, 左, 上, 右, 下 —— 都是这一页的 PDF 坐标）
    region_picked = Signal(int, float, float, float, float)

    def __init__(
        self,
        render_manager,
        store: AnnotationStore | None = None,
        undo_stack=None,
        sequence=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.render_manager = render_manager
        self.store = store
        self.undo_stack = undo_stack
        self.sequence = sequence

        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)

        self._items: list[PdfPageItem] = []
        self._tops: list[float] = []
        self._sizes: list[PageSize] = []
        self._current = 0
        self._zoom = 1.0
        self._single_page = False
        self._fit_mode = "none"  # none | width | page
        #: 识别区域（core.region.Region）与正在拖拽的框
        self._scan_region = None
        #: 数据匹配标注的落字区域（Region，页面比例）
        self._annot_region = None
        #: 材料表识别区域（Region，页面比例）
        self._material_region = None
        #: 页码规格（core.page_number.PageNumberSpec，不写进标注库）
        self._page_number_spec = None
        self._region_page = -1
        self._region_origin = None
        self._content_w = 0.0
        self._content_h = 0.0

        # 缩放中先只画预览图，等手停下 120ms 再按新倍率渲染瓦片。
        # 这样滚轮缩放是"图跟着走"，而不是"每次换档都先白一下"。
        self._zooming = False
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.setInterval(120)
        self._zoom_timer.timeout.connect(self._on_zoom_settled)
        # 翻页时先只铺预览图，等用户停下来再补高清瓦片——
        # 一次瓦片渲染就是几十毫秒的主线程停顿，连翻十页就卡成幻灯片
        self._settled = True
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(180)
        self._settle_timer.timeout.connect(self._on_page_settled)

        # 一页往往同时回来好几块瓦片。每块回来都整视口重绘一次，等于一次翻页
        # 画 N 遍全屏；攒 15ms（约一帧）再画，只要最后能看见就行。
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(15)
        self._update_timer.timeout.connect(self.viewport().update)

        # 标注层
        self._ann_items: dict[str, AnnotationItem] = {}
        self._number_text_resolver = None
        self._mode = ToolMode.BROWSE
        self._default_style = AnnotationStyle()
        self._number_style = DEFAULT_NUMBER_STYLE
        self._clipboard: list[Annotation] = []
        self._detect_radius = DEFAULT_DETECT_RADIUS
        self._annotations_visible = True

        # 内联编辑状态
        self._editor = InlineTextEditor(self.viewport())
        self._editor.committed.connect(self._on_editor_committed)
        self._editor.cancelled.connect(self._on_editor_cancelled)
        self._editing_ann: Annotation | None = None
        self._editing_item: AnnotationItem | None = None
        self._editing_page = 0
        self._editing_local = QPointF()

        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        self.setBackgroundBrush(QColor(theme.canvas_color()))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setOptimizationFlag(QGraphicsView.OptimizationFlag.DontSavePainterState, True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.render_manager.tile_ready.connect(self._on_tile_ready)
        self._scene.selectionChanged.connect(self._on_scene_selection_changed)
        if self.store is not None:
            self.store.added.connect(self._on_annotation_added)
            self.store.removed.connect(self._on_annotation_removed)
            self.store.changed.connect(self._on_annotation_changed)
            self.store.cleared.connect(self._on_annotations_cleared)
        if self.sequence is not None:
            # 数字标注的显示文本永远现算，不读 ann.content
            self.set_number_text_resolver(self.sequence.text_for)

    # ------------------------------------------------------------------ 状态

    def rebind(self, render_manager, store, undo_stack, sequence) -> None:
        """换到另一个「文档会话」（多标签页切换时用）：把底层对象整套换掉。"""
        if render_manager is self.render_manager and store is self.store:
            return
        try:
            self.render_manager.tile_ready.disconnect(self._on_tile_ready)
        except (RuntimeError, TypeError):
            pass
        if store is not None and store is not self.store:
            for signal, slot in (
                (getattr(self.store, "added", None), self._on_annotation_added),
                (getattr(self.store, "removed", None), self._on_annotation_removed),
                (getattr(self.store, "changed", None), self._on_annotation_changed),
                (getattr(self.store, "cleared", None), self._on_annotations_cleared),
            ):
                if signal is None:
                    continue
                try:
                    signal.disconnect(slot)
                except (RuntimeError, TypeError):
                    pass
        self.render_manager = render_manager
        self.store = store
        self.undo_stack = undo_stack
        self.sequence = sequence
        render_manager.tile_ready.connect(self._on_tile_ready)
        if store is not None:
            store.added.connect(self._on_annotation_added)
            store.removed.connect(self._on_annotation_removed)
            store.changed.connect(self._on_annotation_changed)
            store.cleared.connect(self._on_annotations_cleared)
        if sequence is not None:
            self.set_number_text_resolver(sequence.text_for)

    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def page_count(self) -> int:
        return len(self._items)

    def page_size(self, index: int) -> tuple[float, float] | None:
        """第 index 页的宽高（pt）。工作台/识别区域按页换算坐标要用。"""
        if 0 <= index < len(self._sizes):
            return float(self._sizes[index].width), float(self._sizes[index].height)
        return None

    @property
    def page_sizes(self) -> list[tuple[float, float]]:
        """整份文档每页的宽高（pt），顺序就是页序。数据匹配标注排文字用。"""
        return [(float(size.width), float(size.height)) for size in self._sizes]

    @property
    def current_page(self) -> int:
        return self._current

    @property
    def single_page(self) -> bool:
        return self._single_page

    @property
    def mode(self) -> ToolMode:
        return self._mode

    @property
    def default_style(self) -> AnnotationStyle:
        """当前模式下"新建标注"用的样式。数字和文字各有各的默认值。"""
        return self.style_for_mode(self._mode)

    @property
    def number_style(self) -> AnnotationStyle:
        return self._number_style

    def style_for_mode(self, mode) -> AnnotationStyle:
        if mode in (ToolMode.NUMBER, ToolMode.OVERWRITE, ToolMode.INSERT):
            return self._number_style
        return self._default_style

    def device_pixel_ratio(self) -> float:
        return float(self.devicePixelRatioF())

    def tile_scale(self) -> float:
        """瓦片渲染比例：页面 pt → 输出像素（已含设备像素比）。

        保留两位小数是给缓存键用的：滚轮缩放会经过很多中间倍率，不量化等于
        每滚一格都全部重渲。**绘制时算瓦片几何也用这个值**，所以取整不会
        把图拉糊——键和几何是同一个数。
        """
        return round(self._zoom * self.device_pixel_ratio(), 2)

    def has_document(self) -> bool:
        return bool(self._items)

    # ------------------------------------------------------------------ 模式

    def set_mode(self, mode: ToolMode) -> None:
        if not isinstance(mode, ToolMode):
            mode = ToolMode(mode)
        if mode is self._mode:
            return
        # 中途换模式时把没松手的框选取消掉，否则那个虚线框会一直留在页上
        self._cancel_region_drag()
        if self._editor.active:
            self._editor.finish(True)
        self._mode = mode
        if mode is ToolMode.BROWSE:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self.viewport().setCursor(Qt.CursorShape.OpenHandCursor)
        elif mode is ToolMode.SELECT:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        elif mode is ToolMode.OVERWRITE:
            # 覆盖模式是"点已有的数字"，指针手比十字准星更贴合语义
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setDragMode(QGraphicsView.DragMode.NoDrag)
            self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self.mode_changed.emit(mode)

    def set_default_style(self, style: AnnotationStyle, mode=None) -> None:
        """设置"新建标注"的默认样式。

        有明确对象时按对象走：数字模式改的是数字的默认样式，文字模式改文字的。
        这样连续标数字的时候不会因为上一轮改过字号而跑偏。
        """
        target = self._mode if mode is None else mode
        if target in (ToolMode.NUMBER, ToolMode.OVERWRITE, ToolMode.INSERT):
            self._number_style = style
        else:
            self._default_style = style

    def set_number_style(self, style: AnnotationStyle) -> None:
        self._number_style = style

    def set_number_text_resolver(self, resolver) -> None:
        """Phase 3：数字标注的显示文本由编号序列现算。"""
        self._number_text_resolver = resolver
        for item in self._ann_items.values():
            item.set_text_resolver(resolver)

    # ------------------------------------------------------------------ 背景

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        """画布底色 + 页面投影。

        投影只给视口里看得见的那几页画，2700 页文档也不会因此变慢。
        浅粉主题的画布底是 resources/bg.png（固定贴在视口上，不随滚动跑）。
        """
        wallpaper = theme.wallpaper()
        if wallpaper is not None:
            self._paint_wallpaper(painter, wallpaper)
        else:
            painter.fillRect(rect, QColor(theme.canvas_color()))
        self._paint_edge_shade(painter)
        if not self._items:
            return
        painter.setPen(Qt.PenStyle.NoPen)
        start = max(0, bisect.bisect_right(self._tops, rect.top()) - 1)
        for index in range(start, len(self._items)):
            item = self._items[index]
            if self._tops[index] > rect.bottom():
                break
            page = item.sceneBoundingRect()
            if not page.intersects(rect):
                continue
            # 两层投影：外面一层很淡、铺得远，里面一层稍实、贴着纸边，
            # 比单层大阴影干净（单层会像贴纸）。深色画布上要重一点才看得出层次。
            deep = QColor(0, 0, 0, 62 if theme.is_dark() else 22)
            near = QColor(0, 0, 0, 78 if theme.is_dark() else 30)
            painter.setBrush(deep)
            painter.drawRoundedRect(page.adjusted(-6.0, -2.0, 6.0, 9.0), 6.0, 6.0)
            painter.setBrush(near)
            painter.drawRoundedRect(page.adjusted(-2.5, -0.5, 2.5, 4.0), 3.0, 3.0)

    def _paint_edge_shade(self, painter: QPainter) -> None:
        """画布左右各压一道 16px 渐隐暗边：两侧面板看着是浮在画布上方。

        浅色主题里两侧面板和画布本来就都是浅色，只靠 1px 边框分不开，
        这道"投影"才是让人看出层次的东西。
        """
        viewport = self.viewport()
        width = max(1, viewport.width())
        height = max(1, viewport.height())
        if width < 60:
            return
        shade = QColor(48, 32, 40, 66 if theme.is_dark() else 34)
        clear = QColor(48, 32, 40, 0)
        painter.save()
        painter.resetTransform()
        left = QLinearGradient(QPointF(0, 0), QPointF(16, 0))
        left.setColorAt(0.0, shade)
        left.setColorAt(1.0, clear)
        painter.fillRect(0, 0, 16, height, left)
        right = QLinearGradient(QPointF(width, 0), QPointF(width - 16, 0))
        right.setColorAt(0.0, shade)
        right.setColorAt(1.0, clear)
        painter.fillRect(width - 16, 0, 16, height, right)
        painter.restore()

    def _paint_wallpaper(self, painter: QPainter, pix) -> None:
        """底图按视口大小铺满（等比放大后右对齐，人物留在右下角），上面压一层白纱。"""
        viewport = self.viewport()
        width = max(1, viewport.width())
        height = max(1, viewport.height())
        key = (pix.cacheKey(), width, height)
        if getattr(self, "_wall_key", None) != key:
            self._wall_pix = pix.scaled(
                QSize(width, height),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._wall_key = key
        wall = self._wall_pix
        painter.save()
        # 换成视口坐标：底图不跟着缩放/滚动跑，像贴在屏幕后面的一层壁纸
        painter.resetTransform()
        painter.drawPixmap(width - wall.width(), (height - wall.height()) // 2, wall)
        # 图本身很浅，再压一层画布色 + 一层白纱：白纸、红线、蓝字都还看得清，
        # 同时让画布比两侧面板深一点点——不然粉主题里三层浅色会糊成一片。
        tint = QColor(theme.CANVAS)
        tint.setAlpha(46)
        painter.fillRect(0, 0, width, height, tint)
        painter.fillRect(0, 0, width, height, QColor(255, 255, 255, 30))
        painter.restore()

    # ------------------------------------------------------------------ 文档

    def set_document(self, page_sizes: list[PageSize]) -> None:
        self._cancel_region_drag()
        self._region_page = -1
        self._ann_items.clear()
        self._scene.clear()
        self._items.clear()
        self._tops.clear()
        self._sizes = list(page_sizes)
        self._current = 0

        for index, size in enumerate(self._sizes):
            item = PdfPageItem(self, index, size)
            self._scene.addItem(item)
            self._items.append(item)

        self._relayout()
        self._zoom = 1.0
        self.setTransform(QTransform.fromScale(1.0, 1.0))
        self._fit_mode = "width"
        self.fit_width()
        self._rebuild_annotations()
        self._emit_page_changed(force=True)
        self._sync_requests()

    def clear_document(self) -> None:
        self._cancel_region_drag()
        self._region_page = -1
        if self._editor.active:
            self._editor.finish(False, silent=True)
        self._editing_ann = None
        self._editing_item = None
        self._ann_items.clear()
        self._scene.clear()
        self._items.clear()
        self._tops.clear()
        self._sizes.clear()
        self._current = 0
        self._content_w = self._content_h = 0.0
        self.setSceneRect(QRectF())
        self.setTransform(QTransform())
        self.document_cleared.emit()

    # ------------------------------------------------------------------ 布局

    def _relayout(self) -> None:
        if not self._items:
            return
        max_w = max(size.width for size in self._sizes)
        y = 0.0
        self._tops.clear()
        for item, size in zip(self._items, self._sizes):
            x = (max_w - size.width) / 2.0
            item.setPos(x, y)
            self._tops.append(y)
            y += size.height + PAGE_GAP
        self._content_w = max_w
        self._content_h = y - PAGE_GAP if self._items else 0.0
        self._update_scene_rect()

    def _update_scene_rect(self) -> None:
        if not self._items:
            self.setSceneRect(QRectF())
            return
        if self._single_page:
            rect = self._items[self._current].sceneBoundingRect()
            self.setSceneRect(rect.adjusted(-SCENE_MARGIN, -SCENE_MARGIN,
                                            SCENE_MARGIN, SCENE_MARGIN))
            for i, item in enumerate(self._items):
                item.setVisible(i == self._current)
        else:
            self.setSceneRect(
                QRectF(
                    -SCENE_MARGIN,
                    -SCENE_MARGIN,
                    self._content_w + 2 * SCENE_MARGIN,
                    self._content_h + 2 * SCENE_MARGIN,
                )
            )
            for item in self._items:
                item.setVisible(True)

    def set_single_page(self, enabled: bool) -> None:
        if enabled == self._single_page:
            return
        self._single_page = enabled
        self._update_scene_rect()
        self.goto_page(self._current)
        if self._fit_mode == "page":
            self.fit_page()

    # ------------------------------------------------------------------ 缩放

    def set_zoom(self, zoom: float, anchor: QPointF | None = None) -> None:
        """显式缩放。**任何一次显式缩放都退出「适应窗口/适应宽度」模式。**

        以前只有 `wheelEvent` 会清 `_fit_mode`，于是"滚轮放大 → 拖窗口边（或最大化）"
        会重新按宽度排一遍，画面突然跳回 fit-width。现在把这件事收在 set_zoom 里，
        `fit_width()` / `fit_page()` 改成**调用之后再**把模式设回去，语义反过来。
        """
        if not self._items:
            return
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, float(zoom)))
        if abs(zoom - self._zoom) < 1e-6:
            return
        if anchor is None:
            anchor = QPointF(self.viewport().rect().center())

        self._fit_mode = "none"
        scene_anchor = self.mapToScene(anchor.toPoint())
        self._zoom = zoom
        self._zooming = True
        self._zoom_timer.start()
        self.setTransform(QTransform.fromScale(zoom, zoom))
        after = self.mapFromScene(scene_anchor)
        dx = anchor.x() - after.x()
        dy = anchor.y() - after.y()
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            self.translate(dx / zoom, dy / zoom)
        self.viewport().update()
        self.zoom_changed.emit(zoom)

    def _on_zoom_settled(self) -> None:
        self._zooming = False
        self._sync_requests()
        self.viewport().update()

    @property
    def zooming(self) -> bool:
        return self._zooming

    @property
    def settled(self) -> bool:
        """用户是否已经停下（翻页/滚动都停了），停下才值得渲染高清瓦片。"""
        return self._settled and not self._zooming

    def _mark_unsettled(self) -> None:
        self._settled = False
        self._settle_timer.start()

    def _on_page_settled(self) -> None:
        self._settled = True
        self._sync_requests()
        self.viewport().update()

    def zoom_in(self) -> None:
        self._fit_mode = "none"
        self.set_zoom(self._zoom * 1.25)

    def zoom_out(self) -> None:
        self._fit_mode = "none"
        self.set_zoom(self._zoom / 1.25)

    def fit_width(self) -> None:
        if not self._items:
            return
        width = self._sizes[self._current].width
        available = max(50, self.viewport().width() - 24)
        # 先缩放、后置模式：set_zoom 会把模式清成 none，顺序反了"适应宽度"就失效
        self.set_zoom(available / width)
        self._fit_mode = "width"
        self.goto_page(self._current)
        self.zoom_changed.emit(self._zoom)

    def fit_page(self) -> None:
        if not self._items:
            return
        size = self._sizes[self._current]
        avail_w = max(50, self.viewport().width() - 24)
        avail_h = max(50, self.viewport().height() - 24)
        zoom = min(avail_w / size.width, avail_h / size.height)
        self.set_zoom(zoom)
        self._fit_mode = "page"
        self.centerOn(self._items[self._current].sceneBoundingRect().center())
        self.zoom_changed.emit(self._zoom)

    def reset_zoom(self) -> None:
        self.set_zoom(1.0)
        self.goto_page(self._current)

    def clear_fit_mode(self) -> None:
        """切标签回来时用：锁住恢复出来的倍率。

        不然 ``_fit_mode`` 还是 "width"，下一次窗口尺寸变化（多半就在切回来
        之后的那一帧）又会按宽度重新排一遍，把恢复好的倍率和页码冲掉。
        """
        self._fit_mode = "none"

    # ------------------------------------------------------------------ 翻页

    def goto_page(self, index: int, align_top: bool = True) -> None:
        if not self._items:
            return
        index = max(0, min(len(self._items) - 1, int(index)))
        self._current = index
        rect = self._items[index].sceneBoundingRect()

        if self._single_page:
            self._update_scene_rect()
            self.centerOn(rect.center())
        elif align_top:
            visible_h = self.viewport().height() / max(self._zoom, 1e-6)
            self.centerOn(rect.center().x(), rect.top() + visible_h / 2.0)
        else:
            self.centerOn(rect.center())
        self._emit_page_changed(force=True)

    def next_page(self) -> None:
        self.goto_page(self._current + 1)

    def prev_page(self) -> None:
        self.goto_page(self._current - 1)

    def _page_at_y(self, y: float) -> int:
        if not self._tops:
            return 0
        idx = bisect.bisect_right(self._tops, y) - 1
        return max(0, min(len(self._tops) - 1, idx))

    def _emit_page_changed(self, force: bool = False) -> None:
        if not self._items:
            return
        if not force:
            center = self.mapToScene(self.viewport().rect().center())
            idx = self._page_at_y(center.y())
            if idx == self._current:
                self._sync_focus()
                return
            self._current = idx
            self._mark_unsettled()
        self.page_changed.emit(self._current)
        self._sync_focus()
        self._warm_neighbors()

    def _warm_neighbors(self) -> None:
        """顺手把前后各一页的预览图排上。

        快速翻页最烦的是"下一页一片白"——等它自己请求得 60 ms 起。
        提前把邻居页的**低分辨率预览**（640 px）渲好，翻过去立刻有图，
        接着再补高清瓦片。只排预览，不排瓦片，免得抢当前页的带宽。
        """
        manager = self.render_manager
        if manager is None or not self._items:
            return
        # 优先级给**负数**：这两张排到当前页的瓦片后面去。
        # 抢在瓦片前面会让"当前页变清晰"变慢，翻页体感反而更差。
        for offset in (1, -1):
            page = self._current + offset
            if 0 <= page < len(self._items):
                item = self._items[page]
                if isinstance(item, PdfPageItem):
                    manager.request_page(item.index, item._preview_scale, priority=-5)

    def _sync_focus(self) -> None:
        """把"用户现在在看哪几页"告诉渲染调度器，范围外的排队任务会被作废。"""
        manager = self.render_manager
        if manager is None or not self._tops:
            return
        view_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        first = self._page_at_y(view_rect.top())
        last = self._page_at_y(view_rect.bottom())
        manager.set_focus(first - 1, last + 1)

    # ------------------------------------------------------------------ 标注层

    def _page_item_at(self, scene_pos: QPointF) -> PdfPageItem | None:
        for item in self._scene.items(scene_pos):
            if isinstance(item, PdfPageItem):
                return item
        return None

    def _annotation_at(self, scene_pos: QPointF) -> AnnotationItem | None:
        for item in self._scene.items(scene_pos):
            if isinstance(item, AnnotationItem):
                return item
        return None

    def _make_item(self, ann: Annotation) -> AnnotationItem | None:
        if not (0 <= ann.page < len(self._items)):
            return None
        item = AnnotationItem(ann, self._items[ann.page], self._number_text_resolver)
        item.move_finished.connect(self._on_item_moved)
        item.set_annotations_visible(self._annotations_visible)
        self._ann_items[ann.id] = item
        return item

    def _rebuild_annotations(self) -> None:
        if self.store is None:
            return
        for ann in self.store.all():
            self._make_item(ann)

    def _on_annotation_added(self, ann: Annotation) -> None:
        self._make_item(ann)

    def _on_annotation_removed(self, ann_id: str) -> None:
        item = self._ann_items.pop(ann_id, None)
        if item is not None and item.scene() is not None:
            self._scene.removeItem(item)

    def _on_annotation_changed(self, ann: Annotation) -> None:
        item = self._ann_items.get(ann.id)
        if item is None:
            self._make_item(ann)
            return
        if item.annotation.page != ann.page:
            self._on_annotation_removed(ann.id)
            self._make_item(ann)
            return
        item.set_annotation(ann)

    def _on_annotations_cleared(self) -> None:
        for item in self._ann_items.values():
            if item.scene() is not None:
                self._scene.removeItem(item)
        self._ann_items.clear()

    def _on_item_moved(self, old: Annotation, new: Annotation) -> None:
        if self.undo_stack is None or self.store is None:
            return
        self.undo_stack.push(ModifyAnnotationCommand(self.store, old, new, "移动标注"))

    def selected_annotations(self) -> list[Annotation]:
        return [
            item.annotation
            for item in self._scene.selectedItems()
            if isinstance(item, AnnotationItem)
        ]

    def select_annotation(self, ann_id: str) -> None:
        item = self._ann_items.get(ann_id)
        if item is None:
            return
        self._scene.clearSelection()
        item.setSelected(True)

    def _on_scene_selection_changed(self) -> None:
        self.selection_changed.emit(self.selected_annotations())

    # ------------------------------------------------------------------ 标注编辑

    def _push(self, command) -> None:
        if self.undo_stack is not None:
            self.undo_stack.push(command)

    def _font_for(self, style: AnnotationStyle) -> QFont:
        font = QFont(style.font_family)
        font.setPointSizeF(max(1.0, float(style.font_size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        return font

    def _create_number(self, scene_pos: QPointF) -> Annotation | None:
        """连续数字标注：点一下出一个数（第 5.2 节）。

        整个过程只碰内存和 SQLite 事务，**一次 PDF 写入都没有**——
        这是"点击不卡"的根本原因（第 15.2 节：不在鼠标点击事件里做耗时 PDF 写入）。
        """
        if self.sequence is None or self.store is None:
            return None
        page_item = self._page_item_at(scene_pos)
        if page_item is None:
            return None
        local = page_item.mapFromScene(scene_pos)
        ann = self.sequence.make_number(
            page_item.index, local.x(), local.y(), self._number_style
        )
        self._push(AddAnnotationCommand(self.store, ann, "添加数字"))
        return ann

    def place_number_at(self, page: int, x: float, y: float) -> Annotation | None:
        """按页面坐标放一个数字（测试和将来的"指定编号"功能都用得上）。"""
        if self.sequence is None or self.store is None:
            return None
        if not (0 <= page < len(self._items)):
            return None
        ann = self.sequence.make_number(page, x, y, self._number_style)
        self._push(AddAnnotationCommand(self.store, ann, "添加数字"))
        return ann

    def place_text_at(
        self, page: int, x: float, y: float, text: str, style: AnnotationStyle | None = None
    ) -> Annotation | None:
        """按页面坐标放一条文字标注（测试与脚本用，界面上走内联编辑器）。"""
        if self.store is None:
            return None
        if not (0 <= page < len(self._items)) or not text:
            return None
        ann = Annotation(
            id=new_id(),
            kind=KIND_TEXT,
            page=page,
            x=float(x),
            y=float(y),
            content=text,
            style=style or self._default_style,
        )
        self._push(AddAnnotationCommand(self.store, ann, "添加文字"))
        return ann

    def apply_batch(self, adds=(), removes=(), updates=(), label="批量标注") -> int:
        """一次多放/多删/多改，整体算**一步**撤销（数据匹配标注用）。

        生成 200 条标注如果推 200 条命令，用户按一次 Ctrl+Z 只退一条，
        要按 200 次——所以走 BatchCommand。「换了新版表格重新匹配」时
        已有标注只改内容，不删了重加，位置和 id 都留着。
        """
        if self.store is None:
            return 0
        ops: list = []
        for ann_id in removes:
            old = self.store.get(ann_id)
            if old is not None:
                ops.append((old, None))
        for old, new in updates:
            if old is not None and new is not None:
                ops.append((old, new))
        for ann in adds:
            ops.append((None, ann))
        if not ops:
            return 0
        self._push(BatchCommand(self.store, ops, label))
        return len(ops)

    # ------------------------------------------------------------------ 覆盖（第 6.1 节）

    @property
    def detect_radius(self) -> float:
        return self._detect_radius

    def set_detect_radius(self, radius: float) -> None:
        self._detect_radius = max(1.0, float(radius))

    def find_numbers_near(self, scene_pos: QPointF, radius: float | None = None) -> list[Annotation]:
        """碰撞检测（第 6.1.2 节）：找点击位置附近**本工具创建的数字**。

        只查 ``AnnotationStore``，所以 PDF 原始文字、原始数字、别的软件做的注释
        根本不在候选里——第 6.1.1 节的检测规则是靠"只认识自己的数据"天然满足的，
        不是靠过滤别人的。
        """
        page_item = self._page_item_at(scene_pos)
        if page_item is None or self.store is None:
            return []
        local = page_item.mapFromScene(scene_pos)
        limit = self._detect_radius if radius is None else float(radius)
        found: list[tuple[float, Annotation]] = []
        for ann in self.store.on_page(page_item.index):
            if not ann.is_number:
                continue
            distance = math.hypot(ann.x - local.x(), ann.y - local.y())
            if distance <= limit:
                found.append((distance, ann))
        found.sort(key=lambda pair: pair[0])
        return [ann for _dist, ann in found]

    def overwrite_number(self, ann_id: str, page: int, x: float, y: float) -> Annotation | None:
        """第 6.1.3 节（已拍板"后者"）：**保留原编号，只修正位置**。

        所以"覆盖"不是删掉再放一个新的，而是把这一个数字挪到新位置——
        编号、样式、所属序列全部原样保留，撤销就是挪回去。
        """
        if self.store is None or self.sequence is None:
            return None
        ann = self.store.get(ann_id)
        if ann is None or not ann.is_number:
            return None
        new = ann.moved_to(x, y, page)
        self._push(ModifyAnnotationCommand(self.store, ann, new, "覆盖数字"))
        return new

    def _handle_overwrite_click(self, scene_pos: QPointF) -> None:
        candidates = self.find_numbers_near(scene_pos)
        if not candidates:
            self.message.emit(
                f"附近没有找到本工具的数字（检测半径 {self._detect_radius:g} pt）"
            )
            return
        target = candidates[0]
        if len(candidates) > 1:
            # 第 6.1.2 节：范围内有多个候选时必须让用户确认是哪一个
            target = self._ask_target(candidates)
            if target is None:
                return
        page_item = self._page_item_at(scene_pos)
        if page_item is None:
            return
        local = page_item.mapFromScene(scene_pos)
        moved = self.overwrite_number(target.id, page_item.index, local.x(), local.y())
        if moved is not None:
            self.message.emit(
                f"已把第 {self.sequence.text_for(moved)} 号移到新位置（编号不变）"
            )

    def _ask_target(self, candidates: list[Annotation]) -> Annotation | None:
        """多个候选时弹一个小菜单让用户点选（第 6.1.2 节）。"""
        from PySide6.QtWidgets import QMenu

        menu = QMenu(self)
        for ann in candidates:
            action = menu.addAction(f"第 {self.sequence.text_for(ann)} 号（第 {ann.page + 1} 页）")
            action.setData(ann.id)
        chosen = menu.exec(self.cursor().pos())
        if chosen is None:
            return None
        chosen_id = chosen.data()
        return next((ann for ann in candidates if ann.id == chosen_id), None)

    def _handle_insert_click(self, scene_pos: QPointF) -> None:
        """插队（第 6.2 节）：点哪就在哪插，插入序号由附近的数字决定。"""
        if self.sequence is None:
            return
        page_item = self._page_item_at(scene_pos)
        if page_item is None:
            return
        local = page_item.mapFromScene(scene_pos)
        candidates = self.find_numbers_near(scene_pos)
        if candidates:
            at_index = candidates[0].seq_index or 0
            group = candidates[0].seq_group or self.sequence.current_group
        else:
            at_index = self.sequence.next_index()
            group = self.sequence.current_group
        self.insert_requested.emit(page_item.index, local.x(), local.y(), int(at_index), group)

    def apply_insert(
        self,
        page: int,
        x: float,
        y: float,
        at_index: int,
        scope: str = SCOPE_PAGE,
        page_from: int = 0,
        page_to: int = 0,
        style: AnnotationStyle | None = None,
        group: str | None = None,
    ) -> int:
        """执行一次插队：新数字占 ``at_index``，范围内后面的数字整体后移。

        返回受影响的标注总数（含新数字），0 表示没做。
        """
        if self.sequence is None or self.store is None:
            return 0
        shifts = self.sequence.plan_insert(
            at_index, scope, page=page, page_from=page_from, page_to=page_to, group=group
        )
        if style is None:
            style = self._number_style
        ann = self.sequence.make_number_at(page, x, y, at_index, style, group=group)
        command = BatchCommand.insert_number(
            self.store, ann, list(shifts), f"插队：插入第 {self.sequence.text_for(ann)} 号"
        )
        self._push(command)
        return 1 + len(shifts)

    def _begin_new_text(self, scene_pos: QPointF) -> None:
        page_item = self._page_item_at(scene_pos)
        if page_item is None:
            return
        local = page_item.mapFromScene(scene_pos)
        style = self._default_style
        view_pos = self.mapFromScene(scene_pos)
        self._editing_ann = None
        self._editing_item = None
        self._editing_page = page_item.index
        self._editing_local = QPointF(local)
        self._editor.open_at(
            QPoint(view_pos.x(), view_pos.y()),
            "",
            self._font_for(style),
            QColor(style.color),
            200.0,
            select_all=False,
        )

    def _begin_edit_existing(self, item: AnnotationItem) -> None:
        ann = item.annotation
        if ann.is_number:
            # 数字的内容由序号决定，不能直接改字——要改编号请走覆盖/插队（Phase 4）
            return
        rect = item.mapRectToScene(item.text_rect)
        view_pos = self.mapFromScene(rect.topLeft())
        self._editing_ann = ann
        self._editing_item = item
        self._editing_page = ann.page
        self._editing_local = QPointF(ann.x, ann.y)
        self._editor.open_at(
            QPoint(view_pos.x(), view_pos.y()),
            ann.content,
            self._font_for(ann.style),
            QColor(ann.style.color),
            max(80.0, rect.width() * self._zoom),
            select_all=True,
        )

    def _on_editor_committed(self, text: str) -> None:
        ann = self._editing_ann
        item = self._editing_item
        created: Annotation | None = None

        if ann is None:
            if text:
                created = Annotation(
                    kind=KIND_TEXT,
                    page=self._editing_page,
                    x=self._editing_local.x(),
                    y=self._editing_local.y(),
                    content=text,
                    style=self._default_style,
                )
                self._push(AddAnnotationCommand(self.store, created, "添加文字"))
        elif text and text != ann.content:
            self._push(
                ModifyAnnotationCommand(self.store, ann, ann.with_content(text), "修改文字")
            )
        elif not text:
            # 内容被清空 => 等价删除，给撤销留一条路
            self._push(RemoveAnnotationsCommand(self.store, [ann], "删除文字"))

        self._editing_ann = None
        self._editing_item = None
        if created is not None:
            self.select_annotation(created.id)
        elif item is not None:
            item.setSelected(True)
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_editor_cancelled(self) -> None:
        self._editing_ann = None
        self._editing_item = None
        self.setFocus(Qt.FocusReason.OtherFocusReason)

    def edit_selected(self) -> None:
        """F5/双击之后，回车改字；给主窗口菜单用。"""
        selected = [
            item for item in self._scene.selectedItems() if isinstance(item, AnnotationItem)
        ]
        if selected:
            self._begin_edit_existing(selected[0])

    def delete_selected(self) -> int:
        selected = [
            item for item in self._scene.selectedItems() if isinstance(item, AnnotationItem)
        ]
        if not selected or self.store is None:
            return 0
        # 第 8.3 节：锁定的标注不能删（要删先解锁）
        anns = [item.annotation for item in selected if not item.annotation.locked]
        skipped = len(selected) - len(anns)
        if skipped:
            self.message.emit(f"有 {skipped} 个标注已锁定，跳过未删（右键可解锁）")
        if not anns:
            return 0
        self._push(RemoveAnnotationsCommand(self.store, anns))
        return len(anns)

    def toggle_locked_selected(self) -> int:
        """锁定 / 解锁选中标注（第 8.3 节）。"""
        return self._set_flag_for_selection("locked", "锁定标注", "解锁标注")

    def toggle_hidden_selected(self) -> int:
        """隐藏 / 显示选中标注（第 8.3 节）。"""
        return self._set_flag_for_selection("hidden", "隐藏标注", "显示标注")

    def _set_flag_for_selection(self, field_name: str, on_label: str, off_label: str) -> int:
        selected = [ann for ann in self.selected_annotations()]
        if not selected or self.store is None:
            return 0
        target = not all(getattr(ann, field_name) for ann in selected)
        label = on_label if target else off_label
        if self.undo_stack is not None:
            self.undo_stack.beginMacro(f"{label}（{len(selected)} 个）")
        try:
            for ann in selected:
                if bool(getattr(ann, field_name)) == target:
                    continue
                method = ann.with_locked if field_name == "locked" else ann.with_hidden
                self._push(ModifyAnnotationCommand(self.store, ann, method(target), label))
        finally:
            if self.undo_stack is not None:
                self.undo_stack.endMacro()
        return len(selected)

    def set_annotations_visible(self, visible: bool) -> None:
        """视图级显示/隐藏全部标注（不影响单条标注的 hidden 状态）。"""
        self._annotations_visible = bool(visible)
        for item in self._ann_items.values():
            item.set_annotations_visible(self._annotations_visible)

    @property
    def annotations_visible(self) -> bool:
        return self._annotations_visible

    def apply_style_to_selection(self, style: AnnotationStyle) -> int:
        selected = [
            item for item in self._scene.selectedItems() if isinstance(item, AnnotationItem)
        ]
        if not selected or self.store is None:
            return 0
        if self.undo_stack is not None:
            self.undo_stack.beginMacro(f"修改 {len(selected)} 个标注的样式")
        try:
            for item in selected:
                ann = item.annotation
                self._push(ModifyAnnotationCommand(self.store, ann, ann.with_style(style), "修改样式"))
        finally:
            if self.undo_stack is not None:
                self.undo_stack.endMacro()
        return len(selected)

    def copy_selected(self) -> int:
        # 数字标注是"编号序列里的槽位"，复制出来会和原标注撞号，所以不复制
        anns = [ann for ann in self.selected_annotations() if not ann.is_number]
        if not anns:
            return 0
        self._clipboard = list(anns)
        return len(anns)

    def paste_clipboard(self) -> int:
        if not self._clipboard or self.store is None or not self._items:
            return 0
        if self.undo_stack is not None:
            self.undo_stack.beginMacro("粘贴标注")
        try:
            for ann in self._clipboard:
                clone = Annotation(
                    kind=ann.kind,
                    page=self._current,
                    x=ann.x + PASTE_OFFSET,
                    y=ann.y + PASTE_OFFSET,
                    content=ann.content,
                    style=ann.style,
                    seq_group=ann.seq_group,
                    seq_index=ann.seq_index,
                )
                self._push(AddAnnotationCommand(self.store, clone, "粘贴"))
        finally:
            if self.undo_stack is not None:
                self.undo_stack.endMacro()
        return len(self._clipboard)

    # ------------------------------------------------------------------ 右键菜单

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """第 7.3 节：右键菜单删除；第 8.3 节：锁定/隐藏也放这里。"""
        item = self._annotation_at(self.mapToScene(event.pos()))
        if item is not None and not item.isSelected():
            self._scene.clearSelection()
            item.setSelected(True)
        anns = self.selected_annotations()
        if not anns:
            # 数字标注模式下对着空白处点右键 = 打开「起始值与步长」。
            # 打号打到一半发现起始值/步长不对，是现场最常见的操作，
            # 不该逼用户跑回菜单栏（工具 → 数字起始值与步长…）。
            if self._mode is ToolMode.NUMBER:
                self.number_settings_requested.emit()
                event.accept()
                return
            super().contextMenuEvent(event)
            return

        count = len(anns)
        suffix = "" if count == 1 else f" {count} 个"
        menu = QMenu(self)

        if count == 1 and not anns[0].is_number:
            menu.addAction("编辑文字").triggered.connect(self.edit_selected)
        if any(not ann.is_number for ann in anns):
            menu.addAction("复制").triggered.connect(self.copy_selected)

        all_locked = all(ann.locked for ann in anns)
        menu.addAction("解锁" if all_locked else f"锁定{suffix}").triggered.connect(
            self.toggle_locked_selected
        )
        menu.addAction(f"隐藏{suffix}").triggered.connect(self.toggle_hidden_selected)

        menu.addSeparator()
        menu.addAction(f"删除{suffix}").triggered.connect(self.delete_selected)

        menu.exec(event.globalPos())
        event.accept()

    # ------------------------------------------------------------------ 事件

    def _on_tile_ready(self) -> None:
        # 别在这一帧里直接 update()：一页的瓦片是成串回来的，攒到下一帧一起画
        if not self._update_timer.isActive():
            self._update_timer.start()

    def _sync_requests(self) -> None:
        """把「当前看得见的预览图和瓦片」排进渲染队列。

        这是**唯一**发渲染请求的地方（除了专门预热邻居页的 ``_warm_neighbors``）。
        以前写在 ``PdfPageItem.paint()`` 里，而 ``request_page`` 会同步 ``load_page``：
        等于每画一帧就在绘制路径里解析一次 PDF，翻页/拖动就一顿一顿的。
        """
        manager = self.render_manager
        if manager is None or not self._items:
            return
        view = self.mapToScene(self.viewport().rect()).boundingRect()
        for item in self._scene.items(view):
            if isinstance(item, PdfPageItem):
                item.request_visible(item.mapRectFromScene(view))

    # --- 识别区域（第 10 章）------------------------------------------------

    def refresh_tiles(self) -> None:
        """让所有页面重新走一遍绘制/请求。

        从工作台切回单页视图时要用：那边设置了"只看这几页"的过滤，
        切回来必须重新发请求，否则之前被判掉的任务永远不会重来。
        """
        for item in self._items:
            item.update()
        self._sync_requests()
        self.viewport().update()

    def force_refresh(self) -> None:
        """丢掉缓存、重新渲染当前画面（工具栏的「强制刷新」，也给切标签兜底）。

        正常流程里缓存是为了快；但万一画面真的空了（比如渲染任务被作废后
        没人再发），这是用户手里那颗"不用想为什么，先让我看见图"的按钮。
        """
        manager = self.render_manager
        if manager is not None:
            try:
                manager.invalidate()
            except Exception:  # noqa: BLE001 - 清缓存失败也不该卡住界面
                pass
        # 刚切回来时可能还挂着「未安定」，强制刷新就是要立刻出图
        self._settled = True
        self._zooming = False
        try:
            self._settle_timer.stop()
            self._zoom_timer.stop()
        except RuntimeError:
            pass
        for item in self._items:
            item.update()
        self._sync_requests()
        self.viewport().update()

    def set_scan_region(self, region) -> None:
        """设置（或清空）识别区域。存的是页面比例，每页各自换算。"""
        self._scan_region = region
        for item in self._items:
            item.scan_rect = self._region_rect(item)
            item.update()

    @property
    def scan_region(self):
        return self._scan_region

    def set_annot_region(self, region) -> None:
        """设置（或清空）数据标注的落字区域：文字从这块地方的左上角开始写。"""
        self._annot_region = region
        for item in self._items:
            item.annot_rect = self._annot_rect(item)
            item.update()

    def set_material_region(self, region) -> None:
        """设置（或清空）材料表识别区域；每页按同样比例换算。"""
        self._material_region = region
        for item in self._items:
            item.material_rect = self._material_rect(item)
            item.update()

    def _material_rect(self, item: "PdfPageItem"):
        region = self._material_region
        if region is None:
            return None
        x0, y0, x1, y1 = region.to_pdf(item.page_w, item.page_h)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    @property
    def annot_region(self):
        return self._annot_region

    @property
    def material_region(self):
        return self._material_region

    def set_page_number(self, spec) -> None:
        """设置/清空页码预览；spec 为 core.page_number.PageNumberSpec。"""
        self._page_number_spec = spec
        for item in self._items:
            item.update()

    @property
    def page_number_spec(self):
        return self._page_number_spec

    def _annot_rect(self, item: "PdfPageItem"):
        region = self._annot_region
        if region is None:
            return None
        x0, y0, x1, y1 = region.to_pdf(item.page_w, item.page_h)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _region_rect(self, item: "PdfPageItem"):
        region = self._scan_region
        if region is None:
            return None
        x0, y0, x1, y1 = region.to_pdf(item.page_w, item.page_h)
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _begin_region_drag(self, viewport_pos) -> None:
        scene_pos = self.mapToScene(viewport_pos)
        item = self._page_item_at(scene_pos)
        if item is None:
            self.message.emit("请在页面上框选识别区域")
            return
        local = item.mapFromScene(scene_pos)
        self._region_page = item.index
        self._region_origin = local
        item.drag_rect = QRectF(local, local)
        item.update()

    def _update_region_drag(self, viewport_pos) -> None:
        if self._region_origin is None or not (0 <= self._region_page < len(self._items)):
            return
        item = self._items[self._region_page]
        local = item.mapFromScene(self.mapToScene(viewport_pos))
        item.drag_rect = QRectF(self._region_origin, local).normalized()
        item.update()

    def _cancel_region_drag(self) -> None:
        """把没框完的选区丢掉（换模式、Esc、拖出窗口、换文档时都要来一下）。"""
        if self._region_origin is None:
            return
        self._region_origin = None
        if 0 <= self._region_page < len(self._items):
            item = self._items[self._region_page]
            if item.drag_rect is not None:
                item.drag_rect = None
                item.update()

    def _finish_region_drag(self, viewport_pos) -> None:
        if self._region_origin is None:
            return
        self._update_region_drag(viewport_pos)
        item = self._items[self._region_page] if 0 <= self._region_page < len(self._items) else None
        rect = item.drag_rect if item is not None else None
        self._region_origin = None
        if item is not None:
            item.drag_rect = None
            item.update()
        if rect is None or rect.width() < 2.0 or rect.height() < 2.0:
            self.message.emit("框选的区域太小了，再拖大一点")
            return
        self.region_picked.emit(
            item.index, rect.left(), rect.top(), rect.right(), rect.bottom()
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._editor.active:
            # 点击别处 = 确认当前输入，且这次点击不再触发新建
            self._editor.finish(True)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._mode is ToolMode.REGION:
            self._begin_region_drag(event.position().toPoint())
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._mode in (
            ToolMode.TEXT,
            ToolMode.NUMBER,
            ToolMode.OVERWRITE,
            ToolMode.INSERT,
        ):
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._mode is ToolMode.OVERWRITE:
                # 覆盖模式不新建，只把附近已有的数字挪到点击处（第 6.1 节）
                self._handle_overwrite_click(scene_pos)
                event.accept()
                return
            if self._mode is ToolMode.INSERT:
                self._handle_insert_click(scene_pos)
                event.accept()
                return
            # 点在已有标注上 = 选中/拖动它；点空白才算"新建"
            if self._annotation_at(scene_pos) is None:
                if self._mode is ToolMode.TEXT:
                    self._begin_new_text(scene_pos)
                else:
                    self._create_number(scene_pos)
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._region_origin is not None and not (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            # 鼠标左键已经松开（或者用户在窗口外松的手），这次框选作废
            self._cancel_region_drag()
        if self._region_origin is not None:
            self._update_region_drag(event.position().toPoint())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._region_origin is not None and event.button() == Qt.MouseButton.LeftButton:
            self._finish_region_drag(event.position().toPoint())
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and not self._editor.active:
            scene_pos = self.mapToScene(event.position().toPoint())
            item = self._annotation_at(scene_pos)
            if item is not None:
                self._begin_edit_existing(item)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._editor.active:
            self._editor.finish(True)
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            if steps:
                factor = 1.12**steps
                self._fit_mode = "none"
                self.set_zoom(self._zoom * factor, QPointF(event.position()))
            event.accept()
            return
        super().wheelEvent(event)

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        super().scrollContentsBy(dx, dy)
        # 横向也要降级：A3 图在 fit-width 下根本没法整页塞进屏幕，横拖是常态。
        # 以前只判 dy，横拖全程按当前倍率排满瓦片，每块几十毫秒握着 GIL，
        # 拖起来就是一顿一顿。
        if (dx or dy) and self._items:
            # 滚动中先看预览图，停下来再补高清——否则滚动一次就排一堆渲染
            self._mark_unsettled()
        self._emit_page_changed()
        self._sync_requests()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if not self._items:
            return
        if self._fit_mode == "width":
            self.fit_width()
        elif self._fit_mode == "page":
            self.fit_page()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            self._cancel_region_drag()
        if key == Qt.Key.Key_Escape and self._mode is not ToolMode.BROWSE:
            self.set_mode(ToolMode.BROWSE)
            event.accept()
            return
        if key in (Qt.Key.Key_PageDown, Qt.Key.Key_Space):
            self.next_page()
            event.accept()
            return
        if key == Qt.Key.Key_PageUp:
            self.prev_page()
            event.accept()
            return
        if key == Qt.Key.Key_Home:
            self.goto_page(0)
            event.accept()
            return
        if key == Qt.Key.Key_End:
            self.goto_page(len(self._items) - 1)
            event.accept()
            return
        super().keyPressEvent(event)

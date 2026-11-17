"""工作台：把相关的几页摆在一块画布上，一起看、一起标。

和 ``pdf_viewer`` 的区别：
- 那边是"按 PDF 页序连续翻页"，这边是"我自己挑几页、自己摆位置"。
- 页面在这里是**卡片**，卡片可以拖动、可以缩放画布看细节，但**不改 PDF 页序**。
- 标注还是同一套数据（`AnnotationStore` + 全局编号），所以在卡片上标数字，
  序号和单页视图里连续。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.annotation import (
    ALIGN_CENTER,
    KIND_TEXT,
    Annotation,
    AnnotationStore,
    AnnotationStyle,
)
from core.history import AddAnnotationCommand, ModifyAnnotationCommand, RemoveAnnotationsCommand
from core.sequence import SequenceManager
from core.workbench import DEFAULT_CARD_SCALE, Board, BoardStore, parse_page_spec
from ui import theme
from ui.annotation_items import AnnotationItem
from ui.inline_editor import InlineTextEditor
from ui.pdf_viewer import ToolMode

#: 卡片顶部的页码标签高度（场景单位 px）
BADGE_H = 22.0

#: 工作台上标注的固定屏幕缩放（配合 ItemIgnoresTransformations，
#: 一张 A1 卡片缩到 0.2 倍时，16pt 的数字仍然有十几像素高，看得见也点得中）
WORKBENCH_LABEL_SCALE = 0.8


class _PageLayer(QGraphicsItem):
    """卡片里的"页面坐标系"层：把标注放在 PDF 坐标上，靠父项缩放显示。"""

    def __init__(self, width: float, height: float, parent: QGraphicsItem | None = None) -> None:
        super().__init__(parent)
        self._rect = QRectF(0.0, 0.0, width, height)

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(self._rect)

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        return


class WorkbenchCard(QGraphicsObject):
    """工作台上的一张页面卡片。"""

    moved = Signal(int, float, float)  # page, x, y

    def __init__(
        self,
        page: int,
        width: float,
        height: float,
        scale: float,
        render_manager,
        parent: QGraphicsItem | None = None,
    ) -> None:
        super().__init__(parent)
        self.page = int(page)
        self.page_width = float(width)
        self.page_height = float(height)
        self.scale = float(scale) or DEFAULT_CARD_SCALE
        self._render_manager = render_manager
        self._rect = QRectF(
            0.0, 0.0, self.page_width * self.scale, self.page_height * self.scale + BADGE_H
        )
        self._render_scale = max(self.scale * 2.0, 0.5)
        #: 上一次渲染好的位图。换分辨率时先拿它拉伸顶着，别闪成"渲染中…"
        self._fallback: QPixmap | None = None
        #: 缩放时不要每动一下就重新渲染：停下来 160 ms 再算（不然一直在换 key）
        self._want_scale = self._render_scale
        self._scale_timer = QTimer(self)
        self._scale_timer.setSingleShot(True)
        self._scale_timer.setInterval(160)
        self._scale_timer.timeout.connect(self._apply_render_scale)

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(1)
        self.setAcceptHoverEvents(True)

        self.layer = _PageLayer(self.page_width, self.page_height, self)
        self.layer.setScale(self.scale)
        self.layer.setPos(0.0, BADGE_H)

        self._pixmap_key = self._render_manager.page_key(self.page, self._render_scale)
        self._request()

    # -- 视口缩放联动 -----------------------------------------------------

    def set_view_zoom(self, view_zoom: float) -> None:
        """工作台放大到多少，就按多少的分辨率渲染这张卡片。

        卡片按 0.2 缩小时，固定 2 倍渲染在放大后就是糊的；这里让渲染倍率跟着
        视口缩放走，放多大都是实的。不缩水：至少保持 2 倍超采样（跟改之前一样清楚），
        放大时再往上加。上限 6 倍——再往上单张位图就太吃内存了。
        """
        base = max(self.scale * float(view_zoom), self.scale)
        wanted = min(max(base * 2.0, 0.4), 6.0)
        # 一张图最多 1200 万像素（A1 约 2.9 倍），再大就太吃内存了
        max_scale = (12_000_000.0 / max(self.page_width * self.page_height, 1.0)) ** 0.5
        wanted = min(wanted, max(max_scale, 0.4))
        # 量化到 0.05 一档：滚轮连续微调时不会每隔一点就换一个缓存键，
        # 换了键就等于缓存全丢、重新渲染一次
        wanted = round(wanted * 20.0) / 20.0
        self._want_scale = wanted
        self._scale_timer.start()
        self.update()

    def _apply_render_scale(self) -> None:
        """缩放停下来之后才真的换分辨率。"""
        wanted = self._want_scale
        if abs(wanted - self._render_scale) / max(self._render_scale, 1e-6) < 0.06:
            return
        self._render_scale = wanted
        self._pixmap_key = self._render_manager.page_key(self.page, self._render_scale)
        self._request()
        self.update()

    # -- 几何 -------------------------------------------------------------

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(self._rect)

    def card_rect(self) -> QRectF:
        """页面本体在卡片内的矩形（不含页码标签）。"""
        return QRectF(
            0.0, BADGE_H, self.page_width * self.scale, self.page_height * self.scale
        )

    def content_rect(self) -> QRectF:
        return QRectF(self._rect)

    def page_point(self, scene_pos: QPointF) -> QPointF:
        """场景坐标 → PDF 页面坐标。"""
        local = self.mapFromScene(scene_pos)
        x = (local.x()) / self.scale
        y = (local.y() - BADGE_H) / self.scale
        return QPointF(min(max(x, 0.0), self.page_width), min(max(y, 0.0), self.page_height))

    # -- 渲染 -------------------------------------------------------------

    def _request(self) -> None:
        self._render_manager.request_page(self.page, self._render_scale)

    def pixmap(self) -> QPixmap | None:
        return self._render_manager.lookup(self._pixmap_key)

    def refresh_pixmap(self) -> None:
        pixmap = self.pixmap()
        if pixmap is not None and not pixmap.isNull():
            # 自己留一份强引用：卡片上画的东西不能指望缓存——缓存是按兆压的，
            # 两张 A1 的整页图就可能把它挤爆，一挤爆就成了"取不到→再请求"的死循环。
            self._fallback = pixmap
        elif self._fallback is None:
            self._request()
        self.update()

    def release_pixmap(self) -> None:
        """离开工作台时把整页图交还出去（一张 A1 就十几兆）。"""
        self._fallback = None
        self.update()

    # -- 绘制 -------------------------------------------------------------

    def paint(self, painter: QPainter, option, widget=None) -> None:  # noqa: N802
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        body = QRectF(0.0, BADGE_H, self.page_width * self.scale, self.page_height * self.scale)

        painter.fillRect(body, QColor(theme.SURFACE))
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            # 换分辨率期间先拿旧图画着（拉伸），比闪一下"渲染中…"强得多
            pixmap = self._fallback
        if pixmap is not None and not pixmap.isNull():
            painter.drawPixmap(body, pixmap, QRectF(pixmap.rect()))
        else:
            painter.setPen(QPen(QColor(theme.FAINT)))
            painter.drawText(body, Qt.AlignmentFlag.AlignCenter, f"第 {self.page + 1} 页 渲染中…")

        painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(body)

        if self.isSelected():
            pen = QPen(QColor(theme.ACCENT), 2.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawRect(self._rect.adjusted(0.5, 0.5, -0.5, -0.5))

        # 页码标签
        badge = QRectF(0.0, 0.0, self._rect.width(), BADGE_H)
        painter.fillRect(badge, QColor(theme.SUNKEN))
        painter.setPen(QPen(QColor(theme.MUTED)))
        font = QFont(theme.FONT_FAMILY)
        font.setPointSizeF(8.5)
        painter.setFont(font)
        painter.drawText(
            badge.adjusted(8, 0, -8, 0),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            f"第 {self.page + 1} 页",
        )

    # -- 拖动 -------------------------------------------------------------

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.moved.emit(self.page, self.pos().x(), self.pos().y())
        return super().itemChange(change, value)


class WorkbenchView(QGraphicsView):
    """工作台画布。"""

    message = Signal(str)
    mode_changed = Signal(object)
    selection_changed = Signal(list)
    board_touched = Signal()   # 卡片移动等，需要让项目变脏

    MIN_ZOOM = 0.05
    MAX_ZOOM = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setBackgroundBrush(QColor(theme.canvas_color()))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # 从左侧缩略图拖页进来（见 dragEnterEvent / dropEvent）
        self.setAcceptDrops(True)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)

        self.boards: BoardStore | None = None
        self.render_manager = None
        self.store: AnnotationStore | None = None
        self.sequence: SequenceManager | None = None
        self.undo_stack = None
        self.page_sizes: list[tuple[float, float]] = []

        self._cards: dict[int, WorkbenchCard] = {}
        self._items: dict[str, AnnotationItem] = {}
        self._mode = ToolMode.BROWSE
        self._default_style = AnnotationStyle()
        self._number_style = AnnotationStyle(
            font_size=16.0, color="#d0342c", bold=True, align=ALIGN_CENTER
        )
        self._pan_origin: QPoint | None = None
        self._pan_scroll: tuple[int, int] = (0, 0)

        self.editor = InlineTextEditor(self.viewport())
        self.editor.committed.connect(self._on_editor_committed)
        self.editor.cancelled.connect(self._on_editor_cancelled)
        self._editing_page: int | None = None
        self._editing_pos: QPointF | None = None
        self._editing_ann: Annotation | None = None
        #: 正在拖的卡片页号（松开时判断有没有拖到垃圾桶上）
        self._dragging_page: int | None = None
        self._trash_hot = False
        #: 从缩略图拖页进来时，鼠标在视口里的位置（用来画落点提示）
        self._drop_hint: QPoint | None = None

        self._scene.selectionChanged.connect(self._on_scene_selection)

    # ------------------------------------------------------------------ 接入

    def set_document(
        self,
        page_sizes: list[tuple[float, float]],
        render_manager,
        store: AnnotationStore,
        sequence: SequenceManager,
        undo_stack,
        boards: BoardStore,
    ) -> None:
        self.page_sizes = [(float(w), float(h)) for w, h in page_sizes]
        self.render_manager = render_manager
        self.store = store
        self.sequence = sequence
        self.undo_stack = undo_stack
        self.boards = boards
        self.render_manager.tile_ready.connect(self._on_tile_ready)
        self.store.added.connect(self._on_store_changed)
        self.store.removed.connect(self._on_store_changed)
        self.store.changed.connect(self._on_store_changed)
        self.store.cleared.connect(self._on_store_changed)
        self.refresh_board()

    def release_pixmaps(self) -> None:
        """离开工作台时把卡片持有的整页图全放掉（配合清空预览缓存）。"""
        for card in self._cards.values():
            card.release_pixmap()

    def clear_document(self) -> None:
        if self.render_manager is not None:
            try:
                self.render_manager.tile_ready.disconnect(self._on_tile_ready)
            except (RuntimeError, TypeError):
                pass
        if self.store is not None:
            for signal in (self.store.added, self.store.removed, self.store.changed, self.store.cleared):
                try:
                    signal.disconnect(self._on_store_changed)
                except (RuntimeError, TypeError):
                    pass
        if self.render_manager is not None:
            self.render_manager.clear_focus_pages()
        self._items.clear()
        self._cards.clear()
        self._scene.clear()
        self.page_sizes = []
        # 这里必须清空 render_manager：会话关掉后还会再 detach 一次，
        # 留着它就会对着已经断过的信号再 disconnect，Qt 每次都警告一句。
        self.render_manager = None
        self.store = None
        self.sequence = None
        self.undo_stack = None
        self.boards = None

    # ------------------------------------------------------------------ 布局

    def refresh_board(self) -> None:
        """按当前工作台重建卡片。"""
        self._finish_editing(commit=True)
        self._items.clear()
        self._cards.clear()
        self._scene.clear()

        board = self.boards.current() if self.boards is not None else None
        if board is None or not self.page_sizes:
            self._update_scene_rect()
            return

        for card_data in board.ordered():
            if not 0 <= card_data.page < len(self.page_sizes):
                continue
            width, height = self.page_sizes[card_data.page]
            card = WorkbenchCard(
                card_data.page, width, height, card_data.scale, self.render_manager
            )
            card.setPos(card_data.x, card_data.y)
            card.setZValue(float(card_data.z))
            card.moved.connect(self._on_card_moved)
            self._scene.addItem(card)
            self._cards[card_data.page] = card
            self._build_annotations(card_data.page)

        self._update_scene_rect()
        self.set_mode(self._mode)
        self._sync_render_scale()

        # 台上这些页都算"正在看"：否则渲染调度的单页视野过滤会把它们当翻走的页丢掉，
        # 卡片就一直停在"渲染中"。
        if self.render_manager is not None:
            self.render_manager.set_focus_pages(self._cards.keys())

    def _build_annotations(self, page: int) -> None:
        card = self._cards.get(page)
        if card is None or self.store is None:
            return
        for ann in self.store.on_page(page):
            self._make_item(ann, card)

    def _make_item(self, ann: Annotation, card: WorkbenchCard) -> AnnotationItem:
        resolver = self.sequence.text_for if self.sequence is not None else None
        item = AnnotationItem(ann, card.layer, text_resolver=resolver)
        # 卡片是按缩小的比例画整页的（默认 0.2），16pt 的数字缩到卡片上只有 3 个像素，
        # 在"一眼看几张图"的工作台上根本认不出。所以让标注忽略缩放、
        # 始终以固定的屏幕尺寸显示——位置上仍然钉死在页面坐标上（像地图上的图钉）。
        item.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        item.setScale(WORKBENCH_LABEL_SCALE)
        item.move_finished.connect(self._on_item_moved)
        self._items[ann.id] = item
        return item

    def _update_scene_rect(self) -> None:
        board = self.boards.current() if self.boards is not None else None
        if board is None or not self._cards:
            self._scene.setSceneRect(QRectF(-400, -300, 1600, 1200))
            return
        rect = QRectF()
        for card in self._cards.values():
            rect = rect.united(card.sceneBoundingRect())
        self._scene.setSceneRect(rect.adjusted(-240, -200, 240, 200))

    def auto_layout(self, columns: int = 0) -> None:
        """按图幅大小自动排成网格（大张占位大，比例不变）。"""
        if self.boards is None or not self._cards:
            return
        cards = list(self.boards.current().cards)
        if not cards:
            return
        count = len(cards)
        if columns <= 0:
            columns = max(1, int(count ** 0.5 + 0.999))

        gap = 40.0
        x_cursor = 0.0
        row_heights: dict[int, float] = {}
        positions: dict[int, tuple[float, float]] = {}
        for index, card_data in enumerate(cards):
            card = self._cards.get(card_data.page)
            width = card.card_rect().width() if card else 480.0
            height = card.card_rect().height() if card else 360.0
            row, column = divmod(index, columns)
            if column == 0:
                x_cursor = 0.0
            y = 0.0
            for prev_row in range(row):
                y += row_heights.get(prev_row, 0.0) + gap
            positions[card_data.page] = (x_cursor, y)
            row_heights[row] = max(row_heights.get(row, 0.0), height)
            x_cursor += width + gap

        self.boards.set_positions(positions)
        for page, (x, y) in positions.items():
            card = self._cards.get(page)
            if card is not None:
                card.blockSignals(False)
                card.setPos(x, y)
        self._update_scene_rect()
        self.fit_all()

    def fit_all(self) -> None:
        if not self._cards:
            return
        rect = QRectF()
        for card in self._cards.values():
            rect = rect.united(card.sceneBoundingRect())
        if rect.isNull():
            return
        rect = rect.adjusted(-40, -40, 40, 40)
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._sync_render_scale()

    def zoom_in(self) -> None:
        self._zoom_by(1.15)

    def zoom_out(self) -> None:
        self._zoom_by(1 / 1.15)

    def _zoom_by(self, factor: float) -> None:
        zoom = self.transform().m11() * factor
        if zoom < self.MIN_ZOOM or zoom > self.MAX_ZOOM:
            return
        self.scale(factor, factor)
        self._sync_render_scale()

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._sync_render_scale()

    def _sync_render_scale(self) -> None:
        """把当前视口缩放告诉每张卡片，让它们按屏幕实际分辨率渲染。"""
        zoom = self.transform().m11()
        for card in self._cards.values():
            card.set_view_zoom(zoom)

    # ------------------------------------------------------------------ 模式

    mode = property(lambda self: self._mode)

    def set_mode(self, mode: ToolMode) -> None:
        if mode not in (ToolMode.BROWSE, ToolMode.TEXT, ToolMode.NUMBER):
            mode = ToolMode.BROWSE
        self._mode = mode
        if mode == ToolMode.BROWSE:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

        buttons = (
            Qt.MouseButton.LeftButton
            if mode in (ToolMode.TEXT, ToolMode.NUMBER)
            else Qt.MouseButton.NoButton
        )
        for item in self._items.values():
            item.setAcceptedMouseButtons(buttons)
            if buttons == Qt.MouseButton.NoButton:
                item.setSelected(False)
        for card in self._cards.values():
            card.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, mode == ToolMode.BROWSE)
        self.mode_changed.emit(mode)

    @property
    def default_style(self) -> AnnotationStyle:
        return self._number_style if self._mode == ToolMode.NUMBER else self._default_style

    def style_for_mode(self, mode: ToolMode) -> AnnotationStyle:
        if mode == ToolMode.NUMBER:
            return self._number_style
        return self._default_style

    def set_default_style(self, style: AnnotationStyle, mode: ToolMode | None = None) -> None:
        target = mode or self._mode
        if target == ToolMode.NUMBER:
            self._number_style = style
        else:
            self._default_style = style

    def set_number_style(self, style: AnnotationStyle) -> None:
        self._number_style = style

    def set_number_text_resolver(self, resolver) -> None:
        for item in self._items.values():
            item.set_text_resolver(resolver)

    # ------------------------------------------------------------------ 鼠标

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self.editor.active and event.button() == Qt.MouseButton.LeftButton:
            self._finish_editing(commit=True)
            event.accept()
            return

        if event.button() == Qt.MouseButton.MiddleButton:
            self._pan_origin = event.position().toPoint()
            self._pan_scroll = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._mode in (
            ToolMode.TEXT,
            ToolMode.NUMBER,
        ):
            card = self._card_at(event.position().toPoint())
            # 注意：不能用 itemAt() 判断"是不是点在空白处"——卡片自己就铺满整页，
            # itemAt 永远返回卡片。要判的是"别落在已有标注上"，让标注自己处理拖动。
            if card is not None and self._annotation_at(event.position().toPoint()) is None:
                scene_pos = self.mapToScene(event.position().toPoint())
                if self._mode == ToolMode.NUMBER:
                    self._create_number(card, scene_pos)
                else:
                    self._begin_new_text(card, scene_pos)
                event.accept()
                return

        if event.button() == Qt.MouseButton.LeftButton:
            # 记住"正在拖哪张卡"，松开时看是不是拖到垃圾桶上了
            card = self._card_at(event.position().toPoint())
            if card is not None and self._annotation_at(event.position().toPoint()) is None:
                self._dragging_page = card.page
        super().mousePressEvent(event)

    # -- 拖到垃圾桶 -------------------------------------------------------
    def _trash_rect(self) -> QRectF:
        """右下角那块"垃圾桶"，视口坐标。"""
        size = 88.0
        margin = 18.0
        width = self.viewport().width()
        height = self.viewport().height()
        return QRectF(width - size - margin, height - size - margin, size, size)

    def _over_trash(self, pos) -> bool:
        return self._trash_rect().contains(QPointF(pos))

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if self._dragging_page is None and self._drop_hint is None:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._drop_hint is not None:
            self._paint_drop_hint(painter)
        if self._dragging_page is not None:
            rect = self._trash_rect()
            hot = self._trash_hot
            painter.setPen(
                QPen(QColor(theme.DANGER), 2.0 if hot else 1.2, Qt.PenStyle.DashLine)
            )
            painter.setBrush(QColor(217, 58, 63, 46 if hot else 20))
            painter.drawRoundedRect(rect, 10.0, 10.0)
            painter.setPen(QColor(theme.DANGER if hot else theme.MUTED))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "拖到这里\n移出工作台")
        painter.end()

    def _paint_drop_hint(self, painter: QPainter) -> None:
        """从缩略图拖页进来时，先在地上画个影子告诉用户"会落在这"。"""
        pos = self._drop_hint
        if pos is None:
            return
        painter.setPen(QPen(QColor(theme.ACCENT), 1.4, Qt.PenStyle.DashLine))
        painter.setBrush(QColor(47, 107, 246, 22))
        rect = QRectF(pos.x() - 62, pos.y() - 44, 124, 88)
        painter.drawRoundedRect(rect, 8.0, 8.0)
        painter.setPen(QColor(theme.ACCENT))
        painter.drawText(
            QRectF(pos.x() - 120, pos.y() + 48, 240, 22),
            Qt.AlignmentFlag.AlignCenter,
            "松手就放到这里",
        )

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pan_origin is not None:
            delta = event.position().toPoint() - self._pan_origin
            self.horizontalScrollBar().setValue(self._pan_scroll[0] - delta.x())
            self.verticalScrollBar().setValue(self._pan_scroll[1] - delta.y())
            event.accept()
            return
        if self._dragging_page is not None:
            hot = self._over_trash(event.position())
            if hot != self._trash_hot:
                self._trash_hot = hot
                self.viewport().update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._pan_origin is not None and event.button() == Qt.MouseButton.MiddleButton:
            self._pan_origin = None
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._mode == ToolMode.BROWSE
                else Qt.CursorShape.CrossCursor
            )
            event.accept()
            return
        dropped = None
        if self._dragging_page is not None and event.button() == Qt.MouseButton.LeftButton:
            if self._over_trash(event.position()):
                dropped = self._dragging_page
            self._dragging_page = None
            self._trash_hot = False
            self.viewport().update()
        super().mouseReleaseEvent(event)
        if dropped is not None:
            self.remove_pages([dropped])
            return
        self._update_scene_rect()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        item = self._annotation_at(event.position().toPoint())
        if item is not None and not item.annotation.is_number:
            self._begin_edit_existing(item)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = event.angleDelta().y() / 120.0
            self._zoom_by(1.12 ** steps)
            event.accept()
            return
        delta = event.angleDelta().y() or event.angleDelta().x()
        bar = self.verticalScrollBar() if event.angleDelta().y() else self.horizontalScrollBar()
        bar.setValue(bar.value() - int(delta * 0.6))
        event.accept()
    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key == Qt.Key.Key_Escape:
            if self.editor.active:
                self._finish_editing(commit=False)
            else:
                self.set_mode(ToolMode.BROWSE)
                self.clear_selection()
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.delete_selected():
                event.accept()
                return
            cards = self.selected_cards()
            if cards:
                self.remove_pages([card.page for card in cards])
                event.accept()
                return
        if key == Qt.Key.Key_A and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            for item in self._items.values():
                if item.isVisible():
                    item.setSelected(True)
            event.accept()
            return
        super().keyPressEvent(event)

    # -- 从缩略图拖进来 -----------------------------------------------------
    def _drop_page_index(self, mime) -> int | None:
        """解析拖进来的页号；不是我们自己的格式就返回 None。"""
        if mime is None or not mime.hasFormat("application/x-qpdf-pages"):
            return None
        try:
            return int(bytes(mime.data("application/x-qpdf-pages")).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if self._drop_page_index(event.mimeData()) is not None:
            self._drop_hint = event.position().toPoint()
            self.viewport().update()
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if self._drop_page_index(event.mimeData()) is not None:
            self._drop_hint = event.position().toPoint()
            self._autoscroll(self._drop_hint)
            self.viewport().update()
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_hint = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def _autoscroll(self, pos) -> None:
        """拖到视口边缘时自己滚一滚，省得"想放到远处还得先滚过去"。"""
        viewport = self.viewport()
        margin = 36
        step = 18
        bar_h = self.horizontalScrollBar()
        bar_v = self.verticalScrollBar()
        if pos.x() < margin:
            bar_h.setValue(bar_h.value() - step)
        elif pos.x() > viewport.width() - margin:
            bar_h.setValue(bar_h.value() + step)
        if pos.y() < margin:
            bar_v.setValue(bar_v.value() - step)
        elif pos.y() > viewport.height() - margin:
            bar_v.setValue(bar_v.value() + step)

    def dropEvent(self, event) -> None:  # noqa: N802
        page = self._drop_page_index(event.mimeData())
        self._drop_hint = None
        self.viewport().update()
        if page is None:
            super().dropEvent(event)
            return
        # 让卡片的**中心**落在鼠标位置，松手后不用再挪一下
        scene_pos = self.mapToScene(event.position().toPoint())
        size = self.page_sizes[page] if 0 <= page < len(self.page_sizes) else (0.0, 0.0)
        half_w = float(size[0]) * DEFAULT_CARD_SCALE / 2 if size else 0.0
        half_h = float(size[1]) * DEFAULT_CARD_SCALE / 2 if size else 0.0
        added = self.boards.add_page(
            page, scene_pos.x() - half_w, scene_pos.y() - half_h
        )
        if not added:
            self.message.emit(f"第 {page + 1} 页已经在工作台上了")
        else:
            self.refresh_board()
            self._update_scene_rect()
            self.message.emit(f"已把第 {page + 1} 页拖进工作台")
        event.acceptProposedAction()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        item = self._annotation_at(event.pos())
        if item is not None:
            if not item.isSelected():
                self.clear_selection()
                item.setSelected(True)
            anns = self.selected_annotations()
            menu = QMenu(self)
            if len(anns) == 1 and not anns[0].is_number:
                menu.addAction("编辑文字", lambda: self._begin_edit_existing(item))
            menu.addAction("删除标注", self.delete_selected)
            menu.exec(event.globalPos())
            return
        card = self._card_at(event.pos())
        menu = QMenu(self)
        if card is not None:
            page = card.page
            board = self.boards.current() if self.boards is not None else None
            ordered = board.ordered() if board is not None else []
            rank = next((i for i, c in enumerate(ordered) if c.page == page), 0)
            z_menu = menu.addMenu("叠放次序")
            act_top = z_menu.addAction("置于顶层", lambda: self._z_order("front", page))
            act_up = z_menu.addAction("上移一层", lambda: self._z_order("raise", page))
            act_down = z_menu.addAction("下移一层", lambda: self._z_order("lower", page))
            act_back = z_menu.addAction("置于底层", lambda: self._z_order("back", page))
            act_up.setEnabled(rank < len(ordered) - 1)
            act_top.setEnabled(rank < len(ordered) - 1)
            act_down.setEnabled(rank > 0)
            act_back.setEnabled(rank > 0)
            menu.addAction(f"从工作台移除第 {page + 1} 页", lambda: self._remove_page(page))
        menu.addSeparator()
        menu.addAction("自动排列", self.auto_layout)
        menu.addAction("适应窗口", self.fit_all)
        menu.exec(event.globalPos())

    def _z_order(self, action: str, page: int) -> None:
        """调整卡片叠放次序。改完不重建整个工作台，只刷 zValue，避免闪一下。"""
        if self.boards is None:
            return
        changed = {
            "front": self.boards.bring_to_front,
            "back": self.boards.send_to_back,
            "raise": self.boards.raise_card,
            "lower": self.boards.lower_card,
        }[action](page)
        if not changed:
            return
        board = self.boards.current()
        for card_data in board.ordered():
            card = self._cards.get(card_data.page)
            if card is not None:
                card.setZValue(float(card_data.z))
        self.board_touched.emit()

    # ------------------------------------------------------------------ 标注

    def _card_at(self, viewport_pos: QPoint) -> WorkbenchCard | None:
        for item in self.items(viewport_pos):
            node = item
            while node is not None:
                if isinstance(node, WorkbenchCard):
                    return node
                node = node.parentItem()
        return None

    def _annotation_at(self, viewport_pos: QPoint) -> AnnotationItem | None:
        for item in self.items(viewport_pos):
            if isinstance(item, AnnotationItem):
                return item
        return None

    def selected_annotations(self) -> list[Annotation]:
        result: list[Annotation] = []
        for item in self._scene.selectedItems():
            if isinstance(item, AnnotationItem):
                result.append(item.annotation)
        return result

    def clear_selection(self) -> None:
        for item in self._items.values():
            item.setSelected(False)

    def select_annotation(self, ann_id: str) -> None:
        item = self._items.get(ann_id)
        if item is not None:
            self.clear_selection()
            item.setSelected(True)

    def _on_scene_selection(self) -> None:
        self.selection_changed.emit(self.selected_annotations())

    def _create_number(self, card: WorkbenchCard, scene_pos: QPointF) -> None:
        if self.sequence is None or self.undo_stack is None:
            return
        point = card.page_point(scene_pos)
        ann = self.sequence.make_number(
            card.page, point.x(), point.y(), style=self._number_style
        )
        self.undo_stack.push(AddAnnotationCommand(self.store, ann, "添加数字"))

    def delete_selected(self) -> int:
        anns = [ann for ann in self.selected_annotations() if not ann.locked]
        if not anns or self.undo_stack is None:
            return 0
        skipped = len(self.selected_annotations()) - len(anns)
        if skipped:
            self.message.emit(f"有 {skipped} 个标注已锁定，跳过未删（右键可解锁）")
        self.undo_stack.push(RemoveAnnotationsCommand(self.store, anns, "删除标注"))
        return len(anns)

    def apply_style_to_selection(self, style: AnnotationStyle) -> int:
        if self.undo_stack is None:
            return 0
        anns = self.selected_annotations()
        if not anns:
            return 0
        self.undo_stack.beginMacro("修改标注样式")
        for ann in anns:
            new = ann.with_style(style)
            if new == ann:
                continue
            self.undo_stack.push(ModifyAnnotationCommand(self.store, ann, new, "修改样式"))
        self.undo_stack.endMacro()
        return len(anns)

    # -- 空间整体平移（卡片拖动）-------------------------------------------

    def _on_card_moved(self, page: int, x: float, y: float) -> None:
        if self.boards is None:
            return
        self.boards.move_card(page, x, y)
        self.board_touched.emit()

    def _on_item_moved(self, old: Annotation, new: Annotation) -> None:
        if self.undo_stack is None:
            return
        self.undo_stack.push(ModifyAnnotationCommand(self.store, old, new, "移动标注"))

    def _remove_page(self, page: int) -> None:
        if self.boards is None:
            return
        self.boards.remove_page(page)
        self.refresh_board()
        self.message.emit(f"第 {page + 1} 页已移出工作台（PDF 本身没动）")

    def remove_pages(self, pages) -> int:
        """批量移出工作台（拖到垃圾桶 / 选中后按 Delete 走这里）。"""
        if self.boards is None:
            return 0
        done = 0
        for page in sorted({int(p) for p in pages}):
            if self.boards.remove_page(page):
                done += 1
        if done:
            self.refresh_board()
            self.message.emit(f"已从工作台移出 {done} 页（PDF 本身没动）")
        return done

    def selected_cards(self) -> list:
        out = []
        for item in self._scene.selectedItems():
            if isinstance(item, WorkbenchCard):
                out.append(item)
        return out

    # -- 数据变化 ---------------------------------------------------------

    def _on_store_changed(self, *_args) -> None:
        self.sync_annotations()

    def sync_annotations(self) -> None:
        """按 store 重建标注图元（工作台卡片不多，直接整体重建最省心）。"""
        if self.store is None:
            return
        for item in self._items.values():
            scene = item.scene()
            if scene is not None:
                scene.removeItem(item)
        self._items.clear()
        for page, card in self._cards.items():
            self._build_annotations(page)
        self.set_mode(self._mode)

    def _on_tile_ready(self) -> None:
        for card in self._cards.values():
            card.refresh_pixmap()

    # ------------------------------------------------------------------ 文字

    def _begin_new_text(self, card: WorkbenchCard, scene_pos: QPointF) -> None:
        point = card.page_point(scene_pos)
        self._editing_page = card.page
        self._editing_pos = point
        self._editing_ann = None
        self._open_editor(card, point, "", self._default_style)

    def _begin_edit_existing(self, item: AnnotationItem) -> None:
        card = self._cards.get(item.annotation.page)
        if card is None:
            return
        self._editing_page = item.annotation.page
        self._editing_pos = QPointF(item.annotation.x, item.annotation.y)
        self._editing_ann = item.annotation
        self._open_editor(card, self._editing_pos, item.annotation.content, item.annotation.style)

    def _open_editor(
        self, card: WorkbenchCard, point: QPointF, text: str, style: AnnotationStyle
    ) -> None:
        scene = card.layer.mapToScene(point)
        viewport = self.mapFromScene(scene)
        zoom = self.transform().m11()
        font = QFont(style.font_family)
        font.setPointSizeF(max(7.0, style.font_size * card.scale * zoom))
        if style.bold:
            font.setBold(True)
        if style.italic:
            font.setItalic(True)
        width_hint = max(180.0, 220.0 * card.scale * zoom)
        self.editor.open_at(viewport, text, font, QColor(style.color), width_hint)

    def _finish_editing(self, commit: bool) -> None:
        if self.editor.active:
            self.editor.finish(commit)

    def _on_editor_committed(self, text: str) -> None:
        text = (text or "").strip()
        page = self._editing_page
        point = self._editing_pos
        existing = self._editing_ann
        self._editing_page = None
        self._editing_pos = None
        self._editing_ann = None
        if page is None or point is None or self.undo_stack is None:
            return
        if not text:
            return
        if existing is not None:
            if text != existing.content:
                self.undo_stack.push(
                    ModifyAnnotationCommand(
                        self.store, existing, existing.with_content(text), "修改文字"
                    )
                )
            return
        ann = Annotation(
            id=new_id(),
            kind=KIND_TEXT,
            page=page,
            x=point.x(),
            y=point.y(),
            content=text,
            style=self._default_style,
        )
        self.undo_stack.push(AddAnnotationCommand(self.store, ann, "添加文字"))

    def _on_editor_cancelled(self) -> None:
        self._editing_page = None
        self._editing_pos = None
        self._editing_ann = None


class WorkbenchPanel(QWidget):
    """工作台 = 顶部工具条（切换工作台 / 加页 / 排布） + 画布。"""

    message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.view = WorkbenchView(self)
        self.view.message.connect(self.message)

        self._boards = QComboBox(self)
        self._boards.setMinimumWidth(120)
        self._boards.setToolTip("切换工作台")
        self._boards.currentIndexChanged.connect(self._on_board_changed)
        self._boards.setContextMenuPolicy(Qt.CustomContextMenu)
        self._boards.customContextMenuRequested.connect(self._on_combo_menu)

        def tool(text: str, tip: str, slot) -> QToolButton:
            button = QToolButton(self)
            button.setText(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            return button

        self._new_btn = tool("＋", "新建工作台", self.new_board)
        self._copy_btn = tool("复制", "复制当前工作台（页和摆位一起带走）", self.duplicate_board)
        self._rename_btn = tool("改名", "给当前工作台改名", self.rename_board)
        self._note_btn = tool("备注", "给当前工作台写一句备注", self.edit_note)
        self._delete_btn = tool("删除", "删除当前工作台（至少保留一块）", self.delete_board)

        self._add_btn = QPushButton("添加页面…", self)
        self._add_btn.setProperty("primary", False)
        self._add_btn.clicked.connect(self.add_pages_dialog)
        self._layout_btn = QPushButton("自动排列", self)
        self._layout_btn.clicked.connect(self.view.auto_layout)
        self._fit_btn = QPushButton("适应窗口", self)
        self._fit_btn.clicked.connect(self.view.fit_all)
        self._clear_btn = QPushButton("清空", self)
        self._clear_btn.setToolTip("把当前工作台上的页全部移出（PDF 本身不动）")
        self._clear_btn.clicked.connect(self.clear_board)
        self._clear_armed = False
        self._clear_timer = QTimer(self)
        self._clear_timer.setSingleShot(True)
        self._clear_timer.setInterval(3000)
        self._clear_timer.timeout.connect(self._disarm_clear)

        self._hint = QLabel("从缩略图右键「加入工作台」，或点「添加页面…」输入 4,7,9-15")
        self._hint.setObjectName("panelHint")

        # 备注行：左边写备注，右边写"建于 / 改于"
        self._note_label = QLabel("")
        self._note_label.setObjectName("panelHint")
        self._note_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._meta_label = QLabel("")
        self._meta_label.setObjectName("panelHint")
        note_bar = QHBoxLayout()
        note_bar.setContentsMargins(12, 0, 12, 6)
        note_bar.setSpacing(10)
        note_bar.addWidget(self._note_label, 1)
        note_bar.addWidget(self._meta_label, 0)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 12, 8)
        bar.setSpacing(8)
        bar.addWidget(QLabel("工作台"))
        bar.addWidget(self._boards)
        bar.addWidget(self._new_btn)
        bar.addWidget(self._copy_btn)
        bar.addWidget(self._rename_btn)
        bar.addWidget(self._note_btn)
        bar.addWidget(self._delete_btn)
        bar.addSpacing(12)
        bar.addWidget(self._add_btn)
        bar.addWidget(self._layout_btn)
        bar.addWidget(self._fit_btn)
        bar.addWidget(self._clear_btn)
        bar.addStretch(1)
        bar.addWidget(self._hint)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QWidget(self)
        header.setObjectName("panelHeader")
        header.setLayout(bar)
        note_wrap = QWidget(self)
        note_wrap.setObjectName("panelHeader")
        note_wrap.setLayout(note_bar)
        head_box = QVBoxLayout()
        head_box.setContentsMargins(0, 0, 0, 0)
        head_box.setSpacing(0)
        head_box.addWidget(header)
        head_box.addWidget(note_wrap)
        head = QWidget(self)
        head.setLayout(head_box)
        root.addWidget(head)
        root.addWidget(self.view, 1)

    # -- 对外 -------------------------------------------------------------

    def attach(
        self,
        page_sizes: list[tuple[float, float]],
        render_manager,
        store: AnnotationStore,
        sequence: SequenceManager,
        undo_stack,
        boards: BoardStore,
    ) -> None:
        self.view.set_document(page_sizes, render_manager, store, sequence, undo_stack, boards)
        self._refresh_combo()

    def detach(self) -> None:
        self.view.clear_document()
        self._boards.blockSignals(True)
        self._boards.clear()
        self._boards.blockSignals(False)

    # -- 工作台管理 -------------------------------------------------------

    def _refresh_combo(self) -> None:
        boards = self.view.boards
        self._boards.blockSignals(True)
        self._boards.clear()
        if boards is not None:
            for board in boards.boards():
                label = f"{board.name}（{len(board.cards)} 页）"
                self._boards.addItem(label, board.id)
                self._boards.setItemData(self._boards.count() - 1,
                                         self._board_tooltip(board), Qt.ToolTipRole)
            index = self._boards.findData(boards.current_id())
            if index >= 0:
                self._boards.setCurrentIndex(index)
            self._delete_btn.setEnabled(boards.count() > 1)
        self._boards.blockSignals(False)
        self._refresh_note()

    @staticmethod
    def _board_tooltip(board) -> str:
        lines = [board.name, f"{len(board.cards)} 页"]
        if board.note:
            lines.append(board.note)
        lines.append(WorkbenchPanel._stamp(board))
        return "\n".join(lines)

    @staticmethod
    def _stamp(board) -> str:
        def fmt(value: float) -> str:
            return time.strftime("%m-%d %H:%M", time.localtime(value)) if value else "—"
        return f"建于 {fmt(board.created_at)} · 改于 {fmt(board.updated_at)}"

    def _refresh_note(self) -> None:
        boards = self.view.boards
        if boards is None:
            self._note_label.setText("")
            self._meta_label.setText("")
            return
        board = boards.current()
        if board.note:
            self._note_label.setText(f"备注：{board.note}")
            self._note_label.setToolTip(board.note)
        else:
            self._note_label.setText("备注：（还没有，点「备注」写一句）")
            self._note_label.setToolTip("")
        self._meta_label.setText(self._stamp(board))

    def _on_combo_menu(self, pos) -> None:
        boards = self.view.boards
        if boards is None:
            return
        menu = QMenu(self)
        act_up = menu.addAction("上移")
        act_down = menu.addAction("下移")
        index = boards.boards().index(boards.current())
        act_up.setEnabled(index > 0)
        act_down.setEnabled(index < boards.count() - 1)
        menu.addSeparator()
        act_copy = menu.addAction("复制这块工作台")
        act_note = menu.addAction("写备注…")
        act_rename = menu.addAction("改名…")
        menu.addSeparator()
        act_delete = menu.addAction("删除这块工作台")
        act_delete.setEnabled(boards.count() > 1)
        chosen = menu.exec(self._boards.mapToGlobal(pos))
        if chosen is act_up:
            self.move_board(-1)
        elif chosen is act_down:
            self.move_board(1)
        elif chosen is act_copy:
            self.duplicate_board()
        elif chosen is act_note:
            self.edit_note()
        elif chosen is act_rename:
            self.rename_board()
        elif chosen is act_delete:
            self.delete_board()

    def _on_board_changed(self, index: int) -> None:
        boards = self.view.boards
        if boards is None or index < 0:
            return
        board_id = self._boards.itemData(index)
        if board_id and boards.set_current(board_id):
            self.view.refresh_board()
            self.view.fit_all()

    def new_board(self) -> None:
        boards = self.view.boards
        if boards is None:
            return
        board = boards.add_board()
        boards.set_current(board.id)
        self._refresh_combo()
        self.view.refresh_board()
        self.message.emit(f"已新建「{board.name}」")

    def duplicate_board(self) -> None:
        boards = self.view.boards
        if boards is None:
            return
        source = boards.current()
        copy = boards.duplicate_board(source.id)
        if copy is None:
            return
        self._refresh_combo()
        self.view.refresh_board()
        self.view.fit_all()
        self.message.emit(f"已复制「{source.name}」→「{copy.name}」（{len(copy.cards)} 页）")

    def edit_note(self) -> None:
        boards = self.view.boards
        if boards is None:
            return
        board = boards.current()
        text, ok = QInputDialog.getMultiLineText(
            self, "工作台备注", f"给「{board.name}」写一句备注：", board.note
        )
        if not ok:
            return
        if boards.set_note(text, board.id):
            self._refresh_combo()
            self.message.emit("备注已保存" if text.strip() else "备注已清空")

    def move_board(self, delta: int) -> None:
        boards = self.view.boards
        if boards is None:
            return
        if boards.move_board(boards.current_id(), delta):
            self._refresh_combo()
            self.message.emit("工作台顺序已调整")

    def rename_board(self) -> None:
        boards = self.view.boards
        if boards is None:
            return
        current = boards.current()
        name, ok = QInputDialog.getText(self, "重命名工作台", "名称：", text=current.name)
        if ok and name.strip() and boards.rename_board(name):
            self._refresh_combo()
            self.message.emit(f"工作台已改名为「{name.strip()}」")

    def delete_board(self) -> None:
        boards = self.view.boards
        if boards is None or boards.count() <= 1:
            return
        current = boards.current()
        boards.remove_board(current.id)
        self._refresh_combo()
        self.view.refresh_board()
        self.message.emit(f"已删除「{current.name}」")

    def add_pages_dialog(self) -> None:
        if not self.view.page_sizes:
            return
        count = len(self.view.page_sizes)
        text, ok = QInputDialog.getText(
            self,
            "添加页面到工作台",
            f"页码（共 {count} 页，支持 4,7,9-15）：",
        )
        if not ok or not text.strip():
            return
        pages = parse_page_spec(text, count)
        if not pages:
            self.message.emit("没看懂要加哪几页，示例：4,7,9-15")
            return
        added = self.view.boards.add_pages(pages)
        if added:
            self.view.refresh_board()
            self.view.auto_layout()
        self._refresh_combo()
        self.message.emit(f"加入 {added} 页到「{self.view.boards.current().name}」")

    def add_page(self, page: int) -> None:
        if self.view.boards is None:
            return
        if self.view.boards.add_page(page):
            self.view.refresh_board()
            self.view.auto_layout()
            self._refresh_combo()
            self.message.emit(
                f"第 {page + 1} 页已加入「{self.view.boards.current().name}」"
            )
        else:
            self.message.emit(f"第 {page + 1} 页已经在工作台上了")

    # -- 清空（点两次，避免误伤）------------------------------------------
    def clear_board(self) -> None:
        if self.view.boards is None:
            return
        if not self.view.boards.current().cards:
            self.message.emit("这块工作台上没有页面")
            return
        if not self._clear_armed:
            self._clear_armed = True
            self._clear_btn.setText("真的清空？")
            self._clear_timer.start()
            return
        self._clear_timer.stop()
        self._disarm_clear()
        count = len(self.view.boards.current().cards)
        self.view.boards.clear_cards()
        self.view.refresh_board()
        self._refresh_combo()
        self.message.emit(f"已清空 {count} 页（PDF 本身没动）")

    def _disarm_clear(self) -> None:
        self._clear_armed = False
        self._clear_btn.setText("清空")

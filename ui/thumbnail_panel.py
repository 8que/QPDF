"""左侧缩略图导航。

2700 页文档不能一次性把 2700 张缩略图都渲染出来（约 200MB），所以：
- 列表项一次性建好（纯内存字符串，几十毫秒），图标先用共享的占位图；
- 只给"视口内 + 上下各 3 行"排渲染任务；
- 离开视口超过 120 行就把图标换回占位图，内存有上限。

关于"打开文件不要卡"：MuPDF 渲染时会一直握着 GIL，所以哪怕放在后台线程，
渲染一张 1191×842 的重矢量页缩略图也要 60–80ms，**这几十毫秒里主线程是停的**。
一上来就排 15 个任务，就等于打开文件时先冻住 1 秒。所以这里做两件事：
1. `set_document` 只排当前页，其余等 260ms 之后（窗口已经画出来了）再排；
2. 任务一张一张发，每张完成后再排下一张，让主线程在两张之间有机会喘气、响应鼠标。
代价是缩略图是"一张张冒出来"的，但换来的是打开大文件不再假死。
"""

from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import QObject, QRectF, QRunnable, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtCore import QMimeData
from PySide6.QtGui import (
    QColor,
    QDrag,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from ui import theme

THUMB_BOX_W = 140
THUMB_BOX_H = 100
PREFETCH_ROWS = 3
EVICT_DISTANCE = 120
MAX_ICONS = 400
#: 打开文档后先让主窗口画出来，隔这么久再开始补缩略图
FIRST_DELAY_MS = 260
#: 两张缩略图之间留一点缝，让主线程有机会处理输入和重绘
BREATH_MS = 12


class _ThumbSignals(QObject):
    finished = Signal(int, int, object)  # index, epoch, QImage | None


class _ThumbJob(QRunnable):
    __slots__ = ("_doc", "_index", "_rect", "_scale", "_epoch", "_signals")

    def __init__(self, doc, index, rect, scale, epoch, signals):
        super().__init__()
        self._doc = doc
        self._index = index
        self._rect = rect
        self._scale = scale
        self._epoch = epoch
        self._signals = signals
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover
        image = None
        try:
            image = self._doc.render(self._index, self._rect, self._scale)
        except Exception:  # noqa: BLE001
            image = None
        self._signals.finished.emit(self._index, self._epoch, image)


#: 拖到工作台时用的 MIME 类型（内容是页号，0 起）
PAGES_MIME = "application/x-qpdf-pages"


class _ThumbList(QListWidget):
    """缩略图列表：能把自己的行拖出去（拖到工作台就加一页）。"""

    def startDrag(self, supported_actions) -> None:  # noqa: N802 - Qt 命名
        item = self.currentItem()
        if item is None:
            return
        row = self.row(item)
        mime = QMimeData()
        mime.setData(PAGES_MIME, str(row).encode("ascii"))
        mime.setText(f"第 {row + 1} 页")

        drag = QDrag(self)
        drag.setMimeData(mime)
        icon = item.icon()
        if not icon.isNull():
            pixmap = icon.pixmap(THUMB_BOX_W, THUMB_BOX_H)
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())
        drag.exec(Qt.DropAction.CopyAction)


class ThumbnailPanel(QWidget):
    page_selected = Signal(int)
    board_requested = Signal(int)  # 「加入工作台」

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("thumbHost")
        self.setMinimumWidth(160)
        self.setMaximumWidth(240)

        self._title = QLabel("页面")
        self._title.setObjectName("panelHeader")
        self._count = QLabel("—")
        self._count.setObjectName("panelHint")
        head = QHBoxLayout()
        head.setContentsMargins(14, 10, 12, 6)
        head.setSpacing(6)
        head.addWidget(self._title)
        head.addStretch(1)
        head.addWidget(self._count)

        self._list = _ThumbList(self)
        self._list.setObjectName("thumbList")
        self._list.setIconSize(QSize(THUMB_BOX_W, THUMB_BOX_H))
        self._list.setUniformItemSizes(True)
        self._list.setSpacing(1)
        self._list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        # 拖到工作台：开拖放，只许拖出（不许外部东西拖进来）
        self._list.setDragEnabled(True)
        self._list.setDragDropMode(QListWidget.DragDropMode.DragOnly)
        self._list.setDefaultDropAction(Qt.DropAction.CopyAction)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(head)
        layout.addWidget(self._list)

        self._doc = None
        self._epoch = 0
        self._sizes: list = []
        self._requested: set[int] = set()
        self._queue: list[int] = []
        self._inflight = False
        self._render_manager = None
        self._breath = QTimer(self)
        self._breath.setSingleShot(True)
        self._breath.setInterval(BREATH_MS)
        self._breath.timeout.connect(self._pump)
        self._delayed = QTimer(self)
        self._delayed.setSingleShot(True)
        self._delayed.setInterval(FIRST_DELAY_MS)
        self._delayed.timeout.connect(self._schedule_visible)
        self._loaded: "OrderedDict[int, QIcon]" = OrderedDict()
        self._suppress = False
        self._last_first = -999

        self._placeholder = self._make_placeholder()
        self._icon_placeholder = QIcon(self._placeholder)

        self._signals = _ThumbSignals()
        self._signals.finished.connect(self._on_thumb_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)

        self._list.verticalScrollBar().valueChanged.connect(self._schedule_visible)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)

    # ------------------------------------------------------------------ 构建

    @staticmethod
    def _frame(pixmap: QPixmap, fill: str = "#ffffff") -> QPixmap:
        """给缩略图画一个圆角白卡片 + 细边框，比裸贴图干净。"""
        out = QPixmap(pixmap.size())
        out.setDevicePixelRatio(pixmap.devicePixelRatio() or 1.0)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0.6, 0.6, out.width() - 1.2, out.height() - 1.2)
        path = QPainterPath()
        path.addRoundedRect(rect, 6.0, 6.0)
        painter.fillPath(path, QColor(fill))
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.setClipping(False)
        painter.setPen(QPen(QColor(theme.BORDER_STRONG), 1.0))
        painter.drawPath(path)
        painter.end()
        return out

    @classmethod
    def _make_placeholder(cls) -> QPixmap:
        pixmap = QPixmap(THUMB_BOX_W, THUMB_BOX_H)
        pixmap.fill(QColor("#f0f2f5"))
        return cls._frame(pixmap, "#ffffff")

    def set_render_manager(self, manager) -> None:
        """接上渲染管理器：缩略图优先拿现成的整页预览缩，省一次整页解析。"""
        self._render_manager = manager

    def set_document(self, doc, sizes: list) -> None:
        self._epoch += 1
        self._doc = doc
        self._sizes = list(sizes)
        self._requested.clear()
        self._loaded.clear()

        self._suppress = True
        self._list.clear()
        icon = self._icon_placeholder
        for index in range(len(self._sizes)):
            item = QListWidgetItem(icon, str(index + 1))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._list.addItem(item)
        self._suppress = False

        self._count.setText(f"共 {len(self._sizes)} 页" if self._sizes else "—")

        if self._sizes:
            self._list.setCurrentRow(0)
            # 只排当前页这一张——打开大文件时先让窗口出来
            self._ensure_loaded(0, priority=10)
            self._delayed.start()

    def reload_current(self) -> None:
        """重画当前页那张缩略图（强制刷新时用：缩略图也可能缓存了旧的）。"""
        if self._doc is None:
            return
        row = self._list.currentRow()
        if row < 0:
            return
        self._epoch += 1
        self._loaded.pop(row, None)
        self._requested.discard(row)
        item = self._list.item(row)
        if item is not None:
            item.setIcon(QIcon())
        self._ensure_loaded(row, priority=20)

    def clear_document(self) -> None:
        self._epoch += 1
        self._delayed.stop()
        self._breath.stop()
        self._doc = None
        self._sizes = []
        self._requested.clear()
        self._queue.clear()
        self._inflight = False
        self._loaded.clear()
        self._list.clear()
        self._count.setText("—")

    # ------------------------------------------------------------------ 同步

    def set_current_page(self, index: int) -> None:
        if self._suppress or not (0 <= index < self._list.count()):
            return
        self._suppress = True
        try:
            self._list.setCurrentRow(index)
            item = self._list.item(index)
            # 只在这一项已经被滚出视野时才滚过去。
            # 缩放（Ctrl+滚轮）会顺带改当前页，那时用户根本没想动左边的列表，
            # 无条件 scrollToItem 就会让缩略图自己往下滑——这是那个 bug 的根源。
            if item is not None and not self._is_visible(item):
                self._list.scrollToItem(item)
        finally:
            self._suppress = False
        # 当前页先来：翻到哪页，那页的缩略图必须马上有
        if self._doc is not None:
            self._ensure_loaded(index, priority=10)
        self._schedule_visible()

    def _is_visible(self, item) -> bool:
        """列表项是不是已经露在视口里（哪怕只露出一半，也算——不再追着滚）。"""
        try:
            rect = self._list.visualItemRect(item)
            return rect.isValid() and self._list.viewport().rect().intersects(rect)
        except Exception:  # noqa: BLE001 - 列表还没布局完，当作"看不见"
            return False

    def _on_row_changed(self, row: int) -> None:
        if self._suppress or row < 0:
            return
        self.page_selected.emit(row)

    def _on_context_menu(self, pos) -> None:
        item = self._list.itemAt(pos)
        if item is None:
            return
        row = self._list.row(item)
        menu = QMenu(self)
        act_goto = menu.addAction(f"跳到第 {row + 1} 页")
        act_board = menu.addAction(f"把第 {row + 1} 页加入工作台")
        chosen = menu.exec(self._list.viewport().mapToGlobal(pos))
        if chosen is act_goto:
            self.page_selected.emit(row)
        elif chosen is act_board:
            self.board_requested.emit(row)

    # ------------------------------------------------------------------ 懒加载

    def _visible_rows(self) -> tuple[int, int]:
        """当前实际看得见的行范围。

        不能用 ``itemAt(0, 0)``——列表项之间有 1px 间距，正好点在缝里就返回 None，
        于是永远算成"从第 0 行开始"，翻到第 50 页时排的还是第 1~8 页的缩略图，
        用户看到的是一列空白卡片。改成按滚动偏移和行高算。
        """
        count = self._list.count()
        if count == 0:
            return 0, -1
        viewport_h = self._list.viewport().height()
        scroll = self._list.verticalScrollBar().value()
        row_h = self._list.sizeHintForRow(0) + 2 * self._list.spacing()
        if row_h <= 0:
            row_h = THUMB_BOX_H + 24
        first = max(0, int(scroll // row_h) - 1)
        last = min(count - 1, int((scroll + viewport_h) // row_h) + 1)
        if last < first:
            last = min(count - 1, first + 8)
        return first, last

    def _schedule_visible(self) -> None:
        if self._doc is None:
            return
        first, last = self._visible_rows()
        # 一下子跳了几百页（比如直接输入页码翻过去），队列里攒着的旧页已经没人看了，
        # 清掉它们，免得当前页的缩略图排在几十个废弃任务后面慢慢等。
        if abs(first - self._last_first) > 40:
            self._pool.clear()
            self._queue.clear()
            self._requested.clear()
        self._last_first = first
        lo = max(0, first - PREFETCH_ROWS)
        hi = min(self._list.count() - 1, last + PREFETCH_ROWS)
        # 先排视口里真的看得见的，再排预取的那几行
        for row in range(first, last + 1):
            self._ensure_loaded(row)
        for row in range(lo, first):
            self._ensure_loaded(row)
        for row in range(last + 1, hi + 1):
            self._ensure_loaded(row)
        self._evict_far_rows(first, last)

    def _ensure_loaded(self, row: int, priority: int = 0) -> None:
        if row in self._requested or row in self._loaded:
            return
        if not (0 <= row < len(self._sizes)):
            return
        if priority > 0:
            # 当前页插队
            if row in self._queue:
                self._queue.remove(row)
            self._queue.insert(0, row)
        elif row not in self._queue:
            self._queue.append(row)
        self._pump()

    def _pump(self) -> None:
        """一次只发一张，发完歇一口气——MuPDF 渲染期间主线程是被 GIL 卡住的。"""
        if self._inflight or self._doc is None or not self._queue:
            return
        row = self._queue.pop(0)
        if row in self._requested or row in self._loaded:
            self._pump()
            return
        size = self._sizes[row]
        if size.width <= 0 or size.height <= 0:
            return
        scale = min(THUMB_BOX_W / size.width, THUMB_BOX_H / size.height)

        # 主视图已经渲过这一页的整页预览图（640 px）→ 直接缩它，
        # 省掉一次几十毫秒的"解析整页内容流"。这是缩略图慢的主要来源。
        manager = self._render_manager
        if manager is not None:
            preview_scale = max(0.02, 640.0 / max(size.width, 1.0))
            preview = manager.cached_preview(row, preview_scale)
            if preview is not None and not preview.isNull():
                icon = QIcon(self._frame(preview.scaled(
                    THUMB_BOX_W, THUMB_BOX_H,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)))
                self._loaded[row] = icon
                self._loaded.move_to_end(row)
                if row < self._list.count():
                    self._list.item(row).setIcon(icon)
                while len(self._loaded) > MAX_ICONS:
                    self._loaded.popitem(last=False)
                self._pump()
                return
        self._requested.add(row)
        self._inflight = True
        job = _ThumbJob(
            self._doc,
            row,
            QRectF(0.0, 0.0, size.width, size.height),
            scale,
            self._epoch,
            self._signals,
        )
        self._pool.start(job)

    def _on_thumb_done(self, index: int, epoch: int, image: QImage | None) -> None:
        self._inflight = False
        if epoch != self._epoch:
            self._breath.start()
            return
        self._requested.discard(index)
        if image is None or image.isNull():
            self._breath.start()
            return
        pixmap = self._frame(QPixmap.fromImage(image))
        icon = QIcon(pixmap)
        self._loaded[index] = icon
        self._loaded.move_to_end(index)
        if index < self._list.count():
            self._list.item(index).setIcon(icon)
        while len(self._loaded) > MAX_ICONS:
            self._loaded.popitem(last=False)
        # 下一张等主线程把这口气缓过来再发
        self._breath.start()

    def _evict_far_rows(self, first: int, last: int) -> None:
        if not self._loaded:
            return
        stale = [
            row
            for row in self._loaded
            if row < first - EVICT_DISTANCE or row > last + EVICT_DISTANCE
        ]
        for row in stale:
            self._loaded.pop(row, None)
            if 0 <= row < self._list.count():
                self._list.item(row).setIcon(self._icon_placeholder)

    def shutdown(self) -> None:
        self._epoch += 1
        self._pool.clear()
        self._pool.waitForDone(1500)

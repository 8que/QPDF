"""标注的图元实现。

``AnnotationItem`` 是标注在页面上的可视对象，挂在对应 ``PdfPageItem`` 下面，
所以它的局部坐标就是 **该页的 PDF 坐标（pt）**，缩放由视图变换统一处理——
标注永远不会因为缩放而漂移。

数字标注不存文本：``display_text()`` 通过外部注入的 ``text_resolver`` 现算，
这样 Phase 4 插队只需要改序号，不需要改任何字符串。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QGraphicsObject, QStyleOptionGraphicsItem, QWidget

from core.annotation import ALIGN_CENTER, ALIGN_RIGHT, Annotation

SELECT_COLOR = QColor("#1a73e8")
LOCK_COLOR = QColor("#9aa0a6")
PADDING = 2.0  # 包围盒外扩（场景单位 pt）

TextResolver = Callable[[Annotation], str]


class AnnotationItem(QGraphicsObject):
    """一条标注。位置 = ``(ann.x, ann.y)``，是该标注的锚点。"""

    #: 拖动结束（未发生位移时不发）。参数为旧 / 新 Annotation 快照。
    move_finished = Signal(object, object)

    def __init__(
        self,
        ann: Annotation,
        parent=None,
        text_resolver: TextResolver | None = None,
    ) -> None:
        super().__init__(parent)
        self._ann = ann
        self._text_resolver = text_resolver
        self._rect = QRectF()
        self._drag_start: QPointF | None = None

        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setPos(ann.x, ann.y)
        self._sync_state()
        self._recompute_geometry()

    # -- 数据 -------------------------------------------------------------

    @property
    def annotation(self) -> Annotation:
        return self._ann

    def set_text_resolver(self, resolver: TextResolver | None) -> None:
        self._text_resolver = resolver
        self.prepareGeometryChange()
        self._recompute_geometry()
        self.update()

    def set_annotation(self, ann: Annotation) -> None:
        """外部（撤销/重做、属性面板）改过模型后调用。"""
        self._ann = ann
        if self.pos() != QPointF(ann.x, ann.y):
            self.setPos(ann.x, ann.y)
        self._sync_state()
        self.prepareGeometryChange()
        self._recompute_geometry()
        self.update()

    def _sync_state(self) -> None:
        """把 locked / hidden 落到图元上（第 8.3 节）。"""
        locked = bool(self._ann.locked)
        # 锁定：不能拖动、不给移动光标，但仍可选中查看
        self.setFlag(QGraphicsObject.GraphicsItemFlag.ItemIsMovable, not locked)
        self.setAcceptHoverEvents(not locked)
        if locked:
            self.unsetCursor()
        # 隐藏：不绘制、点不到、框选也选不上
        self.setVisible(not self._ann.hidden)
        if self._ann.hidden:
            self.setSelected(False)

    def set_annotations_visible(self, visible: bool) -> None:
        """视图级"显示/隐藏全部标注"，与单条隐藏互不影响。"""
        self.setVisible(bool(visible) and not self._ann.hidden)
        if not visible:
            self.setSelected(False)

    def display_text(self) -> str:
        if self._ann.is_number and self._text_resolver is not None:
            return self._text_resolver(self._ann)
        return self._ann.content

    # -- 几何 -------------------------------------------------------------

    def _font(self) -> QFont:
        style = self._ann.style
        font = QFont(style.font_family)
        font.setPointSizeF(max(1.0, float(style.font_size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        return font

    def _recompute_geometry(self) -> None:
        font = self._font()
        metrics = QFontMetricsF(font)
        lines = self.display_text().split("\n") or [""]
        width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        width = max(width, metrics.horizontalAdvance("M") * 0.6)
        height = metrics.lineSpacing() * len(lines)

        align = self._ann.style.align
        if self._ann.is_number:
            # 第 5.1 节：数字的定位方式是"鼠标点击位置居中"。
            # 所以锚点 (x, y) 就是数字的中心，横竖都要减半。
            rect = QRectF(-width / 2.0, -height / 2.0, width, height)
        elif align == ALIGN_CENTER:
            rect = QRectF(-width / 2.0, 0.0, width, height)
        elif align == ALIGN_RIGHT:
            rect = QRectF(-width, 0.0, width, height)
        else:
            rect = QRectF(0.0, 0.0, width, height)
        self._rect = rect

    def boundingRect(self) -> QRectF:  # noqa: N802 - Qt 接口
        return self._rect.adjusted(-PADDING, -PADDING, PADDING, PADDING)

    @property
    def text_rect(self) -> QRectF:
        """文字实际占位（局部坐标，用于把编辑框摆到正确位置）。"""
        return QRectF(self._rect)

    # -- 绘制 -------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionGraphicsItem,
        widget: QWidget | None = None,
    ) -> None:
        text = self.display_text()
        style = self._ann.style
        if text:
            color = QColor(style.color)
            if not color.isValid():
                color = QColor("#000000")
            color.setAlphaF(max(0.0, min(1.0, style.opacity / 100.0)))
            painter.setFont(self._font())
            painter.setPen(color)
            if self._ann.is_number:
                flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
            else:
                flags = Qt.AlignmentFlag.AlignTop
                if style.align == ALIGN_CENTER:
                    flags |= Qt.AlignmentFlag.AlignHCenter
                elif style.align == ALIGN_RIGHT:
                    flags |= Qt.AlignmentFlag.AlignRight
                else:
                    flags |= Qt.AlignmentFlag.AlignLeft
            painter.drawText(self._rect, flags, text)

        if self._ann.locked and text:
            # 锁定的标注在左上角点一个小灰点，提醒"这个动不了"
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(LOCK_COLOR)
            painter.drawRect(QRectF(self._rect.left() - 4.0, self._rect.top() - 4.0, 2.5, 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)

        if self.isSelected():
            pen = QPen(SELECT_COLOR)
            pen.setStyle(Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            pen.setWidthF(1.2)
            painter.setPen(pen)
            painter.setBrush(QColor(47, 107, 246, 16))
            painter.drawRoundedRect(self.boundingRect(), 3.0, 3.0)
            painter.setBrush(Qt.BrushStyle.NoBrush)

    # -- 交互 -------------------------------------------------------------

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_start = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        start = self._drag_start
        self._drag_start = None
        if start is None:
            return
        moved = self.pos() - start
        if abs(moved.x()) < 0.01 and abs(moved.y()) < 0.01:
            return
        self.move_finished.emit(self._ann, self._ann.moved_to(self.pos().x(), self.pos().y()))

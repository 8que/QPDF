"""代码画的图标（零图片素材，和 QPDF 主程序一个路子）。"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _base(size: int, color: str) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                        Qt.PenJoinStyle.RoundJoin))
    return pixmap, painter


def icon(name: str, color: str = "#5b6478", size: int = 16) -> QIcon:
    """按名字画一个线性图标：folder / export / sun / moon / refresh / search / database。"""
    pixmap, painter = _base(size, color)
    s = float(size)
    try:
        if name == "folder":
            path = QPainterPath()
            path.moveTo(s * 0.12, s * 0.28)
            path.lineTo(s * 0.42, s * 0.28)
            path.lineTo(s * 0.52, s * 0.38)
            path.lineTo(s * 0.88, s * 0.38)
            path.lineTo(s * 0.88, s * 0.78)
            path.lineTo(s * 0.12, s * 0.78)
            path.closeSubpath()
            painter.drawPath(path)
        elif name == "export":
            painter.drawLine(QPointF(s * 0.5, s * 0.14), QPointF(s * 0.5, s * 0.62))
            painter.drawLine(QPointF(s * 0.3, s * 0.42), QPointF(s * 0.5, s * 0.62))
            painter.drawLine(QPointF(s * 0.7, s * 0.42), QPointF(s * 0.5, s * 0.62))
            painter.drawLine(QPointF(s * 0.18, s * 0.78), QPointF(s * 0.82, s * 0.78))
        elif name == "sun":
            painter.drawEllipse(QPointF(s * 0.5, s * 0.5), s * 0.18, s * 0.18)
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-0.7, -0.7), (0.7, 0.7),
                           (-0.7, 0.7), (0.7, -0.7)):
                painter.drawLine(
                    QPointF(s * (0.5 + dx * 0.3), s * (0.5 + dy * 0.3)),
                    QPointF(s * (0.5 + dx * 0.42), s * (0.5 + dy * 0.42)),
                )
        elif name == "moon":
            path = QPainterPath()
            path.addEllipse(QPointF(s * 0.5, s * 0.5), s * 0.3, s * 0.3)
            cut = QPainterPath()
            cut.addEllipse(QPointF(s * 0.66, s * 0.36), s * 0.3, s * 0.3)
            painter.drawPath(path.subtracted(cut))
        elif name == "refresh":
            painter.drawArc(QRectF(s * 0.18, s * 0.18, s * 0.64, s * 0.64), 40 * 16, 280 * 16)
            painter.drawLine(QPointF(s * 0.72, s * 0.2), QPointF(s * 0.78, s * 0.4))
            painter.drawLine(QPointF(s * 0.72, s * 0.2), QPointF(s * 0.52, s * 0.24))
        elif name == "search":
            painter.drawEllipse(QPointF(s * 0.44, s * 0.44), s * 0.24, s * 0.24)
            painter.drawLine(QPointF(s * 0.62, s * 0.62), QPointF(s * 0.84, s * 0.84))
        elif name == "database":
            painter.drawEllipse(QRectF(s * 0.2, s * 0.16, s * 0.6, s * 0.18))
            painter.drawLine(QPointF(s * 0.2, s * 0.25), QPointF(s * 0.2, s * 0.75))
            painter.drawLine(QPointF(s * 0.8, s * 0.25), QPointF(s * 0.8, s * 0.75))
            painter.drawArc(QRectF(s * 0.2, s * 0.66, s * 0.6, s * 0.18), 180 * 16, 180 * 16)
            painter.drawArc(QRectF(s * 0.2, s * 0.42, s * 0.6, s * 0.16), 180 * 16, 180 * 16)
    finally:
        painter.end()
    return QIcon(pixmap)


def logo(size: int = 28, color: str = "#2f6bf6") -> QPixmap:
    """左上角那个小圆标：一个 Q 字 + 强调色渐变。"""
    pixmap = QPixmap(size * 2, size * 2)
    pixmap.setDevicePixelRatio(2.0)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(0, 0, size * 2, size * 2))
    painter.setPen(QPen(QColor("#ffffff"), size * 0.14, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap))
    painter.drawEllipse(QRectF(size * 0.55, size * 0.55, size * 0.9, size * 0.9))
    painter.drawLine(QPointF(size * 1.22, size * 1.22), QPointF(size * 1.55, size * 1.55))
    painter.end()
    return pixmap

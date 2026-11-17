"""用代码画的线性图标。

不依赖任何图片文件：绿色版打包不用带素材，换配色只改一个参数。
字形都画在 20×20 的设计格（BOX）里，输出时放进 24×24 的格子（CELL）——
四周留 2 格空白，图标之间不会挤成一团，抗锯齿 + 2 倍像素密度，缩放到 125% / 150% 也不糊。
颜色不写死：默认取 ui.theme.TEXT，深色主题下自动跟着变（见 _resolve）。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from ui import theme

BOX = 20.0        # 字形自己的设计格
CELL = 24.0       # 实际输出格（比设计格大一圈，留白）
STROKE = 1.7


def _resolve(color: str | QColor | None) -> QColor:
    """取真实颜色：没指定就用当前主题的文字色（不能在 import 期求值，否则换肤后不生效）。"""
    if color is None:
        return QColor(theme.TEXT)
    return QColor(color) if isinstance(color, str) else color


def _pen(color: QColor, width: float = STROKE) -> QPen:
    pen = QPen(color)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    return pen


def _draw_folder(p: QPainter, color: QColor) -> None:
    p.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(3.0, 16.0)
    path.lineTo(3.0, 5.6)
    path.lineTo(8.2, 5.6)
    path.lineTo(10.0, 8.2)
    path.lineTo(17.0, 8.2)
    path.lineTo(17.0, 16.0)
    path.closeSubpath()
    p.drawPath(path)


def _draw_save(p: QPainter, color: QColor) -> None:
    # 软盘：外框 + 上方卡片 + 下方标签
    p.setPen(_pen(color))
    p.drawRoundedRect(QRectF(3.6, 3.6, 12.8, 12.8), 2.2, 2.2)
    p.drawLine(QPointF(7.0, 3.6), QPointF(7.0, 7.6))
    p.drawLine(QPointF(7.0, 7.6), QPointF(12.6, 7.6))
    p.drawLine(QPointF(12.6, 7.6), QPointF(12.6, 3.6))
    p.drawRoundedRect(QRectF(6.4, 11.6, 7.2, 4.8), 1.0, 1.0)


def _draw_export(p: QPainter, color: QColor) -> None:
    # 托盘 + 向外（上）走的箭头
    p.setPen(_pen(color))
    path = QPainterPath()
    path.moveTo(4.0, 11.6)
    path.lineTo(4.0, 16.4)
    path.lineTo(16.0, 16.4)
    path.lineTo(16.0, 11.6)
    p.drawPath(path)
    p.drawLine(QPointF(10.0, 13.2), QPointF(10.0, 3.6))
    head = QPainterPath()
    head.moveTo(6.8, 6.6)
    head.lineTo(10.0, 3.4)
    head.lineTo(13.2, 6.6)
    p.drawPath(head)


def _chevron(p: QPainter, color: QColor, left: bool) -> None:
    p.setPen(_pen(color, 2.0))
    path = QPainterPath()
    if left:
        path.moveTo(12.6, 4.6)
        path.lineTo(6.8, 10.0)
        path.lineTo(12.6, 15.4)
    else:
        path.moveTo(7.4, 4.6)
        path.lineTo(13.2, 10.0)
        path.lineTo(7.4, 15.4)
    p.drawPath(path)


def _draw_magnifier(p: QPainter, color: QColor, plus: bool) -> None:
    p.setPen(_pen(color))
    p.drawEllipse(QPointF(8.8, 8.8), 5.4, 5.4)
    p.drawLine(QPointF(12.8, 12.8), QPointF(17.0, 17.0))
    p.drawLine(QPointF(6.2, 8.8), QPointF(11.4, 8.8))
    if plus:
        p.drawLine(QPointF(8.8, 6.2), QPointF(8.8, 11.4))


def _draw_fit_page(p: QPainter, color: QColor) -> None:
    # 竖向的纸 = 整页放进窗口
    p.setPen(_pen(color, 1.6))
    p.drawRoundedRect(QRectF(4.6, 2.8, 10.8, 14.4), 1.8, 1.8)
    p.setPen(_pen(QColor(color.red(), color.green(), color.blue(), 150), 1.4))
    p.drawRoundedRect(QRectF(7.6, 6.6, 4.8, 7.0), 1.0, 1.0)


def _draw_fit_width(p: QPainter, color: QColor) -> None:
    # 横向的纸 + 左右箭头 = 适应宽度
    p.setPen(_pen(color, 1.6))
    p.drawRoundedRect(QRectF(2.8, 5.6, 14.4, 8.8), 1.8, 1.8)
    p.setPen(_pen(color, 1.4))
    p.drawLine(QPointF(6.6, 10.0), QPointF(13.4, 10.0))
    left = QPainterPath()
    left.moveTo(8.6, 8.0)
    left.lineTo(6.4, 10.0)
    left.lineTo(8.6, 12.0)
    p.drawPath(left)
    right = QPainterPath()
    right.moveTo(11.4, 8.0)
    right.lineTo(13.6, 10.0)
    right.lineTo(11.4, 12.0)
    p.drawPath(right)


def _draw_undo(p: QPainter, color: QColor, mirror: bool) -> None:
    p.setPen(_pen(color))
    path = QPainterPath()
    if mirror:
        path.moveTo(5.0, 15.4)
        path.lineTo(5.0, 9.6)
        path.cubicTo(5.0, 8.4, 5.9, 7.6, 7.2, 7.6)
        path.lineTo(13.4, 7.6)
        head_x, tip_x = 11.2, 13.6
    else:
        path.moveTo(15.0, 15.4)
        path.lineTo(15.0, 9.6)
        path.cubicTo(15.0, 8.4, 14.1, 7.6, 12.8, 7.6)
        path.lineTo(6.6, 7.6)
        head_x, tip_x = 8.8, 6.4
    p.drawPath(path)
    head = QPainterPath()
    head.moveTo(head_x, 5.0)
    head.lineTo(tip_x, 7.6)
    head.lineTo(head_x, 10.2)
    p.drawPath(head)


def _draw_text(p: QPainter, color: QColor) -> None:
    p.setPen(_pen(color))
    p.drawLine(QPointF(5.5, 5.0), QPointF(14.5, 5.0))
    p.drawLine(QPointF(10.0, 5.0), QPointF(10.0, 15.0))


def _draw_marquee(p: QPainter, color: QColor) -> None:
    pen = _pen(color, 1.4)
    pen.setStyle(Qt.PenStyle.DashLine)
    pen.setDashPattern([2.2, 1.8])
    p.setPen(pen)
    p.drawRect(QRectF(3.0, 4.0, 14.0, 12.0))


# —— 语义图标（工具栏第二段 / 模式胶囊用） ------------------------------

def _draw_select(p: QPainter, color: QColor) -> None:
    """箭头光标：浏览 / 选择。"""
    path = QPainterPath()
    path.moveTo(5.2, 3.4)
    path.lineTo(5.2, 15.8)
    path.lineTo(8.7, 12.3)
    path.lineTo(10.9, 16.8)
    path.lineTo(13.0, 15.9)
    path.lineTo(10.7, 11.5)
    path.lineTo(15.4, 11.1)
    path.closeSubpath()
    p.setPen(_pen(color, 1.3))
    p.setBrush(color)
    p.drawPath(path)


def _draw_number(p: QPainter, color: QColor) -> None:
    """井号：编号标注。"""
    p.setPen(_pen(color))
    p.drawLine(QPointF(7.9, 4.6), QPointF(7.1, 15.4))
    p.drawLine(QPointF(12.7, 4.6), QPointF(11.9, 15.4))
    p.drawLine(QPointF(4.8, 8.2), QPointF(15.2, 8.2))
    p.drawLine(QPointF(4.4, 12.0), QPointF(14.8, 12.0))


def _draw_layers(p: QPainter, color: QColor) -> None:
    """叠放的纸：覆盖 / 图层。"""
    p.setPen(_pen(color, 1.6))
    back = QPainterPath()
    back.moveTo(6.6, 7.2)
    back.lineTo(6.6, 4.4)
    back.lineTo(16.2, 4.4)
    back.lineTo(16.2, 12.2)
    back.lineTo(13.4, 12.2)
    p.drawPath(back)
    p.drawRoundedRect(QRectF(3.8, 7.6, 9.6, 8.0), 1.6, 1.6)


def _draw_insert(p: QPainter, color: QColor) -> None:
    """列表里插一行：插队。"""
    p.setPen(_pen(color, 1.6))
    p.drawLine(QPointF(4.0, 6.2), QPointF(12.6, 6.2))
    p.drawLine(QPointF(4.0, 14.0), QPointF(12.6, 14.0))
    p.setPen(_pen(color, 1.5))
    p.drawLine(QPointF(15.2, 8.0), QPointF(15.2, 12.2))
    p.drawLine(QPointF(13.1, 10.1), QPointF(17.3, 10.1))
    p.setPen(_pen(color, 1.4))
    p.drawLine(QPointF(4.0, 10.1), QPointF(10.6, 10.1))


def _draw_grid(p: QPainter, color: QColor) -> None:
    """四宫格：工作台。"""
    p.setPen(_pen(color, 1.5))
    for x, y in ((3.8, 3.8), (11.2, 3.8), (3.8, 11.2), (11.2, 11.2)):
        p.drawRoundedRect(QRectF(x, y, 5.0, 5.0), 1.3, 1.3)


def _draw_eye(p: QPainter, color: QColor) -> None:
    """眼睛：护眼底色。"""
    p.setPen(_pen(color, 1.5))
    eye = QPainterPath()
    eye.moveTo(3.0, 10.0)
    eye.cubicTo(5.6, 5.4, 14.4, 5.4, 17.0, 10.0)
    eye.cubicTo(14.4, 14.6, 5.6, 14.6, 3.0, 10.0)
    p.drawPath(eye)
    p.drawEllipse(QPointF(10.0, 10.0), 2.2, 2.2)


def _draw_lens_window(p: QPainter, color: QColor) -> None:
    """放大窗：一个窗口里放一个放大镜。"""
    p.setPen(_pen(color, 1.5))
    p.drawRoundedRect(QRectF(3.2, 3.2, 13.6, 13.6), 2.6, 2.6)
    p.drawEllipse(QPointF(8.2, 8.2), 3.4, 3.4)
    p.drawLine(QPointF(10.7, 10.7), QPointF(13.9, 13.9))


def _draw_table(p: QPainter, color: QColor) -> None:
    """带表头的表格：材料表。"""
    p.setPen(_pen(color, 1.5))
    p.drawRoundedRect(QRectF(3.6, 4.6, 12.8, 10.8), 1.8, 1.8)
    p.drawLine(QPointF(3.6, 8.3), QPointF(16.4, 8.3))
    p.drawLine(QPointF(8.6, 8.3), QPointF(8.6, 15.4))


def _draw_link(p: QPainter, color: QColor) -> None:
    """两环相扣：数据匹配。"""
    p.setPen(_pen(color, 1.5))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRoundedRect(QRectF(2.9, 7.2, 8.6, 5.6), 2.8, 2.8)
    p.drawRoundedRect(QRectF(8.5, 7.2, 8.6, 5.6), 2.8, 2.8)


def _draw_page(p: QPainter, color: QColor) -> None:
    """一张纸：空状态。"""
    p.setPen(_pen(color, 1.4))
    path = QPainterPath()
    path.moveTo(5.4, 3.2)
    path.lineTo(12.2, 3.2)
    path.lineTo(15.2, 6.4)
    path.lineTo(15.2, 16.8)
    path.lineTo(5.4, 16.8)
    path.closeSubpath()
    p.drawPath(path)
    p.drawLine(QPointF(12.2, 3.2), QPointF(12.2, 6.5))
    p.drawLine(QPointF(12.2, 6.5), QPointF(15.2, 6.5))


_GLYPHS = {
    "open": _draw_folder,
    "save": _draw_save,
    "export": _draw_export,
    "prev": lambda p, c: _chevron(p, c, True),
    "next": lambda p, c: _chevron(p, c, False),
    "zoom_in": lambda p, c: _draw_magnifier(p, c, True),
    "zoom_out": lambda p, c: _draw_magnifier(p, c, False),
    "fit_page": _draw_fit_page,
    "fit_width": _draw_fit_width,
    "undo": lambda p, c: _draw_undo(p, c, False),
    "redo": lambda p, c: _draw_undo(p, c, True),
    "text": _draw_text,
    "marquee": _draw_marquee,
    "select": _draw_select,
    "number": _draw_number,
    "layers": _draw_layers,
    "insert": _draw_insert,
    "grid": _draw_grid,
    "eye": _draw_eye,
    "page": _draw_page,
    "lens_window": _draw_lens_window,
    "table": _draw_table,
    "link": _draw_link,
}


def glyph_pixmap(
    name: str,
    color: str | QColor | None = None,
    size: int = 20,
    ratio: float = 2.0,
) -> QPixmap:
    """把某个字形画成 QPixmap（size 是逻辑尺寸，内部按 ratio 放大保证清晰）。"""
    pixmap = QPixmap(int(round(size * ratio)), int(round(size * ratio)))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # pixmap 设了 devicePixelRatio 之后，QPainter 用的是逻辑坐标，
    # 所以这里只需要把 24×24 的输出格缩到逻辑尺寸，别再乘 ratio（否则会被裁成左上角一块）
    scale = size / CELL
    painter.scale(scale, scale)
    painter.translate((CELL - BOX) / 2.0, (CELL - BOX) / 2.0)   # 20 格居中放进 24 格
    fn = _GLYPHS.get(name)
    if fn is not None:
        fn(painter, _resolve(color))
    painter.end()
    return pixmap


def glyph_icon(name: str, color: str | QColor | None = None, size: int = 20) -> QIcon:
    return QIcon(glyph_pixmap(name, color, size))

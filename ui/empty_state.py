"""空状态页：没打开图纸时，画布那块不该是一张白纸。

三件事按重要程度排：一句人话（还没有打开图纸）、一个看得懂的入口（拖进来 / 打开）、
最近开过的几份（省掉一次找路径的时间）。整块都是弱化的，别抢打开后的注意力。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui import theme
from ui.icons import glyph_icon

RECENT_SHOWN = 3


class EmptyState(QWidget):
    """欢迎/空状态页。信号交给主窗口去开文件，自己不碰文档。"""

    open_requested = Signal()
    path_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        self._recent_box: list[QToolButton] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 32, 32, 32)
        outer.setSpacing(0)
        outer.addStretch(1)

        card = QFrame(self)
        card.setObjectName("emptyCard")
        card.setFixedWidth(400)
        card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        outer.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)

        column = QVBoxLayout(card)
        column.setContentsMargins(28, 26, 28, 24)
        column.setSpacing(0)

        mark = QLabel(card)
        mark.setObjectName("emptyMark")
        mark.setFixedSize(56, 56)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(glyph_icon("page", theme.MUTED, 26).pixmap(26, 26))
        column.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)

        title = QLabel("还没有打开图纸", card)
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(title)
        column.addSpacing(6)

        hint = QLabel(
            "把 PDF 拖进窗口，或者选一个文件开始。\n"
            "标注只写在本程序的项目库里，原始 PDF 不会被改动。",
            card,
        )
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setWordWrap(True)
        column.addWidget(hint)
        column.addSpacing(18)

        self._open_btn = QPushButton("打开 PDF…", card)
        self._open_btn.setProperty("primary", True)
        self._open_btn.setFixedWidth(148)
        self._open_btn.setMinimumHeight(34)
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.clicked.connect(self.open_requested.emit)
        column.addWidget(self._open_btn, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(20)

        self._recent_title = QLabel("最近打开", card)
        self._recent_title.setObjectName("groupTitle")
        column.addWidget(self._recent_title)
        column.addSpacing(6)

        self._recent_layout = QVBoxLayout()
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(6)
        column.addLayout(self._recent_layout)

        outer.addStretch(1)

        self.set_recent([])

    # ------------------------------------------------------------------ 数据

    def set_recent(self, paths: list[str]) -> None:
        """刷新最近打开列表（最多 3 条，路径按最近的在前）。"""
        for button in self._recent_box:
            self._recent_layout.removeWidget(button)
            button.hide()
            button.deleteLater()
        self._recent_box.clear()

        shown = [p for p in paths if p][:RECENT_SHOWN]
        self._recent_title.setVisible(bool(shown))

        for path in shown:
            button = QToolButton(self)
            button.setObjectName("emptyAction")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setIcon(glyph_icon("open", theme.MUTED, 16))
            button.setIconSize(QSize(16, 16))
            button.setText(self._shorten(os.path.basename(path)))
            button.setToolTip(path)
            button.setMinimumHeight(32)
            button.clicked.connect(lambda _=False, p=path: self.path_requested.emit(p))
            self._recent_layout.addWidget(button)
            self._recent_box.append(button)

    @staticmethod
    def _shorten(name: str) -> str:
        return name if len(name) <= 40 else name[:20] + "…" + name[-17:]

    # ------------------------------------------------------------------ 换肤

    def paintEvent(self, event) -> None:  # noqa: N802
        """浅粉主题下，空状态铺 bg.png：人物摆右下角，上沿渐隐进底色，别留一条硬边。"""
        wallpaper = theme.wallpaper()
        if wallpaper is None:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        width, height = self.width(), self.height()
        painter.fillRect(self.rect(), QColor(theme.canvas_color()))
        scaled = wallpaper.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        top = height - scaled.height()
        painter.drawPixmap(width - scaled.width(), top, scaled)
        painter.fillRect(self.rect(), QColor(255, 255, 255, 46))
        if top > 0:
            fade = min(140, max(40, int(scaled.height() * 0.30)))
            gradient = QLinearGradient(0.0, float(top), 0.0, float(top + fade))
            base = QColor(theme.canvas_color())
            base.setAlpha(255)
            clear = QColor(theme.canvas_color())
            clear.setAlpha(0)
            gradient.setColorAt(0.0, base)
            gradient.setColorAt(1.0, clear)
            painter.fillRect(0, top, width, fade, gradient)
        painter.end()

    def refresh_icons(self) -> None:
        """换主题以后把图标重画一遍（QIcon 是位图，不会自己跟着变）。"""
        mark = self.findChild(QLabel, "emptyMark")
        if mark is not None:
            mark.setPixmap(glyph_icon("page", theme.MUTED, 26).pixmap(26, 26))
        for button in self._recent_box:
            button.setIcon(glyph_icon("open", theme.MUTED, 16))


__all__ = ["EmptyState"]

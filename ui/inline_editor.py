"""页面内联文字编辑器。

直接浮在 ``QGraphicsView.viewport()`` 上，不弹对话框——第 4.1 节要求「鼠标
点击目标位置→输入文字→确认」，弹窗会把这一连贯动作打断。

按键约定（与常见 PDF 软件一致）：
- Enter          确认
- Shift+Enter    换行
- Esc            取消
- 鼠标点到别处   确认（等同失焦提交）
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor, QTextOption
from PySide6.QtWidgets import QFrame, QPlainTextEdit, QWidget

MIN_WIDTH = 180
MIN_HEIGHT = 30


class InlineTextEditor(QPlainTextEdit):
    """一次只编辑一个标注；用完即隐藏。"""

    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setTabChangesFocus(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.setProperty("qpdf-inline-editor", True)
        self.setStyleSheet(
            "QPlainTextEdit[qpdf-inline-editor=\"true\"] {"
            " background: #ffffff; border: 1px solid #2f6bf6;"
            " border-radius: 6px; padding: 3px 4px; }"
        )
        self._active = False
        self._closing = False
        self.textChanged.connect(self._adjust_height)
        self.hide()

    # -- 生命周期 ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._active

    def open_at(
        self,
        top_left: QPoint,
        text: str,
        font: QFont,
        color: QColor,
        width_hint: float,
        select_all: bool = True,
    ) -> None:
        self._active = True
        self._closing = False
        self.setFont(font)
        self.setStyleSheet(
            f"QPlainTextEdit[qpdf-inline-editor=\"true\"] {{"
            f" background: #ffffff; border: 1px solid #2f6bf6;"
            f" border-radius: 6px; padding: 3px 4px; color: {color.name()}; }}"
        )
        self.setPlainText(text)
        self.move(top_left)
        self.resize(max(MIN_WIDTH, int(width_hint) + 24), MIN_HEIGHT)
        self._adjust_height()
        self.show()
        self.raise_()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        if select_all:
            self.selectAll()
        else:
            self.moveCursor(QTextCursor.MoveOperation.End)

    def finish(self, commit: bool, silent: bool = False) -> str | None:
        """结束编辑。``commit`` 为真时发 ``committed``。返回提交的文本。"""
        if not self._active or self._closing:
            return None
        self._active = False
        self._closing = True
        text = self.toPlainText()
        self.hide()
        self._closing = False
        if commit:
            self.committed.emit(text.strip())
            return text.strip()
        self.cancelled.emit()
        return None

    # -- 事件 -------------------------------------------------------------

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.finish(True)
            event.accept()
            return
        if key == Qt.Key.Key_Escape:
            self.finish(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        if self._active and not self._closing:
            self.finish(True)

    def _adjust_height(self) -> None:
        layout = self.document().documentLayout()
        height = layout.documentSize().height() if layout is not None else MIN_HEIGHT
        self.resize(self.width(), max(MIN_HEIGHT, int(height) + 10))

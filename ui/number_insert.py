"""插队确认对话框（第 6.2 节）。

按第 6.2.2 节把影响范围摊开给用户选：当前页 / 指定页面范围 / 整份 PDF。
默认选中"当前页"——第 6.2.2 节明确"默认不允许未经确认修改整份 PDF 的编号"，
所以改成整份 PDF 必须是用户自己点出来的，并且预览那一栏会写清楚要动多少个数字。

预览用的数据由外面传进来的 ``preview_fn(scope, page_from, page_to)`` 现算，
对话框本身不碰数据——"预览"和"真正执行"用同一份计算，不会出现
"预览说改 3 个、执行改了 5 个"这种情况。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.sequence import SCOPE_ALL, SCOPE_PAGE, SCOPE_RANGE
from ui import theme


class InsertNumberDialog(QDialog):
    """确认"在某个位置插一个数字"，并选择影响范围。"""

    def __init__(
        self,
        at_index: int,
        value_text: str,
        page_index: int,
        page_count: int,
        preview_fn,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("插队标注")
        self.setMinimumWidth(420)

        self._at_index = int(at_index)
        self._page = int(page_index)
        self._page_count = max(1, int(page_count))
        self._preview_fn = preview_fn

        root = QVBoxLayout(self)

        head = QLabel(
            f"将在第 <b>{value_text}</b> 号之前插入一个新编号，"
            f"原来的第 {value_text} 号及之后的数字依次后移。"
        )
        head.setWordWrap(True)
        root.addWidget(head)

        # -- 影响范围（第 6.2.2 节）------------------------------------------
        box = QGroupBox("编号影响范围")
        form = QFormLayout(box)

        self._radio_page = QRadioButton(f"当前页（第 {self._page + 1} 页）")
        self._radio_page.setChecked(True)
        self._radio_range = QRadioButton("指定页面范围：")
        self._radio_all = QRadioButton(f"整份 PDF（共 {self._page_count} 页）")

        range_row = QWidget()
        range_form = QFormLayout(range_row)
        range_form.setContentsMargins(0, 0, 0, 0)
        self._spin_from = QSpinBox()
        self._spin_from.setRange(1, self._page_count)
        self._spin_from.setValue(self._page + 1)
        self._spin_from.setMaximumWidth(96)
        self._spin_to = QSpinBox()
        self._spin_to.setRange(1, self._page_count)
        self._spin_to.setValue(self._page + 1)
        self._spin_to.setMaximumWidth(96)
        range_form.addRow("从第", self._spin_from)
        range_form.addRow("到第", self._spin_to)
        self._range_row = range_row

        for radio in (self._radio_page, self._radio_range, self._radio_all):
            radio.toggled.connect(self._refresh_preview)
            form.addRow(radio)
        form.addRow("", range_row)

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        form.addRow("将影响：", self._hint)

        root.addWidget(box)

        self._warning = QLabel(
            "注意：整份 PDF 范围会重新编号后面所有页面的数字，请先确认预览无误。"
        )
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        root.addWidget(self._warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("插入")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        theme.mark_primary(ok_button)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_preview()

    # ------------------------------------------------------------------ 状态

    def scope(self) -> tuple[str, int, int]:
        """返回 ``(范围, 起始页, 结束页)``，页码都是从 0 开始。"""
        if self._radio_all.isChecked():
            return SCOPE_ALL, 0, self._page_count - 1
        if self._radio_range.isChecked():
            low = min(self._spin_from.value(), self._spin_to.value()) - 1
            high = max(self._spin_from.value(), self._spin_to.value()) - 1
            return SCOPE_RANGE, low, high
        return SCOPE_PAGE, self._page, self._page

    def _refresh_preview(self, *_args) -> None:
        scope, page_from, page_to = self.scope()
        is_range = scope == SCOPE_RANGE
        self._range_row.setEnabled(is_range)
        self._spin_from.setEnabled(is_range)
        self._spin_to.setEnabled(is_range)
        self._warning.setVisible(scope == SCOPE_ALL)

        count, detail = self._preview_fn(scope, page_from, page_to)
        if count <= 0:
            self._hint.setText("不会改动任何已有数字（这个位置后面还没有编号）")
        else:
            self._hint.setText(f"{count} 个数字后移一格 —— {detail}")


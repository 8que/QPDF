"""数字标注的起始值与步长设置（第 5.4 节）。

只从"下一个数字"开始生效，不动已有的数字。因为对已经存在的数字改起始值
等价于**重新编号**，那是第 6 章插队/覆盖要谨慎处理、且必须可撤销的操作，
不该藏在设置对话框里悄悄发生。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from ui import theme


class NumberSettingsDialog(QDialog):
    def __init__(self, start: int = 1, step: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("数字标注设置")
        self.setMinimumWidth(380)

        self._start = QSpinBox(self)
        self._start.setRange(-999999, 999999)
        self._start.setValue(int(start))
        self._start.setMaximumWidth(150)

        self._step = QSpinBox(self)
        self._step.setRange(1, 9999)
        self._step.setValue(max(1, int(step)))
        self._step.setMaximumWidth(150)

        form = QFormLayout()
        form.addRow("起始数字", self._start)
        form.addRow("步长", self._step)

        hint = QLabel(
            "从下一个数字开始生效，已经标过的数字不会被改动。\n"
            "想看下一组编号从头开始，用「重新从 1 编号」(Ctrl+Shift+R)。",
            self,
        )
        hint.setWordWrap(True)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        theme.mark_primary(ok_button)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 14)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addWidget(buttons)

    def values(self) -> tuple[int, int]:
        return self._start.value(), self._step.value()

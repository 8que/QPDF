"""插入/设置页码的小对话框。

页码不是标注，所以这里不碰 AnnotationStore；只收集「位置、格式、样式」，
关掉之后由主窗口存进项目设置并用 core.page_number.PageNumberSpec 预览/导出。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFontComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
)

from core.annotation import AnnotationStyle
from core.page_number import DEFAULT_TEMPLATE, PageNumberSpec
from core.region import Region
from ui.property_panel import _color_icon


class PageNumberDialog(QDialog):
    region_requested = Signal()

    def __init__(self, spec: PageNumberSpec | None, total_pages: int, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("页码")
        self.setMinimumWidth(460)
        self._region = spec.region if spec is not None else None
        self._style = spec.style if spec is not None else AnnotationStyle()
        self._total = max(1, int(total_pages))

        title = QLabel("每页在同一个位置写页码；页码不进 .qpdf 标注库，qreport 不会把它算成焊口数字。")
        title.setWordWrap(True)

        self._region_label = QLabel()
        self._region_label.setWordWrap(True)
        pick_btn = QPushButton("重新框选位置…")
        pick_btn.clicked.connect(self._request_region)

        self._format_edit = QLineEdit(spec.template if spec is not None else DEFAULT_TEMPLATE)
        self._format_edit.setPlaceholderText("例如：第 {page} 页 / 共 {total} 页")
        hint = QLabel("占位符：{page} 或 {n} / {页码}；总页数 {total} / {总页数}。前后可以写任意文字。")
        hint.setWordWrap(True)
        hint.setObjectName("panelHint")

        self._preview = QLabel()
        self._preview.setWordWrap(True)
        self._preview.setObjectName("panelHint")

        self._enabled = QCheckBox("在页面上显示页码")
        self._enabled.setChecked(bool(spec.enabled) if spec is not None else True)

        self._font_combo = QFontComboBox()
        self._font_combo.setCurrentFont(QFont(self._style.font_family))
        self._size_spin = QDoubleSpinBox()
        self._size_spin.setRange(4.0, 400.0)
        self._size_spin.setDecimals(1)
        self._size_spin.setSuffix(" pt")
        self._size_spin.setValue(float(self._style.font_size))
        self._color_btn = QToolButton()
        self._color_btn.setIcon(_color_icon(QColor(self._style.color)))
        self._color_btn.setFixedSize(38, 32)
        self._color_btn.setToolTip(self._style.color)
        self._color_btn.clicked.connect(self._pick_color)
        self._bold = QToolButton()
        self._bold.setText("B")
        self._bold.setCheckable(True)
        self._bold.setChecked(bool(self._style.bold))
        self._italic = QToolButton()
        self._italic.setText("I")
        self._italic.setCheckable(True)
        self._italic.setChecked(bool(self._style.italic))

        row = QHBoxLayout()
        row.addWidget(QLabel("字体"))
        row.addWidget(self._font_combo, 1)
        row.addWidget(QLabel("字号"))
        row.addWidget(self._size_spin)
        row.addWidget(self._color_btn)
        row.addWidget(self._bold)
        row.addWidget(self._italic)

        form = QVBoxLayout(self)
        form.setSpacing(8)
        form.addWidget(title)
        form.addWidget(self._region_label)
        form.addWidget(pick_btn)
        form.addWidget(QLabel("显示格式"))
        form.addWidget(self._format_edit)
        form.addWidget(hint)
        form.addWidget(self._preview)
        form.addWidget(self._enabled)
        form.addLayout(row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addWidget(buttons)

        self._format_edit.textChanged.connect(self._update_preview)
        self._font_combo.currentFontChanged.connect(self._update_preview)
        self._size_spin.valueChanged.connect(self._update_preview)
        self._bold.toggled.connect(self._update_preview)
        self._italic.toggled.connect(self._update_preview)
        self._enabled.toggled.connect(self._update_preview)
        self._update_region()
        self._update_preview()

    def set_region(self, region: Region | None) -> None:
        self._region = region
        self._update_region()

    @property
    def region(self) -> Region | None:
        return self._region

    def _request_region(self) -> None:
        self.region_requested.emit()

    def _update_region(self) -> None:
        if self._region is None:
            self._region_label.setText("还没有框选位置：点下面的按钮，在页面上框出页码要放的地方。")
        else:
            r = self._region
            self._region_label.setText(
                "位置：左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}（所有页共用）".format(
                    r.x0, r.y0, r.x1, r.y1
                )
            )

    def _style_now(self) -> AnnotationStyle:
        return AnnotationStyle(
            font_family=self._font_combo.currentFont().family(),
            font_size=float(self._size_spin.value()),
            color=self._style.color,
            bold=self._bold.isChecked(),
            italic=self._italic.isChecked(),
            opacity=int(self._style.opacity),
            align=self._style.align,
        )

    def _update_preview(self, *_args) -> None:
        self._style = self._style_now()
        self._preview.setText(
            f"预览（第 1 / {self._total} 页）：{self._spec_now().text_for(1, self._total)}"
            if self._region is not None
            else "预览：先框选位置"
        )

    def _spec_now(self) -> PageNumberSpec:
        region = self._region or Region(0.0, 0.0, 0.0, 0.0)
        return PageNumberSpec(
            region=region,
            template=self._format_edit.text(),
            style=self._style,
            enabled=self._enabled.isChecked(),
        )

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._style.color), self, "选择页码颜色")
        if not color.isValid():
            return
        self._style = AnnotationStyle(
            font_family=self._style.font_family,
            font_size=self._style.font_size,
            color=color.name(),
            bold=self._style.bold,
            italic=self._style.italic,
            opacity=self._style.opacity,
            align=self._style.align,
        )
        self._color_btn.setIcon(_color_icon(color))
        self._color_btn.setToolTip(color.name())
        self._update_preview()

    def result_spec(self) -> PageNumberSpec | None:
        if self._region is None:
            return None
        return self._spec_now()

    def accept(self) -> None:  # noqa: D401
        if self._region is None:
            return
        super().accept()

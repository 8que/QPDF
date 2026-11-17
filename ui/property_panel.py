"""属性面板（第 4.2 节 / 第 5.3 节）。

两种工作状态：
- **有选中标注**：面板显示选中标注的样式，改动直接作用到选中项（可撤销）。
- **无选中**：面板显示「新建标注默认样式」，改动只影响之后新建的标注。

连续数字标注过程中绝不弹窗，样式一律在这里预先设好。
"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFontComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from core.annotation import ALIGN_CENTER, ALIGN_LEFT, ALIGN_RIGHT, AnnotationStyle

#: 面板上「常用字号」的候选。图纸标注 6pt 最常见，往上一档给标题用。
SIZE_PRESETS = (6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 20.0, 24.0, 28.0, 36.0)


def _color_icon(color: QColor) -> QIcon:
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    from PySide6.QtGui import QPainter, QPainterPath

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path = QPainterPath()
    path.addRoundedRect(1.0, 1.0, 18.0, 18.0, 5.0, 5.0)
    painter.fillPath(path, color)
    painter.setPen(QColor(0, 0, 0, 40))
    painter.drawPath(path)
    painter.end()
    return QIcon(pixmap)


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("panelHint")
    return label


class PropertyPanel(QWidget):
    """样式编辑器。发出的 ``AnnotationStyle`` 是完整快照。"""

    style_changed = Signal(object)  # AnnotationStyle
    page_style_changed = Signal(object)  # 页码用的 AnnotationStyle
    page_mode_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("propPanel")
        self._loading = False
        self._style = AnnotationStyle()
        self._page_mode = False

        # -- 标题区 ----------------------------------------------------
        self._title = QLabel("样式")
        self._title.setObjectName("panelTitle")
        self._header = QLabel("新建标注的默认样式")
        self._header.setObjectName("panelHint")
        self._header.setWordWrap(True)
        # 页码和普通标注共用这套字体/颜色/字号控件，但样式写到两处不同的地方；
        # 这个「页码」开关决定当前编辑的是谁，避免改页码时把默认标注样式也改了。
        self._page_btn = QToolButton()
        self._page_btn.setText("页码")
        self._page_btn.setObjectName("segBtn")
        self._page_btn.setCheckable(True)
        self._page_btn.setToolTip("切换到页码样式：这里的字体、字号、颜色只改页码")
        self._page_btn.setFixedHeight(26)
        self._page_btn.setMinimumWidth(52)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(self._title)
        title_row.addStretch(1)
        title_row.addWidget(self._page_btn)

        # -- 字体 ------------------------------------------------------
        self._font_combo = QFontComboBox()
        self._font_combo.setMinimumWidth(150)
        # 说清楚边界：界面字体只影响预览；导出时中文统一用内置字体（随文件嵌入），
        # 选的衬线/等宽只对英文生效。不说清楚，用户会以为导出的字不对是 bug。
        self._font_note = _hint("仅预览；导出中文用内置字体，英文按衬线/等宽走")
        self._font_note.setWordWrap(True)

        # -- 字号 / 颜色 -----------------------------------------------
        self._size_spin = QDoubleSpinBox()
        self._size_spin.setRange(4.0, 400.0)
        self._size_spin.setDecimals(1)
        self._size_spin.setSingleStep(1.0)
        self._size_spin.setSuffix(" pt")

        # 常用字号：图纸上 6pt 这种小字不可能每次都手打，点一下就走
        self._size_preset = QComboBox()
        self._size_preset.setToolTip("常用字号")
        self._size_preset.setMaximumWidth(74)
        self._size_preset.addItem("常用", -1.0)
        for preset in SIZE_PRESETS:
            self._size_preset.addItem(f"{preset:g}", preset)

        self._color_btn = QToolButton()
        self._color_btn.setObjectName("swatch")
        self._color_btn.setToolTip("标注颜色")
        self._color_btn.setIconSize(QSize(20, 20))
        self._color_btn.setFixedSize(38, 32)

        size_row = QHBoxLayout()
        size_row.setSpacing(8)
        size_row.addWidget(_hint("字号"))
        self._size_spin.setMaximumWidth(96)
        size_row.addWidget(self._size_spin)
        size_row.addWidget(self._size_preset)
        size_row.addSpacing(6)
        size_row.addWidget(_hint("颜色"))
        size_row.addWidget(self._color_btn)
        size_row.addStretch(1)

        # -- 粗斜体 / 对齐 ---------------------------------------------
        self._bold_btn = QToolButton()
        self._bold_btn.setText("B")
        self._bold_btn.setObjectName("segBtn")
        self._bold_btn.setCheckable(True)
        self._bold_btn.setToolTip("加粗")
        self._bold_btn.setFixedSize(30, 28)
        font = self._bold_btn.font()
        font.setBold(True)
        self._bold_btn.setFont(font)

        self._italic_btn = QToolButton()
        self._italic_btn.setText("I")
        self._italic_btn.setObjectName("segBtn")
        self._italic_btn.setCheckable(True)
        self._italic_btn.setToolTip("斜体")
        self._italic_btn.setFixedSize(30, 28)
        font = self._italic_btn.font()
        font.setItalic(True)
        font.setFamily("Georgia")
        font.setPointSize(10)
        self._italic_btn.setFont(font)

        self._align_group = QButtonGroup(self)
        self._align_group.setExclusive(True)
        self._align_buttons: dict[str, QToolButton] = {}
        for key, text, tip in (
            (ALIGN_LEFT, "左", "左对齐"),
            (ALIGN_CENTER, "中", "居中对齐"),
            (ALIGN_RIGHT, "右", "右对齐"),
        ):
            button = QToolButton()
            button.setObjectName("segBtn")
            button.setText(text)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setFixedSize(34, 28)
            self._align_group.addButton(button)
            self._align_buttons[key] = button

        style_row = QHBoxLayout()
        style_row.setSpacing(4)
        style_row.addWidget(self._bold_btn)
        style_row.addWidget(self._italic_btn)
        style_row.addStretch(1)
        style_row.addWidget(self._align_buttons[ALIGN_LEFT])
        style_row.addWidget(self._align_buttons[ALIGN_CENTER])
        style_row.addWidget(self._align_buttons[ALIGN_RIGHT])

        # -- 不透明度 ---------------------------------------------------
        self._opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self._opacity_slider.setRange(20, 100)
        self._opacity_slider.setToolTip("不透明度")
        self._opacity_value = QLabel("100%")
        self._opacity_value.setObjectName("muted")
        self._opacity_value.setMinimumWidth(38)
        self._opacity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        opacity_row = QHBoxLayout()
        opacity_row.setSpacing(8)
        opacity_row.addWidget(self._opacity_slider, 1)
        opacity_row.addWidget(self._opacity_value)

        # -- 组装 ------------------------------------------------------
        def card(title: str, *items) -> QFrame:
            """面板里的一张小卡：8pt 小标题 + 内容。

            以前这里是一排裸的灰色小字当分隔，整块面板糊成一片；
            改成浅底圆角卡之后，字体 / 字号 / 对齐各归各位，扫一眼就知道改哪儿。
            """
            frame = QFrame()
            frame.setObjectName("groupCard")
            box = QVBoxLayout(frame)
            box.setContentsMargins(12, 10, 12, 12)
            box.setSpacing(8)
            box.addWidget(_hint(title))
            for item in items:
                if isinstance(item, QLayout):
                    box.addLayout(item)
                else:
                    box.addWidget(item)
            return frame

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)
        root.addLayout(title_row)
        root.addWidget(self._header)
        root.addWidget(card("字体", self._font_combo, self._font_note))
        root.addWidget(card("字号与颜色", size_row))
        root.addWidget(card("样式与对齐", style_row))
        root.addWidget(card("不透明度", opacity_row))
        root.addStretch(1)

        self._font_combo.currentFontChanged.connect(self._on_changed)
        self._size_spin.valueChanged.connect(self._on_changed)
        self._size_preset.activated.connect(self._on_preset)
        self._color_btn.clicked.connect(self._pick_color)
        self._bold_btn.toggled.connect(self._on_changed)
        self._italic_btn.toggled.connect(self._on_changed)
        self._align_group.buttonClicked.connect(self._on_changed)
        self._opacity_slider.valueChanged.connect(self._on_changed)
        self._page_btn.toggled.connect(self._on_page_mode_toggled)

        self.set_style(AnnotationStyle(), selection_count=0)

    # -- 对外 -------------------------------------------------------------

    def _on_preset(self, index: int) -> None:
        """点「常用字号」里的一项，直接把字号设过去并把下拉框收回到「常用」。"""
        value = self._size_preset.itemData(index)
        self._size_preset.setCurrentIndex(0)
        if value is None or float(value) < 0:
            return
        self._size_spin.setValue(float(value))

    def style(self) -> AnnotationStyle:
        return self._style

    @property
    def page_mode(self) -> bool:
        return self._page_mode

    def set_page_mode(self, on: bool) -> None:
        if bool(on) == self._page_mode:
            return
        self._page_btn.setChecked(bool(on))

    def _on_page_mode_toggled(self, on: bool) -> None:
        self._page_mode = bool(on)
        self._header.setText("页码样式" if on else "新建标注的默认样式")
        self.page_mode_changed.emit(self._page_mode)

    def set_style(self, style: AnnotationStyle, selection_count: int | None = None) -> None:
        self._loading = True
        try:
            self._style = style
            self._font_combo.setCurrentFont(QFont(style.font_family))
            self._size_spin.setValue(float(style.font_size))
            self._color_btn.setIcon(_color_icon(QColor(style.color)))
            self._color_btn.setToolTip(style.color)
            self._bold_btn.setChecked(bool(style.bold))
            self._italic_btn.setChecked(bool(style.italic))
            button = self._align_buttons.get(style.align) or self._align_buttons[ALIGN_LEFT]
            button.setChecked(True)
            self._opacity_slider.setValue(int(style.opacity))
            self._opacity_value.setText(f"{int(style.opacity)}%")
        finally:
            self._loading = False
        if self._page_mode:
            self._header.setText("页码样式")
        elif selection_count is None or selection_count == 0:
            self._header.setText("新建标注的默认样式")
        elif selection_count == 1:
            self._header.setText("已选中 1 个标注")
        else:
            self._header.setText(f"已选中 {selection_count} 个标注")

    # -- 内部 -------------------------------------------------------------

    def _current_style(self) -> AnnotationStyle:
        align = ALIGN_LEFT
        for key, button in self._align_buttons.items():
            if button.isChecked():
                align = key
                break
        return AnnotationStyle(
            font_family=self._font_combo.currentFont().family(),
            font_size=float(self._size_spin.value()),
            color=self._style.color,
            bold=self._bold_btn.isChecked(),
            italic=self._italic_btn.isChecked(),
            opacity=int(self._opacity_slider.value()),
            align=align,
        )

    def _on_changed(self, *args) -> None:
        if self._loading:
            return
        self._opacity_value.setText(f"{self._opacity_slider.value()}%")
        self._style = self._current_style()
        if self._page_mode:
            self.page_style_changed.emit(self._style)
        else:
            self.style_changed.emit(self._style)

    def _pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._style.color), self, "选择标注颜色")
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
        if self._page_mode:
            self.page_style_changed.emit(self._style)
        else:
            self.style_changed.emit(self._style)

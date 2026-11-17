"""QReport 的可视化界面：左边挑管线号，右边看统计结果。

只读：这里做的每一件事都是 ``qreport.reader.QpdfReader`` 提供的读操作，
不会写 ``.qpdf``、更不会碰原始 PDF。
"""

from __future__ import annotations

import csv
import json
import os
import time

from PySide6.QtCore import QSettings, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from qreport import QpdfError, QpdfReader
from qreport.welds import weld_rows
from qreport.xlsx import weld_table_xlsx
from qreport_app import icons, theme

TAG_ROLE = Qt.ItemDataRole.UserRole + 1
PAGES_ROLE = Qt.ItemDataRole.UserRole + 2

APP_VERSION = "1.3.2"


# --------------------------------------------------------------------------- 组件


class TagDelegate(QStyledItemDelegate):
    """管线号那一行：左边编号，右边「N 页」小胶囊。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.palette = theme.LIGHT

    def set_palette(self, palette: theme.Palette) -> None:
        self.palette = palette

    def sizeHint(self, option, index):  # noqa: N802
        return QSize(0, 40)

    def paint(self, painter, option, index) -> None:  # noqa: N802
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = option.rect.adjusted(10, 3, -10, -3)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected:
            painter.setBrush(QColor(self.palette.accent_soft))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 9, 9)
        elif hovered:
            painter.setBrush(QColor(self.palette.surface2))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 9, 9)

        tag = str(index.data(TAG_ROLE) or "")
        pages = int(index.data(PAGES_ROLE) or 0)
        badge = f"{pages} 页"

        font = QFont(option.font)
        font.setPointSizeF(10.0)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        badge_width = metrics.horizontalAdvance(badge) + 16
        text_rect = rect.adjusted(9, 0, -badge_width - 8, 0)
        painter.setPen(QColor(self.palette.text if selected else self.palette.muted))
        elided = metrics.elidedText(tag, Qt.TextElideMode.ElideMiddle, text_rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter, elided)

        badge_rect = rect.adjusted(rect.width() - badge_width - 5, 9, -5, -9)
        painter.setBrush(QColor(self.palette.accent_soft if selected else self.palette.surface2))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(badge_rect, 8, 8)
        font.setPointSizeF(8.5)
        painter.setFont(font)
        painter.setPen(QColor(self.palette.accent if selected else self.palette.faint))
        painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)
        painter.restore()


class Section(QWidget):
    """一个「卡片 + 表格」区：明细 / 跳过 / 全部编号。"""

    def __init__(self, columns: list[str], parent=None) -> None:
        super().__init__(parent)
        self.model = QStandardItemModel(0, len(columns), self)
        self.model.setHorizontalHeaderLabels(columns)
        self.view = QTableView(self)
        self.view.setModel(self.model)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.verticalHeader().setVisible(False)
        self.view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.view.setWordWrap(False)
        header = self.view.horizontalHeader()
        header.setStretchLastSection(True)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    def clear(self) -> None:
        self.model.setRowCount(0)

    def add_row(self, values: list) -> None:
        row = self.model.rowCount()
        self.model.insertRow(row)
        for column, value in enumerate(values):
            item = QStandardItem(str(value))
            item.setEditable(False)
            self.model.setItem(row, column, item)

    def set_column_widths(self, widths: list[int]) -> None:
        """列宽表；传 -1 表示这一列自动伸展（吃掉剩下的宽度）。"""
        header = self.view.horizontalHeader()
        header.setStretchLastSection(False)
        for index, width in enumerate(widths):
            if index >= header.count():
                continue
            if width < 0:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
                self.view.setColumnWidth(index, width)
        # 最后一列没指定时，让最右边那列吃掉余量，别留一条空带
        if len(widths) < header.count():
            header.setStretchLastSection(True)


# --------------------------------------------------------------------------- 主窗口


class MainWindow(QMainWindow):
    def __init__(self, dark: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("QReport —— 管线号数字标注统计")
        self.resize(1180, 760)
        self.setMinimumSize(940, 600)
        self.setAcceptDrops(True)

        self._settings = QSettings("QPDF", "QReport")
        self._reader: QpdfReader | None = None
        self._report = None
        self._all_reports: dict | None = None
        self.dark = dark
        self.palette = theme.DARK if dark else theme.LIGHT
        self._tag_delegate = TagDelegate(self)

        self._build_ui()
        self._apply_theme()
        self._update_empty_state()

    # ------------------------------------------------------------------ 搭界面

    def _build_ui(self) -> None:
        root = QWidget(self)
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(16)

        outer.addWidget(self._build_sidebar())
        outer.addWidget(self._build_content(), 1)

    def _shadow(self, widget: QWidget, blur: int = 26, dy: int = 6) -> None:
        effect = QGraphicsDropShadowEffect(widget)
        effect.setBlurRadius(blur)
        effect.setOffset(0, dy)
        effect.setColor(QColor(15, 23, 42, 46 if not self.dark else 120))
        widget.setGraphicsEffect(effect)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(318)
        self._shadow(sidebar)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(12)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        self._logo = QLabel()
        self._logo.setPixmap(icons.logo(30).scaled(60, 60, Qt.AspectRatioMode.KeepAspectRatio,
                                                   Qt.TransformationMode.SmoothTransformation))
        self._logo.setFixedSize(32, 32)
        brand.addWidget(self._logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel("QReport")
        name.setObjectName("brand")
        sub = QLabel("只读统计 · 管线号 → 最大编号")
        sub.setObjectName("brandSub")
        brand_text.addWidget(name)
        brand_text.addWidget(sub)
        brand.addLayout(brand_text)
        layout.addLayout(brand)

        self.btn_open = QPushButton(icons.icon("folder", "#ffffff"), "  打开项目文件…")
        self.btn_open.setObjectName("primary")
        self.btn_open.setMinimumHeight(38)
        self.btn_open.clicked.connect(self.choose_file)
        layout.addWidget(self.btn_open)

        project_card = QFrame()
        project_card.setObjectName("card")
        card_layout = QVBoxLayout(project_card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(4)
        self.lbl_project = QLabel("还没有打开项目")
        self.lbl_project.setObjectName("cardValue")
        self.lbl_project.setWordWrap(True)
        self.lbl_project_sub = QLabel("把 .qpdf 拖进窗口，或点上面的按钮")
        self.lbl_project_sub.setObjectName("hint")
        self.lbl_project_sub.setWordWrap(True)
        card_layout.addWidget(self.lbl_project)
        card_layout.addWidget(self.lbl_project_sub)
        layout.addWidget(project_card)

        search_row = QHBoxLayout()
        search_row.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索管线号…")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icons.icon("search", self.palette.faint), QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(self._on_search)
        search_row.addWidget(self.search, 1)
        layout.addLayout(search_row)

        self.tag_list = QListView()
        self.tag_list.setObjectName("tagList")
        self.tag_list.setUniformItemSizes(True)
        self.tag_list.setMouseTracking(True)
        self.tag_list.setItemDelegate(self._tag_delegate)
        self.tag_list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.tag_model = QStandardItemModel(self)
        self.tag_list.setModel(self.tag_model)
        self.tag_list.selectionModel().currentChanged.connect(self._on_tag_changed)
        layout.addWidget(self.tag_list, 1)

        footer = QLabel("只读：.qpdf 与原始 PDF 都不会被改动")
        footer.setObjectName("hint")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        return sidebar

    def _build_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        # 顶部：当前编号 + 操作
        header = QHBoxLayout()
        header.setSpacing(8)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.lbl_tag = QLabel("未选择管线号")
        self.lbl_tag.setObjectName("tagTitle")
        self.lbl_tag.setWordWrap(False)
        self.lbl_pages = QLabel("")
        self.lbl_pages.setObjectName("hint")
        title_col.addWidget(self.lbl_tag)
        title_col.addWidget(self.lbl_pages)
        header.addLayout(title_col, 1)

        self.btn_excel = QPushButton(icons.icon("export", self.palette.muted), " 导出 Excel")
        self.btn_excel.setObjectName("primary")
        self.btn_excel.setEnabled(False)
        self.btn_excel.setToolTip("按 PDF 页序导出「管线号 / 焊口号 / 页码 / 探伤比例」四列（整本）")
        self.btn_excel.clicked.connect(self.export_weld_xlsx)
        self.btn_csv = QPushButton(icons.icon("export", self.palette.muted), " 导出 CSV")
        self.btn_csv.clicked.connect(lambda: self.export("csv"))
        self.btn_json = QPushButton(icons.icon("export", self.palette.muted), " 导出 JSON")
        self.btn_json.clicked.connect(lambda: self.export("json"))
        self.btn_theme = QPushButton()
        self.btn_theme.setObjectName("ghost")
        self.btn_theme.setToolTip("切换浅色 / 深色")
        self.btn_theme.setIconSize(QSize(16, 16))
        self.btn_theme.clicked.connect(self.toggle_theme)
        header.addWidget(self.btn_excel)
        header.addWidget(self.btn_csv)
        header.addWidget(self.btn_json)
        header.addWidget(self.btn_theme)
        layout.addLayout(header)

        # 主数字卡 + 三个统计格
        hero_row = QHBoxLayout()
        hero_row.setSpacing(12)
        hero = QFrame()
        hero.setObjectName("hero")
        hero.setMinimumHeight(118)
        self._shadow(hero, 30, 8)
        hero_layout = QHBoxLayout(hero)
        hero_layout.setContentsMargins(22, 14, 22, 14)
        left = QVBoxLayout()
        left.setSpacing(0)
        caption = QLabel("最大编号")
        caption.setObjectName("heroCaption")
        left.addWidget(caption)
        self.lbl_max = QLabel("—")
        self.lbl_max.setObjectName("heroNumber")
        left.addWidget(self.lbl_max)
        hero_layout.addLayout(left, 1)
        hint = QLabel("只统计纯整数\n百分比会被跳过")
        hint.setObjectName("heroSuffix")
        hint.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        hero_layout.addWidget(hint, 0)
        hero_row.addWidget(hero, 3)

        chips = QVBoxLayout()
        chips.setSpacing(10)
        self._chip_pages = self._stat_card("相关页面", "0")
        self._chip_marks = self._stat_card("可统计标注", "0")
        self._chip_skipped = self._stat_card("跳过（需人工看）", "0")
        chips.addWidget(self._chip_pages[0])
        chips.addWidget(self._chip_marks[0])
        chips.addWidget(self._chip_skipped[0])
        hero_row.addLayout(chips, 1)
        layout.addLayout(hero_row)

        # 分段按钮 + 三个表格
        segment_row = QHBoxLayout()
        segment_row.setSpacing(4)
        self._segment_group = QButtonGroup(self)
        self._segment_group.setExclusive(True)
        self._segments: list[QPushButton] = []
        for index, text in enumerate(("明细", "跳过项", "全部编号")):
            button = QPushButton(text)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self._segment_group.addButton(button, index)
            self._segments.append(button)
            segment_row.addWidget(button)
        segment_row.addStretch(1)
        layout.addLayout(segment_row)
        self._segments[0].setChecked(True)
        self._segment_group.idClicked.connect(self._on_segment)

        self.stack = QStackedWidget()
        self.detail = Section(["页", "编号", "编号组", "序号", "来源", "隐藏"])
        self.detail.set_column_widths([70, 90, 110, 80, 110])
        self.skipped = Section(["页", "内容", "原因"])
        self.skipped.set_column_widths([70, 160])
        self.all_tags = Section(["管线号", "页数", "可统计", "最大编号"])
        self.all_tags.set_column_widths([-1, 80, 100, 110])
        self.all_tags.view.doubleClicked.connect(self._jump_from_all)
        self.stack.addWidget(self.detail)
        self.stack.addWidget(self.skipped)
        self.stack.addWidget(self.all_tags)
        layout.addWidget(self.stack, 1)

        self.status = QLabel("等待打开项目文件")
        self.status.setObjectName("status")
        layout.addWidget(self.status)
        return content

    def _stat_card(self, title: str, value: str) -> tuple[QWidget, QLabel]:
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumHeight(34)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 6, 14, 6)
        label = QLabel(title)
        label.setObjectName("cardTitle")
        number = QLabel(value)
        number.setObjectName("cardValue")
        layout.addWidget(label)
        layout.addStretch(1)
        layout.addWidget(number)
        return card, number

    # ------------------------------------------------------------------ 外观

    def _apply_theme(self) -> None:
        self.palette = theme.DARK if self.dark else theme.LIGHT
        self.setStyleSheet(theme.stylesheet(self.palette))
        self._tag_delegate.set_palette(self.palette)
        self.btn_theme.setIcon(
            icons.icon("sun" if self.dark else "moon", self.palette.muted)
        )
        self._logo.setPixmap(
            icons.logo(30, self.palette.accent).scaled(
                60, 60, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )
        self.btn_csv.setIcon(icons.icon("export", self.palette.muted))
        self.btn_json.setIcon(icons.icon("export", self.palette.muted))
        self.search.actions()[0].setIcon(icons.icon("search", self.palette.faint))
        self.tag_list.viewport().update()

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self._apply_theme()
        self._settings.setValue("dark", self.dark)

    # ------------------------------------------------------------------ 打开文件

    def choose_file(self) -> None:
        start = str(self._settings.value("last_dir", "", type=str) or "")
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 QPDF 项目文件", start, "QPDF 项目 (*.qpdf);;所有文件 (*)"
        )
        if path:
            self.open_project(path)

    def open_project(self, path: str) -> bool:
        try:
            reader = QpdfReader(path)
        except QpdfError as exc:
            QMessageBox.warning(self, "打不开", str(exc))
            return False
        self._close_reader()
        self._reader = reader
        self._settings.setValue("last_dir", os.path.dirname(os.path.abspath(path)))

        info = reader.source_info()
        self.lbl_project.setText(os.path.basename(path))
        pdf_name = os.path.basename(info["pdf"]) if info["pdf"] else "（记录里没有 PDF 路径）"
        self.lbl_project_sub.setText(
            f"原始 PDF：{pdf_name}\n{info['page_count']} 页 ｜ 保存于 "
            f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(info['saved_at'])) if info['saved_at'] else '—'}"
        )
        self.status.setText("正在读管线号…")

        self.tag_model.clear()
        tags = reader.tags()
        for tag in tags:
            pages = len(reader.pages_for_tag(tag))
            item = QStandardItem(tag)
            item.setData(tag, TAG_ROLE)
            item.setData(pages, PAGES_ROLE)
            item.setEditable(False)
            self.tag_model.appendRow(item)
        self._all_reports = None
        self.all_tags.clear()
        self.btn_excel.setEnabled(True)
        self.btn_csv.setEnabled(True)
        self.btn_json.setEnabled(True)
        self.status.setText(f"共 {len(tags)} 个管线号 ｜ 只读打开：{os.path.basename(path)}")
        if self.tag_model.rowCount():
            self.tag_list.setCurrentIndex(self.tag_model.index(0, 0))
        else:
            self._clear_report("这个项目里还没有识别出的管线号（先在 QPDF 里跑一次批量识别）")
        return True

    def _close_reader(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._report = None

    # ------------------------------------------------------------------ 选中编号

    def _current_tag(self) -> str:
        index = self.tag_list.currentIndex()
        return str(index.data(TAG_ROLE) or "") if index.isValid() else ""

    def _on_search(self, text: str) -> None:
        text = (text or "").strip().lower()
        for row in range(self.tag_model.rowCount()):
            item = self.tag_model.item(row)
            tag = str(item.data(TAG_ROLE) or "")
            self.tag_list.setRowHidden(row, bool(text) and text not in tag.lower())

    def _on_tag_changed(self, current, previous) -> None:
        tag = str(current.data(TAG_ROLE) or "") if current.isValid() else ""
        if not tag or self._reader is None:
            return
        started = time.perf_counter()
        report = self._reader.report_for_tag(tag)
        elapsed = (time.perf_counter() - started) * 1000
        self._report = report
        self.lbl_tag.setText(tag)
        self.lbl_pages.setText(
            "相关页面：" + ("、".join(str(no) for no in report.page_nos) or "无")
        )
        self.lbl_max.setText(str(report.max_value) if report.max_value is not None else "—")
        self._chip_pages[1].setText(str(len(report.pages)))
        self._chip_marks[1].setText(str(len(report.marks)))
        self._chip_skipped[1].setText(str(len(report.skipped)))

        self.detail.clear()
        for mark in report.marks:
            self.detail.add_row(
                [
                    mark.page_no,
                    mark.value,
                    mark.seq_group or "—",
                    mark.seq_index if mark.seq_index is not None else "—",
                    "编号组" if mark.source == "sequence" else "内容兜底",
                    "是" if mark.hidden else "",
                ]
            )
        self.skipped.clear()
        for item in report.skipped:
            self.skipped.add_row([item.page_no, item.raw or "（空）", item.reason])
        self.status.setText(
            f"{len(report.marks)} 条可统计 ｜ {len(report.skipped)} 条跳过 ｜ 用时 {elapsed:.0f} ms"
        )

    def _clear_report(self, message: str = "") -> None:
        self._report = None
        self.lbl_tag.setText("未选择管线号")
        self.lbl_pages.setText("")
        self.lbl_max.setText("—")
        self._chip_pages[1].setText("0")
        self._chip_marks[1].setText("0")
        self._chip_skipped[1].setText("0")
        self.detail.clear()
        self.skipped.clear()
        if message:
            self.status.setText(message)

    def _update_empty_state(self) -> None:
        self.btn_excel.setEnabled(False)
        self.btn_csv.setEnabled(False)
        self.btn_json.setEnabled(False)
        self._clear_report("把 .qpdf 拖进窗口，或点左上角的「打开项目文件…」")

    # ------------------------------------------------------------------ 分段

    def _on_segment(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        if index == 2 and self._all_reports is None:
            self._load_all_reports()

    def _load_all_reports(self) -> None:
        if self._reader is None:
            return
        self.status.setText("正在把所有编号算一遍…")
        QTimer.singleShot(0, self._fill_all_reports)

    def _fill_all_reports(self) -> None:
        if self._reader is None:
            return
        started = time.perf_counter()
        reports = self._reader.report_all()
        self._all_reports = reports
        self.all_tags.clear()
        for tag, report in reports.items():
            self.all_tags.add_row(
                [
                    tag,
                    len(report.pages),
                    len(report.marks),
                    report.max_value if report.max_value is not None else "—",
                ]
            )
        elapsed = (time.perf_counter() - started) * 1000
        self.status.setText(f"共 {len(reports)} 个管线号，统计用时 {elapsed:.0f} ms")

    def _jump_from_all(self, index) -> None:
        row = index.row()
        tag = self.all_tags.model.item(row, 0).text()
        for row_index in range(self.tag_model.rowCount()):
            item = self.tag_model.item(row_index)
            if str(item.data(TAG_ROLE) or "") == tag:
                self.tag_list.setCurrentIndex(self.tag_model.index(row_index, 0))
                self._segments[0].setChecked(True)
                self.stack.setCurrentIndex(0)
                break

    # ------------------------------------------------------------------ 导出

    def export_weld_xlsx(self) -> None:
        """导出「管线号 / 焊口号」两列（整本，按 PDF 页序）。"""
        if self._reader is None:
            return
        base = os.path.splitext(os.path.basename(self._reader.path))[0]
        default = os.path.join(
            str(self._settings.value("last_dir", "", type=str) or ""), f"{base}_管线号焊口号.xlsx"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 Excel", default, "Excel 文件 (*.xlsx)"
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            rows = weld_rows(self._reader)
            if not rows:
                QMessageBox.information(self, "没有可导出的", "这个项目里还没有可用的焊口号。")
                return
            count = weld_table_xlsx(path, rows)
            self.status.setText(
                f"已导出 {count} 行、{len({row.tag for row in rows})} 个管线号：{os.path.basename(path)}"
            )
        except Exception as exc:  # noqa: BLE001 - 写盘失败要让人看见
            QMessageBox.warning(self, "导出失败", str(exc))

    def export(self, kind: str) -> None:
        if self._report is None:
            return
        tag = self._report.tag or "管线号"
        safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in tag)[:60]
        suffix = ".csv" if kind == "csv" else ".json"
        default = os.path.join(
            str(self._settings.value("last_dir", "", type=str) or ""), f"{safe}_统计{suffix}"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出结果", default, "CSV 文件 (*.csv)" if kind == "csv" else "JSON 文件 (*.json)"
        )
        if not path:
            return
        if not path.lower().endswith(suffix):
            path += suffix
        try:
            if kind == "csv":
                # utf-8-sig：Excel 直接双击打开不乱码
                with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["页", "编号", "编号组", "序号", "来源", "隐藏", "X", "Y"])
                    for mark in self._report.marks:
                        writer.writerow(
                            [
                                mark.page_no,
                                mark.value,
                                mark.seq_group,
                                mark.seq_index if mark.seq_index is not None else "",
                                "编号组" if mark.source == "sequence" else "内容兜底",
                                1 if mark.hidden else 0,
                                round(mark.x, 2),
                                round(mark.y, 2),
                            ]
                        )
                    for item in self._report.skipped:
                        writer.writerow([item.page_no, "", "", "", "跳过", 0, item.raw, item.reason])
            else:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(self._report.to_dict(), handle, ensure_ascii=False, indent=1)
            self.status.setText(f"已导出：{os.path.basename(path)}")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))

    # ------------------------------------------------------------------ 事件

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".qpdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path.lower().endswith(".qpdf"):
                self.open_project(path)
                break
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.setValue("dark", self.dark)
        self._close_reader()
        super().closeEvent(event)

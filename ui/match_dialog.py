"""数据匹配标注对话框（用户给的第 1~8 步）。

流程按那个顺序摆，但界面只有一屏：

1. 选 CSV / Excel；
2. 选"匹配字段"（管线号）+ 写进图里的内容字段；
3. 框识别区域、框标注位置（框完自动回填，不用来回切窗口）；
4. 后台识别 + 匹配，一页一行铺在表里；
5. 冲突 / 未匹配 / 字段为空 单独标出来，**绝不替用户猜**；
6. 生成标注（走雀雀 PDF 自己的文字标注，一次 Ctrl+Z 能整批退掉）；
7. 改完表格重新匹配，表和项目里的记录对一遍；
8. 先生成"变更预览"再确认。

三条铁律（都在 core/dataset.py 里实现，这里只负责展示）：

- 匹配不到就显示未匹配，最多给候选，不拿"最像的"顶上；
- 同一个键有多条记录 = 冲突，必须人手指定；
- OCR 修正过的编号命中表记录要把状态标出来（"说明"列）。

生成时会往 .qpdf 里写追溯记录（match_annotations）：原始编号、
匹配到的整条记录、写上去的字、导入版本、生成时间，以后能查是哪来的。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import auto_annotate as auto
from core import dataset
from core import toc as tocmodel
from core.annotation import ALIGN_CENTER, ALIGN_LEFT, ALIGN_RIGHT, AnnotationStyle
from core.match_job import MatchWorker
from ui import theme

#: 预览表的列
COL_USE, COL_PAGE, COL_TAG, COL_STATUS, COL_RECORD, COL_CONTENT, COL_NOTE = range(7)
HEADERS = ["生成", "页", "识别的编号", "状态", "表里的记录", "要写的字", "说明"]

#: 状态 → 颜色（红 = 需要人看一眼，灰 = 没识别到）
_STATUS_COLOR = {
    auto.ST_MATCHED: theme.TEXT,
    auto.ST_CONFLICT: theme.DANGER,
    auto.ST_UNMATCHED: theme.DANGER,
    auto.ST_NOVALUE: theme.DANGER,
    auto.ST_EMPTY: theme.FAINT,
    auto.ST_SKIP: theme.MUTED,
    auto.ST_DUP: theme.MUTED,
    auto.ST_MANUAL: theme.TEXT,
}

_ALIGN_ITEMS = (("左对齐", ALIGN_LEFT), ("居中", ALIGN_CENTER), ("右对齐", ALIGN_RIGHT))


def _region_text(region) -> str:
    if region is None or region.is_empty:
        return "还没框"
    return "左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}".format(
        region.x0, region.y0, region.x1, region.y1
    )


def _record_text(row: "auto.PlanRow") -> str:
    if row.status == auto.ST_CONFLICT:
        return "（多条记录冲突，双击选）"
    if row.status == auto.ST_UNMATCHED:
        if row.candidates:
            return "候选：" + "、".join(key for key, _ratio in row.candidates[:3])
        return "表里没有"
    if not row.record:
        return ""
    text = " · ".join(
        f"{name}={value}" for name, value in row.record.items() if str(value).strip()
    )
    return text if len(text) <= 120 else text[:117] + "…"


class RecordPickerDialog(QDialog):
    """手动挑一条记录（冲突、未匹配、写错了都用它）。

    搜索框里打几个字就过滤；列表显示整行的字段，够认人了。
    """

    def __init__(self, records: "dataset.RecordTable", initial=None, keyword="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择记录")
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.resize(760, 520)
        self._records = records
        self._all = records.all_records()
        self.record: dict | None = None
        self.picked = False

        layout = QVBoxLayout(self)
        hint = QLabel("表里的记录，双击一条就用它。也可以在下面搜编号。", self)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(hint)
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜编号 / 任意字段…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._refresh)
        layout.addWidget(self._search)
        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())
        layout.addWidget(self._list, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("用这条")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        clear = QPushButton("这页不匹配", self)
        buttons.addButton(clear, QDialogButtonBox.ResetRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        clear.clicked.connect(self._clear_pick)
        layout.addWidget(buttons)
        if keyword:
            self._search.setText(keyword)
        self._refresh()

    def _refresh(self) -> None:
        text = self._search.text().strip()
        self._list.clear()
        shown = 0
        for record in self._all:
            line = " · ".join(
                f"{name}={value}" for name, value in record.items() if str(value).strip()
            )
            if text and text not in line and text.upper() not in line.upper():
                continue
            item = QListWidgetItem(line)
            item.setData(Qt.UserRole, record)
            self._list.addItem(item)
            shown += 1
            if shown >= 300:
                break

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self.record = item.data(Qt.UserRole)
        self.picked = True
        self.accept()

    def _clear_pick(self) -> None:
        self.record = None
        self.picked = True
        self.accept()


class ChangePreviewDialog(QDialog):
    """生成前先看一眼：哪页新增、哪页内容变了、哪页记录消失了。"""

    def __init__(self, changes: list, parent=None):
        super().__init__(parent)
        self.setWindowTitle("变更预览")
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.resize(860, 520)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"一共 {len(changes)} 处变化，确认后按这个结果生成：", self))
        table = QTableWidget(len(changes), 5, self)
        table.setHorizontalHeaderLabels(["动作", "页", "原来的字", "新的字", "编号"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        for index, change in enumerate(changes):
            cells = [
                change.label,
                str(change.page + 1),
                change.old,
                change.new,
                change.tag,
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(
                        QColor(theme.DANGER if change.kind == "remove" else theme.TEXT)
                    )
                table.setItem(index, column, item)
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("按这个生成")
        buttons.button(QDialogButtonBox.Cancel).setText("再改改")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CandidatePickerDialog(QDialog):
    """表里没有这个编号时给的候选：点一条就用它，也可以再回整张表里搜。

    候选只是「长得像」，所以必须人手点一下才算数，绝不自动采用。
    """

    def __init__(self, records, candidates, keyword="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("挑一条候选记录")
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self.resize(720, 460)
        self._records = records
        self._keyword = keyword or ""
        self.record: dict | None = None
        self.picked = False

        layout = QVBoxLayout(self)
        hint = QLabel(
            "表里没有这个编号，下面是长得最像的几条。点一条看全字段，再用它。", self
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.MUTED};")
        layout.addWidget(hint)
        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())
        for key, ratio in candidates or []:
            item = QListWidgetItem(f"{key}    相似度 {ratio:.0%}")
            item.setData(Qt.UserRole, key)
            self._list.addItem(item)
        if self._list.count():
            self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)
        self._detail = QLabel("", self)
        self._detail.setWordWrap(True)
        layout.addWidget(self._detail)
        self._list.currentItemChanged.connect(self._on_current)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.button(QDialogButtonBox.Ok).setText("用这条")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        search = QPushButton("搜索整张表…", self)
        buttons.addButton(search, QDialogButtonBox.ActionRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        search.clicked.connect(self._search_all)
        layout.addWidget(buttons)
        self._on_current(self._list.currentItem(), None)

    def _key(self) -> str:
        item = self._list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def _on_current(self, item, _previous) -> None:
        key = str(item.data(Qt.UserRole)) if item is not None else ""
        record = self._records.record_for_key(key) if key else None
        text = ""
        if record:
            text = " · ".join(
                f"{name}={value}" for name, value in record.items() if str(value).strip()
            )
        self._detail.setText(text)

    def _accept(self) -> None:
        key = self._key()
        record = self._records.record_for_key(key) if key else None
        if record is None:
            return
        self.record = record
        self.picked = True
        self.accept()

    def _search_all(self) -> None:
        dialog = RecordPickerDialog(self._records, None, self._keyword, self)
        if dialog.exec() != QDialog.Accepted or not dialog.picked or dialog.record is None:
            return
        self.record = dialog.record
        self.picked = True
        self.accept()


class MatchDialog(QDialog):
    """数据匹配标注主窗口。非模态，开着它也能在图上翻页、框区域。"""

    #: 让主窗口去画布上框区域（"scan" / "annot"）
    region_requested = Signal(str)

    def __init__(
        self,
        path: str,
        page_count: int,
        project,
        viewer,
        page_sizes,
        parent=None,
        settings=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("数据匹配标注")
        self.resize(1180, 720)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self._path = path
        self._page_count = int(page_count)
        self._project = project
        self._viewer = viewer
        self._page_sizes = list(page_sizes or [])
        self._table: dataset.Table | None = None
        self._records: dataset.RecordTable | None = None
        self._fingerprint = ""
        self._import_id = 0
        self._scans: dict[int, dict] = {}
        self._rows: list[auto.PlanRow] = []
        self._by_page: dict[int, auto.PlanRow] = {}
        self._row_of: dict[int, int] = {}
        self._worker: MatchWorker | None = None
        self._filling = False
        # 和「设置」里那个「同名只标一次」是同一个开关。
        # settings 可以注入（测试用），否则读写程序自己的 QSettings。
        self._settings = settings if settings is not None else QSettings("QPDF", "QPDF")
        self._skip_dup = self._settings.value("match_skip_duplicates", False, type=bool)
        self._key_rule = self._settings.value("match_key_rule", dataset.KEY_AUTO, type=str) or dataset.KEY_AUTO
        # 内容字段可以是空的：写点什么比空着强，但得是用户自己交代的
        self._empty_text = self._settings.value("match_empty_text", "", type=str) or ""
        self._prefix = self._settings.value("match_prefix", "", type=str) or ""
        self._suffix = self._settings.value("match_suffix", "", type=str) or ""
        # 勾上「默认字不加前后缀」= 占位的默认字不套前后缀
        self._decorate_placeholder = not bool(
            self._settings.value("match_plain_placeholder", False, type=bool)
        )

        self._build()
        self.refresh_regions()
        self._show_import_info()
        # 上次那张表在窗口出来之后再读，别挡着界面显示。
        # 只把「表 / 工作表 / 字段 / 模板」这些配置捡回来，不当场去识图——
        # 识别是点「开始识别」才做的事，开窗就扫一遍太吓人（大文件要几分钟）。
        QTimer.singleShot(0, self._restore_last)

    # ------------------------------------------------------------------ 搭建

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self._build_dataset_box(), 1)
        top.addWidget(self._build_place_box())
        layout.addLayout(top)
        layout.addWidget(self._build_run_box())
        layout.addWidget(self._build_table(), 1)
        layout.addLayout(self._build_bottom())

    def _build_dataset_box(self) -> QWidget:
        box = QGroupBox("1 数据表", self)
        form = QFormLayout(box)
        row = QHBoxLayout()
        self._file_edit = QLineEdit(box)
        self._file_edit.setReadOnly(True)
        self._file_edit.setPlaceholderText("还没导入 CSV / Excel")
        browse = QPushButton("浏览…", box)
        browse.clicked.connect(self.choose_file)
        row.addWidget(self._file_edit, 1)
        row.addWidget(browse)
        form.addRow("文件", row)

        self._sheet_combo = QComboBox(box)
        self._sheet_combo.setEnabled(False)
        self._sheet_combo.currentTextChanged.connect(self._on_sheet_changed)
        form.addRow("工作表", self._sheet_combo)

        self._key_combo = QComboBox(box)
        self._key_combo.currentTextChanged.connect(self._on_key_changed)
        form.addRow("匹配字段", self._key_combo)

        self._content_combo = QComboBox(box)
        self._content_combo.currentTextChanged.connect(self._on_content_changed)
        form.addRow("内容字段", self._content_combo)

        self._rule_combo = QComboBox(box)
        for rule, label in dataset.KEY_RULES:
            self._rule_combo.addItem(label, rule)
        index = self._rule_combo.findData(self._key_rule)
        self._rule_combo.setCurrentIndex(max(0, index))
        self._rule_combo.setToolTip(
            "图上的管线号常是「口径-单元-线号-材料等级-保温」，表里记的是「单元-线号」。"
            "自动：先拿整串查，查不到再取中间两段。"
        )
        self._rule_combo.currentIndexChanged.connect(self._on_rule_changed)
        form.addRow("编号取法", self._rule_combo)

        self._template_edit = QLineEdit(box)
        self._template_edit.setPlaceholderText("例如 {探伤比例}，也可以写成 RT {探伤比例}")
        self._template_edit.editingFinished.connect(self._rebuild_records)
        form.addRow("模板", self._template_edit)

        self._empty_edit = QLineEdit(box)
        self._empty_edit.setText(self._empty_text)
        self._empty_edit.setPlaceholderText("内容字段是空的时候写这个；留空 = 这页不生成")
        self._empty_edit.setToolTip(
            "表里这一格是空的（比如探伤比例写「/」）时，用这句话顶上；留空就还是记成「字段为空」不生成。"
        )
        self._empty_edit.editingFinished.connect(self._on_empty_text)
        form.addRow("字段为空写", self._empty_edit)

        decor = QHBoxLayout()
        self._prefix_edit = QLineEdit(box)
        self._prefix_edit.setText(self._prefix)
        self._prefix_edit.setPlaceholderText("前加，如 RT ")
        self._prefix_edit.editingFinished.connect(self._on_decor_changed)
        self._suffix_edit = QLineEdit(box)
        self._suffix_edit.setText(self._suffix)
        self._suffix_edit.setPlaceholderText("后加，如 级")
        self._suffix_edit.editingFinished.connect(self._on_decor_changed)
        decor.addWidget(self._prefix_edit, 1)
        decor.addWidget(self._suffix_edit, 1)
        form.addRow("字前后加", decor)

        self._plain_placeholder = QCheckBox("默认字不加前后缀", box)
        self._plain_placeholder.setChecked(bool(self._decorate_placeholder) is False)
        self._plain_placeholder.setToolTip(
            "勾上：表里字段为空、用「字段为空写」补的那句话，不再套上面的前后加。"
        )
        self._plain_placeholder.toggled.connect(self._on_plain_placeholder)
        form.addRow("", self._plain_placeholder)

        self._info_label = QLabel("", box)
        self._info_label.setWordWrap(True)
        self._info_label.setStyleSheet(f"color: {theme.MUTED};")
        form.addRow(self._info_label)
        return box

    def _build_place_box(self) -> QWidget:
        box = QGroupBox("2 图上的位置", self)
        form = QFormLayout(box)

        # 有书签的 PDF 不用去图上认字：直接拿目录标题当编号查表
        self._bookmark_check = QCheckBox("用目录书签（不识别图上区域）", box)
        self._bookmark_check.setToolTip(
            "勾上：拿这份 PDF 自带的目录标题当编号去查表，识别区域就不用了。\n"
            "适合已经做过书签、或者目录里就写着管线号的图纸。"
        )
        form.addRow("", self._bookmark_check)

        scan_row = QHBoxLayout()
        self._scan_label = QLabel("还没框", box)
        self._scan_label.setStyleSheet(f"color: {theme.MUTED};")
        self._scan_button = QPushButton("框识别区域", box)
        self._scan_button.clicked.connect(lambda: self.region_requested.emit("scan"))
        scan_row.addWidget(self._scan_label, 1)
        scan_row.addWidget(self._scan_button)
        form.addRow("识别区域", scan_row)
        self._bookmark_check.toggled.connect(self._on_bookmark_toggled)

        annot_row = QHBoxLayout()
        self._annot_label = QLabel("还没框", box)
        self._annot_label.setStyleSheet(f"color: {theme.MUTED};")
        annot_button = QPushButton("框标注位置", box)
        annot_button.clicked.connect(lambda: self.region_requested.emit("annot"))
        annot_row.addWidget(self._annot_label, 1)
        annot_row.addWidget(annot_button)
        form.addRow("标注位置", annot_row)

        self._size_spin = QDoubleSpinBox(box)
        self._size_spin.setRange(4.0, 72.0)
        self._size_spin.setDecimals(1)
        self._size_spin.setSingleStep(0.5)
        self._size_spin.setSuffix(" pt")
        style = getattr(self._viewer, "default_style", None)
        self._size_spin.setValue(float(getattr(style, "font_size", 12.0) or 12.0))
        form.addRow("字号", self._size_spin)

        self._align_combo = QComboBox(box)
        for label, _value in _ALIGN_ITEMS:
            self._align_combo.addItem(label)
        form.addRow("对齐", self._align_combo)

        self._pad_spin = QDoubleSpinBox(box)
        self._pad_spin.setRange(0.0, 48.0)
        self._pad_spin.setDecimals(1)
        self._pad_spin.setValue(3.0)
        self._pad_spin.setSuffix(" pt")
        form.addRow("边距", self._pad_spin)
        return box

    def _build_run_box(self) -> QWidget:
        box = QGroupBox("3 识别并匹配", self)
        layout = QHBoxLayout(box)
        layout.addWidget(QLabel("从第", box))
        self._start_spin = QSpinBox(box)
        self._start_spin.setRange(1, max(1, self._page_count))
        layout.addWidget(self._start_spin)
        layout.addWidget(QLabel("页到第", box))
        self._end_spin = QSpinBox(box)
        self._end_spin.setRange(1, max(1, self._page_count))
        self._end_spin.setValue(max(1, self._page_count))
        layout.addWidget(self._end_spin)
        layout.addWidget(QLabel("页", box))

        self._start_button = QPushButton("开始识别", box)
        self._start_button.clicked.connect(self.start_scan)
        layout.addWidget(self._start_button)
        self._stop_button = QPushButton("停止", box)
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self.stop_scan)
        layout.addWidget(self._stop_button)
        self._progress = QProgressBar(box)
        self._progress.setMinimumWidth(200)
        self._progress.setTextVisible(True)
        layout.addWidget(self._progress, 1)

        self._dup_check = QCheckBox("同名只标一次", box)
        self._dup_check.setChecked(bool(self._skip_dup))
        self._dup_check.setToolTip(
            "勾上：同一个管线号只在第一次出现的那页标，后面的记成「同名跳过」（还能单独勾回来）"
        )
        self._dup_check.toggled.connect(self._on_skip_dup)
        layout.addWidget(self._dup_check)
        return box

    def _build_table(self) -> QWidget:
        self._table_widget = QTableWidget(0, len(HEADERS), self)
        self._table_widget.setHorizontalHeaderLabels(HEADERS)
        self._table_widget.verticalHeader().setVisible(False)
        self._table_widget.setAlternatingRowColors(True)
        self._table_widget.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table_widget.setWordWrap(False)
        self._table_widget.setEditTriggers(
            QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
        )
        self._table_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table_widget.customContextMenuRequested.connect(self._on_menu)
        self._table_widget.itemChanged.connect(self._on_item_changed)
        self._table_widget.itemDoubleClicked.connect(self._on_double_click)
        header = self._table_widget.horizontalHeader()
        widths = {
            COL_USE: 46,
            COL_PAGE: 56,
            COL_TAG: 150,
            COL_STATUS: 84,
            COL_RECORD: 300,
            COL_CONTENT: 220,
        }
        for column, width in widths.items():
            self._table_widget.setColumnWidth(column, width)
        header.setSectionResizeMode(COL_NOTE, QHeaderView.Stretch)
        return self._table_widget

    def _build_bottom(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self._status_label = QLabel("先导入 CSV / Excel，再框区域。", self)
        self._status_label.setStyleSheet(f"color: {theme.MUTED};")
        row.addWidget(self._status_label, 1)
        preview_button = QPushButton("变更预览…", self)
        preview_button.clicked.connect(self.preview_changes)
        row.addWidget(preview_button)
        generate_button = QPushButton("生成标注", self)
        theme.mark_primary(generate_button)
        generate_button.clicked.connect(self.generate)
        row.addWidget(generate_button)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.close)
        row.addWidget(close_button)
        return row

    # -------------------------------------------------------------- 数据表

    def choose_file(self) -> None:
        start = os.path.dirname(self._file_edit.text()) if self._file_edit.text() else ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "选数据表", start, "表格 (*.csv *.xlsx *.xlsm);;所有文件 (*.*)"
        )
        if path:
            self.load_dataset(path)

    def load_dataset(self, path: str) -> None:
        try:
            table = dataset.read_table(path)
        except dataset.DataError as exc:
            QMessageBox.warning(self, "读不了这张表", str(exc))
            return
        self._table = table
        self._fingerprint = dataset.file_fingerprint(path)
        self._file_edit.setText(path)
        self._file_edit.setToolTip(path)
        self._sheet_combo.blockSignals(True)
        self._sheet_combo.clear()
        if table.sheets:
            self._sheet_combo.addItems(table.sheets)
            self._sheet_combo.setCurrentText(table.sheet)
        else:
            # CSV 没有工作表的概念，也占一行写清楚，别留个空白框让人猜
            self._sheet_combo.addItem(table.sheet or "CSV")
        self._sheet_combo.setEnabled(len(table.sheets) > 1)
        self._sheet_combo.blockSignals(False)
        self._fill_columns()
        self._info_label.setText(table.describe())
        self._remember_dataset()

    # -------------------------------------------------------- 记住上次这张表

    def _remember_dataset(self) -> None:
        """把「上次用的这张表」记下来，下次开窗自动接着用。"""
        if self._table is None:
            return
        self._settings.setValue("match_last_path", self._table.path or "")
        self._settings.setValue("match_last_sheet", self._table.sheet or "")
        self._settings.setValue("match_last_key", self._key_combo.currentText())
        self._settings.setValue("match_last_content", self._content_combo.currentText())
        self._settings.setValue("match_last_template", self._template_edit.text())

    def _restore_last(self) -> None:
        """开窗时把上次的表捡回来；文件没了就安静地什么都不做。"""
        path = self._settings.value("match_last_path", "", type=str) or ""
        if not path or not os.path.isfile(path):
            return
        if self._table is not None:
            return
        self.load_dataset(path)
        sheet = self._settings.value("match_last_sheet", "", type=str) or ""
        if sheet and sheet != getattr(self._table, "sheet", ""):
            index = self._sheet_combo.findText(sheet)
            if index >= 0:
                self._sheet_combo.setCurrentIndex(index)
        key = self._settings.value("match_last_key", "", type=str) or ""
        content = self._settings.value("match_last_content", "", type=str) or ""
        template = self._settings.value("match_last_template", "", type=str) or ""
        if key and self._key_combo.findText(key) >= 0:
            self._key_combo.setCurrentText(key)
        if content and self._content_combo.findText(content) >= 0:
            self._content_combo.setCurrentText(content)
        if template:
            self._template_edit.setText(template)
        self._last_content = self._content_combo.currentText()
        self._rebuild_records()
        # 只把配置捡回来，不在这里重算/重识别——识别要用户点「开始识别」才算数
        self._say(f"接着上次的表：{os.path.basename(path)}（点「开始识别」才会读图上的编号）")

    def _on_sheet_changed(self, name: str) -> None:
        if not self._table or not name or name == self._table.sheet:
            return
        try:
            table = dataset.read_table(self._table.path, name)
        except dataset.DataError as exc:
            QMessageBox.warning(self, "这张工作表读不了", str(exc))
            return
        self._table = table
        self._fill_columns()
        self._info_label.setText(table.describe())

    def _fill_columns(self) -> None:
        table = self._table
        if table is None:
            return
        guess = dataset.guess_key_column(table)
        self._key_combo.blockSignals(True)
        self._key_combo.clear()
        self._key_combo.addItems(table.headers)
        if guess:
            self._key_combo.setCurrentText(guess)
        self._key_combo.blockSignals(False)

        self._content_combo.blockSignals(True)
        self._content_combo.clear()
        self._content_combo.addItems([name for name in table.headers if name != guess])
        columns = dataset.guess_display_columns(table, guess)
        if columns:
            self._content_combo.setCurrentText(columns[0])
        self._content_combo.blockSignals(False)
        self._last_content = self._content_combo.currentText()

        if columns:
            self._template_edit.setText("{" + columns[0] + "}")
        else:
            self._template_edit.setText("{" + guess + "}")
        self._rebuild_records()

    def _on_key_changed(self, _name: str) -> None:
        self._fill_columns_content_only()

    def _fill_columns_content_only(self) -> None:
        table = self._table
        if table is None:
            return
        key = self._key_combo.currentText()
        self._content_combo.blockSignals(True)
        self._content_combo.clear()
        self._content_combo.addItems([name for name in table.headers if name != key])
        self._content_combo.blockSignals(False)
        self._last_content = self._content_combo.currentText()
        self._rebuild_records()

    def _on_content_changed(self, name: str) -> None:
        """换内容字段时，把模板里跟着它变的那个占位符一起换掉。

        默认模板是 ``{探伤比例}``；换成「备注」还留着 ``{探伤比例}`` 的话，
        写进图里的其实还是老字段，看上去就像没换。只替换上一次的内容字段，
        用户自己写的前后缀（``RT {探伤比例}``）保留。
        """
        template = self._template_edit.text()
        previous = getattr(self, "_last_content", "")
        if name and previous and previous != name:
            old_token = "{" + previous + "}"
            if old_token in template:
                self._template_edit.setText(template.replace(old_token, "{" + name + "}"))
        elif name and not template:
            self._template_edit.setText("{" + name + "}")
        self._last_content = name
        self._rebuild_records()

    def _rebuild_records(self) -> None:
        table = self._table
        key = self._key_combo.currentText()
        if table is None or not key:
            self._records = None
            return
        content = self._content_combo.currentText()
        columns = [content] if content else []
        template = self._template_edit.text().strip()
        self._records = dataset.RecordTable(table, key, columns, template, self._key_rule)
        self._replan_all()

    def _on_rule_changed(self, _index: int = 0) -> None:
        """换了「编号取法」：不用重扫，拿存着的识别结果重算一遍就行。"""
        rule = self._rule_combo.currentData() or dataset.KEY_WHOLE
        if rule == self._key_rule:
            return
        self._key_rule = str(rule)
        self._settings.setValue("match_key_rule", self._key_rule)
        if self._table is not None:
            self._rebuild_records()
            self._summarize()

    # -------------------------------------------------------------- 识别结果

    def _seed_from_project(self) -> None:
        """上次识别过的结果直接捡回来，改字段映射不用重扫两千页。"""
        if not getattr(self._project, "is_open", False):
            return
        for page, row in self._project.load_scan_rows().items():
            self._scans[int(page)] = {
                "page": int(page),
                "raw": row.get("raw", ""),
                "tag": row.get("tag", ""),
                "source": row.get("source", ""),
                "fixed": bool(row.get("fixed")),
                "error": row.get("note", ""),
            }
        if self._scans:
            self._replan_all()
            self._say(
                f"捡到上次的识别结果 {len(self._scans)} 页，换字段会直接重算，不用重扫。"
            )

    def _replan_all(self) -> None:
        if not self._records or not self._scans:
            self._rows = []
            self._fill_table()
            self._summarize()
            return
        scans = [self._scans[page] for page in sorted(self._scans)]
        self._rows = auto.plan_from_scans(scans, self._records, self._empty_text)
        auto.apply_duplicates(self._rows, bool(self._skip_dup))
        self._fill_table()
        self._summarize()

    def _on_skip_dup(self, checked: bool) -> None:
        """「同名只标一次」被拨动：整套计划重算一遍，并记住这个选择。"""
        self._skip_dup = bool(checked)
        self._settings.setValue("match_skip_duplicates", bool(checked))
        if not self._rows:
            return
        skipped = auto.apply_duplicates(self._rows, bool(checked))
        self._fill_table()
        if checked:
            self._summarize(f"同名跳过 {skipped} 页。")
        else:
            self._summarize("同名页面都标。")

    def _on_empty_text(self) -> None:
        """「字段为空写」改了：记住，并把整套计划按新规矩重算一遍。"""
        text = self._empty_edit.text().strip()
        if text == self._empty_text:
            return
        self._empty_text = text
        self._settings.setValue("match_empty_text", text)
        self._replan_all()
        if text:
            self._say(f"内容字段为空的页，改写「{text}」。")
        else:
            self._say("内容字段为空的页不生成。")

    def _on_decor_changed(self) -> None:
        """「字前后加」改了：只影响显示和写出去的字，不用重新查表。"""
        prefix = self._prefix_edit.text()
        suffix = self._suffix_edit.text()
        if prefix == self._prefix and suffix == self._suffix:
            return
        self._prefix = prefix
        self._suffix = suffix
        self._settings.setValue("match_prefix", prefix)
        self._settings.setValue("match_suffix", suffix)
        for row in self._rows:
            self._update_row(row)
        self._summarize()

    def _on_plain_placeholder(self, checked: bool) -> None:
        """「默认字不加前后缀」：只改显示和落笔，不重新查表。"""
        self._decorate_placeholder = not bool(checked)
        self._settings.setValue("match_plain_placeholder", bool(checked))
        for row in self._rows:
            self._update_row(row)
        self._summarize()

    def _fill_table(self) -> None:
        self._filling = True
        self._table_widget.setRowCount(0)
        self._by_page = {}
        self._row_of = {}
        for row in self._rows:
            self._append_row(row)
        self._filling = False

    def _append_plan_row(self, row: "auto.PlanRow") -> None:
        self._rows.append(row)
        self._filling = True
        self._append_row(row)
        self._filling = False

    def _text_of(self, row: "auto.PlanRow") -> str:
        """这一行最后会写到图上的字（含前后加）。"""
        return auto.final_text(row, self._prefix, self._suffix, self._decorate_placeholder)

    def _append_row(self, row: "auto.PlanRow") -> None:
        index = self._table_widget.rowCount()
        self._table_widget.insertRow(index)
        self._by_page[row.page] = row
        self._row_of[row.page] = index
        use_item = QTableWidgetItem("")
        use_item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        use_item.setCheckState(Qt.Checked if row.use else Qt.Unchecked)
        use_item.setData(Qt.UserRole, row.page)
        self._table_widget.setItem(index, COL_USE, use_item)
        for column, text in (
            (COL_PAGE, str(row.page + 1)),
            (COL_TAG, row.tag or "—"),
            (COL_STATUS, row.label),
            (COL_RECORD, _record_text(row)),
            (COL_CONTENT, self._text_of(row)),
            (COL_NOTE, row.note),
        ):
            item = QTableWidgetItem(text)
            item.setData(Qt.UserRole, row.page)
            if column in (COL_RECORD, COL_CONTENT):
                item.setToolTip(text)
            if column not in (COL_TAG, COL_NOTE, COL_CONTENT):
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            if column == COL_STATUS:
                item.setForeground(QColor(_STATUS_COLOR.get(row.status, theme.TEXT)))
            self._table_widget.setItem(index, column, item)

    def _update_row(self, row: "auto.PlanRow") -> None:
        index = self._row_of.get(row.page)
        if index is None:
            return
        self._filling = True
        for column, text in (
            (COL_TAG, row.tag or "—"),
            (COL_STATUS, row.label),
            (COL_RECORD, _record_text(row)),
            (COL_CONTENT, self._text_of(row)),
            (COL_NOTE, row.note),
        ):
            item = self._table_widget.item(index, column)
            if item is None:
                continue
            item.setText(text)
            if column == COL_STATUS:
                item.setForeground(QColor(_STATUS_COLOR.get(row.status, theme.TEXT)))
        use_item = self._table_widget.item(index, COL_USE)
        if use_item is not None:
            use_item.setCheckState(Qt.Checked if row.use else Qt.Unchecked)
        self._filling = False
        self._summarize()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._filling:
            return
        page = item.data(Qt.UserRole)
        if page is None:
            return
        row = self._by_page.get(int(page))
        if row is None:
            return
        if item.column() == COL_USE:
            row.use = item.checkState() == Qt.Checked
            self._summarize()
        elif item.column() == COL_TAG:
            row.tag = item.text().strip()
            self._replan_row(row)
        elif item.column() == COL_CONTENT:
            self._edit_content(row, item.text())

    def _edit_content(self, row: "auto.PlanRow", text: str) -> None:
        """双击「要写的字」改过：这是人自己定的字，生成前要单独提醒一次。"""
        row.content = text
        row.edited = True
        row.placeholder = False
        if row.record:
            row.status = auto.ST_MATCHED
        elif text.strip():
            row.status = auto.ST_MANUAL
            row.manual = True
        else:
            row.status = auto.ST_UNMATCHED if row.tag else auto.ST_EMPTY
            row.manual = False
        row.note = auto._join(row.note, "内容是人手改的")
        self._update_row(row)

    def _on_double_click(self, item: QTableWidgetItem) -> None:
        if item.column() not in (COL_STATUS, COL_RECORD):
            return
        page = item.data(Qt.UserRole)
        if page is None:
            return
        row = self._by_page.get(int(page))
        # 「表里的记录」那格双击：有候选就先把候选摆出来，再决定去不去搜整张表
        if item.column() == COL_RECORD and row is not None and row.candidates:
            self._pick_candidate(int(page))
            return
        self._choose_record(int(page))

    def _pick_candidate(self, page: int) -> None:
        row = self._by_page.get(page)
        if row is None or not self._records or not row.candidates:
            return
        dialog = CandidatePickerDialog(
            self._records, row.candidates, row.lookup_key or row.tag, self
        )
        if dialog.exec() != QDialog.Accepted or not dialog.picked or dialog.record is None:
            return
        auto.apply_choice(row, self._records, dialog.record)
        row.candidates = []
        row.note = auto._join(row.note, "从候选里选的")
        self._update_row(row)

    def _replan_row(self, row: "auto.PlanRow") -> None:
        if not self._records:
            return
        match = self._records.lookup(row.tag)
        row.candidates = match.candidates
        row.lookup_key = match.key
        row.manual = False
        row.edited = False
        row.placeholder = False
        row.record = {}
        row.content = ""
        via_note = (
            f"按中间两段匹配（{match.key}）"
            if match.via == dataset.KEY_MIDDLE and match.key
            else ""
        )
        if not row.tag:
            row.status = auto.ST_EMPTY
            row.note = ""
        elif match.status == dataset.CONFLICT:
            row.status = auto.ST_CONFLICT
            row.note = auto._join(via_note, f"表里有 {len(match.rows)} 条同名记录，必须选一条")
        elif match.status == dataset.MATCHED:
            row.record = match.rows[0]
            row.content, state = self._records.content_for(row.record)
            if state == dataset.NOVALUE and self._empty_text:
                row.content = self._empty_text
                row.placeholder = True
                row.status = auto.ST_MATCHED
                row.note = auto._join(via_note, f"表格里字段为空，写了「{self._empty_text}」")
            else:
                row.status = auto.ST_MATCHED if state == dataset.MATCHED else auto.ST_NOVALUE
                row.note = auto._join(
                    via_note, "OCR 改过字，但命中了表记录" if row.fixed else ""
                )
        else:
            row.status = auto.ST_UNMATCHED
            row.note = auto._join(via_note, "表里没有这个编号（不自动代填）")
        self._scans[row.page] = {
            "page": row.page,
            "raw": row.raw,
            "tag": row.tag,
            "source": row.source,
            "fixed": row.fixed,
            "error": row.note if row.status == auto.ST_EMPTY else "",
        }
        self._update_row(row)

    # -------------------------------------------------------------- 区域

    def refresh_regions(self) -> None:
        self._scan_label.setText(_region_text(getattr(self._viewer, "scan_region", None)))
        self._annot_label.setText(_region_text(getattr(self._viewer, "annot_region", None)))

    def _on_bookmark_toggled(self, checked: bool) -> None:
        """勾上「用目录书签」就不需要识别区域了，把那个按钮灰掉。"""
        self._scan_button.setEnabled(not checked)
        self._scan_label.setEnabled(not checked)
        if checked:
            self._say("用目录书签：点「开始识别」会读 PDF 自带的目录，按标题里的编号查表。")
        else:
            self._say("改回用识别区域。")

    def _scan_from_bookmarks(self, pages: list[int]) -> None:
        """不读图，直接拿 PDF 目录里的标题当编号去查表。"""
        try:
            rows = tocmodel.outline_rows(self._path)
        except Exception:  # noqa: BLE001 - 目录读不了就当没有
            rows = []
        if not rows:
            QMessageBox.information(
                self,
                "这份 PDF 没有目录",
                "PDF 自带目录是空的。可以先在「目录树」里导入/识别出目录，"
                "或者取消勾选、改用识别区域。",
            )
            return
        keep = set(pages)
        picked = [row for row in rows if int(row["page"]) in keep]
        if not picked:
            self._say("这个页码范围里没有目录项，换个范围再试。")
            return
        self._scans.clear()
        self._rows = []
        self._fill_table()
        for item in picked:
            self._scans[int(item["page"])] = {
                "page": int(item["page"]),
                "raw": item["raw"],
                "tag": item["tag"],
                "source": "bookmark",
                "fixed": False,
                "error": "",
            }
        scans = [self._scans[page] for page in sorted(self._scans)]
        self._rows = auto.plan_from_scans(scans, self._records, self._empty_text)
        if self._skip_dup:
            auto.apply_duplicates(self._rows, True)
        self._fill_table()
        self._summarize()
        self._say(f"按目录书签读了 {len(scans)} 页（不识别图上的字）。")
        self._progress.setRange(0, len(scans))
        self._progress.setValue(len(scans))

    # -------------------------------------------------------------- 识别

    def start_scan(self) -> None:
        if self._worker is not None:
            return
        if not self._records:
            QMessageBox.information(
                self, "还没导入数据表", "先选一个 CSV / Excel，并确认匹配字段。"
            )
            return
        start = self._start_spin.value() - 1
        end = self._end_spin.value() - 1
        if end < start:
            start, end = end, start
        pages = list(range(start, end + 1))

        if getattr(self, "_bookmark_check", None) is not None and self._bookmark_check.isChecked():
            self._scan_from_bookmarks(pages)
            return

        region = getattr(self._viewer, "scan_region", None)
        if region is None or region.is_empty:
            QMessageBox.information(
                self, "还没框识别区域", "点上面的「框识别区域」，在图上把管线号框起来。"
            )
            return
        worker = MatchWorker(self._path, region, self._records, pages, self)
        worker.progress.connect(self._on_progress)
        worker.rows.connect(self._on_rows)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_finished)
        self._worker = worker
        self._scans.clear()
        self._rows = []
        self._fill_table()
        self._progress.setRange(0, len(pages))
        self._progress.setValue(0)
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._say(f"开始识别 {len(pages)} 页…")
        worker.start()

    def stop_scan(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._say("正在停…已经识别完的页会留下。")

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setMaximum(max(1, total))
        self._progress.setValue(done)
        self._progress.setFormat(f"{done} / {total} 页")

    def _on_rows(self, rows) -> None:
        for row in rows:
            self._scans[row.page] = {
                "page": row.page,
                "raw": row.raw,
                "tag": row.tag,
                "source": row.source,
                "fixed": row.fixed,
                "error": row.note if row.status == auto.ST_EMPTY else "",
            }
            self._append_plan_row(row)
        if self._skip_dup and self._rows:
            auto.apply_duplicates(self._rows, True)
            for row in rows:
                if row.status == auto.ST_DUP:
                    self._update_row(row)
        self._summarize()

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self._start_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        QMessageBox.warning(self, "识别失败", message)
        self._say("识别没跑起来。")

    def _on_finished(self, pages: int, ready: int, seconds: float) -> None:
        self._worker = None
        self._start_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._save_scans()
        self._summarize(done_text=f"识别完 {pages} 页（{seconds:.1f} 秒），{ready} 页可生成。")

    def _save_scans(self) -> None:
        if not self._scans or not getattr(self._project, "is_open", False):
            return
        rows = []
        for page in sorted(self._scans):
            scan = self._scans[page]
            rows.append(
                {
                    "page": page,
                    "raw": scan.get("raw", ""),
                    "tag": scan.get("tag", ""),
                    "source": scan.get("source", ""),
                    "fixed": bool(scan.get("fixed")),
                    "confirmed": True,
                    "note": scan.get("error", ""),
                }
            )
        self._project.save_scan_rows(rows)

    # -------------------------------------------------------------- 统计

    def _summarize(self, done_text: str = "") -> None:
        counts = auto.summarize(self._rows)
        parts = [f"共 {counts['total']} 页"]
        if counts[auto.ST_MATCHED]:
            parts.append(f"可生成 {counts[auto.ST_MATCHED]}")
        for status in (
            auto.ST_CONFLICT,
            auto.ST_UNMATCHED,
            auto.ST_NOVALUE,
            auto.ST_EMPTY,
            auto.ST_DUP,
        ):
            if counts.get(status):
                parts.append(f"{auto.LABELS[status]} {counts[status]}")
        text = " · ".join(parts)
        if done_text:
            text = done_text + " " + text
        self._status_label.setText(text)

    def _say(self, text: str) -> None:
        self._status_label.setText(text)

    # -------------------------------------------------------------- 选择记录

    def _choose_record(self, page: int) -> None:
        row = self._by_page.get(page)
        if row is None or not self._records:
            return
        dialog = RecordPickerDialog(self._records, row.record or None, row.lookup_key or row.tag, self)
        if dialog.exec() != QDialog.Accepted or not dialog.picked:
            return
        if dialog.record is None:
            auto.apply_choice(row, self._records, None)
            row.use = False
        else:
            auto.apply_choice(row, self._records, dialog.record)
        self._update_row(row)

    def _set_use(self, page: int, value: bool) -> None:
        row = self._by_page.get(page)
        if row is None:
            return
        if value and row.status == auto.ST_DUP:
            # 「同名跳过」的那行被重新勾上：按原状态恢复
            auto.unskip_duplicate(row)
            self._update_row(row)
            return
        row.use = value
        if value and row.status == auto.ST_SKIP:
            if row.edited:
                # 人工改过的字不能因为重新打钩就丢
                row.status = auto.ST_MATCHED if row.record else auto.ST_MANUAL
                row.note = auto._join(row.note, "重新勾上（保留人工改的字）")
                self._update_row(row)
                return
            self._replan_row(row)
            return
        if not value:
            row.status = auto.ST_SKIP
            row.note = "手动跳过"
        self._update_row(row)

    def _on_menu(self, pos) -> None:
        item = self._table_widget.itemAt(pos)
        if item is None:
            return
        page = item.data(Qt.UserRole)
        if page is None:
            return
        page = int(page)
        menu = QMenu(self)
        row = self._by_page.get(page)
        menu.addAction("选择记录…", lambda _checked=False: self._choose_record(page))
        if row is not None and row.candidates:
            menu.addAction(
                f"从候选里选（{len(row.candidates)} 条）…",
                lambda _checked=False: self._pick_candidate(page),
            )
        menu.addAction(
            "用这个编号重新匹配",
            lambda _checked=False: self._retag(page),
        )
        menu.addAction(
            "这一页不生成（不打钩）", lambda _checked=False: self._set_use(page, False)
        )
        menu.addSeparator()
        menu.addAction(
            "重新按表匹配这一页", lambda _checked=False: self._set_use(page, True)
        )
        menu.exec(self._table_widget.viewport().mapToGlobal(pos))

    def _retag(self, page: int) -> None:
        """编号改过之后按改过的再查一遍（也用于「拿错的编号重搜」）。"""
        row = self._by_page.get(page)
        if row is None:
            return
        self._replan_row(row)
        self._say(f"第 {page + 1} 页按「{row.tag or '空'}」重新匹配过了。")

    # -------------------------------------------------------------- 生成

    def _style(self) -> AnnotationStyle:
        align = _ALIGN_ITEMS[max(0, self._align_combo.currentIndex())][1]
        return AnnotationStyle(font_size=float(self._size_spin.value()), align=align)

    def _measure(self, text: str, size: float) -> float:
        """量这句话按这么大多宽（pt）：字太长时按标注区域的宽度缩字号。"""
        style = self._style()
        font = QFont(style.font_family)
        font.setPointSizeF(max(1.0, float(size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        return QFontMetricsF(font).horizontalAdvance(text)

    def _data_imports(self) -> list[dict]:
        """项目里的导入版本；旧项目/测试替身没有列表接口时退回「最后一次」。"""
        getter = getattr(self._project, "data_imports", None)
        if getter is None:
            last = self._project.last_data_import()
            return [last] if last else []
        try:
            return list(getter() or [])
        except Exception:  # noqa: BLE001
            return []

    def _batch_signature(self) -> tuple[str, str]:
        """判断「是不是同一批标注」只看匹配字段和模板。

        同一张表改了数据、重新导入，只是导入版本变了，模板没变，仍算同一批，
        应该原地更新；换了内容字段/模板才算另一批，要保留旧的那一批。
        """
        return (
            self._key_combo.currentText().strip(),
            self._template_edit.text().strip(),
        )

    def _same_batch(self, item: dict) -> bool:
        signature = (
            (item.get("key_column") or "").strip(),
            (item.get("template") or "").strip(),
        )
        return signature == self._batch_signature()

    def _existing(self) -> dict[int, dict]:
        """当前这批已经生成过的数据标注：``{页号: {annotation_id, content, tag}}``。

        只认当前「匹配字段 + 模板」的导入版本；换了内容字段/模板生成的另一批，
        不参与这里的更新和删除，这样两批标注可以同时留在同一页上。
        """
        out: dict[int, dict] = {}
        store = getattr(self._viewer, "store", None)
        if store is None or not getattr(self._project, "is_open", False):
            return out
        versions = self._data_imports()
        imports = {int(item.get("id", 0) or 0): item for item in versions}
        legacy = not versions
        for prov in self._project.load_match_annotations():
            import_id = int(prov.get("import_id") or 0)
            if not legacy:
                if not import_id:
                    # 有版本表但这条没记版本号：信息不足，保留不猜着改。
                    continue
                version = imports.get(import_id)
                if version is None or not self._same_batch(version):
                    continue
            ann = store.get(prov.get("annotation_id", ""))
            if ann is None:
                continue
            out[int(prov.get("page", 0))] = {
                "annotation_id": ann.id,
                "content": ann.content,
                "tag": prov.get("tag", ""),
            }
        return out

    def _ensure_import_version(self) -> int:
        """字段映射变了就补记一个导入版本，追的时候知道用的是哪一版表。"""
        if self._table is None or not getattr(self._project, "is_open", False):
            return 0
        key_column = self._key_combo.currentText()
        template = self._template_edit.text().strip()
        for item in self._data_imports():
            if (
                item.get("fingerprint") == self._fingerprint
                and item.get("key_column") == key_column
                and item.get("template") == template
            ):
                self._import_id = int(item.get("id", 0) or 0)
                return self._import_id
        content = self._content_combo.currentText()
        self._import_id = self._project.import_dataset(
            self._table.path,
            self._fingerprint,
            self._table.sheet,
            self._table.row_count,
            key_column,
            [content] if content else [],
            template,
            "数据匹配标注",
        )
        return self._import_id

    def _confirm_manual(self, edited: list) -> bool:
        """有手工改过 / 自己填的字：写进图之前让他点一次确认，还能展开看是哪几条。"""
        box = QMessageBox(self)
        box.setWindowTitle("有些字是你自己改的")
        box.setIcon(QMessageBox.Question)
        box.setText(
            f"有 {len(edited)} 条标注的字被你改过（或表里没有、你自己填的）。\n"
            "生成后图上的字就按你改的这些来。"
        )
        box.setInformativeText("点「显示详情」能看是哪几页；确认后才会写进图里。")
        lines = [
            f"第 {row.page + 1} 页 · {row.tag or '（无编号）'} · {text}"
            for row, text in edited[:80]
        ]
        if len(edited) > 80:
            lines.append(f"…… 还有 {len(edited) - 80} 条")
        box.setDetailedText("\n".join(lines))
        ok = box.addButton("确认生成", QMessageBox.AcceptRole)
        box.addButton("再看看", QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is ok

    def preview_changes(self) -> None:
        if not self._records:
            QMessageBox.information(self, "还没导入数据表", "先选一个 CSV / Excel。")
            return
        plan = [row for row in self._rows if row.ready]
        changes = auto.diff(
            self._existing(), plan, self._prefix, self._suffix, self._decorate_placeholder
        )
        if not changes:
            self._say("和项目里现有的数据标注一致，没有要变的地方。")
            return
        dialog = ChangePreviewDialog(changes, self)
        if dialog.exec() == QDialog.Accepted:
            self.generate()

    def generate(self) -> None:
        """生成标注：出错不许把窗口弄没，要留日志 + 弹出来说清楚。"""
        try:
            self._generate()
        except Exception as exc:  # noqa: BLE001 - 这里就是最后一道网
            from core import crashlog

            path = crashlog.log_exception(exc, "数据匹配标注：生成标注")
            QMessageBox.critical(
                self,
                "生成标注出错了",
                "这次没写进去，窗口和已有标注都还在，可以再来一次。\n\n"
                f"错误：{type(exc).__name__}: {exc}\n"
                + (f"\n已记到日志：{path}" if path else ""),
            )

    def _text_box(self, ann) -> tuple[float, float, float, float] | None:
        """文字标注大致的页面矩形，用来判断两批字会不会叠在一起。"""
        if ann.kind != "text" or not ann.content:
            return None
        style = ann.style
        font = QFont(style.font_family)
        font.setPointSizeF(max(1.0, float(style.font_size)))
        font.setBold(bool(style.bold))
        font.setItalic(bool(style.italic))
        metrics = QFontMetricsF(font)
        lines = ann.content.split("\n")
        width = max((metrics.horizontalAdvance(line) for line in lines), default=0.0)
        height = metrics.lineSpacing() * len(lines)
        if style.align == ALIGN_CENTER:
            left = ann.x - width / 2.0
        elif style.align == ALIGN_RIGHT:
            left = ann.x - width
        else:
            left = ann.x
        return left, ann.y, left + width, ann.y + height

    def _place_new_annotation(self, ann, others: list):
        """新一批的字落下来时，避开已经存在的字，往下错开而不是盖住。"""
        box = self._text_box(ann)
        if box is None:
            return ann
        step = max(4.0, float(ann.style.font_size) * 1.35)
        for _ in range(len(others) + 2):
            x0, y0, x1, y1 = box
            hit_bottoms = []
            for other in others:
                other_box = self._text_box(other)
                if other_box is None or getattr(other, "hidden", False):
                    continue
                ox0, oy0, ox1, oy1 = other_box
                if x0 < ox1 and ox0 < x1 and y0 < oy1 and oy0 < y1:
                    hit_bottoms.append(oy1)
            if not hit_bottoms:
                break
            delta = max(hit_bottoms) + step - y0
            if delta <= 0.1:
                break
            ann = ann.moved_to(ann.x, ann.y + delta)
            box = (x0, y0 + delta, x1, y1 + delta)
        return ann

    def _generate(self) -> None:
        if not self._records:
            QMessageBox.information(
                self, "还没导入数据表", "先选一个 CSV / Excel 并选好字段。"
            )
            return
        region = getattr(self._viewer, "annot_region", None)
        if region is None or region.is_empty:
            QMessageBox.information(
                self, "还没框标注位置", "点「框标注位置」，在图上框一块要写字的区域。"
            )
            return
        plan = [row for row in self._rows if row.ready]
        if not plan:
            self._say("没有可生成的行：未匹配 / 冲突 / 字段为空的先处理掉。")
            return
        edited = auto.manual_rows(plan, self._prefix, self._suffix, self._decorate_placeholder)
        if edited and not self._confirm_manual(edited):
            return
        made = auto.make_annotations(
            plan,
            self._page_sizes,
            region,
            self._style(),
            float(self._pad_spin.value()),
            self._prefix,
            self._suffix,
            self._decorate_placeholder,
            self._measure,
        )
        if not made:
            self._say("标注位置落到页面外面了，一条也没生成。")
            return

        store = getattr(self._viewer, "store", None)
        existing = self._existing()
        adds: list = []
        updates: list = []
        final: list[tuple] = []
        for row, ann in made:
            old = existing.get(row.page)
            old_ann = store.get(old["annotation_id"]) if (old and store) else None
            old_id = old_ann.id if old_ann is not None else ""
            others = [
                item
                for item in (store.on_page(row.page) if store else [])
                if item.id != old_id and item.kind == ann.kind and not item.hidden
            ]
            if old_ann is None:
                placed = self._place_new_annotation(ann, others)
                adds.append(placed)
                final.append((row, placed))
                others.append(placed)
                continue
            new = old_ann.with_content(ann.content)
            if not others:
                # 只有一批时保持原来的行为：区域/边距改了，字跟着落到新位置。
                new = new.moved_to(ann.x, ann.y)
            updates.append((old_ann, new))
            final.append((row, new))

        wanted = {row.page for row, _ann in final}
        removes = []
        removed_ids: list[str] = []
        for page, old in existing.items():
            if page in wanted:
                continue
            old_ann = store.get(old["annotation_id"]) if store else None
            if old_ann is not None:
                removes.append(old_ann.id)
                removed_ids.append(old_ann.id)

        count = self._viewer.apply_batch(adds, removes, updates, f"数据标注 {len(final)} 条")
        if not count:
            self._say("画布没接上标注库，什么也没写。")
            return

        import_id = self._ensure_import_version()
        self._project.save_match_annotations(auto.provenance(final), import_id)
        if removed_ids:
            self._project.delete_match_annotations(removed_ids)
        self._say(
            "已生成 {} 条标注（新增 {} · 更新 {} · 移除 {}），追溯记录已写进 .qpdf。"
            "按 Ctrl+Z 可以整批撤回。".format(len(final), len(adds), len(updates), len(removes))
        )

    # -------------------------------------------------------------- 收尾

    def _show_import_info(self) -> None:
        if not getattr(self._project, "is_open", False):
            return
        last = self._project.last_data_import()
        if last:
            self._info_label.setText(
                "上次导入：{}（{} 行）".format(
                    os.path.basename(last.get("path", "")) or "（未记路径）",
                    last.get("row_count", 0),
                )
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self._save_scans()
        super().closeEvent(event)

"""F01：左侧「目录树」面板。

树上三层：前缀分组 → 编号（挂 N 页）→ 具体页面。

交互按用户实测反馈调过：

- **单击就跳**：点页面跳那一页；点编号直接跳到它的第一页（顺手展开），
  不再要求双击；
- 搜索加了 180 ms 防抖——每敲一个字就重建整棵树，两千多个编号时会明显卡；
- 多了「排序」和「只看待校对」，因为编号动辄两千多个，得能收窄；
- 底部三个按钮：识别编号 / 导入目录书签 / 清空目录树（「写进书签」搬到导出对话框里了）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.pipe_index import UNSORTED, PipeIndex
from ui import theme

_ROLE_PAGE = Qt.ItemDataRole.UserRole + 1
_ROLE_TAG = Qt.ItemDataRole.UserRole + 2

#: 搜索防抖：敲字时不要每个字符都重建一次树
_SEARCH_DELAY_MS = 180


class PipeTreePanel(QWidget):
    """编号目录树。数据来自项目里的识别结果（``scan_results``）。"""

    page_selected = Signal(int)       # 页号（0 起）
    scan_requested = Signal()         # 「识别编号」
    import_requested = Signal()       # 「导入目录书签」（读原 PDF 的目录）
    merge_requested = Signal()        # 「跟 PDF 书签比对…」（识别结果 ↔ 书签，勾选后合并）
    clear_requested = Signal()        # 「清空目录树」
    tag_renamed = Signal(str, str)    # (旧编号, 新编号)——改完要写回项目文件
    export_requested = Signal(str)    # F06：按这个编号导出页面

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._index = PipeIndex()
        self._keyword = ""
        self._sort = PipeIndex.MODE_PAGE
        self._only_pending = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(_SEARCH_DELAY_MS)
        self._search_timer.timeout.connect(self._apply_search)
        self._build()
        self.refresh()

    # -- 搭界面 -------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("panelHeader")
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(10, 6, 10, 6)
        title = QLabel("目录树", header)
        title.setObjectName("panelTitle")
        self._count = QLabel("—", header)
        self._count.setObjectName("panelHint")
        head_row.addWidget(title)
        head_row.addStretch(1)
        head_row.addWidget(self._count)
        outer.addWidget(header)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("搜索编号…")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        wrap = QWidget(self)
        wrap_row = QHBoxLayout(wrap)
        wrap_row.setContentsMargins(8, 6, 8, 4)
        wrap_row.addWidget(self._search)
        outer.addWidget(wrap)

        # 排序 + 只看待校对：编号两千多个，不给收窄手段没法用
        tools = QWidget(self)
        tool_row = QHBoxLayout(tools)
        tool_row.setContentsMargins(8, 0, 8, 6)
        tool_row.setSpacing(6)
        self._sort_combo = QComboBox(tools)
        self._sort_combo.setToolTip(
            "按页码：谁在前面的页谁在前\n"
            "按数字：先按口径（50/100/150）分组，组内按页码\n"
            "按字母：先按介质（CIS/CWR）分组，组内按后面的字母、数字"
        )
        self._sort_combo.addItem("按页码", PipeIndex.MODE_PAGE)
        self._sort_combo.addItem("按数字", PipeIndex.MODE_NUMBER)
        self._sort_combo.addItem("按字母", PipeIndex.MODE_MEDIUM)
        self._sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        self._pending_box = QCheckBox("只看待校对", tools)
        self._pending_box.setToolTip("只留下没识别出内容、有疑似或未确认采用的")
        self._pending_box.toggled.connect(self._on_pending_toggled)
        tool_row.addWidget(self._sort_combo, 1)
        tool_row.addWidget(self._pending_box)
        outer.addWidget(tools)

        self._tree = QTreeWidget(self)
        self._tree.setObjectName("pipeTree")
        self._tree.setHeaderHidden(True)
        self._tree.setColumnCount(2)
        head = self._tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self._tree.setColumnWidth(1, 78)
        self._tree.setRootIsDecorated(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        # 单击就干活（itemClicked），双击只剩展开/收起
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemActivated.connect(self._on_activated)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_menu)
        outer.addWidget(self._tree, 1)

        buttons = QWidget(self)
        btn_row = QHBoxLayout(buttons)
        btn_row.setContentsMargins(8, 6, 8, 8)
        btn_row.setSpacing(6)
        self._scan_btn = QPushButton("识别编号…", buttons)
        self._scan_btn.setToolTip("框选区域并批量识别编号（打开识别窗口）")
        self._scan_btn.clicked.connect(self.scan_requested.emit)
        self._import_btn = QPushButton("导入目录书签", buttons)
        self._import_btn.setToolTip("把这份 PDF 自带的书签目录读进来（原来就有的目录不重复加）")
        self._import_btn.clicked.connect(self.import_requested.emit)
        self._clear_btn = QPushButton("清空", buttons)
        self._clear_btn.setToolTip("清空目录树里的识别结果（PDF 和标注都不动）")
        self._clear_btn.setMaximumWidth(56)
        self._clear_btn.clicked.connect(self.clear_requested.emit)
        btn_row.addWidget(self._scan_btn, 1)
        btn_row.addWidget(self._import_btn, 1)
        btn_row.addWidget(self._clear_btn)
        outer.addWidget(buttons)

    # -- 数据 ---------------------------------------------------------------
    def set_rows(self, rows: dict[int, dict]) -> None:
        self._index.load(rows)
        self.refresh()

    def index(self) -> PipeIndex:
        return self._index

    def clear(self) -> None:
        self._index.clear()
        self.refresh()

    @property
    def keyword(self) -> str:
        return self._keyword

    @property
    def sort_mode(self) -> str:
        return self._sort

    @property
    def only_pending(self) -> bool:
        return self._only_pending

    # -- 刷新 ---------------------------------------------------------------
    def refresh(self) -> None:
        tree = self._tree
        tree.setUpdatesEnabled(False)
        try:
            tree.clear()
            self._count.setText(
                f"{self._index.tag_count} 个编号 / {self._index.page_count} 页"
                if self._index.page_count
                else "还没识别"
            )

            # 「待校对」单独一块放最上面：没识别的、疑似错的、没确认的都在这里，
            # 它们要不要参与下面的排序由用户自己勾「只看待校对」决定。
            pending = self._index.pending_pages
            if pending and not self._keyword and not self._only_pending:
                node = self._make_group(f"待校对（{len(pending)} 页）", warn=True)
                for page in pending:
                    reason = "、".join(page.suspect) if page.suspect else (
                        "未识别" if not page.tag else "未确认"
                    )
                    self._make_page(node, page.page, page.tag or "（空白）", reason, warn=True)

            groups = self._index.arrange(
                self._keyword, mode=self._sort, only_pending=self._only_pending
            )
            for header, entries in groups:
                # 按页码那一档不分层：编号直接铺在顶层（默认收起着，一眼看一屏）
                container = self._make_group(f"{header}（{len(entries)}）") if header else None
                for entry in entries:
                    parent = container if container is not None else tree
                    tag_item = QTreeWidgetItem(parent)
                    tag_item.setText(0, entry.tag)
                    tag_item.setText(1, self._pages_text(entry))
                    tag_item.setData(0, _ROLE_TAG, entry.tag)
                    first = entry.pages[0] if entry.pages else None
                    if first is not None:
                        tag_item.setData(0, _ROLE_PAGE, first.page)
                    pages = "、".join(str(n) for n in entry.page_numbers)
                    tag_item.setToolTip(
                        0, f"{entry.tag}\n出现在第 {pages} 页（点一下跳到第一页，双击展开）"
                    )
                    if entry.needs_check:
                        tag_item.setForeground(0, QColor(theme.DANGER))
                    font = QFont(tag_item.font(0))
                    font.setBold(False)
                    tag_item.setFont(0, font)
                    for page in entry.pages:
                        extra = page.note or ("、".join(page.suspect) if page.suspect else "")
                        self._make_page(tag_item, page.page, f"第 {page.page_no} 页",
                                        extra, warn=page.needs_check)

            # 默认全部收起：两千多个编号全展开能把人淹了；搜索时才铺开命中项
            if self._keyword:
                tree.expandAll()
            else:
                tree.collapseAll()
            self._clear_btn.setEnabled(bool(self._index.page_count))
        finally:
            tree.setUpdatesEnabled(True)

    @staticmethod
    def _pages_text(entry) -> str:
        """编号右边那列：页码多了就截断成「4、9 +3」，完整页码放 tooltip。"""
        numbers = entry.page_numbers
        if not numbers:
            return ""
        if len(numbers) <= 3:
            return "、".join(str(n) for n in numbers)
        return f"{numbers[0]}、{numbers[1]} +{len(numbers) - 2}"

    def _make_group(self, text: str, warn: bool = False) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self._tree)
        item.setText(0, text)
        font = QFont(item.font(0))
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(0, QColor(theme.DANGER if warn else theme.MUTED))
        item.setExpanded(False)
        return item

    def _make_page(self, parent, page: int, text: str, note: str, warn: bool = False) -> None:
        item = QTreeWidgetItem(parent)
        item.setText(0, text)
        item.setText(1, note)
        item.setData(0, _ROLE_PAGE, page)
        item.setToolTip(0, "点一下跳到这一页")
        if warn:
            item.setForeground(0, QColor(theme.DANGER))
        if note:
            item.setToolTip(1, note)

    # -- 交互 ---------------------------------------------------------------
    def _on_search(self, text: str) -> None:
        self._keyword = text.strip()
        # 敲字时先别重建，停手 180 ms 再来
        self._search_timer.start()

    def _apply_search(self) -> None:
        self.refresh()

    def _on_sort_changed(self, index: int) -> None:
        self._sort = self._sort_combo.itemData(index) or PipeIndex.MODE_PAGE
        self.refresh()

    def _on_pending_toggled(self, checked: bool) -> None:
        self._only_pending = bool(checked)
        self.refresh()

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # 页面节点和编号节点都能点了就跳；编号跳它的第一页并展开
        page = item.data(0, _ROLE_PAGE)
        if isinstance(page, int):
            if item.childCount():
                item.setExpanded(True)
            self.page_selected.emit(page)

    def _on_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.childCount():
            item.setExpanded(not item.isExpanded())

    # -- 右键菜单 -----------------------------------------------------------
    def _on_menu(self, pos) -> None:
        item = self._tree.itemAt(pos)
        if item is None:
            return
        tag = item.data(0, _ROLE_TAG)
        page = item.data(0, _ROLE_PAGE)
        menu = QMenu(self)

        if isinstance(tag, str) and tag:
            act_copy = menu.addAction("复制编号")
            act_rename = menu.addAction("重命名…")
            act_filter = menu.addAction("只看这一组")
            menu.addSeparator()
            act_jump = menu.addAction("跳到第一页")
            act_export = menu.addAction("按编号导出这几页…")
            menu.addSeparator()
            act_merge = menu.addAction("跟 PDF 书签比对…")
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is None:
                return
            if chosen is act_merge:
                self.merge_requested.emit()
                return
            if chosen is act_copy:
                QApplication.clipboard().setText(tag)
            elif chosen is act_rename:
                self._rename_tag(tag)
            elif chosen is act_filter:
                self._search.setText(tag)
            elif chosen is act_jump and isinstance(page, int):
                self.page_selected.emit(page)
            elif chosen is act_export:
                self.export_requested.emit(tag)
            return

        if isinstance(page, int):
            act_jump = menu.addAction("跳到这一页")
            act_copy = menu.addAction("复制页码")
            menu.addSeparator()
            act_merge = menu.addAction("跟 PDF 书签比对…")
            chosen = menu.exec(self._tree.viewport().mapToGlobal(pos))
            if chosen is act_merge:
                self.merge_requested.emit()
            elif chosen is act_jump:
                self.page_selected.emit(page)
            elif chosen is act_copy:
                QApplication.clipboard().setText(str(page + 1))

    def index(self) -> PipeIndex:
        """当前目录树里的索引（按编号导出、外部统计要用）。"""
        return self._index

    def _rename_tag(self, old: str) -> None:
        """改一个编号——只改目录里的数据，PDF 本身不动。"""
        text, ok = QInputDialog.getText(
            self, "重命名编号", f"把「{old}」改成：", text=old
        )
        if not ok:
            return
        new = text.strip()
        if not new or new == old:
            return
        if any(p.tag == new for p in self._index.pages):
            QMessageBox.information(
                self, "重命名编号", f"「{new}」已经存在了，换一个名字吧。"
            )
            return
        self.tag_renamed.emit(old, new)

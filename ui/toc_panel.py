"""左侧「目录」页：PDF 自带的 outline 做成树，点一下就跳页。

这一页只做三件事：读出来、点着走、按关键字筛。提取页面那边复用同一套树（勾着提）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import toc as tocmodel
from ui import theme

_ROLE_PAGE = int(Qt.ItemDataRole.UserRole) + 1
_ROLE_END = int(Qt.ItemDataRole.UserRole) + 2


def _item_int(item, role: int, default: int = -1) -> int:
    """取 item 里存的页号。**不能写 ``data(...) or -1``**——第 0 页会被当成假值。"""
    value = item.data(0, role)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class TocPanel(QWidget):
    """目录树。``page_clicked`` 里给的是 0 起页号。"""

    page_clicked = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._nodes: tuple = ()
        self._page_count = 0
        self._checkable = False
        self._items: list[QTreeWidgetItem] = []

        self._search = QLineEdit()
        self._search.setPlaceholderText("找目录项…（标题里的字）")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._apply_filter)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(False)
        self._tree.setIndentation(14)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.itemClicked.connect(self._on_clicked)
        self._tree.itemActivated.connect(self._on_clicked)

        self._empty = QLabel("这份 PDF 没有自带目录。")
        self._empty.setWordWrap(True)
        self._empty.setTextFormat(Qt.TextFormat.RichText)
        self._empty.setStyleSheet(f"color: {theme.MUTED};")
        self._empty.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._search)
        layout.addWidget(self._tree, 1)
        layout.addWidget(self._empty)

        self._set_empty(True)

    # ------------------------------------------------------------------ 对外

    def clear(self) -> None:
        self._nodes = ()
        self._page_count = 0
        self._items = []
        self._tree.clear()
        self._set_empty(True)

    def set_outline(self, nodes: tuple, page_count: int) -> None:
        """塞进一棵新树（``core.toc.read_toc`` 的结果）。"""
        self._nodes = tuple(nodes or ())
        self._page_count = max(0, int(page_count))
        self._tree.clear()
        self._items = []
        if not self._nodes:
            self._set_empty(True)
            return
        self._set_empty(False)
        ranges = tocmodel.plan_ranges(self._nodes, self._page_count)
        # plan_ranges 和树的先序遍历顺序一致（每条目录项都在里面），按下标对
        span_iter = iter(ranges)
        for node in self._nodes:
            self._add(node, self._tree, span_iter)
        if self._search.text().strip():
            self._apply_filter(self._search.text())

    def entry_count(self) -> int:
        return len(self._items)

    def set_checkable(self, enabled: bool) -> None:
        """勾选模式：每条目录项前面出现复选框（提取页面用）。"""
        self._checkable = bool(enabled)
        for item in self._items:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if self._checkable:
                item.setCheckState(0, Qt.CheckState.Unchecked)
            else:
                item.setData(0, Qt.ItemDataRole.CheckStateRole, None)

    def checked_pages(self) -> list[int]:
        """勾上的目录项各自管的那几页（去重、升序）。"""
        pages: set[int] = set()
        for item in self._items:
            if item.checkState(0) != Qt.CheckState.Checked:
                continue
            start = _item_int(item, _ROLE_PAGE)
            end = _item_int(item, _ROLE_END)
            if start < 0:
                continue
            for page in range(start, max(start, end) + 1):
                if 0 <= page < self._page_count:
                    pages.add(page)
        return sorted(pages)

    # ------------------------------------------------------------------ 内部

    def _add(self, node, parent, span_iter) -> None:
        span = next(span_iter, None)
        item = QTreeWidgetItem(parent)
        item.setText(0, node.title)
        item.setData(0, _ROLE_PAGE, int(node.page))
        item.setData(0, _ROLE_END, int(span.end) if span is not None else int(node.page))
        if node.page >= 0:
            item.setToolTip(0, f"{node.title}（第 {node.page + 1} 页）")
        else:
            item.setToolTip(0, f"{node.title}（没有目标页）")
        self._items.append(item)
        for child in node.children:
            self._add(child, item, span_iter)

    def _set_empty(self, empty: bool) -> None:
        self._empty.setVisible(empty)
        self._tree.setVisible(not empty)
        self._search.setEnabled(not empty)

    def _apply_filter(self, text: str) -> None:
        keyword = (text or "").strip().lower()
        for item in self._items:
            item.setHidden(bool(keyword) and keyword not in item.text(0).lower())
        if not keyword:
            return
        # 命中项要把它的上级露出来，否则树里只剩孤零零一行
        for item in self._items:
            if item.isHidden():
                continue
            parent = item.parent()
            while parent is not None:
                parent.setHidden(False)
                parent.setExpanded(True)
                parent = parent.parent()

    def _on_clicked(self, item, _column=0) -> None:
        page = _item_int(item, _ROLE_PAGE)
        if page >= 0:
            self.page_clicked.emit(page)

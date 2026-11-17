"""F06：按管线号批量导出。

选一个（或几个）编号 → 把这几页提取成一个新 PDF（标注、书签跟着走）。
底层就是 ``core/extractor.py`` 那套（选页 → 复制页 → 写标注 → 落盘 → 写后校验），
这里只负责"挑编号 + 挑存哪"。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from core.pipe_index import PipeIndex
from ui import theme


def safe_filename(tag: str) -> str:
    """管线号直接当文件名会带上 ``/``、``:`` 之类；Windows 上必须换掉。"""
    bad = '<>:"/\\|?*'
    out = "".join("_" if ch in bad else ch for ch in (tag or "").strip())
    return out[:80] or "按编号导出"


class ExportTagDialog(QDialog):
    """挑编号 + 挑存哪。"""

    def __init__(
        self,
        index: PipeIndex,
        default_target: str,
        keep_toc_default: bool = True,
        preselect: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("按编号导出")
        self.setMinimumWidth(620)
        self._index = index
        self._default_target = default_target

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        title = QLabel("按管线号导出页面")
        title.setObjectName("panelTitle")
        outer.addWidget(title)

        hint = QLabel(
            "同一个编号出现在几页上，就把这几页一起提出来（按原页序排）。\n"
            "标注跟着页走；书签会按新页码重排。原始 PDF 不会被改动。"
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        # 选编号
        row = QHBoxLayout()
        row.setSpacing(8)
        self._combo = QComboBox(self)
        self._combo.setEditable(True)
        for tag, pages in self._tags_with_pages():
            self._combo.addItem(f"{tag}（{pages} 页）", tag)
        self._combo.setMinimumWidth(320)
        row.addWidget(QLabel("编号"))
        row.addWidget(self._combo, 1)
        btn_add = QPushButton("加入")
        theme.mark_primary(btn_add)
        btn_add.clicked.connect(self._add_current)
        row.addWidget(btn_add)
        outer.addLayout(row)

        self._chosen = QListWidget(self)
        self._chosen.setMinimumHeight(140)
        outer.addWidget(QLabel("要导出的编号（每个编号的所有页）"))
        outer.addWidget(self._chosen, 1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        btn_remove = QPushButton("移除选中")
        btn_remove.clicked.connect(self._remove_selected)
        row2.addWidget(btn_remove)
        row2.addStretch(1)
        self._summary = QLabel("还没有选编号")
        self._summary.setObjectName("muted")
        row2.addWidget(self._summary)
        outer.addLayout(row2)

        # 目标文件
        path_row = QHBoxLayout()
        path_row.setSpacing(8)
        self._path = QLineEdit(default_target, self)
        path_row.addWidget(QLabel("存到"))
        path_row.addWidget(self._path, 1)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse)
        path_row.addWidget(btn_browse)
        outer.addLayout(path_row)

        self._keep_toc = QCheckBox("保留这几页上的书签（页码会重排）", self)
        self._keep_toc.setChecked(bool(keep_toc_default))
        outer.addWidget(self._keep_toc)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        self._btn_ok = QPushButton("开始导出")
        theme.mark_primary(self._btn_ok)
        self._btn_ok.setEnabled(False)
        self._btn_ok.clicked.connect(self.accept)
        buttons.addWidget(self._btn_ok)
        outer.addLayout(buttons)

        if preselect:
            # 从目录树右键过来的：把那个编号直接放进"已选"。
            # 必须放在按钮都建好之后——_add_current() 会刷新 _btn_ok。
            row = self._combo.findData(preselect)
            if row >= 0:
                self._combo.setCurrentIndex(row)
            self._add_current()

    # ------------------------------------------------------------------ 数据

    def _tags_with_pages(self) -> list[tuple[str, int]]:
        rows: list[tuple[str, int]] = []
        seen: set[str] = set()
        for entry in self._index.entries():
            tag = (entry.tag or "").strip()
            if not tag or tag in seen:
                continue
            seen.add(tag)
            rows.append((tag, len(entry.page_numbers)))
        rows.sort(key=lambda item: item[0])
        return rows

    def _add_current(self) -> None:
        tag = self._combo.currentData() or self._combo.currentText()
        tag = str(tag or "").split("（")[0].strip()
        if not tag:
            return
        for row in range(self._chosen.count()):
            if self._chosen.item(row).data(Qt.ItemDataRole.UserRole) == tag:
                return
        item = QListWidgetItem(f"{tag}（{len(self.pages_for(tag))} 页）")
        item.setData(Qt.ItemDataRole.UserRole, tag)
        self._chosen.addItem(item)
        self._refresh()

    def _remove_selected(self) -> None:
        for item in self._chosen.selectedItems():
            self._chosen.takeItem(self._chosen.row(item))
        self._refresh()

    def _refresh(self) -> None:
        pages = self.pages()
        if pages:
            self._summary.setText(f"已选 {self._chosen.count()} 个编号、{len(pages)} 页")
            self._btn_ok.setEnabled(True)
        else:
            self._summary.setText("还没有选编号")
            self._btn_ok.setEnabled(False)

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出到", self._path.text(), "PDF 文件 (*.pdf)"
        )
        if path:
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            self._path.setText(path)

    # ------------------------------------------------------------------ 对外

    def pages_for(self, tag: str) -> list[int]:
        """这个编号对应的 0 起页码（去重、按页序）。"""
        return [int(no) - 1 for no in self._index.pages_for(tag)]

    def tags(self) -> list[str]:
        return [
            str(self._chosen.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(self._chosen.count())
        ]

    def pages(self) -> list[int]:
        """所有选中编号合起来的页（0 起，去重、按原页序）。"""
        merged: set[int] = set()
        for tag in self.tags():
            merged.update(self.pages_for(tag))
        return sorted(merged)

    def path(self) -> str:
        path = self._path.text().strip() or self._default_target
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        return os.path.abspath(path)

    def keep_toc(self) -> bool:
        return self._keep_toc.isChecked()

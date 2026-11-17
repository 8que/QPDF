"""第 11 章：导出对话框（目标位置 + 注释模式选择）。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from core.annotation import KIND_NUMBER
from ui import theme


class ExportDialog(QDialog):
    """问清楚两件事：存到哪里、写成标准注释还是扁平化。"""

    def __init__(
        self,
        source: str,
        target: str,
        page_count: int,
        items: list,
        has_bookmarks: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("导出 PDF")
        self.setMinimumWidth(560)
        self._source = source

        intro = QLabel(
            f"<b>{os.path.basename(source)}</b><br>"
            f"{page_count} 页 ｜ 本次导出 {len(items)} 个标注"
            f"（{sum(1 for i in items if i.kind == KIND_NUMBER)} 个数字、"
            f"{sum(1 for i in items if i.kind != KIND_NUMBER)} 段文字）"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)

        self._path_edit = QLineEdit(target)
        # 光标放末尾，长路径默认显示的是文件名那一头，别一打开就停在盘符上
        self._path_edit.setCursorPosition(len(target))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)

        form = QFormLayout()
        form.addRow("保存到", path_row)

        self._radio_annot = QRadioButton("标准注释（推荐）")
        self._radio_flat = QRadioButton("扁平化")
        self._radio_annot.setChecked(True)
        self._radio_annot.toggled.connect(self._refresh_hint)
        form.addRow("形式", self._radio_annot)
        form.addRow("", self._radio_flat)

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setMinimumHeight(56)

        self._open_folder = QCheckBox("导出完成后打开所在文件夹")
        self._open_folder.setChecked(True)

        self._with_toc = QCheckBox("把目录树写进 PDF 书签（覆盖原文件里的书签）")
        self._with_toc.setChecked(bool(has_bookmarks))
        self._with_toc.setEnabled(bool(has_bookmarks))
        if not has_bookmarks:
            self._with_toc.setText("把目录树写进 PDF 书签（目录树现在是空的）")

        note = QLabel(
            "原始 PDF 不会被改动——导出写的是新文件，写完之后会重新打开检查页数和注释，"
            "检查通过才算导出成功。"
        )
        note.setWordWrap(True)

        buttons = QDialogButtonBox()
        self._ok = buttons.addButton("导出", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        theme.mark_primary(self._ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addWidget(self._with_toc)
        layout.addWidget(self._open_folder)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._refresh_hint()

    # -- 对外 ---------------------------------------------------------

    def path(self) -> str:
        return self._path_edit.text().strip()

    def flatten(self) -> bool:
        return self._radio_flat.isChecked()

    def open_folder(self) -> bool:
        return self._open_folder.isChecked()

    def with_toc(self) -> bool:
        return self._with_toc.isChecked() and self._with_toc.isEnabled()

    def accept(self) -> None:
        path = self.path()
        if not path:
            self._hint.setText("<b>请先选择保存位置。</b>")
            return
        if os.path.abspath(path) == os.path.abspath(self._source):
            self._hint.setText("<b>不能覆盖原始 PDF，请换一个文件名。</b>")
            return
        super().accept()

    # -- 内部 ---------------------------------------------------------

    def _refresh_hint(self) -> None:
        if self._radio_flat.isChecked():
            self._hint.setText(
                "扁平化：把标注画进页面内容，所有阅读器都能显示，但<b>之后不能再单独编辑</b>。"
                "适合打印、归档、发给别人看。"
            )
        else:
            self._hint.setText(
                "标准注释：文字和数字都写成 FreeText 注释，<b>别的软件还能继续编辑</b>，"
                "删除标注也不影响底图。适合还要修改的图纸。"
            )

    def _browse(self) -> None:
        start = self._path_edit.text() or ""
        start_dir = os.path.dirname(start) if start else ""
        chosen, _ = QFileDialog.getSaveFileName(
            self, "导出到", start, "PDF 文件 (*.pdf)"
        )
        if chosen:
            if not chosen.lower().endswith(".pdf"):
                chosen += ".pdf"
            self._path_edit.setText(chosen)

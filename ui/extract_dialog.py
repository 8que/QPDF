"""F04：提取页面对话框（挑哪几页 / 存到哪 / 带不带标注和书签）。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from core.workbench import parse_page_spec
from ui import theme
from ui.toc_panel import TocPanel


def _spec_text(pages: list[int]) -> str:
    """把 0 起页码压成 ``4,7,9-15`` 这种写法。"""
    ordered = sorted({int(page) for page in pages})
    if not ordered:
        return ""
    parts: list[str] = []
    start = prev = ordered[0]
    for page in ordered[1:]:
        if page == prev + 1:
            prev = page
            continue
        parts.append(_range_text(start, prev))
        start = prev = page
    parts.append(_range_text(start, prev))
    return ",".join(parts)


def _range_text(start: int, end: int) -> str:
    first, last = start + 1, end + 1
    return str(first) if first == last else f"{first}-{last}"


class ExtractDialog(QDialog):
    """问清楚：哪几页、要不要按顺序、存到哪、带不带标注和书签。"""

    def __init__(
        self,
        source: str,
        target: str,
        page_count: int,
        current_page: int = 0,
        annot_count: int = 0,
        bookmark_count: int = 0,
        outline=(),
        project_open: bool = False,
        page_numbers_enabled: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("提取页面")
        self.setMinimumWidth(560)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self._source = source
        self._page_count = max(1, int(page_count))

        intro = QLabel(
            f"<b>{os.path.basename(source)}</b><br>"
            f"共 {self._page_count} 页 ｜ 标注 {annot_count} 条 ｜ 书签 {bookmark_count} 条"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)

        self._pages_edit = QLineEdit(str(current_page + 1))
        self._pages_edit.setPlaceholderText("例如 4,7,9-15,22")
        self._pages_edit.textChanged.connect(self._refresh_hint)

        self._order = QComboBox()
        self._order.addItem("按输入顺序（可倒序、可重复挑）", "input")
        self._order.addItem("从小到大重排", "sorted")

        self._path_edit = QLineEdit(target)
        self._path_edit.setCursorPosition(len(target))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self._path_edit, 1)
        path_row.addWidget(browse)

        self._keep_annot = QCheckBox("把这几页上的标注一起带过去")
        self._keep_annot.setChecked(True)
        self._keep_toc = QCheckBox("书签一起带过去（页码自动改成新位置）")
        self._keep_toc.setChecked(bookmark_count > 0)
        self._keep_toc.setEnabled(bookmark_count > 0)
        if bookmark_count <= 0:
            self._keep_toc.setToolTip("这份图纸还没有书签")

        self._keep_page_number = QCheckBox("页码一起带过去（保留原图页码）")
        self._keep_page_number.setChecked(bool(page_numbers_enabled))
        self._keep_page_number.setEnabled(bool(page_numbers_enabled))
        if not page_numbers_enabled:
            self._keep_page_number.setToolTip("先在工具菜单里设置页码")

        self._hint = QLabel()
        self._hint.setWordWrap(True)
        self._hint.setMinimumHeight(44)
        self._hint.setTextFormat(Qt.TextFormat.RichText)

        # 按目录挑：图纸自带目录时，可以直接勾章节，不用自己算页码
        self._toc = TocPanel(self)
        self._toc.setFixedHeight(180)
        self._toc.set_outline(tuple(outline or ()), self._page_count)
        self._toc.set_checkable(True)
        self._toc_btn = QPushButton("把勾选的章节加进页码")
        self._toc_btn.clicked.connect(self._add_checked_toc)
        self._toc_box = QGroupBox("按目录挑（可选，勾章节比记页码省事）")
        toc_layout = QVBoxLayout(self._toc_box)
        toc_layout.setContentsMargins(10, 8, 10, 8)
        toc_layout.setSpacing(8)
        toc_layout.addWidget(self._toc)
        toc_layout.addWidget(self._toc_btn)
        self._toc_box.setVisible(self._toc.entry_count() > 0)

        # 项目子集：把这几页的标注/识别结果单独存一份 .qpdf
        self._with_qpdf = QCheckBox("同时存一份「只含这几页标注」的项目文件（.qpdf）")
        self._with_qpdf.setChecked(bool(project_open))
        self._with_qpdf.setEnabled(bool(project_open))
        if not project_open:
            self._with_qpdf.setToolTip("还没有项目文件：先标几条，再提取时就能带出去")
        else:
            self._with_qpdf.setToolTip("放在和提取出来的 PDF 同一个文件夹，同名不同后缀")

        self._open_folder = QCheckBox("提取完成后打开所在文件夹")
        self._open_folder.setChecked(True)

        note = QLabel(
            "原始 PDF 不会被改动——提取写的是新文件，写完之后会重新打开核对页数、注释和书签，"
            "检查通过才落到目标位置。"
        )
        note.setWordWrap(True)

        form = QFormLayout()
        form.addRow("提取哪几页", self._pages_edit)
        form.addRow("顺序", self._order)
        form.addRow("保存到", path_row)

        buttons = QDialogButtonBox()
        self._ok = buttons.addButton("提取", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        theme.mark_primary(self._ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self._toc_box)
        layout.addWidget(self._hint)
        layout.addWidget(self._keep_annot)
        layout.addWidget(self._keep_page_number)
        layout.addWidget(self._keep_toc)
        layout.addWidget(self._with_qpdf)
        layout.addWidget(self._open_folder)
        layout.addWidget(note)
        layout.addWidget(buttons)

        self._refresh_hint()

    # -- 对外 ---------------------------------------------------------

    def pages(self) -> list[int]:
        """0 起页码，已按选定的顺序排好。"""
        found = parse_page_spec(self._pages_edit.text(), self._page_count)
        if self._order.currentData() == "sorted":
            found = sorted(found)
        return found

    def path(self) -> str:
        return self._path_edit.text().strip()

    def keep_annotations(self) -> bool:
        return self._keep_annot.isChecked()

    def keep_page_numbers(self) -> bool:
        return self._keep_page_number.isChecked() and self._keep_page_number.isEnabled()

    def keep_toc(self) -> bool:
        return self._keep_toc.isChecked()

    def open_folder(self) -> bool:
        return self._open_folder.isChecked()

    def want_qpdf(self) -> bool:
        return self._with_qpdf.isChecked() and self._with_qpdf.isEnabled()

    def qpdf_path(self) -> str:
        """子集项目文件放哪：和提取出来的 PDF 同目录、同名，换后缀。"""
        path = self.path()
        if not path:
            return ""
        return os.path.splitext(path)[0] + ".qpdf"

    def accept(self) -> None:
        if not self.pages():
            self._hint.setText("<b>没看懂要提取哪几页，示例：4,7,9-15</b>")
            return
        path = self.path()
        if not path:
            self._hint.setText("<b>请先选择保存位置。</b>")
            return
        if os.path.abspath(path) == os.path.abspath(self._source):
            self._hint.setText("<b>不能覆盖原始 PDF，请换一个文件名。</b>")
            return
        super().accept()

    # -- 内部 ---------------------------------------------------------

    def _add_checked_toc(self) -> None:
        """把勾选的目录项展开成页码，接在现有输入后面（已有就不重复加）。"""
        pages = self._toc.checked_pages()
        if not pages:
            self._hint.setText("先在目录里勾几个章节，再来加页码。")
            return
        have = set(parse_page_spec(self._pages_edit.text(), self._page_count))
        fresh = [page for page in pages if page not in have]
        if not fresh:
            self._hint.setText("勾选的章节已经在页码里了。")
            return
        text = self._pages_edit.text().strip()
        added = _spec_text(fresh)
        self._pages_edit.setText(f"{text},{added}" if text else added)

    def _refresh_hint(self) -> None:
        pages = self.pages()
        if not pages:
            self._hint.setText("输入要提取的页码，支持 <b>4,7,9-15,22</b> 这种写法。")
            return
        span = self._page_count
        first, last = pages[0] + 1, pages[-1] + 1
        head = f"将提取 <b>{len(pages)} 页</b>"
        if pages == sorted(pages) and pages == list(range(pages[0], pages[0] + len(pages))):
            head += f"（第 {first}–{last} 页，连续）"
        else:
            head += f"（第 {first} … {last} 页，共 {span} 页里挑的）"
        self._hint.setText(head)

    def _browse(self) -> None:
        start = self._path_edit.text() or ""
        start_dir = os.path.dirname(start) if start else ""
        chosen, _ = QFileDialog.getSaveFileName(
            self, "提取到", start_dir, "PDF 文件 (*.pdf)"
        )
        if chosen:
            if not chosen.lower().endswith(".pdf"):
                chosen += ".pdf"
            self._path_edit.setText(chosen)

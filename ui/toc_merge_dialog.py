"""识别结果 ↔ PDF 原书签：比对、勾选、一起写进目录树。

为什么单独一个对话框：批量识别（Ctrl+Shift+O）出来的管线号是 OCR 猜的，
PDF 自带书签是画图的人写的，两边经常对不上——有的是同一页叫法不同，
有的是只识别到、原本没书签，也有原本有书签、这一页没识别出来。
硬合并会把这三种混成一锅，所以让人看一眼、勾一勾再落库。

这个模块只负责"看懂 + 选"，写库在 MainWindow.merge_toc_with_bookmarks()。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyledItemDelegate,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ui import theme

# 四种状态：两边都对得上 / 只有识别 / 只有书签 / 两边都有但叫法不同
BOTH_SAME = "both_same"
BOTH_DIFF = "both_diff"
OCR_ONLY = "ocr_only"
OUTLINE_ONLY = "outline_only"

STATUS_TEXT = {
    BOTH_SAME: "两边一致",
    BOTH_DIFF: "叫法不同",
    OCR_ONLY: "只有识别",
    OUTLINE_ONLY: "只有书签",
}

# 默认勾选：只有「原本没书签、只能靠识别结果」的页默认勾上。
# 其余一律以 PDF 原书签为准——勾上才表示"用识别到的替换掉原书签的写法"。
DEFAULT_CHECKED = {
    BOTH_SAME: False,
    OCR_ONLY: True,
    BOTH_DIFF: False,
    OUTLINE_ONLY: False,
}


@dataclass
class MergeRow:
    """一行 = 一页：识别到的管线号 + 这一页原本的书签名。"""

    page: int                      # 0 起，和项目里的 scan_rows 一致
    tag: str = ""                  # 识别到的管线号（没有就是空）
    raw: str = ""                  # 识别的原始文字
    outline_title: str = ""        # PDF 书签里这一页的标题
    status: str = OCR_ONLY
    checked: bool = False          # 勾上 = 用识别结果替换这一页的书签名
    extra: dict = field(default_factory=dict)   # 原 scan_rows 里的其它字段，落库时带上
    chosen: str = ""               # 双击改过名之后的写法（空表示没改）

    @property
    def ocr_title(self) -> str:
        return (self.tag or self.raw or "").strip()

    @property
    def title(self) -> str:
        """写进目录树用的名字（默认是 PDF 原书签，勾上的才换成识别结果）。

        改过名就用改后的名字；没勾就回到书签名；两边都没有就是空（这页不进目录树）。
        """
        if self.chosen:
            return self.chosen.strip()
        if self.checked and self.ocr_title:
            return self.ocr_title
        return (self.outline_title or "").strip()


def _squash(text: str) -> str:
    """比较用的归一：全角半角、大小写、空白、连字符都不算差别。"""
    out = []
    for char in str(text or ""):
        if char.isspace():
            continue
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E:          # 全角 → 半角
            char = chr(code - 0xFEE0)
        out.append(char.lower())
    return "".join(out).replace("-", "").replace("—", "").replace("_", "")


def _same(a: str, b: str) -> bool:
    left, right = _squash(a), _squash(b)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def outline_by_page(outline_entries) -> dict[int, str]:
    """``[[level, title, page(1 起)], ...]`` → ``{page0: title}``。

    同一页有多条书签时留第一条（最靠前的那条通常就是这一页的标题）。
    """
    pages: dict[int, str] = {}
    for entry in outline_entries or []:
        try:
            level, title, page = entry[0], entry[1], int(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        if page <= 0:
            continue
        index = page - 1
        title = str(title or "").strip()
        if index not in pages and title:
            pages[index] = title
    return pages


def build_merge_rows(
    ocr_rows: dict[int, dict],
    outline_entries,
    page_count: int = 0,
) -> list[MergeRow]:
    """把两边的数据对起来，按页号排序返回。纯函数，方便测。"""
    outline = outline_by_page(outline_entries)
    limit = int(page_count or 0)
    pages = set()
    for key in (ocr_rows or {}):
        try:
            pages.add(int(key))
        except (TypeError, ValueError):
            continue
    pages.update(outline.keys())
    if limit > 0:
        pages = {page for page in pages if 0 <= page < limit}

    rows: list[MergeRow] = []
    for page in sorted(pages):
        source = dict((ocr_rows or {}).get(page) or {})
        tag = str(source.get("tag") or "").strip()
        raw = str(source.get("raw") or "").strip()
        title = outline.get(page, "")
        if tag and title:
            status = BOTH_SAME if _same(tag, title) else BOTH_DIFF
        elif tag:
            status = OCR_ONLY
        else:
            status = OUTLINE_ONLY
        rows.append(
            MergeRow(
                page=page,
                tag=tag,
                raw=raw,
                outline_title=title,
                status=status,
                checked=DEFAULT_CHECKED[status],
                extra={key: value for key, value in source.items()
                       if key not in ("page", "tag", "raw")},
            )
        )
    return rows


def summarize(rows: list[MergeRow]) -> dict:
    """统计各行数量，给标题下面的那句说明用。"""
    counts = {BOTH_SAME: 0, BOTH_DIFF: 0, OCR_ONLY: 0, OUTLINE_ONLY: 0}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    counts["total"] = len(rows)
    counts["ocr"] = counts[BOTH_SAME] + counts[BOTH_DIFF] + counts[OCR_ONLY]
    counts["outline"] = counts[BOTH_SAME] + counts[BOTH_DIFF] + counts[OUTLINE_ONLY]
    return counts


class _TitleDelegate(QStyledItemDelegate):
    """只有「写进目录树」那一列能编辑，别的列双击不出输入框。"""

    EDITABLE_COLUMN = 3

    def createEditor(self, parent, option, index):   # noqa: N802 (Qt 命名)
        if index.column() != self.EDITABLE_COLUMN:
            return None
        return super().createEditor(parent, option, index)


class TocMergeDialog(QDialog):
    """勾选要留在目录树里的条目。``result_rows()`` 返回整份（书签 + 勾选的识别）。"""

    def __init__(
        self,
        parent=None,
        *,
        rows: list[MergeRow],
        page_count: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("比对书签与识别结果")
        self.setMinimumSize(860, 560)
        self._rows = list(rows)
        self._page_count = int(page_count or 0)
        self._only_todo = False
        self._filling = False

        counts = summarize(self._rows)
        title = QLabel("「写进目录树」这一列默认是 PDF 原书签；勾上左边那一格，才把这一页换成"
                       "识别到的编号（原本没书签的页只能靠识别）。双击那一格也能直接改名。"
                       "最后整棵目录树会换成这份结果。")
        title.setWordWrap(True)

        self._summary = QLabel(self._summary_text(counts))
        self._summary.setObjectName("panelHint")
        self._summary.setWordWrap(True)

        self._todo_check = QCheckBox("只看需要处理的（叫法不同 / 只有书签）")
        self._todo_check.toggled.connect(self._on_todo_toggled)

        pick_all = QPushButton("全选")
        pick_all.clicked.connect(lambda: self._set_all(True))
        pick_none = QPushButton("全不选")
        pick_none.clicked.connect(lambda: self._set_all(False))
        pick_diff = QPushButton("只勾不一致")
        pick_diff.clicked.connect(self._pick_diff)
        pick_suggest = QPushButton("回到建议")
        pick_suggest.clicked.connect(self._reset_suggested)

        tools = QHBoxLayout()
        tools.setSpacing(8)
        tools.addWidget(self._todo_check)
        tools.addStretch(1)
        for button in (pick_diff, pick_all, pick_none, pick_suggest):
            button.setAutoDefault(False)
            tools.addWidget(button)

        self._tree = QTreeWidget()
        self._tree.setColumnCount(5)
        self._tree.setHeaderLabels(
            ["页码", "识别到的管线号", "PDF 书签", "写进目录树", "判断"]
        )
        self._tree.setRootIsDecorated(False)
        self._tree.setUniformRowHeights(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self._tree.setEditTriggers(
            QTreeWidget.EditTrigger.DoubleClicked | QTreeWidget.EditTrigger.EditKeyPressed
        )
        self._tree.setColumnWidth(0, 78)
        self._tree.setColumnWidth(1, 190)
        self._tree.setColumnWidth(2, 190)
        self._tree.setColumnWidth(4, 110)
        header = self._tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._tree.itemChanged.connect(self._on_item_changed)
        self._tree.setItemDelegate(_TitleDelegate(self._tree))
        self._fill_tree()

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        self._apply = QPushButton()
        self._apply.setProperty("primary", True)
        self._apply.clicked.connect(self.accept)
        self._buttons.addButton(self._apply, QDialogButtonBox.ButtonRole.AcceptRole)
        self._buttons.rejected.connect(self.reject)
        cancel = self._buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setText("取消")   # 不依赖 Qt 的中文翻译包

        box = QVBoxLayout(self)
        box.setContentsMargins(20, 18, 20, 16)
        box.setSpacing(10)
        box.addWidget(title)
        box.addWidget(self._summary)
        box.addLayout(tools)
        box.addWidget(self._tree, 1)
        box.addWidget(self._buttons)
        self._refresh_apply()

    # ------------------------------------------------------------------ 构建

    def _summary_text(self, counts: dict) -> str:
        parts = [
            f"一共 {counts['total']} 页有信息：识别到 {counts['ocr']} 条，"
            f"PDF 自带的书签 {counts['outline']} 条。",
            f"两边一致 {counts[BOTH_SAME]} 条，叫法不同 {counts[BOTH_DIFF]} 条，"
            f"只有识别 {counts[OCR_ONLY]} 条，只有书签 {counts[OUTLINE_ONLY]} 条。",
        ]
        return "\n".join(parts)

    def _fill_tree(self) -> None:
        self._filling = True
        self._tree.blockSignals(True)
        self._tree.clear()
        for row in self._rows:
            if self._only_todo and row.status == BOTH_SAME:
                continue
            item = QTreeWidgetItem([
                str(row.page + 1),
                row.tag or "—",
                row.outline_title or "—",
                row.title or "—",
                STATUS_TEXT.get(row.status, ""),
            ])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable
                          | Qt.ItemFlag.ItemIsEditable)
            item.setCheckState(0, Qt.CheckState.Checked if row.checked else Qt.CheckState.Unchecked)
            item.setData(0, Qt.ItemDataRole.UserRole, row.page)
            item.setToolTip(3, "默认是 PDF 原书签；勾上左边 = 用识别结果替换它。双击可以改名")
            if row.status == BOTH_DIFF:
                item.setForeground(4, QColor(theme.WARN))
            elif row.status == OCR_ONLY:
                item.setForeground(4, QColor(theme.ACCENT_HOVER))
            elif row.status == OUTLINE_ONLY:
                item.setForeground(4, QColor(theme.MUTED))
            else:
                item.setForeground(4, QColor(theme.MUTED))
            if row.chosen or (row.checked and row.ocr_title):
                item.setForeground(3, QColor(theme.ACCENT_HOVER))
            if not row.title:
                # 这页两边都没有可用名字：勾着也不会进目录树
                for column in range(5):
                    item.setForeground(column, QColor(theme.FAINT))
            self._tree.addTopLevelItem(item)
        self._tree.blockSignals(False)
        self._filling = False

    # ------------------------------------------------------------------ 交互

    def _row_for(self, page) -> MergeRow | None:
        for row in self._rows:
            if row.page == page:
                return row
        return None

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._filling:
            return
        row = self._row_for(item.data(0, Qt.ItemDataRole.UserRole))
        if row is None:
            return
        if column == 0:
            row.checked = item.checkState(0) == Qt.CheckState.Checked
            self._refresh_apply()
        elif column == 3:
            text = (item.text(3) or "").strip()
            if not text or text == "—":
                # 改空了就还原
                self._filling = True
                item.setText(3, row.title or "—")
                self._filling = False
                return
            row.chosen = text
            row.checked = True
            self._filling = True
            item.setCheckState(0, Qt.CheckState.Checked)
            item.setForeground(3, QColor(theme.ACCENT_HOVER))
            for other in range(5):
                if other != 3:
                    item.setForeground(other, QBrush())
            self._filling = False
            self._refresh_apply()

    def _on_todo_toggled(self, checked: bool) -> None:
        self._only_todo = bool(checked)
        self._fill_tree()

    def _set_all(self, value: bool) -> None:
        for row in self._rows:
            row.checked = value
        self._fill_tree()
        self._refresh_apply()

    def _pick_diff(self) -> None:
        """勾上"叫法不一样"和"只有识别"的行：前者换成识别结果，后者只能靠识别。"""
        for row in self._rows:
            row.checked = row.status in (BOTH_DIFF, OCR_ONLY)
        self._fill_tree()
        self._refresh_apply()

    def _reset_suggested(self) -> None:
        for row in self._rows:
            row.checked = DEFAULT_CHECKED.get(row.status, False)
        self._fill_tree()
        self._refresh_apply()

    def _refresh_apply(self) -> None:
        count = sum(1 for row in self._rows if row.title)
        self._apply.setText("换成目录树（%d 条）" % count)
        self._apply.setEnabled(count > 0)

    # ------------------------------------------------------------------ 结果

    def selected_rows(self) -> list[MergeRow]:
        return [row for row in self._rows if row.checked]

    def result_rows(self) -> list[dict]:
        """这份结果会**整体**写进目录树：以 PDF 书签为底，勾上的用识别结果替换。

        每行默认给的都是书签名，勾上（或改过名）才换成识别结果；两边都没有名字的
        页不产出（OCR 乱猜又没书签的那种，不勾就当它不存在）。
        """
        import time

        now = time.time()
        payload: list[dict] = []
        for row in self._rows:
            title = row.title
            if not title:
                continue
            from_ocr = bool(row.chosen) or (row.checked and bool(row.ocr_title))
            if from_ocr:
                item = dict(row.extra)
                item.update({
                    "page": int(row.page),
                    "raw": row.raw or title,
                    "tag": title[:200],
                    "source": "ocr",
                    "fixed": 1 if item.get("fixed") else 0,
                    "confirmed": 1,
                    "updated_at": item.get("updated_at") or now,
                })
                if row.outline_title and _squash(title) != _squash(row.outline_title):
                    # 用了另一套写法（包括双击改的名），把书签原文留个备注
                    item["note"] = "书签原作：%s" % row.outline_title[:80]
                payload.append(item)
            else:
                payload.append({
                    "page": int(row.page),
                    "raw": row.outline_title,
                    "tag": title[:200],
                    "source": "outline",
                    "fixed": 0,
                    "confirmed": 1,
                    "note": "来自 PDF 目录",
                    "updated_at": now,
                })
        payload.sort(key=lambda item: item["page"])
        return payload

"""设置面板：缓存上限、导出文件夹、备份文件夹。

三条原则：

1. **默认都在程序自己这边**——用户原话"默认放 qpdf 的文件夹里面，不要再放桌面了"。
   绿色版的习惯是"整包自己管自己"，输出和备份默认落在程序目录下的 ``输出/``、``备份/``。
2. 留空就回到默认值，不需要"恢复默认"按钮也算得清。
3. 缓存上限改小要能立刻生效（见 ``RenderManager.set_budget_mb``）。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ui import shortcuts as shortcutmap
from ui import theme

def _app_dir() -> str:
    """程序所在目录：绿色版是 exe 旁边，开发时是项目根。

    注意不能直接用 ``__file__`` 往上两级——打包后那是 ``_internal/``，
    文件会跑到解压目录里，用户根本找不到。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 这些地方不拿来当默认目录——用户把绿色包解压到桌面上很常见，
#: 结果「输出/」「备份/」全堆在桌面，他自己也嫌乱
_BAD_DIR_NAMES = ("desktop", "桌面", "downloads", "下载", "temp", "tmp")


def _documents_dir() -> str:
    """用户的「文档」目录；取不到就退回主目录。"""
    home = os.path.expanduser("~")
    for name in ("Documents", "文档"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    return home


def _default_root() -> str:
    """默认放输出/备份的根目录。

    优先程序自己所在的目录（绿色版习惯：整包自己管自己）；
    但只要路径里出现桌面、下载、临时这些地方，就改放到「文档\\雀雀PDF」——
    用户把绿色包解压到桌面文件夹里是很常见的，别让软件把文件铺在桌面上。
    """
    base = _app_dir()
    parts = [p for p in os.path.abspath(base).replace("\\", "/").split("/") if p]
    if any(p.strip().lower() in _BAD_DIR_NAMES for p in parts):
        return os.path.join(_documents_dir(), "雀雀PDF")
    try:
        home = os.path.abspath(os.path.expanduser("~"))
        if os.path.abspath(base) == home:
            return os.path.join(_documents_dir(), "雀雀PDF")
    except OSError:
        pass
    return base


def default_export_dir() -> str:
    """默认导出目录：``<默认根>/输出``。"""
    return os.path.join(_default_root(), "输出")


def default_backup_dir() -> str:
    """默认备份目录：``<默认根>/备份``。"""
    return os.path.join(_default_root(), "备份")


def default_project_dir_value() -> str:
    """默认项目库：``文档\\雀雀PDF\\项目``。

    专门不跟 PDF 放一起——图纸经常直接从桌面、U 盘、共享盘打开，
    往那儿撒 ``.qpdf`` 又乱又容易跟别人的文件混。
    """
    return os.path.join(_documents_dir(), "雀雀PDF", "项目")


class _PathRow(QWidget):
    """一行"输入框 + 浏览…"。"""

    def __init__(self, placeholder: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(placeholder)
        self.button = QPushButton("浏览…", self)
        self.button.setMaximumWidth(84)
        self.button.clicked.connect(self._browse)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button)

    def _browse(self) -> None:
        start = self.edit.text().strip() or _default_root()
        path = QFileDialog.getExistingDirectory(self, "选择文件夹", start)
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, text: str) -> None:
        self.edit.setText(text or "")


class SettingsDialog(QDialog):
    """工具 → 设置…"""

    def __init__(
        self,
        cache_mb: int,
        export_dir: str = "",
        backup_dir: str = "",
        project_dir: str = "",
        skip_duplicates: bool = False,
        threads: int = 1,
        shortcuts: dict | None = None,
        mouse_keys: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        title = QLabel("设置", self)
        title.setObjectName("panelTitle")
        outer.addWidget(title)

        # 内容放进滚动区：小屏（1366×768）上整页塞不下，别把「确定」按钮挤到屏幕外
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget(scroll)
        inner = QVBoxLayout(body)
        inner.setContentsMargins(0, 0, 6, 0)
        inner.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            scroll.setMinimumHeight(320)
            self.resize(min(640, avail.width() - 80), min(900, avail.height() - 80))

        render_card, render_col = self._card("渲染与缓存")
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._cache = QSpinBox(self)
        self._cache.setRange(64, 2048)
        self._cache.setSingleStep(32)
        self._cache.setSuffix(" MB")
        self._cache.setValue(int(cache_mb))
        self._cache.setMaximumWidth(150)
        cache_hint = QLabel(
            "页面图片缓存上限。机器内存富余就调大（少重渲、翻回去更快），"
            "吃紧就调小。默认 512 MB；整页预览图另算，按这里的一半。",
            self,
        )
        cache_hint.setObjectName("panelHint")
        cache_hint.setWordWrap(True)
        cache_box = QWidget(self)
        cache_col = QVBoxLayout(cache_box)
        cache_col.setContentsMargins(0, 0, 0, 0)
        cache_col.setSpacing(4)
        cache_col.addWidget(self._cache)
        cache_col.addWidget(cache_hint)
        form.addRow("缓存上限", cache_box)

        self._threads = QSpinBox(self)
        self._threads.setRange(1, 8)
        self._threads.setSuffix(" 个")
        self._threads.setValue(max(1, min(8, int(threads))))
        self._threads.setMaximumWidth(150)
        threads_hint = QLabel(
            "当前 PDF 引擎一次只让一个线程真正出图，所以调大基本不会更快（实测"
            "反而略慢）。留在 1 最稳；以后改成每线程独立文档才需要调大。",
            self,
        )
        threads_hint.setObjectName("panelHint")
        threads_hint.setWordWrap(True)
        threads_box = QWidget(self)
        threads_col = QVBoxLayout(threads_box)
        threads_col.setContentsMargins(0, 0, 0, 0)
        threads_col.setSpacing(4)
        threads_col.addWidget(self._threads)
        threads_col.addWidget(threads_hint)
        form.addRow("渲染线程", threads_box)
        render_col.addLayout(form)
        inner.addWidget(render_card)

        dirs_card, dirs_col = self._card("默认位置")
        form_dirs = QFormLayout()
        form_dirs.setSpacing(10)
        form_dirs.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._project = _PathRow(default_project_dir_value(), self)
        form_dirs.addRow("项目文件夹", self._project)

        self._export = _PathRow(default_export_dir(), self)
        form_dirs.addRow("导出文件夹", self._export)

        self._backup = _PathRow(default_backup_dir(), self)
        form_dirs.addRow("备份文件夹", self._backup)
        dirs_col.addLayout(form_dirs)
        inner.addWidget(dirs_card)

        annot_card, annot_col = self._card("按表格标注")
        # -- 自动标注（F05）---------------------------------------------------
        self._skip_dup = QCheckBox("同一个管线号只标一次", self)
        self._skip_dup.setChecked(bool(skip_duplicates))
        dup_hint = QLabel(
            "不勾（默认）：一个管线号出现在几页上，这几页都标。\n"
            "勾上：只在第一次出现的那页标，后面同名页面记成「同名跳过」，"
            "预览表里还可以单独把它们勾回来。",
            self,
        )
        dup_hint.setObjectName("panelHint")
        dup_hint.setWordWrap(True)
        dup_box = QWidget(self)
        dup_col = QVBoxLayout(dup_box)
        dup_col.setContentsMargins(0, 0, 0, 0)
        dup_col.setSpacing(4)
        dup_col.addWidget(self._skip_dup)
        dup_col.addWidget(dup_hint)
        annot_col.addWidget(dup_box)
        inner.addWidget(annot_card)

        inner.addWidget(self._build_shortcut_box(shortcuts or {}, mouse_keys or {}))

        hint = QLabel(
            f"留空就用默认位置：\n项目 → {default_project_dir_value()}\n"
            f"导出 → {default_export_dir()}\n备份 → {default_backup_dir()}\n"
            "（项目文件不放在 PDF 旁边；图纸从桌面或 U 盘打开也不会在那里撒文件）",
            self,
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        hint.setMinimumHeight(72)
        inner.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        self._ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._ok.setText("确定")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        theme.mark_primary(self._ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # -- 版式 ---------------------------------------------------------------
    def _card(self, caption: str) -> tuple:
        """一张分组卡：和属性面板用同一套 QSS（QFrame#groupCard + groupTitle）。"""
        frame = QFrame(self)
        frame.setObjectName("groupCard")
        column = QVBoxLayout(frame)
        column.setContentsMargins(14, 12, 14, 14)
        column.setSpacing(10)
        if caption:
            label = QLabel(caption, frame)
            label.setObjectName("groupTitle")
            column.addWidget(label)
        return frame, column

    # -- 快捷键 -------------------------------------------------------------
    def _build_shortcut_box(self, current: dict, mouse: dict) -> QWidget:
        box, column = self._card("快捷键")

        hint = QLabel(
            "点右边那一格，直接按新的键就改好了（按 Backspace 清空 = 不用快捷键）。"
            "默认值没变，改过的地方才记下来。",
            self,
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        column.addWidget(hint)

        table = QTableWidget(len(shortcutmap.SHORTCUTS), 2, self)
        table.setHorizontalHeaderLabels(["功能", "快捷键"])
        table.verticalHeader().setVisible(False)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(1, 190)
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setFixedHeight(230)

        self._shortcut_edits: dict[str, QKeySequenceEdit] = {}
        for row, item in enumerate(shortcutmap.SHORTCUTS):
            label = QTableWidgetItem(item.label)
            table.setItem(row, 0, label)
            text = shortcutmap.normalize(current.get(item.key, item.default))
            edit = QKeySequenceEdit(QKeySequence(text), table)
            try:  # Qt 6.5+ 只收单个组合键，省得录成一串
                edit.setMaximumSequenceLength(1)
            except AttributeError:  # pragma: no cover - 老版本没有这个方法
                pass
            table.setCellWidget(row, 1, edit)
            self._shortcut_edits[item.key] = edit

        column.addWidget(table)

        # 鼠标侧键：键盘那套 QKeySequence 表示不了，单独用两个下拉
        mouse_hint = QLabel(
            "鼠标侧键（大拇指那两个键）也能绑功能：在下拉里挑一个，"
            "挑「不用」就是不绑。"
        )
        mouse_hint.setObjectName("panelHint")
        mouse_hint.setWordWrap(True)
        column.addWidget(mouse_hint)
        self._mouse_combos: dict[str, QComboBox] = {}
        choices = [("", "不用")] + [(item.key, item.label) for item in shortcutmap.SHORTCUTS]
        for key, label in shortcutmap.MOUSE_KEYS:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(QLabel(label, self))
            combo = QComboBox(self)
            for value, text in choices:
                combo.addItem(text, value)
            current_action = (mouse or {}).get(key) or ""
            index = combo.findData(current_action)
            combo.setCurrentIndex(max(0, index))
            self._mouse_combos[key] = combo
            row.addWidget(combo, 1)
            column.addLayout(row)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        reset = QPushButton("全部还原默认", self)
        reset.clicked.connect(self._reset_shortcuts)
        buttons.addWidget(reset)
        buttons.addStretch(1)
        column.addLayout(buttons)
        return box

    def _reset_shortcuts(self) -> None:
        for item in shortcutmap.SHORTCUTS:
            edit = self._shortcut_edits.get(item.key)
            if edit is not None:
                edit.setKeySequence(QKeySequence(item.default))

    def shortcut_values(self) -> dict:
        values: dict[str, str] = {}
        for key, edit in self._shortcut_edits.items():
            values[key] = shortcutmap.normalize(
                edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
            )
        return values

    def mouse_values(self) -> dict:
        return {key: str(combo.currentData() or "") for key, combo in self._mouse_combos.items()}

    def accept(self) -> None:  # noqa: D401 - Qt 命名
        clashes = shortcutmap.conflicts(self.shortcut_values())
        if clashes:
            text = "\n".join(f"{seq}：{'、'.join(labels)}" for seq, labels in clashes)
            QMessageBox.warning(
                self,
                "快捷键重复了",
                f"这几个键被两个功能占着，改一个再确定：\n\n{text}",
            )
            return
        super().accept()

    # -- 取值 ---------------------------------------------------------------
    def cache_mb_value(self) -> int:
        return int(self._cache.value())

    def export_dir_value(self) -> str:
        return self._export.value()

    def project_dir_value(self) -> str:
        return self._project.value()

    def backup_dir_value(self) -> str:
        return self._backup.value()

    def skip_duplicates_value(self) -> bool:
        return bool(self._skip_dup.isChecked())

    def threads_value(self) -> int:
        return int(self._threads.value())

"""备份与还原对话框（第 16 章：自动备份 / 备份列表与还原）。

只做三件事：把 ``备份/`` 里属于当前项目的几份列出来、点一下还原、打开备份文件夹。
还原只换 ``.qpdf``，原始 PDF 一个字节都不动。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core.project_manager import ProjectManager, ProjectError
from ui import theme


def _fmt_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{max(1, size // 1024)} KB"


class BackupDialog(QDialog):
    """列出备份 + 还原。非模态，开着也不影响继续标注。"""

    def __init__(self, project: ProjectManager, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("备份与还原")
        self.setMinimumSize(620, 420)
        self._project = project

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(10)

        title = QLabel("项目备份")
        title.setObjectName("panelTitle")
        outer.addWidget(title)

        folder = project.backup_dir()
        hint = QLabel(
            f"备份目录：{folder}\n"
            "自动备份每 10 分钟一次（只在这个项目有未保存改动时写）；"
            "手动「备份项目」也会往这里放一份。还原只换项目文件，原始 PDF 不动。"
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        self._table = QTableWidget(0, 3, self)
        self._table.setHorizontalHeaderLabels(["时间", "大小", "文件"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.itemDoubleClicked.connect(lambda *_: self._restore_selected())
        outer.addWidget(self._table, 1)

        self._status = QLabel("")
        self._status.setObjectName("muted")
        outer.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._btn_restore = QPushButton("还原选中的备份")
        theme.mark_primary(self._btn_restore)
        self._btn_restore.clicked.connect(self._restore_selected)
        buttons.addWidget(self._btn_restore)

        btn_folder = QPushButton("打开备份文件夹")
        btn_folder.clicked.connect(self._open_folder)
        buttons.addWidget(btn_folder)

        btn_backup_now = QPushButton("立刻备份一份")
        btn_backup_now.clicked.connect(self._backup_now)
        buttons.addWidget(btn_backup_now)

        buttons.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        buttons.addWidget(btn_close)
        outer.addLayout(buttons)

        self.reload()

    # ------------------------------------------------------------------ 列表

    def reload(self) -> None:
        rows = self._project.list_backups()
        self._table.setRowCount(0)
        for row in rows:
            index = self._table.rowCount()
            self._table.insertRow(index)
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["mtime"]))
            self._table.setItem(index, 0, QTableWidgetItem(when))
            self._table.setItem(index, 1, QTableWidgetItem(_fmt_size(row["size"])))
            name_item = QTableWidgetItem(row["name"])
            name_item.setData(Qt.ItemDataRole.UserRole, row["path"])
            self._table.setItem(index, 2, name_item)
        if rows:
            self._table.selectRow(0)
            self._status.setText(f"共 {len(rows)} 份备份（最多保留最近 5 份）")
        else:
            self._status.setText("还没有备份。标注过之后会自动留，也可以点「立刻备份一份」。")
        self._btn_restore.setEnabled(bool(rows))

    def _selected_path(self) -> str:
        items = self._table.selectedItems()
        if not items:
            return ""
        row = items[0].row()
        cell = self._table.item(row, 2)
        return str(cell.data(Qt.ItemDataRole.UserRole) or "") if cell else ""

    # ------------------------------------------------------------------ 动作

    def _backup_now(self) -> None:
        target = self._project.backup()
        if target:
            self.reload()
            self._status.setText(f"已备份：{os.path.basename(target)}")
        else:
            self._status.setText("没备成（项目可能还没建，或目录写不进去）")

    def _open_folder(self) -> None:
        folder = self._project.backup_dir()
        if folder and os.path.isdir(folder):
            os.startfile(folder)  # noqa: S606 - Windows 上打开资源管理器
        else:
            self._status.setText("备份目录还不存在")

    def _restore_selected(self) -> None:
        path = self._selected_path()
        if not path:
            self._status.setText("先选一行")
            return
        answer = QMessageBox.question(
            self,
            "还原备份",
            f"用这份备份覆盖当前项目文件？\n\n{os.path.basename(path)}\n\n"
            "覆盖之后，现在还没保存的改动会丢掉（覆盖前会先自动备份一份，想反悔还能回来）。\n"
            "原始 PDF 不受影响。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            ok = self._project.restore_backup(path)
        except ProjectError as exc:
            QMessageBox.warning(self, "还原失败", str(exc))
            return
        if ok:
            self.reload()
            self._status.setText("已还原，标注已重新载入")
            QMessageBox.information(self, "还原完成", "项目已还原到选中的那份备份。")
        else:
            self._status.setText("还原没成功，看看状态栏的提示")

"""材料表识别：框选图纸右上角的材料表，逐页 OCR，汇总导出 Excel。

界面只负责范围、进度和结果表；识别与解析在 ``core.material_scan`` 的后台线程里。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core import material_scan, textscan
from ui import theme


class MaterialDialog(QDialog):
    """材料表批量识别 + 校对 + 导出。"""

    def __init__(self, path: str, region, page_count: int, project=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("材料表识别")
        self.setMinimumSize(1040, 620)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self._path = path
        self._region = region
        self._page_count = max(1, int(page_count))
        self._project = project
        self._worker: material_scan.MaterialScanWorker | None = None
        self._orphan_workers: list = []
        self._rows: list[material_scan.MaterialRow] = []
        self._started_at = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)

        title = QLabel(
            "识别区域：左 {:.1%} · 上 {:.1%} · 右 {:.1%} · 下 {:.1%}".format(
                region.x0, region.y0, region.x1, region.y1
            )
        )
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        hint = QLabel(
            "每页按同样的位置取材料表，读「序号 / 材料描述 / 规格」；"
            "数量不用框。管线号取该书签，页码取 PDF 页码。空页会自动跳过。"
        )
        hint.setObjectName("panelHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("页码"))
        self._spin_from = QSpinBox()
        self._spin_from.setRange(1, self._page_count)
        self._spin_from.setValue(1)
        self._spin_from.setMaximumWidth(90)
        row.addWidget(self._spin_from)
        row.addWidget(QLabel("到"))
        self._spin_to = QSpinBox()
        self._spin_to.setRange(1, self._page_count)
        self._spin_to.setValue(self._page_count)
        self._spin_to.setMaximumWidth(90)
        row.addWidget(self._spin_to)
        self._btn_start = QPushButton("开始识别")
        theme.mark_primary(self._btn_start)
        self._btn_start.clicked.connect(self.start_scan)
        row.addWidget(self._btn_start)
        self._btn_stop = QPushButton("停止")
        self._btn_stop.setEnabled(False)
        self._btn_stop.clicked.connect(self.stop_scan)
        row.addWidget(self._btn_stop)
        row.addStretch(1)
        layout.addLayout(row)

        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        self._bar.setFormat("%v / %m 页")
        layout.addWidget(self._bar)
        self._status = QLabel("还没开始")
        self._status.setObjectName("muted")
        layout.addWidget(self._status)

        self._table = QTableWidget(0, len(material_scan.COLUMNS), self)
        self._table.setHorizontalHeaderLabels(list(material_scan.COLUMNS))
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        for index in range(len(material_scan.COLUMNS)):
            if material_scan.COLUMNS[index] == "材料":
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)
            else:
                header.setSectionResizeMode(index, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._btn_export = QPushButton("导出 Excel…")
        self._btn_export.setEnabled(False)
        theme.mark_primary(self._btn_export)
        self._btn_export.clicked.connect(self.export_excel)
        buttons.addWidget(self._btn_export)
        self._count_label = QLabel("0 条")
        self._count_label.setObjectName("muted")
        buttons.addWidget(self._count_label)
        buttons.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        buttons.addWidget(btn_close)
        layout.addLayout(buttons)

        self._load_existing()

    # ------------------------------------------------------------ 数据
    def _load_existing(self) -> None:
        if self._project is None:
            return
        stored = self._project.get_setting("material_rows", None) or []
        for item in stored:
            try:
                row = material_scan.MaterialRow(
                    pipeline=str(item.get("pipeline", "")),
                    page=int(item.get("page", 0) or 0),
                    seq=int(item.get("seq", 0) or 0),
                    category=str(item.get("category", "")),
                    grade=str(item.get("grade", "")),
                    standard=str(item.get("standard", "")),
                    phi=str(item.get("phi", "")),
                    dn=str(item.get("dn", "")),
                    qty=str(item.get("qty", "")),
                    raw=str(item.get("raw", "")),
                )
            except Exception:  # noqa: BLE001 - 旧数据坏一条不影响其它
                continue
            self._rows.append(row)
            self._append_row(row)
        if self._rows:
            self._status.setText(f"已从项目载入 {len(self._rows)} 条材料表结果")
            self._btn_export.setEnabled(True)

    def _save_existing(self) -> None:
        if self._project is None:
            return
        try:
            self._project.set_setting(
                "material_rows",
                [
                    {
                        "pipeline": row.pipeline,
                        "page": row.page,
                        "seq": row.seq,
                        "category": row.category,
                        "grade": row.grade,
                        "standard": row.standard,
                        "phi": row.phi,
                        "dn": row.dn,
                        "qty": row.qty,
                        "raw": row.raw,
                    }
                    for row in self._rows
                ],
            )
        except Exception:  # noqa: BLE001 - 保存失败不该挡住导出
            pass

    # ------------------------------------------------------------ 扫描
    def start_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            self._status.setText("上一次识别还没结束，等它跑完或先点「停止」")
            return
        if textscan.ocr_available() is False:
            self._status.setText(f"无法识别：{textscan.ocr_error()}")
            return
        first = self._spin_from.value()
        last = self._spin_to.value()
        if last < first:
            first, last = last, first
        self._table.setRowCount(0)
        self._rows.clear()
        self._bar.setRange(0, max(1, last - first + 1))
        self._bar.setValue(0)
        self._started_at = time.perf_counter()
        self._status.setText(f"正在识别第 {first}–{last} 页（后台线程，界面不会卡）")
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_export.setEnabled(False)
        self._count_label.setText("0 条")

        worker = material_scan.MaterialScanWorker(
            self._path,
            self._region,
            first_page=first,
            last_page=last,
            parent=self,
        )
        worker.progress.connect(self._on_progress)
        worker.rows.connect(self._on_rows)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_finished)
        worker.finished.connect(self._on_worker_finished)
        self._worker = worker
        worker.start()

    def stop_scan(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._status.setText("正在停止…")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.stop()
                if not worker.wait(1500):
                    self._orphan_workers.append(worker)
                    worker.finished.connect(worker.deleteLater)
            except RuntimeError:
                pass
        self._save_existing()
        super().closeEvent(event)

    def _on_progress(self, done: int, total: int) -> None:
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(done)
        elapsed = time.perf_counter() - self._started_at
        left = (elapsed / done) * (total - done) if done else 0.0
        self._status.setText(
            f"已识别 {done}/{total} 页 · {len(self._rows)} 条材料 · 已用 {elapsed:.0f} 秒"
            f" · 预计还要 {left:.0f} 秒"
        )

    def _on_rows(self, batch: list[material_scan.MaterialRow]) -> None:
        for row in batch:
            self._rows.append(row)
            self._append_row(row)
        self._count_label.setText(f"{len(self._rows)} 条")

    def _append_row(self, row: material_scan.MaterialRow) -> None:
        index = self._table.rowCount()
        self._table.insertRow(index)
        for col, value in enumerate(row.as_tuple()):
            item = QTableWidgetItem(str(value))
            if col in (1, 2, 7):
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(index, col, item)
        self._table.scrollToBottom()

    def _on_failed(self, message: str) -> None:
        self._status.setText(f"识别失败：{message}")
        self._reset_buttons()

    def _on_finished(self, pages: int, count: int, seconds: float) -> None:
        rate = pages / seconds if seconds > 0 else 0.0
        self._status.setText(
            f"识别完成：{pages} 页，{count} 条材料，用时 {seconds:.1f} 秒"
            f"（{rate:.2f} 页/秒）"
        )
        if count == 0:
            self._status.setText(
                "没有识别到材料表：请确认框选区域包含「材料描述 + 规格」两列，"
                "表头有没有框到都可以"
            )
        self._reset_buttons()
        self._save_existing()

    def _on_worker_finished(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.deleteLater()

    def _reset_buttons(self) -> None:
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._btn_export.setEnabled(bool(self._rows))

    # ------------------------------------------------------------ 导出
    def export_excel(self) -> None:
        if not self._rows:
            self._status.setText("还没有材料表结果，先跑一遍识别")
            return
        start = os.path.dirname(self._path) if self._path else ""
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "导出材料表汇总",
            os.path.join(start, "材料表汇总.xlsx"),
            "Excel 工作簿 (*.xlsx)",
        )
        if not path:
            return
        if not path.lower().endswith(".xlsx"):
            path += ".xlsx"
        try:
            count = material_scan.write_material_xlsx(path, self._rows)
        except Exception as exc:  # noqa: BLE001 - 导出失败要让人看见
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self._status.setText(f"已导出 {count} 条材料表到 {path}")
        QMessageBox.information(self, "导出完成", f"已写入 {count} 条：\n{path}")

"""批量区域识别对话框（Phase 10 第 4、5 步）。

界面只做三件事：把区域告诉后台线程、把结果铺成一张可改的表格、把改完的结果变成书签。
**扫描本身一行都不在主线程跑**——见 ``core/scan_job.py``。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from core import dataset, qc, textscan
from core.bookmarks import build_bookmarks
from core.scan_job import ScanWorker
from ui import theme

#: 表格列号（改列序时只改这里）
COL_USE, COL_PAGE, COL_TAG, COL_SUSPECT, COL_SOURCE, COL_NOTE = range(6)


def _note_with(note: str, extra: str) -> str:
    """往备注后面接一句，不重复接同一句。"""
    if not extra or extra in note:
        return note
    return f"{note}；{extra}" if note else extra


class ScanDialog(QDialog):
    """识别 + 校对。非模态，扫描过程中还能翻页看图纸。"""

    bookmarks_ready = Signal(object)  # list[Bookmark]

    def __init__(self, path: str, region, page_count: int, project=None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量识别管线号")
        self.setMinimumSize(760, 560)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self._path = path
        self._region = region
        self._page_count = int(page_count)
        self._project = project
        self._worker: ScanWorker | None = None
        #: 关窗口时还没停下来的扫描线程：留个引用，等它自己收尾再让 Qt 删
        self._orphan_workers: list = []
        self._rows: dict[int, dict] = {}
        self._started_at = 0.0
        #: 提示规则（"以后不再提示这类"），存在项目的 ocr_rules 里
        self._rules = qc.empty_rules()
        if project is not None:
            try:
                self._rules = qc.load_rules(project.get_setting("ocr_rules"))
            except Exception:  # noqa: BLE001 - 规则读不出来就当没有
                self._rules = qc.empty_rules()

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
            "每页按同样的比例取这块地方。结果可以直接在表格里改，改完点「生成书签」。"
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
        self._btn_correct = QPushButton("用数据表校对…")
        self._btn_correct.setToolTip(
            "拿 CSV / Excel 里记的管线号对一遍：表里有的按表里的写法改，表里没有的只列候选。"
        )
        self._btn_correct.clicked.connect(self.correct_with_dataset)
        row.addWidget(self._btn_correct)
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

        self._table = QTableWidget(0, 6, self)
        self._table.setHorizontalHeaderLabels(
            ["采用", "页", "识别结果（可改）", "疑似", "来源", "备注"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemDoubleClicked.connect(self._on_jump)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_menu)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self._btn_save = QPushButton("保存到项目")
        self._btn_save.setEnabled(False)
        self._btn_save.clicked.connect(self.save_to_project)
        buttons.addWidget(self._btn_save)
        self._btn_bookmarks = QPushButton("生成书签")
        self._btn_bookmarks.setEnabled(False)
        theme.mark_primary(self._btn_bookmarks)
        self._btn_bookmarks.clicked.connect(self.make_bookmarks)
        buttons.addWidget(self._btn_bookmarks)
        buttons.addStretch(1)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        buttons.addWidget(btn_close)
        layout.addLayout(buttons)

        self._load_existing()

    # ------------------------------------------------------------ 数据
    def tags(self) -> dict[int, str]:
        """只返回"采用"打钩的行——没打钩的不进书签。"""
        self.collect_from_table()  # 勾选状态在表格里，先收回来
        return {
            page: row["tag"]
            for page, row in self._rows.items()
            if row.get("tag") and row.get("use", True)
        }

    def _load_existing(self) -> None:
        if self._project is None:
            return
        stored = self._project.load_scan_rows()
        if not stored:
            return
        for page, info in sorted(stored.items()):
            self._rows[page] = {
                "page": page,
                "raw": info.get("raw", ""),
                "tag": info.get("tag", ""),
                "source": info.get("source", ""),
                "fixed": info.get("fixed", False),
                "use": True,
                "error": "" if info.get("tag") else "上次没认出来",
            }
            self._append_row(self._rows[page])
        self._status.setText(f"已从项目载入 {len(stored)} 条识别结果")
        self._btn_save.setEnabled(True)
        self._btn_bookmarks.setEnabled(True)

    def correct_with_dataset(self) -> None:
        """拿数据表校对识别结果：表里有的按表里的写法改，表里没有的只列候选。

        只在内存里改，点「保存到项目」才落盘——他那些表是原样，不能碰。
        """
        if not self._rows:
            self._status.setText("还没有识别结果：先跑一遍识别，或者从项目里载入上次的。")
            return
        start = os.path.dirname(self._path) if self._path else ""
        path, _filter = QFileDialog.getOpenFileName(
            self, "选数据表", start, "表格 (*.csv *.xlsx *.xlsm);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            table = dataset.read_table(path)
        except dataset.DataError as exc:
            QMessageBox.warning(self, "读不了这张表", str(exc))
            return
        key_column = dataset.guess_key_column(table)
        if not key_column:
            QMessageBox.information(
                self, "找不到管线号列", "这张表里没找到像「管线号 / 管道号」的列。"
            )
            return
        rule = self._pick_key_rule()
        if rule is None:
            return
        records = dataset.RecordTable(table, key_column, key_rule=rule)
        found = 0
        changed = 0
        for page in sorted(self._rows):
            row = self._rows[page]
            tag = (row.get("tag") or "").strip()
            if not tag:
                continue
            match = records.lookup(tag)
            note = row.get("error", "") or ""
            if match.status == dataset.MATCHED and match.key:
                found += 1
                if match.key != tag:
                    note = _note_with(note, f"按表校正：{tag} → {match.key}")
                    row["tag"] = match.key
                    changed += 1
            elif match.status == dataset.CONFLICT:
                note = _note_with(note, "表里有多条同名记录，得手动选一条")
            else:
                names = "、".join(key for key, _ratio in (match.candidates or [])[:3])
                note = _note_with(
                    note, "表里没有" + (f"，相近：{names}" if names else "")
                )
            row["error"] = note
        self._refresh_tags()
        self._refresh_suspects()
        self._status.setText(
            f"对照 {os.path.basename(path)} 校完了：表里有 {found} 条，改写法 {changed} 条。"
            "想保留就点「保存到项目」。"
        )

    def _pick_key_rule(self):
        """对表之前先问一句：整串对，还是取中间两段对。

        图和表的写法常常不一样（图上带口径/材质等级，表里只记中间两段），
        这里给了选项，并且记住上次选的那个。
        """
        settings = QSettings("QPDF", "QPDF")
        last = settings.value("scan_key_rule", dataset.KEY_AUTO, type=str) or dataset.KEY_AUTO
        labels = [label for _rule, label in dataset.KEY_RULES]
        current = 0
        for index, (rule, _label) in enumerate(dataset.KEY_RULES):
            if rule == last:
                current = index
                break
        choice, ok = QInputDialog.getItem(
            self,
            "编号怎么对",
            "图上的管线号常带口径、材料等级，表里只记中间两段。\n"
            "选「自动」就先拿整串对，对不上再取中间两段。",
            labels,
            current,
            False,
        )
        if not ok or not choice:
            return None
        for rule, label in dataset.KEY_RULES:
            if label == choice:
                settings.setValue("scan_key_rule", rule)
                return rule
        return dataset.KEY_AUTO

    def _refresh_tags(self) -> None:
        for row_index in range(self._table.rowCount()):
            page_item = self._table.item(row_index, COL_PAGE)
            if page_item is None:
                continue
            page = int(page_item.data(Qt.ItemDataRole.UserRole))
            row = self._rows.get(page) or {}
            tag_item = self._table.item(row_index, COL_TAG)
            if tag_item is not None:
                tag_item.setText(row.get("tag", ""))
            note = row.get("error", "") or ""
            if row.get("fixed"):
                note = (note + " " if note else "") + "已自动修正"
            note_item = self._table.item(row_index, COL_NOTE)
            if note_item is not None:
                note_item.setText(note)

    # ------------------------------------------------------------ 扫描
    def start_scan(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            # 连点两次「开始识别」会起两个线程同时写同一张表，直接挡住
            self._status.setText("上一次识别还没结束，等它跑完或先点「停止」")
            return
        if textscan.ocr_available() is False:
            self._status.setText(f"无法识别：{textscan.ocr_error()}")
            return
        first = self._spin_from.value() - 1
        last = self._spin_to.value() - 1
        if last < first:
            first, last = last, first
        pages = list(range(first, last + 1))
        self._table.setRowCount(0)
        self._rows.clear()
        self._bar.setRange(0, len(pages))
        self._bar.setValue(0)
        self._started_at = time.perf_counter()
        self._status.setText(f"正在识别第 {first + 1}–{last + 1} 页（后台线程，界面不会卡）")
        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._btn_save.setEnabled(False)
        self._btn_bookmarks.setEnabled(False)

        worker = ScanWorker(self._path, self._region, pages, self)
        worker.progress.connect(self._on_progress)
        worker.rows.connect(self._on_rows)
        worker.failed.connect(self._on_failed)
        worker.finished_ok.connect(self._on_finished)
        self._worker = worker
        worker.start()

    def stop_scan(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._status.setText("正在停止…")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """关窗口前先把扫描线程停下来。

        不这么做的话，父对象析构时线程还在跑，Qt 会直接
        「QThread: Destroyed while thread is still running」把整个程序带走；
        切标签时主窗口也会主动 close 这个窗口，走的就是这条路。
        """
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                worker.stop()
                if not worker.wait(1500):
                    # 一页还没扫完：不能在这里干等，也不能让 QThread 在运行中被析构
                    # （Qt 会报 Destroyed while thread is still running 然后带走进程）。
                    # 留引用 + 等它的 finished 自己 deleteLater。
                    self._orphan_workers.append(worker)
                    worker.finished.connect(worker.deleteLater)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def _on_progress(self, done: int, total: int) -> None:
        self._bar.setMaximum(max(1, total))
        self._bar.setValue(done)
        elapsed = time.perf_counter() - self._started_at
        left = (elapsed / done) * (total - done) if done else 0.0
        got = sum(1 for r in self._rows.values() if r.get("tag"))
        self._status.setText(
            f"已识别 {done}/{total} 页 · 有结果 {got} 条 · 已用 {elapsed:.0f} 秒"
            f" · 预计还要 {left:.0f} 秒"
        )

    def _on_rows(self, batch: list[dict]) -> None:
        for row in batch:
            self._rows[int(row["page"])] = row
            self._append_row(row)

    def _append_row(self, row: dict) -> None:
        index = self._table.rowCount()
        self._table.insertRow(index)

        use_item = QTableWidgetItem()
        use_item.setFlags(
            Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
        )
        use_item.setCheckState(
            Qt.CheckState.Checked if row.get("use", True) else Qt.CheckState.Unchecked
        )
        self._table.setItem(index, COL_USE, use_item)

        page_item = QTableWidgetItem(str(int(row["page"]) + 1))
        page_item.setFlags(page_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        page_item.setData(Qt.ItemDataRole.UserRole, int(row["page"]))
        self._table.setItem(index, COL_PAGE, page_item)

        self._table.setItem(index, COL_TAG, QTableWidgetItem(row.get("tag", "")))

        # 只在"归一化之后"还留着的毛病上报警：`一` 这类早被 normalize_tag 改成 `-` 了，
        # 再提示一遍就是噪音（备注里已经写了"已自动修正"）。
        suspects = qc.find_suspects(row.get("tag") or "", self._rules)
        suspect_item = QTableWidgetItem(" · ".join(suspects))
        suspect_item.setFlags(suspect_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if suspects:
            suspect_item.setForeground(theme.qcolor(theme.DANGER))
            suspect_item.setToolTip("这些字符不像管线号该有的：右键可以忽略这一条或这一类")
        self._table.setItem(index, COL_SUSPECT, suspect_item)

        source = "文字层" if row.get("source") == "text" else "OCR"
        self._table.setItem(index, COL_SOURCE, QTableWidgetItem(source))
        note = row.get("error", "")
        if row.get("fixed"):
            note = (note + " " if note else "") + "已自动修正"
        self._table.setItem(index, COL_NOTE, QTableWidgetItem(note))

    # ------------------------------------------------------- 可疑字符/规则
    def _on_table_menu(self, pos) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        row_index = index.row()
        page_item = self._table.item(row_index, COL_PAGE)
        if page_item is None:
            return
        page = int(page_item.data(Qt.ItemDataRole.UserRole))
        row = self._rows.get(page, {})
        tag_text = row.get("tag") or ""
        suspects = qc.find_suspects(tag_text, self._rules)
        if not suspects:
            return
        menu = QMenu(self)
        menu.addAction("这一条不采用（不打钩）", lambda: self._set_use(row_index, False))
        for kind, label in qc.KIND_LABELS.items():
            if any(label in s for s in suspects) and kind not in self._rules["ignore_kinds"]:
                menu.addAction(
                    f"以后不再提示「{label}」这类",
                    lambda k=kind: self._ignore_kind(k),
                )
        for ch in qc.char_candidates(tag_text, self._rules)[:6]:
            menu.addAction(
                f"以后不再提示字符「{ch}」",
                lambda c=ch: self._ignore_char(c),
            )
        if self._rules["ignore_kinds"] or self._rules["ignore_chars"]:
            menu.addSeparator()
            menu.addAction("恢复全部提示", self._reset_rules)
        menu.exec(self._table.viewport().mapToGlobal(pos))

    def _save_rules(self) -> None:
        if self._project is not None:
            try:
                self._project.set_setting("ocr_rules", self._rules)
            except Exception:  # noqa: BLE001
                pass

    def _refresh_suspects(self) -> None:
        for row_index in range(self._table.rowCount()):
            page_item = self._table.item(row_index, COL_PAGE)
            if page_item is None:
                continue
            page = int(page_item.data(Qt.ItemDataRole.UserRole))
            tag = (self._rows.get(page, {}) or {}).get("tag") or ""
            suspects = qc.find_suspects(tag, self._rules)
            item = self._table.item(row_index, COL_SUSPECT)
            if item is None:
                continue
            item.setText(" · ".join(suspects))
            item.setForeground(theme.qcolor(theme.DANGER if suspects else theme.MUTED))

    def _ignore_kind(self, kind: str) -> None:
        if kind not in self._rules["ignore_kinds"]:
            self._rules["ignore_kinds"].append(kind)
        self._save_rules()
        self._refresh_suspects()

    def _ignore_char(self, ch: str) -> None:
        chars = self._rules["ignore_chars"]
        if ch not in chars:
            self._rules["ignore_chars"] = chars + ch
        self._save_rules()
        self._refresh_suspects()

    def _reset_rules(self) -> None:
        self._rules = qc.empty_rules()
        self._save_rules()
        self._refresh_suspects()

    def _set_use(self, row_index: int, on: bool) -> None:
        item = self._table.item(row_index, COL_USE)
        if item is not None:
            item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _on_failed(self, message: str) -> None:
        self._status.setText(f"识别失败：{message}")
        self._reset_buttons()

    def _on_finished(self, pages: int, hits: int, seconds: float) -> None:
        rate = pages / seconds if seconds > 0 else 0.0
        self._status.setText(
            f"识别完成：{pages} 页，{hits} 条有结果，用时 {seconds:.1f} 秒"
            f"（{rate:.0f} 页/秒）—— 可以直接改表格里的字"
        )
        self._reset_buttons()
        self._worker = None

    def _reset_buttons(self) -> None:
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        has = bool(self._rows)
        self._btn_save.setEnabled(has)
        self._btn_bookmarks.setEnabled(has)

    # ------------------------------------------------------------ 校对
    def _on_jump(self, item: QTableWidgetItem) -> None:
        page_item = self._table.item(item.row(), COL_PAGE)
        if page_item is None:
            return
        page = int(page_item.data(Qt.ItemDataRole.UserRole))
        window = self.parent()
        viewer = getattr(window, "viewer", None)
        if viewer is not None:
            viewer.goto_page(page, align_top=True)

    def collect_from_table(self) -> None:
        """把表格里手改过的字、以及"采用"勾选收回 ``_rows``。"""
        for index in range(self._table.rowCount()):
            page_item = self._table.item(index, COL_PAGE)
            tag_item = self._table.item(index, COL_TAG)
            if page_item is None or tag_item is None:
                continue
            page = int(page_item.data(Qt.ItemDataRole.UserRole))
            row = self._rows.setdefault(page, {"page": page, "raw": "", "source": ""})
            row["tag"] = textscan.normalize_tag(tag_item.text())
            use_item = self._table.item(index, COL_USE)
            row["use"] = bool(
                use_item is not None
                and use_item.checkState() == Qt.CheckState.Checked
            )

    # ------------------------------------------------------------ 落库
    def save_to_project(self) -> None:
        if self._project is None:
            return
        self.collect_from_table()
        now = time.time()
        rows = [
            {
                "page": int(page),
                "raw": info.get("raw", ""),
                "tag": info.get("tag", ""),
                "source": info.get("source", ""),
                "fixed": 1 if info.get("fixed") else 0,
                "updated_at": now,
            }
            for page, info in self._rows.items()
        ]
        count = self._project.save_scan_rows(rows)
        self._status.setText(f"已保存 {count} 条识别结果到项目文件")

    def make_bookmarks(self) -> None:
        self.collect_from_table()
        marks = build_bookmarks(self.tags())
        if not marks:
            self._status.setText("还没有可用的识别结果")
            return
        if self._project is not None:
            self._project.set_setting(
                "bookmarks", [m.to_dict() for m in marks]
            )
            self.save_to_project()
        self.bookmarks_ready.emit(marks)
        self._status.setText(f"已生成 {len(marks)} 条书签（导出 PDF 时会写进去）")

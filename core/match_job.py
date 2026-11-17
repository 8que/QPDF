"""数据匹配标注：后台把整份 PDF 的识别区域读一遍，边读边匹配（第 3、4 步）。

跟 ``core/scan_job.py`` 的三条约束完全一样，抄在这里当提醒：

1. 绝不跑在 UI 线程（单页 60~90 ms，两千多页就是好几分钟）；
2. 自己开一份 ``pymupdf.Document``（文档对象不是线程安全的）；
3. 结果分批回传（每 ``BATCH`` 页一批），别拿两千多次信号淹事件循环。

匹配在这一层就做掉：``RecordTable`` 建好之后是只读的，线程里查表很安全，
主线程拿到的已经是"这页该写什么"。用户改字段映射时不用重扫，直接拿
已经回来的 ``_scans`` 重新算一遍就行。
"""

from __future__ import annotations

import time

import pymupdf
from PySide6.QtCore import QThread, Signal

from core import auto_annotate, textscan
from core.dataset import RecordTable
from core.region import Region

#: 每回传这么多页
BATCH = 16


class MatchWorker(QThread):
    """识别 + 匹配的后台线程。

    信号：

    * ``progress(int done, int total)``
    * ``rows(object)`` —— 一批 ``PlanRow``
    * ``failed(str)``
    * ``finished_ok(int pages, int ready, int seconds)``
    """

    progress = Signal(int, int)
    rows = Signal(object)
    failed = Signal(str)
    finished_ok = Signal(int, int, float)

    def __init__(
        self,
        path: str,
        region: Region,
        records: RecordTable,
        pages=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._path = path
        self._region = region
        self._records = records
        self._pages = list(pages) if pages is not None else None
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:  # noqa: C901 - 一个循环，拆开更难读
        started = time.perf_counter()
        try:
            doc = pymupdf.open(self._path)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"打不开 PDF：{exc}")
            return
        try:
            total = doc.page_count
            pages = self._pages if self._pages is not None else range(total)
            pages = [p for p in pages if 0 <= p < total]
            if not pages:
                self.failed.emit("没有要识别的页")
                return
            count = len(pages)
            batch: list = []
            ready = 0
            for done, index in enumerate(pages, start=1):
                if self._stop:
                    break
                scan = self._scan_one(doc, index)
                row = auto_annotate.plan_from_scans([scan], self._records)[0]
                if row.ready:
                    ready += 1
                batch.append(row)
                if len(batch) >= BATCH:
                    self.rows.emit(batch)
                    batch = []
                    self.progress.emit(done, count)
            if batch:
                self.rows.emit(batch)
            self.progress.emit(min(done, count), count)
            self.finished_ok.emit(count, ready, time.perf_counter() - started)
        finally:
            doc.close()

    def _scan_one(self, doc, index: int) -> dict:
        """单页失败不该中断整批：把异常变成一行"未识别"+原因。"""
        try:
            result = textscan.scan_page(doc[index], self._region, index)
        except Exception as exc:  # noqa: BLE001
            return {
                "page": index,
                "raw": "",
                "tag": "",
                "source": "",
                "fixed": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
        tag = result.tag
        fixed_tag = textscan.repair_tag(tag) if tag else ""
        return {
            "page": index,
            "raw": result.compact,
            "tag": fixed_tag,
            "source": result.source,
            "fixed": bool(fixed_tag and fixed_tag != tag),
            "error": result.error,
        }

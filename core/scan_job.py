"""批量区域识别：在后台线程里把整份 PDF 的识别区域读一遍（Phase 10）。

三条硬约束，都是被真实文件逼出来的：

1. **绝不跑在 UI 线程**。单页 60~90 ms，2712 页就是三四分钟；放主线程软件直接僵死。
2. **自己开一份文档**。``pymupdf.Document`` 不是线程安全的，跟界面共用一份会崩；
   另开一份也顺手绕开了界面的页面缓存。
3. **结果分批回传**。2712 次信号会把事件循环淹掉，所以每 ``BATCH`` 页发一次。
"""

from __future__ import annotations

import time

import pymupdf
from PySide6.QtCore import QThread, Signal

from core import textscan
from core.region import Region

#: 每扫这么多页回传一次，界面不容易被信号淹掉
BATCH = 16


class ScanWorker(QThread):
    """后台扫描线程。

    信号：

    * ``progress(int done, int total)``
    * ``rows(object)`` —— 一批 ``[{page, raw, tag, source, fixed, error}]``
    * ``failed(str)``
    * ``finished_ok(int pages, int hits, float seconds)``
    """

    progress = Signal(int, int)
    rows = Signal(object)
    failed = Signal(str)
    finished_ok = Signal(int, int, float)

    def __init__(self, path: str, region: Region, pages=None, parent=None) -> None:
        super().__init__(parent)
        self._path = path
        self._region = region
        self._pages = list(pages) if pages is not None else None
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    # -- 线程体 ---------------------------------------------------------
    def run(self) -> None:  # noqa: C901 - 就是一个循环，拆开反而难读
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
            batch: list[dict] = []
            hits = 0
            for done, index in enumerate(pages, start=1):
                if self._stop:
                    break
                try:
                    result = textscan.scan_page(doc[index], self._region, index)
                except Exception as exc:  # noqa: BLE001 - 单页失败不该中断整批
                    result = None
                    row = {
                        "page": index,
                        "raw": "",
                        "tag": "",
                        "source": "",
                        "fixed": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                else:
                    raw = result.compact
                    tag = result.tag
                    fixed_tag = textscan.repair_tag(tag) if tag else ""
                    row = {
                        "page": index,
                        "raw": raw,
                        "tag": fixed_tag,
                        "source": result.source,
                        "fixed": bool(fixed_tag and fixed_tag != tag),
                        "error": result.error,
                    }
                    if fixed_tag:
                        hits += 1
                batch.append(row)
                if len(batch) >= BATCH:
                    self.rows.emit(batch)
                    batch = []
                    self.progress.emit(done, count)
            if batch:
                self.rows.emit(batch)
            self.progress.emit(min(done, count), count)
            self.finished_ok.emit(count, hits, time.perf_counter() - started)
        finally:
            doc.close()

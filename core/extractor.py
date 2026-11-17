"""F04：把挑出来的页面提取成一个新 PDF，标注一起带过去。

跟「导出 PDF」（``core/exporter.py``）是两件事：

- 导出 = 整份 PDF 原样复制 + 把标注写进去（增量追加，1.2 GB 只要 2 秒）；
- 提取 = **只保留挑出来的那几页**，所以必须重新组一份文档（``insert_pdf`` 逐页拷），
  没法增量，文件越大越慢。

两条共用的东西照抄过来而不是复制一份代码：标注框怎么算（``annot_rect``）、
旋转页怎么换算（``seen_to_pdf``）、注释怎么写（``_add_freetext``）全在 exporter 里。

安全流程跟第 11.5 节一致：先写临时文件 → 重开核对页数与注释数 → 通过了才改名，
失败就把临时文件删掉，原始 PDF 一个字节都不动。
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field

import pymupdf
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from core.exporter import (
    ExportItem,
    _add_freetext,
    _flatten,
    _same_file,
    build_plan,
    has_cjk,
)
from core.fonts import font_file_for

#: 提取前要求的额外磁盘余量（字节）
EXTRACT_HEADROOM = 24 * 1024 * 1024

#: 临时文件后缀——和导出区分开，方便排查
TEMP_SUFFIX = ".qpdf-extract-tmp"


class ExtractError(RuntimeError):
    """提取失败。消息直接给用户看。"""


class ExtractCancelled(Exception):
    """用户点了取消。"""


@dataclass
class ExtractPlan:
    """一次提取任务。``pages`` 是 0 起页码，**按输出顺序**排好。"""

    source: str
    target: str
    pages: list[int]
    items: list[ExportItem] = field(default_factory=list)
    toc: list[list] = field(default_factory=list)
    keep_annotations: bool = True
    #: 页码规格；提取时按**原始页号**写，不按输出顺序重排
    page_numbers: object | None = None
    #: 原始 PDF 总页数（页码模板里的 {total}/{总页数} 用它）
    source_page_count: int = 0


@dataclass
class ExtractResult:
    target: str
    page_count: int
    annot_count: int
    elapsed: float
    size: int


def build_extract_plan(
    source: str,
    target: str,
    pages: list[int],
    store,
    sequence=None,
    toc: list[list] | None = None,
    keep_annotations: bool = True,
    page_numbers=None,
    source_page_count: int = 0,
) -> ExtractPlan:
    """整理提取任务（主线程调用）。

    标注只带**输出里那几页**的；书签按新页号重排，指向不在输出里的页就丢掉。
    """
    pages = [int(page) for page in pages]
    wanted = set(pages)
    items: list[ExportItem] = []
    if keep_annotations:
        full = build_plan(source, target, 0, store, sequence)
        items = [item for item in full.items if item.page in wanted]
    return ExtractPlan(
        source=os.path.abspath(source),
        target=os.path.abspath(target),
        pages=pages,
        items=items,
        toc=remap_toc(toc or [], pages),
        keep_annotations=keep_annotations,
        page_numbers=page_numbers,
        source_page_count=int(source_page_count or 0),
    )


def remap_toc(toc: list[list], pages: list[int]) -> list[list]:
    """把原书的书签按 0 起页码搬到新页号上；不在输出里的直接丢掉。"""
    index_of = {page: position for position, page in enumerate(pages)}
    result: list[list] = []
    for entry in toc:
        if len(entry) < 3:
            continue
        level, title, page_no = entry[0], entry[1], int(entry[2]) - 1
        position = index_of.get(page_no)
        if position is None:
            continue
        result.append([int(level), str(title), position + 1])
    return result


def _disk_check(target_dir: str, source: str, estimated: int) -> None:
    try:
        free = shutil.disk_usage(target_dir).free
    except OSError as exc:
        raise ExtractError(f"看不到目标磁盘的可用空间：{exc}") from exc
    need = max(int(estimated), 4 * 1024 * 1024) + EXTRACT_HEADROOM
    if free < need:
        raise ExtractError(
            f"目标磁盘可用 {free / 1048576:.0f} MB，这次大约要 {need / 1048576:.0f} MB，先腾点地方"
        )


def estimate_size(source: str, pages: list[int], page_count: int) -> int:
    """粗估输出大小：按页数比例算，再乘 1.6 的余量。算不准也不要紧，只是给磁盘检查用。"""
    try:
        total = os.path.getsize(source)
    except OSError:
        total = 0
    if not page_count:
        return total
    return int(total / max(1, page_count) * max(1, len(pages)) * 1.6)


def _assemble(plan: ExtractPlan, report, is_cancelled) -> tuple[pymupdf.Document, dict[int, int]]:
    """逐页拷进新文档，返回 (新文档, 原页号→新页号)。"""
    try:
        source = pymupdf.open(plan.source)
    except Exception as exc:  # pragma: no cover - 打不开源文件
        raise ExtractError(f"打不开原始 PDF：{exc}") from exc

    out = pymupdf.open()
    mapping: dict[int, int] = {}
    total = len(plan.pages)
    try:
        for position, page_no in enumerate(plan.pages):
            if is_cancelled():
                raise ExtractCancelled()
            if page_no < 0 or page_no >= source.page_count:
                raise ExtractError(f"第 {page_no + 1} 页不存在（共 {source.page_count} 页）")
            out.insert_pdf(source, from_page=page_no, to_page=page_no)
            mapping[page_no] = position
            report(position + 1, total, f"复制第 {page_no + 1} 页")
    finally:
        source.close()

    if out.page_count != total:
        out.close()
        raise ExtractError(f"拷完页数不对（要 {total} 页，实际 {out.page_count} 页）")
    return out, mapping


def _write_annotations(out: pymupdf.Document, plan: ExtractPlan, mapping: dict[int, int],
                       report, is_cancelled, total_pages: int) -> tuple[int, dict[int, int], dict[int, int]]:
    """写标注，返回 ``(写入总条数, 每页原有注释数, 每页本次写入数)``。

    后两项给写后校验当基线：``insert_pdf`` 会把源页面上**原有的**批注一起拷过来，
    所以只能按"本次多出来的条数"核对，不能拿全页总数去比。
    """
    per_page = [mapping[item.page] for item in plan.items if item.page in mapping]
    baseline = {position: sum(1 for _ in out[position].annots()) for position in set(per_page)}

    written = 0
    written_by_position: dict[int, int] = {}
    for item in plan.items:
        if is_cancelled():
            raise ExtractCancelled()
        position = mapping.get(item.page)
        if position is None:
            continue
        _add_freetext(out[position], item)
        written += 1
        written_by_position[position] = written_by_position.get(position, 0) + 1
    report(total_pages, total_pages, f"写入 {written} 条标注")
    return written, baseline, written_by_position


def _write_page_numbers(out: pymupdf.Document, plan: ExtractPlan,
                        report, is_cancelled) -> int:
    """把页码写进提取出来的页面。

    页码用**原始页号**：从 2712 页里挑第 301、305 页，输出里仍是「第 301 页 / 第 305 页」，
    不按 1、2 重排——施工图上页码是图号的一部分，重排会认错图。
    """
    spec = plan.page_numbers
    if spec is None or not getattr(spec, "enabled", True):
        return 0
    style = spec.style
    total = int(plan.source_page_count or out.page_count or 1)
    written = 0
    for position, original_page in enumerate(plan.pages):
        if is_cancelled():
            raise ExtractCancelled()
        if position >= out.page_count:
            break
        text = spec.text_for(int(original_page) + 1, total)
        if not text:
            continue
        page = out[position]
        x0, y0, _x1, _y1 = spec.region.to_pdf(page.rect.width, page.rect.height)
        item = ExportItem(
            page=position,
            x=float(x0),
            y=float(y0),
            text=text,
            font_size=float(style.font_size),
            color=style.color,
            align=style.align,
            bold=bool(style.bold),
            italic=bool(style.italic),
            opacity=int(style.opacity),
            kind="page_number",
            font_family=style.font_family,
            font_file=font_file_for(style.font_family, bool(style.bold))
            if has_cjk(text)
            else "",
        )
        _flatten(page, item)
        written += 1
        report(len(plan.pages), len(plan.pages), f"正在写第 {original_page + 1} 页页码")
    return written


def _verify(path: str, plan: ExtractPlan, written: int, baseline: dict[int, int],
            written_by_position: dict[int, int]) -> None:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        raise ExtractError(f"提取出来的文件打不开：{exc}") from exc
    try:
        if doc.page_count != len(plan.pages):
            raise ExtractError(
                f"校验没过：要 {len(plan.pages)} 页，文件里是 {doc.page_count} 页"
            )
        if doc.needs_pass:
            raise ExtractError("校验没过：输出文件被加密了")
        for position in sorted(written_by_position):
            existing = baseline.get(position, 0)
            found = sum(1 for _ in doc[position].annots())
            if found - existing != written_by_position[position]:
                raise ExtractError(
                    f"校验没过：第 {position + 1} 页应新增 {written_by_position[position]} 条注释，"
                    f"实际新增 {found - existing} 条（共写入 {written} 条）"
                )
        if plan.toc and len(doc.get_toc()) != len(plan.toc):
            raise ExtractError(
                f"校验没过：应有 {len(plan.toc)} 条书签，实际 {len(doc.get_toc())} 条"
            )
    finally:
        doc.close()


def _cleanup(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def extract_pdf(plan: ExtractPlan, progress=None, cancelled=None) -> ExtractResult:
    """按计划提取页面。返回结果；失败抛 :class:`ExtractError`。"""
    started = time.perf_counter()

    def report(done: int, total: int, message: str) -> None:
        if progress is not None:
            progress(done, total, message)

    def is_cancelled() -> bool:
        return bool(cancelled and cancelled())

    source = os.path.abspath(plan.source)
    target = os.path.abspath(plan.target)
    if not os.path.isfile(source):
        raise ExtractError("原始 PDF 不见了")
    if source == target or _same_file(source, target):
        raise ExtractError("输出文件不能跟原始 PDF 同名（原文件永远不动）")
    if not plan.pages:
        raise ExtractError("没有挑任何页面")

    target_dir = os.path.dirname(target) or "."
    os.makedirs(target_dir, exist_ok=True)
    _disk_check(target_dir, source, estimate_size(source, plan.pages, 0))

    temp = target + TEMP_SUFFIX
    _cleanup(temp)

    out = None
    try:
        out, mapping = _assemble(plan, report, is_cancelled)
        written, baseline, written_by_position = _write_annotations(
            out, plan, mapping, report, is_cancelled, len(plan.pages)
        )
        page_number_count = _write_page_numbers(out, plan, report, is_cancelled)
        if plan.toc:
            out.set_toc(plan.toc)
        if page_number_count:
            # 嵌入的是整份 Windows 字体，子集化后通常只剩几 KB
            try:
                out.subset_fonts()
            except Exception:  # noqa: BLE001 - 字体子集化失败只影响体积，不影响内容
                pass
        report(len(plan.pages), len(plan.pages), "正在写入文件…")
        out.save(temp, garbage=3, deflate=True)
        out.close()
        out = None
        _verify(temp, plan, written, baseline, written_by_position)
        os.replace(temp, target)
    except ExtractCancelled:
        _cleanup(temp)
        raise
    except ExtractError:
        _cleanup(temp)
        raise
    except Exception as exc:
        _cleanup(temp)
        raise ExtractError(f"提取失败：{exc}") from exc
    finally:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass

    return ExtractResult(
        target=target,
        page_count=len(plan.pages),
        annot_count=len(plan.items),
        elapsed=time.perf_counter() - started,
        size=os.path.getsize(target) if os.path.isfile(target) else 0,
    )


class _ExtractSignals(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()


class _ExtractWorker(QRunnable):
    """后台跑提取。MuPDF 全程持 GIL，所以只开一个线程，别让主界面卡死就行。"""

    def __init__(self, plan: ExtractPlan) -> None:
        super().__init__()
        self.plan = plan
        self.signals = _ExtractSignals()
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D401
        try:
            result = extract_pdf(
                self.plan,
                progress=lambda done, total, message: self.signals.progress.emit(done, total, message),
                cancelled=lambda: self._cancelled,
            )
        except ExtractCancelled:
            self.signals.cancelled.emit()
        except ExtractError as exc:
            self.signals.failed.emit(str(exc))
        except Exception as exc:  # pragma: no cover - 兜底
            self.signals.failed.emit(f"提取失败：{exc}")
        else:
            self.signals.finished.emit(result)


class ExtractManager(QObject):
    """把提取任务丢到后台线程，界面只看信号。"""

    started = Signal()
    progress = Signal(int, int, str)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._worker: _ExtractWorker | None = None

    @property
    def is_busy(self) -> bool:
        return self._worker is not None

    def start(self, plan: ExtractPlan) -> bool:
        if self.is_busy:
            return False
        worker = _ExtractWorker(plan)
        worker.signals.progress.connect(self.progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        self._worker = worker
        self.started.emit()
        self._pool.start(worker)
        return True

    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

    def shutdown(self) -> None:
        self.cancel()
        self._pool.waitForDone(3000)

    def _on_finished(self, result) -> None:
        self._worker = None
        self.finished.emit(result)

    def _on_failed(self, message: str) -> None:
        self._worker = None
        self.failed.emit(message)

    def _on_cancelled(self) -> None:
        self._worker = None
        self.cancelled.emit()

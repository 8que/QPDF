"""页面瓦片缓存与后台渲染调度。

为什么是"瓦片"而不是"整页图片"
--------------------------------
CAD 工程图纸单页常有上万条矢量路径。整页按当前缩放渲染，在 300% 下等于把
一条路径重算一遍再放大，低配机（N5095A）会直接卡住；而且整页位图在 8 倍
缩放下是 GB 级内存。

这里把页面切成**固定设备像素**的瓦片（``TILE_PX = 1536`` px 见方）：
- 内存占用与缩放比例无关，一个瓦片恒定 ≈ 1536×1536×4 ≈ 9 MB；
- 只渲染视口内可见的瓦片；
- 缩放在量化档位内变化时直接复用已有瓦片，不重新出图。

注意 ``TILE_PX`` 比常见视口还大：A3 图纸在 100% 上下时一页往往只有一两块瓦片，
等效于"整页一图"；只有放大到多屏尺寸才会真的切成多块。

缩放档位
--------
以 1.25 为公比做量化：``bucket_zoom(i) = 1.25 ** i``。相邻档位之间差异最大
12%，把低档位瓦片拉伸上去几乎看不出来，所以拖动缩放时能立即出图，等停稳后
再按新档位补渲染。
"""

from __future__ import annotations

import math
from collections import OrderedDict

from PySide6.QtCore import QObject, QRectF, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QImage, QPixmap

#: 单个瓦片的设备像素边长（正方形）。调大是为了减少"每页十几次 get_pixmap"
#: 的调用次数——每调用一次 MuPDF 都要重放一遍页面指令流，大图页开销最明显。
TILE_PX = 1536
#: 整页预览图的宽度（设备像素）。翻页/缩放时先顶上，避免白屏干等瓦片。
#: 800 是"看得清标题栏和编号"的下限，再大就要多花几十毫秒出图。
PREVIEW_WIDTH = 800
#: 缩放档位公比
ZOOM_STEP = 1.25
#: 瓦片缓存上限（字节）。用户明确要求"优先流畅"，所以给得比省内存时大方：
#: 512 MB 在 16 GB 机器上很克制，但足够把翻过的几页都留在内存里。
MEMORY_BUDGET = 512 * 1024 * 1024
#: 整页预览图单独一份配额（往前翻一页要立刻有画面，不能跟瓦片抢）。
#: 注意它跟 ``set_budget_mb()`` 里的比例要一致：默认 512 → 320（5/8），
#: 否则用户在设置里把缓存点成同一个 512，预览配额会悄悄从 320 变成 256。
PREVIEW_BUDGET = 320 * 1024 * 1024
#: 后台渲染线程数。**默认 1**：``core/pdf_engine.py`` 用一个 RLock 把整次
#: ``render()`` 串起来（MuPDF 的 Document 不是线程安全的），所以多线程并不能
#: 并行出图，只会多出排队和抢锁的开销——实测 4 线程比串行还慢 10%~40%。
#: 真要并行，得让每个线程持有自己的 ``pymupdf.Document``；在那之前，调大这个
#: 数字只影响"谁先排到"，不影响总吞吐。设置面板仍允许 1~8。
MAX_RENDER_THREADS = 1

_LOG_STEP = math.log(ZOOM_STEP)


def bucket_index(zoom: float) -> int:
    """把连续缩放映射到量化档位序号。"""
    if zoom <= 0.0:
        return 0
    return int(round(math.log(zoom) / _LOG_STEP))


def bucket_zoom(index: int) -> float:
    """档位序号还原为缩放比例。"""
    return ZOOM_STEP**index


def tile_span(scene_scale: float) -> float:
    """一个瓦片在页面坐标系里覆盖的边长（pt）。"""
    if scene_scale <= 0.0:
        return 1.0
    return TILE_PX / scene_scale


def page_key(page: int, scale: float) -> tuple:
    """整页渲染的缓存键（工作台卡片、整页预览图用）。

    第 0 位必须是页号——渲染任务统一用 ``key[0]`` 取页，不能和瓦片键
    ``(页号, 缩放比例, 列, 行)`` 撞上，所以第 1 位放一个 "whole" 当标记。
    缩放取 3 位小数，免得浮点抖动把缓存打散。
    """
    return (int(page), "whole", round(float(scale), 3))


class _WorkerSignals(QObject):
    """QRunnable 不能继承 QObject，信号单独挂一个对象。"""

    finished = Signal(object)


class _TileJob(QRunnable):
    __slots__ = ("_doc", "_epoch", "_key", "_rect", "_scale", "_signals", "_is_stale", "_allow")

    def __init__(self, doc, epoch, key, rect, scale, signals, is_stale, allow):
        super().__init__()
        self._doc = doc
        self._epoch = epoch
        self._key = key
        self._rect = rect
        self._scale = scale
        self._signals = signals
        self._is_stale = is_stale
        self._allow = allow
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - 在工作线程中执行
        if self._is_stale(self._epoch):
            # 必须回报「这张作废了」：调用方拿 (epoch, key) 清 pending，
            # 不回报的话同一个 key 会永远卡在 pending 里，之后每次绘制都以为
            # "正在渲染"，结果既不渲染也不重试 —— 画面就一直糊着/空着。
            self._signals.finished.emit((self._key, self._epoch, None))
            return
        # 用户已经翻走了：别浪费一次几十毫秒的渲染（渲染期间主线程是被 GIL 卡住的）
        if not self._allow(self._key[0]):
            self._signals.finished.emit((self._key, self._epoch, None))
            return
        image = None
        try:
            image = self._doc.render(self._key[0], self._rect, self._scale)
        except Exception:  # noqa: BLE001
            image = None
        # 即使失败也要回报，否则 key 会永远卡在 pending 里
        self._signals.finished.emit((self._key, self._epoch, image))


class RenderManager(QObject):
    """瓦片缓存 + 线程池调度器。全部方法都在 GUI 线程调用。"""

    tile_ready = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._doc = None
        self._epoch = 0
        self._cache: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        # 预算走实例（设置面板可改），默认值仍是模块常量
        self._memory_budget = MEMORY_BUDGET
        self._preview_budget = PREVIEW_BUDGET
        self._bytes = 0
        self._previews: "OrderedDict[tuple, QPixmap]" = OrderedDict()
        self._preview_bytes = 0
        #: 正在渲染的 (epoch, key)；按 epoch 记账，清缓存后旧任务不会堵住新请求
        self._pending: set[tuple] = set()
        self._focus: tuple[int, int] = (-1, -1)
        # 工作台一次要看很多页，用页号集合；两者取或
        self._focus_pages: set[int] = set()
        self._signals = _WorkerSignals()
        self._signals.finished.connect(self._on_worker_done)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(MAX_RENDER_THREADS)

    # ------------------------------------------------------------------ 文档

    def set_document(self, doc) -> None:
        self._epoch += 1
        self._doc = doc
        self._reset()

    def invalidate(self) -> None:
        """丢弃全部缓存（例如页面旋转后）。"""
        self._epoch += 1
        self._reset()

    @property
    def document(self):
        """当前渲染的是哪份 PDF（没绑定时是 None，此时所有请求都会被丢掉）。"""
        return self._doc

    def trim_tiles(self, keep_mb: int = 32) -> None:
        """只放掉高清瓦片，整页预览图留着（切标签时用：回来先有画面）。"""
        budget = max(0, int(keep_mb)) * 1024 * 1024
        while self._bytes > budget and self._cache:
            _, victim = self._cache.popitem(last=False)
            self._bytes -= victim.width() * victim.height() * 4
        self.tile_ready.emit()

    def _reset(self) -> None:
        self._cache.clear()
        self._bytes = 0
        self._previews.clear()
        self._preview_bytes = 0
        self._pending.clear()

    # ------------------------------------------------------------------ 查询

    def lookup(self, key: tuple) -> QPixmap | None:
        pixmap = self._previews.get(key)
        if pixmap is not None:
            self._previews.move_to_end(key)
            return pixmap
        pixmap = self._cache.get(key)
        if pixmap is not None:
            self._cache.move_to_end(key)
        return pixmap

    def contains(self, key: tuple) -> bool:
        return key in self._cache or key in self._previews

    @property
    def cache_bytes(self) -> int:
        return self._bytes + self._preview_bytes

    @property
    def tile_bytes(self) -> int:
        """只算高清瓦片（预览图另算，切标签时留的是它）。"""
        return self._bytes

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------ 请求

    def request(self, key: tuple, rect: QRectF, scale: float, priority: int = 0) -> None:
        if self._doc is None or key in self._cache:
            return
        # pending 按 (epoch, key) 记账：清缓存后旧任务回来只会销掉它自己那一条，
        # 不会把新 epoch 里同一个 key 的请求一起"销"掉或永远堵住。
        token = (self._epoch, key)
        if token in self._pending:
            return
        self._pending.add(token)
        job = _TileJob(
            self._doc,
            self._epoch,
            key,
            QRectF(rect),
            scale,
            self._signals,
            self._is_stale,
            self._page_allowed,
        )
        self._pool.start(job, int(priority))

    def _page_allowed(self, page: int) -> bool:
        lo, hi = self._focus
        if lo >= 0 and lo <= page <= hi:
            return True
        if page in self._focus_pages:
            return True
        return lo < 0 and not self._focus_pages

    def set_focus_pages(self, pages) -> None:
        """工作台：当前这张工作台上的页都算"在看"，别把它们的渲染任务作废掉。

        （前几版只按单页视野过滤，结果工作台里除当前页以外的卡片永远停在"渲染中"。）
        """
        self._focus_pages = {int(p) for p in pages}

    def clear_focus_pages(self) -> None:
        self._focus_pages = set()

    def drop_previews(self) -> int:
        """丢掉整页预览图缓存，返回释放的字节数。

        工作台的整页图都进这里，一张 A1 在 1.35 倍下就是 29 MB；
        离开工作台回到单页视图时必须还回去，否则瓦片缓存被挤掉、
        翻页重新渲染一遍，看起来就像"缓存没释放"。
        """
        freed = self._preview_bytes
        self._previews.clear()
        self._preview_bytes = 0
        return freed

    def set_focus(self, first: int, last: int) -> None:
        """告诉调度器现在用户在看哪几页；范围外的排队任务直接作废。

        MuPDF 渲染时一直握着 GIL，一个几十毫秒的渲染就是一次主线程停顿，
        所以"翻走了的页"必须尽快放弃，不能让它把当前页的预览堵在后面。
        """
        self._focus = (int(first), int(last))

    def page_key(self, page: int, scale: float) -> tuple:
        """整页渲染的缓存键（工作台卡片、整页预览图用）。见模块级同名函数。"""
        return page_key(page, scale)

    def request_page(self, page: int, scale: float, priority: int = 0) -> tuple:
        """请求把整页渲染成一张图，返回它的 key；拿到后 ``lookup(key)`` 取 QPixmap。"""
        key = self.page_key(page, scale)
        if self._doc is None:
            return key
        size = self._doc.page_size(page)
        rect = QRectF(0.0, 0.0, size.width, size.height)
        self.request(key, rect, float(scale), priority)
        return key

    def cached_preview(self, page: int, scale: float) -> QPixmap | None:
        """整页预览图缓存里有没有这一页（缩略图可以拿它缩，省一次整页解析）。"""
        return self.lookup(self.page_key(page, scale))

    def _is_stale(self, epoch: int) -> bool:
        return epoch != self._epoch

    # ------------------------------------------------------------------ 回调

    def _on_worker_done(self, payload) -> None:
        key, epoch, image = payload
        self._pending.discard((epoch, key))
        if epoch != self._epoch or image is None:
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self._put(key, pixmap)
        self.tile_ready.emit()

    def _put(self, key: tuple, pixmap: QPixmap) -> None:
        size = pixmap.width() * pixmap.height() * 4
        # 整页预览图单独留一份配额：它只有一两兆，但"往前翻一页"全靠它，
        # 跟几十兆的瓦片挤一个 LRU 的话翻回去又得重渲一次。
        if len(key) > 1 and key[1] == "whole":
            old = self._previews.pop(key, None)
            if old is not None:
                self._preview_bytes -= old.width() * old.height() * 4
            self._previews[key] = pixmap
            self._preview_bytes += size
            while self._preview_bytes > self._preview_budget and len(self._previews) > 1:
                _, victim = self._previews.popitem(last=False)
                self._preview_bytes -= victim.width() * victim.height() * 4
            return
        old = self._cache.pop(key, None)
        if old is not None:
            self._bytes -= old.width() * old.height() * 4
        self._cache[key] = pixmap
        self._bytes += size
        while self._bytes > self._memory_budget and len(self._cache) > 1:
            _, victim = self._cache.popitem(last=False)
            self._bytes -= victim.width() * victim.height() * 4

    # -- 预算（设置面板可调）------------------------------------------------
    @property
    def budget_mb(self) -> int:
        return int(self._memory_budget // (1024 * 1024))

    @property
    def preview_budget_mb(self) -> int:
        """预览图单独那份配额（翻页/切标签时先拿它顶上）。"""
        return int(self._preview_budget // (1024 * 1024))

    @property
    def threads(self) -> int:
        """同时跑几个渲染线程（设置面板可调）。"""
        return int(self._pool.maxThreadCount())

    def set_threads(self, count: int) -> None:
        """改渲染线程数：1~8。

        **调大不会更快**：``core/pdf_engine.py`` 用一把 RLock 把整次 render()
        串起来（MuPDF 的 Document 不是线程安全的），所以同一时刻只有一个线程
        真的在出图，多出来的只是排队和抢锁。这里保留 1~8 是给"以后改成每线程
        各持一份 Document"留的开关，别指望它加速。
        """
        try:
            value = int(count)
        except (TypeError, ValueError):
            return
        self._pool.setMaxThreadCount(max(1, min(8, value)))

    def set_budget_mb(self, mb: int) -> None:
        """改缓存上限；调小之后立刻按新上限淘汰，不用等下次渲染。

        预览配额固定按 5/8 折算（默认 512 → 320），和模块级 ``PREVIEW_BUDGET``
        保持一致——以前写的 ``mb // 2`` 会让"把缓存改成 512"顺手把预览从 320
        降到 256，用户完全看不见这个副作用。
        """
        mb = max(64, min(2048, int(mb)))
        self._memory_budget = mb * 1024 * 1024
        self._preview_budget = max(64, mb * 5 // 8) * 1024 * 1024
        while self._bytes > self._memory_budget and len(self._cache) > 1:
            _, victim = self._cache.popitem(last=False)
            self._bytes -= victim.width() * victim.height() * 4
        while self._preview_bytes > self._preview_budget and len(self._previews) > 1:
            _, victim = self._previews.popitem(last=False)
            self._preview_bytes -= victim.width() * victim.height() * 4
        self.tile_ready.emit()

    def shutdown(self) -> None:
        self._epoch += 1
        self._pool.clear()
        self._pool.waitForDone(2000)
        self._reset()
        self._doc = None

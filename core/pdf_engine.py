"""PDF 引擎封装。

设计要点
--------
1. PyMuPDF 的 ``Document`` 对象**不是线程安全的**。渲染线程、缩略图线程、
   主线程都可能访问它，因此所有访问统一收敛到 ``PdfDocument._lock`` 上。
2. 大文件（如 1.3GB / 2700 页）必须**延迟加载**：``pymupdf.open`` 本身是
   惰性的，只读 xref 表；页面内容要到 ``load_page`` 才解析。这里不缓存
   页面对象，只缓存页面尺寸。
3. 渲染一律走 ``get_pixmap(clip=...)``，只出需要的区域，绝不整页转图。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass

try:  # PyMuPDF >= 1.24 提供 pymupdf 包名
    import pymupdf
except ImportError:  # pragma: no cover - 老版本兜底
    import fitz as pymupdf  # type: ignore

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage


#: ``close()`` 最多等这么久拿锁（秒）。等不到就交给渲染线程收尾——
#: 关标签、关程序都在主线程上跑，在这里干等就是肉眼可见的假死。
CLOSE_LOCK_TIMEOUT = 0.2


class PdfEngineError(RuntimeError):
    """PDF 打开或渲染失败。"""


@dataclass(frozen=True)
class PageSize:
    """页面尺寸，单位 PDF point（1pt = 1/72 英寸）。"""

    width: float
    height: float

    def to_rect(self) -> QRectF:
        return QRectF(0.0, 0.0, self.width, self.height)


class PdfDocument:
    """线程安全的 PDF 文档包装。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._lock = threading.RLock()
        self._sizes: dict[int, PageSize] = {}
        self._doc = None
        #: close() 抢不到锁时置上，由渲染线程在下次退出临界区时补关
        self._close_pending = False

        if not os.path.isfile(self.path):
            raise PdfEngineError(f"文件不存在：{self.path}")

        try:
            doc = pymupdf.open(self.path)
        except Exception as exc:  # noqa: BLE001 - 统一转成业务异常
            raise PdfEngineError(f"无法打开 PDF：{exc}") from exc

        if getattr(doc, "needs_pass", False):
            doc.close()
            raise PdfEngineError("该 PDF 已加密，当前版本暂不支持")

        if doc.page_count <= 0:
            doc.close()
            raise PdfEngineError("该 PDF 没有可用页面")
        if getattr(doc, "is_repaired", False):
            # 文件结构有损坏，PyMuPDF 已尝试修复。这里只做记录，
            # 真正需要提示用户是在导出阶段，避免误写坏原文件。
            pass

        self._doc = doc
        self.page_count: int = doc.page_count
        self.is_encrypted: bool = bool(getattr(doc, "is_encrypted", False))
        self.file_size: int = os.path.getsize(self.path)

    # ------------------------------------------------------------------ 元信息

    def page_size(self, index: int) -> PageSize:
        with self._lock:
            size = self._sizes.get(index)
            if size is None:
                doc = self._require_doc()
                page = doc.load_page(index)
                rect = page.rect
                size = PageSize(float(rect.width), float(rect.height))
                self._sizes[index] = size
            return size

    def aspect_ratio(self, index: int) -> float:
        size = self.page_size(index)
        return size.width / size.height if size.height else 1.0

    def outline(self) -> list:
        """PDF 自带的目录（``[[level, title, page1based], …]``）。读不出来就当没有。"""
        with self._lock:
            doc = self._require_doc()
            try:
                return list(doc.get_toc(simple=True) or [])
            except Exception:  # noqa: BLE001
                return []

    # -------------------------------------------------------------------- 渲染

    def render(self, index: int, clip: QRectF, scale: float) -> QImage | None:
        """渲染页面局部区域。

        Parameters
        ----------
        index:
            0 基页号。
        clip:
            页面坐标系下的矩形（单位 pt），会被裁剪到页面范围内。
        scale:
            每 pt 对应的输出像素数（已包含设备像素比与缩放比例）。
        """
        if scale <= 0.0 or clip.isEmpty():
            return None

        with self._lock:
            # 无论这次渲染成没成功，都要看一眼「有没有人等我把文档关掉」：
            # 关标签时 close() 抢不到锁只会留个标记，这里是最后一个还握着锁、
            # 还可能碰 doc 的地方，离开临界区之前把它关掉。
            try:
                return self._render_locked(index, clip, scale)
            finally:
                self._close_if_ready()

    def _render_locked(self, index: int, clip: QRectF, scale: float) -> QImage | None:
        """真正出图（调用方必须已经持有 ``self._lock``）。"""
        doc = self._doc
        if doc is None:
            return None
        try:
            page = doc.load_page(index)
            prect = page.rect
            x0 = max(clip.left(), float(prect.x0))
            y0 = max(clip.top(), float(prect.y0))
            x1 = min(clip.right(), float(prect.x1))
            y1 = min(clip.bottom(), float(prect.y1))
            if (x1 - x0) < 0.5 or (y1 - y0) < 0.5:
                return None

            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                clip=pymupdf.Rect(x0, y0, x1, y1),
                alpha=False,
                colorspace=pymupdf.csRGB,
            )
            if pix.width <= 0 or pix.height <= 0:
                return None
            # QImage 不持有 samples 的内存，必须 copy 一份出来，
            # 否则 pix 被回收后就是脏数据。
            return QImage(
                bytes(pix.samples),
                pix.width,
                pix.height,
                pix.stride,
                QImage.Format.Format_RGB888,
            ).copy()
        except Exception:  # noqa: BLE001 - 单页渲染失败不应拖垮整个界面
            return None

    # -------------------------------------------------------------------- 生命周期

    def close(self) -> None:
        """关闭文档。**不能卡界面**。

        渲染线程出图时会一直握着 ``self._lock``（一次几十毫秒，大图页更久），
        而关标签/退出时主线程如果在这里干等锁，表现就是"点了叉，程序像死了一样
        等好几秒"。所以：

        - 抢得到锁（通常是没人在渲染）→ 立刻关；
        - 抢不到 → 打个标记就走，等那一次渲染结束时由渲染线程收尾
          （``_close_if_ready``）。此后的渲染请求一律返回 None，不会再用到旧 doc。
        """
        if not self._lock.acquire(timeout=CLOSE_LOCK_TIMEOUT):
            self._close_pending = True
            return
        try:
            self._close_locked()
        finally:
            self._lock.release()

    def _close_locked(self) -> None:
        """真正关掉（调用方必须已经持有锁）。"""
        self._close_pending = False
        if self._doc is not None:
            try:
                self._doc.close()
            except Exception:  # noqa: BLE001
                pass
            self._doc = None
        self._sizes.clear()

    def _close_if_ready(self) -> None:
        """渲染线程收尾用：调用方正握着锁，把"关了一半"的关闭做完。"""
        if self._close_pending:
            self._close_locked()

    def _require_doc(self):
        if self._doc is None:
            raise PdfEngineError("文档已关闭")
        return self._doc

    def __enter__(self) -> "PdfDocument":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

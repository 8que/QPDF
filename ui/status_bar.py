"""底部状态栏。

除了常规的页码/缩放/模式，额外显示**进程内存占用**和**瓦片缓存占用**。
第 12.3 节要求"建议增加软件自身内存占用显示，便于性能测试"——这两项
是判断 8GB 机器上是否会失控的直接依据。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QStatusBar

from core.sysmon import SystemMonitor
from ui import theme


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]  # x64 下 sizeof == 72


_psapi = None
if hasattr(ctypes, "WinDLL"):
    try:
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
        _psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        _psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    except OSError:  # pragma: no cover - 正常 Windows 上不会发生
        _psapi = None


def process_memory_mb() -> float:
    """当前进程工作集（MB）。非 Windows 或调用失败时返回 0。"""
    if _psapi is None:
        return 0.0
    try:
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        handle = _kernel32.GetCurrentProcess()
        if not _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return 0.0
        return counters.WorkingSetSize / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return 0.0


def _fmt_mb(mb: float) -> str:
    if mb <= 0:
        return "—"
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.0f} MB"


class StatusBar(QStatusBar):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizeGripEnabled(True)
        self.setContentsMargins(6, 0, 8, 0)

        self._page = QLabel("— / —")
        self._zoom = QLabel("100%")
        self._mode = QLabel("浏览")
        self._save = QLabel("未保存")
        self._cache = QLabel("缓存 0 MB")
        self._memory = QLabel("本程序 —")
        self._system = QLabel("CPU — · 内存 —")

        self._page.setObjectName("statusPrimary")
        self._mode.setObjectName("modeChip")
        self._cache.setObjectName("statusDim")
        self._memory.setObjectName("statusDim")
        self._save.setObjectName("statusDim")
        self._system.setObjectName("statusDim")
        self._system.setToolTip("整机 CPU 使用率与物理内存占用（不是本程序）")

        # 等宽数字：页码/缩放/内存每 1.5 秒跳一次，字宽不一就会左右抽动
        for label in (self._page, self._zoom, self._memory, self._system):
            theme.tabular_font(label)

        #: 导出进度条（第 11.4 节：导出在后台跑，这里给进度，平时藏着）
        self._export = QProgressBar()
        self._export.setRange(0, 100)
        self._export.setValue(0)
        self._export.setMinimumWidth(160)
        self._export.setMaximumWidth(220)
        self._export.setTextVisible(True)
        self._export.setFormat("%p%")
        self._export.hide()
        self.addPermanentWidget(self._export)

        # 左边是「图纸在哪儿」——主读数；右边是「机器怎么样」——次要读数。
        # 以前七块等宽小字全右对齐，一排放满，谁都不突出。
        left = [(self._page, 92), (self._zoom, 52), (self._mode, 0)]
        for index, (widget, width) in enumerate(left):
            if index:
                self.addWidget(self._separator())
            if width:
                widget.setMinimumWidth(width)
            self.addWidget(widget)

        right = [
            (self._save, 78),
            (self._cache, 92),
            (self._memory, 100),
            (self._system, 0),
        ]
        for widget, width in right:
            if width:
                widget.setMinimumWidth(width)
            widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.addPermanentWidget(widget)

        #: 整机 CPU / 内存采样器（见 core/sysmon.py）
        self._monitor = SystemMonitor()

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._refresh_memory)
        self._timer.start()
        self._refresh_memory()

    @staticmethod
    def _separator() -> QFrame:
        line = QFrame()
        line.setObjectName("statusSep")
        line.setFrameShape(QFrame.Shape.NoFrame)
        line.setFixedSize(1, 14)
        return line

    # ------------------------------------------------------------------ 设置

    def set_page(self, current: int, total: int) -> None:
        if total <= 0:
            self._page.setText("— / —")
        else:
            self._page.setText(f"{current + 1} / {total}")

    def set_zoom(self, zoom: float) -> None:
        self._zoom.setText(f"{zoom * 100:.0f}%")

    def set_mode(self, text: str, tooltip: str = "") -> None:
        self._mode.setText(text)
        self._mode.setToolTip(tooltip)

    def set_cache_bytes(self, nbytes: int) -> None:
        self._cache.setText(f"缓存 {_fmt_mb(nbytes / (1024 * 1024))}")

    def set_save_state(self, text: str, tooltip: str = "") -> None:
        """第 12.3 节：项目保存状态。

        ``tooltip`` 用来把「未保存」到底是哪种未保存讲清楚：本程序是自动落盘的，
        真正的分界是「有没有按过保存（锁存一份，退出选不保存时用来回退）」。
        """
        self._save.setText(text or "—")
        self._save.setToolTip(tooltip)

    def set_export_progress(self, done: int, total: int) -> None:
        """显示导出进度；total <= 0 表示结束并收起进度条。"""
        if total <= 0:
            self.clear_export_progress()
            return
        self._export.setRange(0, max(1, total))
        self._export.setValue(min(done, total))
        self._export.show()

    def clear_export_progress(self) -> None:
        self._export.hide()
        self._export.setRange(0, 100)
        self._export.setValue(0)

    def _refresh_memory(self) -> None:
        self._memory.setText(f"本程序 {_fmt_mb(process_memory_mb())}")
        cpu, total, used, _load = self._monitor.sample()
        if total <= 0:
            self._system.setText("CPU — · 内存 —")
            return
        self._system.setText(
            f"CPU {cpu:.0f}% · 内存 {used / 1024:.1f}/{total / 1024:.1f} GB"
        )

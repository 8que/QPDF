"""这台机器现在怎么样：CPU 使用率、物理内存占用。

只读两个 Win32 接口，不引入 psutil 之类的依赖（绿色包要小）：

* ``GetSystemTimes`` 给的是开机以来的累计时间，CPU 占比要靠两次采样的差值算，
  所以第一次采样只能是 0 —— 调用方按 1.5 秒左右轮一次就够准了。
* ``GlobalMemoryStatusEx`` 直接给出物理内存总量与可用量。

任何一步失败都返回 0，界面显示"—"，不让监控本身拖累主程序。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_kernel32 = None
if hasattr(ctypes, "WinDLL"):
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetSystemTimes.argtypes = [
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME),
        ]
        _kernel32.GetSystemTimes.restype = wintypes.BOOL
        _kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        _kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    except OSError:  # pragma: no cover - 正常 Windows 上不会发生
        _kernel32 = None


def _to_int(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _system_times() -> tuple[int, int, int] | None:
    """(idle, kernel, user) 累计值，单位 100ns。"""
    if _kernel32 is None:
        return None
    idle = _FILETIME()
    kernel = _FILETIME()
    user = _FILETIME()
    try:
        ok = _kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
    except Exception:  # noqa: BLE001
        return None
    if not ok:
        return None
    return _to_int(idle), _to_int(kernel), _to_int(user)


def memory_status() -> tuple[float, float, float]:
    """(总内存 MB, 可用内存 MB, 负载百分比)。失败返回 (0, 0, 0)。"""
    if _kernel32 is None:
        return 0.0, 0.0, 0.0
    try:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return 0.0, 0.0, 0.0
        total = status.ullTotalPhys / (1024 * 1024)
        avail = status.ullAvailPhys / (1024 * 1024)
        return total, avail, float(status.dwMemoryLoad)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0


class SystemMonitor:
    """每次 ``sample()`` 算一次整机 CPU 占比。

    CPU 是"两次采样之间"的平均值，所以采样间隔要稳定；状态栏 1.5 秒一次正合适。
    """

    def __init__(self) -> None:
        self._last: tuple[int, int, int] | None = _system_times()

    def cpu_percent(self) -> float:
        current = _system_times()
        if current is None:
            return 0.0
        last = self._last
        self._last = current
        if last is None:
            return 0.0
        idle_delta = current[0] - last[0]
        total_delta = (current[1] - last[1]) + (current[2] - last[2])
        if total_delta <= 0:
            return 0.0
        busy = total_delta - idle_delta
        return max(0.0, min(100.0, busy * 100.0 / total_delta))

    def sample(self) -> tuple[float, float, float, float]:
        """(CPU %, 总内存 MB, 已用内存 MB, 内存负载 %)。"""
        cpu = self.cpu_percent()
        total, avail, load = memory_status()
        used = max(0.0, total - avail)
        return cpu, total, used, load

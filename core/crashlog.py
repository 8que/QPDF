"""崩溃日志：把没接住的异常和前几次调用栈写到程序自己的目录里。

规矩（跟项目其它地方一致）：
- **不往桌面撒文件**，也不写用户的 AppData：默认落在程序目录下的 ``日志/``；
- 冻结包里 ``sys.stderr`` 可能是 ``None``，所以**先落盘再考虑打印**；
- 只保留最近 10 个文件，别让日志无限长。

用法：``main.py`` 里启动时 ``crashlog.install()``；界面里捕获到异常用
``crashlog.log_exception(exc, "生成标注")``，返回值是日志文件路径，可以告诉用户。
"""

from __future__ import annotations

import datetime as _dt
import faulthandler
import os
import sys
import traceback

LOGGER_NAME = "qpdf"
MAX_FILES = 10


def app_root() -> str:
    """程序自己所在的目录（源码运行 = 仓库根目录）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log_dir() -> str:
    return os.path.join(app_root(), "日志")


def log_path(when: _dt.date | None = None) -> str:
    day = (when or _dt.date.today()).strftime("%Y-%m-%d")
    return os.path.join(log_dir(), f"crash-{day}.log")


def _ensure_dir() -> str:
    path = log_dir()
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return app_root()
    return path


def _prune() -> None:
    """只留最近 MAX_FILES 个日志文件。"""
    folder = log_dir()
    try:
        items = [
            os.path.join(folder, name)
            for name in os.listdir(folder)
            if name.startswith("crash-") and name.endswith(".log")
        ]
    except OSError:
        return
    if len(items) <= MAX_FILES:
        return
    for path in sorted(items)[: len(items) - MAX_FILES]:
        try:
            os.remove(path)
        except OSError:
            pass


def write(text: str) -> str:
    """往今天的日志里追加一段，返回文件路径。"""
    _ensure_dir()
    path = log_path()
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
            if not text.endswith("\n"):
                fh.write("\n")
    except OSError:
        return ""
    _prune()
    return path


def log_exception(exc: BaseException, note: str = "") -> str:
    """记一条异常（含调用栈），返回日志文件路径。"""
    stamp = _dt.datetime.now().strftime("%H:%M:%S")
    head = f"\n===== {stamp} {note or '异常'} =====\n"
    body = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return write(head + body)


def _hook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    stamp = _dt.datetime.now().strftime("%H:%M:%S")
    body = "".join(traceback.format_exception(exc_type, exc, tb))
    write(f"\n===== {stamp} 未接住的异常 =====\n{body}")


def install() -> str:
    """装上异常钩子和 faulthandler，返回日志目录。"""
    _ensure_dir()
    sys.excepthook = _hook
    try:
        handle = open(log_path(), "a", encoding="utf-8")
        faulthandler.enable(handle, all_threads=True)
        # 留个强引用，别让文件对象被回收
        global _FAULT_HANDLE
        _FAULT_HANDLE = handle
    except (OSError, RuntimeError):
        pass
    return log_dir()


_FAULT_HANDLE = None

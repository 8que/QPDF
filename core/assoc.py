"""让 `.qpdf` 文件在资源管理器里显示 QPDF 的图标、双击就能打开。

只用 **HKCU**（当前用户），不碰 HKEY_LOCAL_MACHINE——绿色版不能要管理员权限，
写当前用户这一段是免管的；卸载/取消关联就是把这两键删掉，不留垃圾。

关联本身不是程序运行的必要条件：不关联照样能"文件 → 打开项目"。
"""
from __future__ import annotations

import os
import sys

try:  # pragma: no cover - 非 Windows 上直接退化
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

#: 文件类型标识（不是扩展名）
PROG_ID = "QPDF.Project"
#: 扩展名
EXT = ".qpdf"
#: 资源管理器里显示的类型名
TYPE_NAME = "雀雀 PDF 项目"


class AssocError(RuntimeError):
    """关联失败。消息直接给用户看。"""


def _exe_path() -> str:
    """双击时要启动的东西。

    打包后是 ``QPDF.exe``；源码运行时退化成 ``pythonw.exe main.py``——
    这样开发机上也能试关联效果，不会把 exe 路径写成一个不存在的文件。
    """
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    main_py = os.path.join(root, "main.py")
    python = sys.executable or "python"
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    return f'"{python}" "{main_py}"'


def command_line() -> str:
    """``"%1"`` 展开后要执行的完整命令行。"""
    exe = _exe_path()
    if exe.startswith('"'):
        return f'{exe} "%1"'
    return f'"{exe}" "%1"'


def icon_path() -> str:
    """默认图标：直接用 exe 自己的图标（第 0 号资源）。"""
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "resources", "qpdf.ico")


def _set(key_path: str, name: str | None, value: str) -> None:
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, name or "", 0, winreg.REG_SZ, value)


def _delete_tree(key_path: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            children = []
            while True:
                try:
                    children.append(winreg.EnumKey(key, len(children)))
                except OSError:
                    break
        for child in children:
            _delete_tree(f"{key_path}\\{child}")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _notify_shell() -> None:
    """告诉资源管理器"关联变了"，不然图标要等重启才刷新。"""
    try:
        import ctypes

        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
        shell32.SHChangeNotify(0x00002000, 0, None, None)  # SHCNE_UPDATEITEM 兜底
    except Exception:  # noqa: BLE001 - 刷不刷新都不算失败
        pass


def is_available() -> bool:
    return winreg is not None and sys.platform.startswith("win")


def is_registered() -> bool:
    if not is_available():
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\{EXT}") as key:
            value, _ = winreg.QueryValueEx(key, "")
        return str(value) == PROG_ID
    except OSError:
        return False


def registered_command() -> str:
    """已经写进去的命令行（没写过返回空串）。"""
    if not is_available():
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\{PROG_ID}\shell\open\command",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "")
        return str(value)
    except OSError:
        return ""


def register() -> str:
    """建立关联，返回给用户看的一句话。"""
    if not is_available():
        raise AssocError("这个功能只在 Windows 上有效")
    if icon_path() and not os.path.isfile(icon_path()):
        raise AssocError("找不到图标文件，先确认 resources/qpdf.ico 还在")
    try:
        _set(rf"Software\Classes\{EXT}", "", PROG_ID)
        _set(rf"Software\Classes\{PROG_ID}", "", TYPE_NAME)
        _set(rf"Software\Classes\{PROG_ID}\DefaultIcon", "", f"{icon_path()},0")
        _set(rf"Software\Classes\{PROG_ID}\shell\open\command", "", command_line())
        _notify_shell()
    except OSError as exc:
        raise AssocError(f"写注册表失败：{exc}") from exc
    return f"已关联：{EXT} 文件现在显示 QPDF 的图标，双击会用本程序打开"


def unregister() -> bool:
    """取消关联。返回是否真的删掉了东西。"""
    if not is_available():
        return False
    removed = _delete_tree(rf"Software\Classes\{PROG_ID}")
    removed = _delete_tree(rf"Software\Classes\{EXT}") or removed
    _notify_shell()
    return removed

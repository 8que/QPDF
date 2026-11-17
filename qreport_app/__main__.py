"""QReport 界面启动。

三种跑法：

- 双击 / 无参数：开窗口（**没有控制台**，打包成 GUI 程序）；
- ``QReport.exe <某个.qpdf>``：开窗口并直接打开这份项目；
- ``QReport.exe --cli <qpdf> --tag …``：临时接上控制台，走原来的命令行（脚本/批处理用）；
- ``QReport.exe --selftest <qpdf>``：只读自检，结果写 exe 旁边的 ``selftest.txt``。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from qreport_app import icons, theme
from qreport_app.window import APP_VERSION, MainWindow

APP_NAME = "QReport"


def _app_dir() -> str:
    """程序所在目录（绿色版 = exe 旁边，开发时 = 仓库根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resource_path(*parts: str) -> str:
    """开发时在仓库根，打包后在 PyInstaller 的 _internal。"""
    base = getattr(sys, "_MEIPASS", _app_dir())
    return os.path.join(base, *parts)


def _portable_settings() -> QSettings:
    """绿色版不写注册表：配置放程序目录下的 config/qreport.ini。"""
    config_dir = os.path.join(_app_dir(), "config")
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError:
        pass
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, config_dir)
    return QSettings("QPDF", "QReport")


def _attach_console() -> None:
    """GUI 版 exe 默认没有控制台；``--cli`` 时借一个，让 print 能看见。

    注意：如果调用方已经把输出**重定向**到文件/管道（``QReport.exe --cli … > out.txt``），
    stdout 是有效的，这时候不要抢着开新控制台，否则结果跑到另一个窗口里去了。
    """
    if os.name != "nt":
        return
    try:
        if sys.stdout is not None:
            return
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if kernel32.GetConsoleWindow() == 0:
            kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001 - 没有控制台也不能让程序起不来
        pass


def _run_cli(args: list[str]) -> int:
    _attach_console()
    from qreport.__main__ import main as cli_main

    return cli_main(args)


def _run_selftest(path: str) -> int:
    """打包后自检：不建窗口，只读打开、数一数、写一行结果。"""
    from qreport import QpdfError, QpdfReader

    def report(line: str, code: int) -> int:
        try:
            with open(os.path.join(_app_dir(), "selftest.txt"), "w", encoding="utf-8-sig") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        try:
            print(line)
        except Exception:  # noqa: BLE001
            pass
        return code

    if not os.path.isfile(path):
        return report(f"[自检失败] 找不到文件：{path}", 2)
    try:
        with QpdfReader(path) as reader:
            info = reader.source_info()
            tags = reader.tags()
            numbers = reader.number_annotations()
            seqs = reader.sequences()
            return report(
                f"[自检通过] {os.path.basename(path)} 页数 {info['page_count']}"
                f" ｜ 管线号 {len(tags)} 个 ｜ 数字标注 {len(numbers)} 条 ｜ 编号组 {len(seqs)} 组",
                0,
            )
    except QpdfError as exc:
        return report(f"[自检失败] {exc}", 3)


def main() -> int:
    args = [item for item in sys.argv[1:] if item]

    if "--selftest" in args:
        rest = [item for item in args if item != "--selftest"]
        if not rest:
            print("用法：QReport.exe --selftest <项目.qpdf>")
            return 2
        code = _run_selftest(rest[0])
        # 和 QPDF 一样：建过 Qt 之后正常退出偶尔会被清理过程挂住
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(code)

    if "--cli" in args:
        rest = [item for item in args if item != "--cli"]
        return _run_cli(rest)

    QApplication.setApplicationName(APP_NAME)
    QApplication.setApplicationDisplayName("QReport")
    QApplication.setOrganizationName("QPDF")
    QApplication.setApplicationVersion(APP_VERSION)
    app = QApplication(sys.argv)
    icon_path = _resource_path("resources", "qreport.ico")
    app.setWindowIcon(QIcon(icon_path) if os.path.isfile(icon_path) else QIcon(icons.logo(64)))
    app.setFont(QFont("Microsoft YaHei UI", 9))

    settings = _portable_settings()
    window = MainWindow(dark=bool(settings.value("dark", False, type=bool)))
    geometry = settings.value("geometry")
    if geometry is not None:
        window.restoreGeometry(geometry)
    window.show()

    if args:
        candidate = args[0]
        if os.path.isfile(candidate) and candidate.lower().endswith(".qpdf"):
            window.open_project(candidate)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

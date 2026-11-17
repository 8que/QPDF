"""QPDF — 雀雀 PDF 入口。

用法：
    python main.py [可选: 直接打开的 PDF 路径]
    QPDF.exe [可选: 直接打开的 PDF 路径]

打包后的自检（第 19 章"测试报告"用得上，也用来验证绿色包在这台机器上能跑）：
    QPDF.exe --selftest <PDF 路径>
  —— 真开文档、真渲染第一页、真读一次像素，然后打印一行结果退出，
     退出码 0 表示这个绿色包在这台机器上是可用的。
"""

from __future__ import annotations

import os
import sys

# 让 `python main.py` 与 PyInstaller 打包后都能以项目根目录为 import 根
if getattr(sys, "frozen", False):
    # PyInstaller 把打包内容解到 _MEIPASS（onedir 模式下是 exe 旁边的 _internal）
    _ROOT = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
else:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402


def _resource(*parts: str) -> str:
    """打包后在 _MEIPASS 里，开发时在源码目录里。"""
    return os.path.join(_ROOT, *parts)


def _app_dir() -> str:
    """程序所在目录（绿色版 = exe 旁边，开发时 = 项目根）。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _ROOT


def _make_settings_portable() -> None:
    """绿色版不该往注册表里写东西：配置放到程序目录下的 config/settings.ini。"""
    config_dir = os.path.join(_app_dir(), "config")
    os.makedirs(config_dir, exist_ok=True)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, config_dir)


def _selftest(pdf: str) -> int:
    """开文档 → 渲染第一页 → 数一下像素颜色，退出。"""
    from PySide6.QtCore import QRectF

    from core.pdf_engine import PdfDocument

    app = QApplication.instance() or QApplication(sys.argv)
    theme.ensure_applied(app)
    _make_settings_portable()

    def report(line: str, code: int) -> int:
        # 绿色版是窗口程序（pythonw / console=False 的 exe），sys.stdout 可能是 None，
        # 这时候 print 会抛 AttributeError 把自检直接搞成退出码 1，所以先写文件再尝试打印。
        if getattr(sys, "frozen", False) or sys.stdout is None:
            try:
                with open(os.path.join(_app_dir(), "selftest.txt"), "w", encoding="utf-8-sig") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        try:
            print(line)
        except Exception:  # noqa: BLE001 - 没有控制台就算了
            pass
        return code

    if not os.path.isfile(pdf):
        return report(f"[自检失败] 找不到 PDF：{pdf}", 2)

    document = PdfDocument(pdf)
    try:
        size = document.page_size(0)
        image = document.render(0, QRectF(0, 0, size.width, size.height), 0.5)
        if image is None or image.isNull():
            return report("[自检失败] 第一页渲染不出来", 3)
        colors = set()
        for y in range(0, image.height(), max(1, image.height() // 40)):
            for x in range(0, image.width(), max(1, image.width() // 40)):
                colors.add(image.pixel(x, y))
        line = (
            f"[自检通过] {os.path.basename(pdf)} 共 {document.page_count} 页，"
            f"第一页 {size.width:.0f}x{size.height:.0f} pt，"
            f"渲染 {image.width()}x{image.height()} 像素、{len(colors)} 种颜色"
        )
        return report(line, 0 if len(colors) > 2 else 4)
    finally:
        document.close()


def _ocrcheck() -> int:
    """打包后专门验 OCR：画一张带字的图，走真实链路，看能不能认出来。"""
    from core import textscan

    app = QApplication.instance() or QApplication(sys.argv)
    theme.ensure_applied(app)

    def report(line: str, code: int) -> int:
        if getattr(sys, "frozen", False) or sys.stdout is None:
            try:
                with open(os.path.join(_app_dir(), "ocrcheck.txt"), "w", encoding="utf-8-sig") as fh:
                    fh.write(line + "\n")
            except OSError:
                pass
        try:
            print(line)
        except Exception:  # noqa: BLE001
            pass
        return code

    if not textscan.ocr_available():
        return report(f"[OCR 不可用] {textscan.ocr_error()}", 5)

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QFont, QImage, QPainter

    image = QImage(520, 120, QImage.Format.Format_RGB888)
    image.fill(QColor("#ffffff"))
    painter = QPainter(image)
    painter.setPen(QColor("#000000"))
    painter.setFont(QFont("Arial", 40))
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "P1302B01-N1B-H60")
    painter.end()

    text = textscan.ocr_image(image)
    if not text:
        return report(f"[OCR 失败] 认不出图里的字（{textscan.ocr_error() or '没有返回结果'}）", 6)
    normalized = textscan.normalize_tag(text)
    return report(f"[OCR 通过] 认出：{text}（归一化后：{normalized}）", 0)


def main() -> int:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontCreateNativeWidgetSiblings, True)

    # 先把"没接住的异常"记下来（日志落在程序目录的 日志/ 里）。
    # 冻结包里 stdout/stderr 常常是 None，不装这个就什么都看不到。
    from core import crashlog  # noqa: PLC0415

    crashlog.install()

    args = [item for item in sys.argv[1:] if item]
    if "--ocrcheck" in args:
        code = _ocrcheck()
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(code)
    if "--selftest" in args:
        rest = [item for item in args if item != "--selftest"]
        if not rest:
            print("用法：QPDF.exe --selftest <PDF 路径>")
            return 2
        code = _selftest(rest[0])
        # 自检是给脚本/打包验证用的：建过 QApplication 之后正常退出偶尔会被
        # Qt 的清理过程挂住（实测进程不退出，还占着 mupdfcpp64.dll 导致下次打包失败），
        # 所以刷掉输出后直接退。窗口程序没有 stdout/stderr，flush 前要先判空。
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - 没有控制台就算了
                pass
        os._exit(code)

    app = QApplication(sys.argv)
    app.setApplicationName("QPDF")
    app.setApplicationDisplayName("雀雀 PDF")
    app.setOrganizationName("QPDF")
    app.setApplicationVersion("2.5.3")
    # 窗口图标：任务栏、Alt+Tab、标题栏都用它（exe 的图标由打包时嵌入）
    icon_path = _resource("resources", "qpdf.ico")
    if os.path.isfile(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    theme.ensure_applied(app)
    _make_settings_portable()

    window = MainWindow()
    window.show()

    if args:
        candidate = args[0]
        if os.path.isfile(candidate):
            # .qpdf 交给"打开项目"（它自己去找源 PDF）；其他按 PDF 直接开
            if candidate.lower().endswith(".qpdf"):
                window.open_project(candidate)
            else:
                window.open_pdf(candidate)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

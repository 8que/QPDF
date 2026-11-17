# -*- mode: python ; coding: utf-8 -*-
"""QPDF 绿色版打包脚本（第 19 章）。

用法：
    .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean QPDF.spec

产物：dist/QPDF/ —— 整个目录拷走就能跑，不需要装 Python / PySide6 / 任何阅读器。
用 onedir（而不是 onefile）是因为 onefile 每次启动都要把几十 MB 解到临时目录，
在 N5095A 这种机器上会明显慢，而且杀软更容易拦。
"""

import os

# 只留 QtWidgets 真正用得到的部分：Qml/Quick/Sql/Network/OpenGL/PrintSupport
# 这些一个都没用到，排除掉能少几十 MB。
EXCLUDES = [
    "tkinter",
    "unittest",
    "pydoc",
    "doctest",
    "test",
    "pytest",
    "numpy",
    "PIL",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtQuickTest",
    "PySide6.QtDesigner",
    "PySide6.QtHelp",
    "PySide6.QtUiTools",
    "PySide6.QtDBus",
    "PySide6.QtSql",
    "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
    "PySide6.QtXml",
    "PySide6.QtConcurrent",
    "PySide6.QtExampleIcons",
]

a = Analysis(
    ["main.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=[("resources/qpdf.ico", "resources"),
           ("resources/bg.png", "resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# ---- 瘦身：把确定用不到的东西从包里剔掉 -------------------------------------
# 逻辑：QPDF 只画文字和线框（图标是 QPainter 现画的），不加载任何图片文件、
# 不联网、不用 SVG、不用 OpenGL。剔掉这几样能省 ~25 MB。
DROP_BINARIES = (
    "opengl32sw.dll",     # 软件 OpenGL 兜底，QtWidgets 用不到（20 MB）
    "qt6network.dll",     # 不联网
    "qtnetwork.pyd",
    "qt6svg.dll",         # 图标是现画的，不读 SVG
    "qtsvg.pyd",
)
DROP_DATA_PARTS = (
    "translations\\",
    "translations/",
    "\\imageformats\\qjpeg.dll",
    "\\imageformats\\qtiff.dll",
    "\\imageformats\\qwebp.dll",
    "\\imageformats\\qicns.dll",
    "\\imageformats\\qtga.dll",
    "\\imageformats\\qwbmp.dll",
    "\\imageformats\\qsvg.dll",
    "\\imageformats\\qgif.dll",
    "\\tls\\",
    "\\networkinformation\\",
)


def _keep(name: str, drops) -> bool:
    low = name.lower()
    return not any(token in low for token in drops)


a.binaries = [item for item in a.binaries if _keep(item[0], DROP_BINARIES)]
a.datas = [item for item in a.datas if _keep(item[0], DROP_DATA_PARTS)]

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QPDF",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 双击不弹黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.abspath("resources/qpdf.ico"),
    version=os.path.abspath("resources/version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QPDF",
)

# -*- mode: python ; coding: utf-8 -*-
"""QReport 的 PyInstaller 打包配置（**带界面的绿色版**，和 QPDF 完全独立）。

结构：onedir（``dist\\QReport\\QReport.exe`` + ``_internal\\``）。
带 PySide6 的程序用 onefile 会每次解压几十 MB，启动明显变慢，所以和 QPDF 一样走 onedir。
"""

import os

a = Analysis(
    ["qreport_app/__main__.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[("resources/qreport.ico", "resources")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 统计工具用不到 PDF 渲染/标注那一套，全排掉
        "pymupdf",
        "fitz",
        "numpy",
        "core",
        "ui",
        "database",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="QReport",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # 窗口程序；要命令行就加 --cli（会自己接一个控制台）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.abspath("resources/qreport.ico"),
    version=os.path.abspath("resources/qreport_version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="QReport",
)

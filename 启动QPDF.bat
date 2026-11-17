@echo off
rem QPDF 启动器（开发阶段用）
rem 双击即可运行；也可以把 PDF 文件拖到这个 bat 上，直接打开该文件。

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo   [错误] 找不到虚拟环境：%~dp0.venv\Scripts\pythonw.exe
    echo   请先执行：
    echo       python -m venv .venv
    echo       .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" "main.py" %*

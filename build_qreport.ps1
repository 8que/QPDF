# QReport 一键打包（带界面的绿色版，和 QPDF 完全独立）
#
# 用法（在项目根目录）：
#     powershell -ExecutionPolicy Bypass -File .\build_qreport.ps1
#
# 产物：
#     dist\QReport\                             ← 绿色版目录（exe + _internal），整个拷走就能跑
#     dist\QReport_绿色版_v1.1.0.zip            ← 发给别人用这个
#
# 打完自动跑一次 --selftest（只读打开一个 .qpdf 数一数），失败就报错。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = "1.3.2"
$python = ".\.venv\Scripts\python.exe"
$exe = "dist\QReport\QReport.exe"
$sample = "tests\out\cases\number\项目\number-b5ea7ebb.qpdf"
$zip = "dist\QReport_绿色版_v$version.zip"

Write-Host "[1/4] PyInstaller 打包（onedir + 界面）…" -ForegroundColor Cyan
Get-Process QReport -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
& $python -m PyInstaller --noconfirm --clean QReport.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }
if (-not (Test-Path $exe)) { throw "没生成 $exe" }

Write-Host "[2/4] 补说明…" -ForegroundColor Cyan
Copy-Item "docs\QReport.md" "dist\QReport\QReport说明.md" -Force

Write-Host "[3/4] 自检…" -ForegroundColor Cyan
if (-not (Test-Path $sample)) { throw "找不到自检用的 .qpdf：$sample" }
Remove-Item "dist\QReport\selftest.txt" -ErrorAction SilentlyContinue
& $exe --selftest $sample | Out-Null
$code = $LASTEXITCODE
if ($code -ne 0) { throw "QReport 自检失败（退出码 $code）" }
$line = Get-Content "dist\QReport\selftest.txt" -Encoding UTF8 -ErrorAction SilentlyContinue
if (-not $line) { throw "自检没写出 selftest.txt" }
Write-Host $line -ForegroundColor Green
Remove-Item "dist\QReport\selftest.txt" -ErrorAction SilentlyContinue

Write-Host "[4/4] 压缩…" -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\QReport\*" -DestinationPath $zip -CompressionLevel Optimal

$dirSize = [math]::Round((Get-ChildItem dist\QReport -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
$fileCount = (Get-ChildItem dist\QReport -Recurse -File).Count
$zipSize = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "完成：目录 $dirSize MB / $fileCount 个文件，压缩包 $zipSize MB -> $zip" -ForegroundColor Green

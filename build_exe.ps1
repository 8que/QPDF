# QPDF 一键打包（第 19 章）
#
# 用法（在项目根目录）：
#     powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#
# 产物：
#     dist\QPDF\                       ← 绿色版目录，整个拷走就能用
#     dist\QPDF_绿色版_v2.5.3.zip      ← 发给别人用这个
#
# 打包完会自动跑一次 --selftest（真开一份 PDF、真渲染一页），失败就报错。

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = "2.5.3"
$python = ".\.venv\Scripts\python.exe"
$exe = "dist\QPDF\QPDF.exe"
$sample = "samples\cad_test.pdf"
$zip = "dist\QPDF_绿色版_v$version.zip"

Write-Host "[1/4] PyInstaller 打包…" -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm --clean QPDF.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败（退出码 $LASTEXITCODE）" }

Write-Host "[2/4] 补文档…" -ForegroundColor Cyan
Copy-Item "docs\使用说明.md" "dist\QPDF\使用说明.md" -Force

Write-Host "[3/4] 自检…" -ForegroundColor Cyan
if (-not (Test-Path $sample)) { throw "缺少自检样本 $sample，请先放一份 PDF 到 samples\ 下" }
$env:QT_QPA_PLATFORM = "offscreen"
try {
    $proc = Start-Process -FilePath (Resolve-Path $exe).Path `
        -ArgumentList @("--selftest", ('"' + (Resolve-Path $sample).Path + '"')) `
        -Wait -PassThru
} finally {
    Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue
}
if ($proc.ExitCode -ne 0) { throw "自检失败（退出码 $($proc.ExitCode)）" }
Get-Content "dist\QPDF\selftest.txt" -Encoding utf8
Remove-Item "dist\QPDF\selftest.txt" -Force

Write-Host "[4/4] 压缩…" -ForegroundColor Cyan
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\QPDF\*" -DestinationPath $zip -CompressionLevel Optimal

$size = [math]::Round((Get-ChildItem "dist\QPDF" -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB, 1)
$zipSize = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "完成：目录 $size MB，压缩包 $zipSize MB -> $zip" -ForegroundColor Green

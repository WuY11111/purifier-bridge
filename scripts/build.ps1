# 打包成免安装版：dist\PurifierApp\  ->  .\散热管家\
# 用法： powershell -ExecutionPolicy Bypass -File scripts\build.ps1
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 找 Python
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $py)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source } else { throw '找不到 python.exe，请先安装 Python 3.10+' }
}
Write-Host "Python: $py"

# 确保有 pyinstaller
& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "安装 PyInstaller…"
    & $py -m pip install --user pyinstaller --quiet
}

Write-Host "开始打包…"
& $py -m PyInstaller --noconfirm --onedir --noconsole --name PurifierApp app.py

$dist = Join-Path $root 'dist\PurifierApp'
if (-not (Test-Path $dist)) { throw '打包失败：找不到 dist\PurifierApp' }

$out = Join-Path $root '散热管家'
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
Copy-Item $dist $out -Recurse
Rename-Item (Join-Path $out 'PurifierApp.exe') '散热管家.exe'

Write-Host ""
Write-Host "完成：$out\散热管家.exe"
Write-Host "把 tools\LibreHardwareMonitor 一起复制到 $out\tools\ 就能读 CPU 温度"

# 注册“登录后以管理员身份自动启动 LibreHardwareMonitor”的计划任务。
# LibreHardwareMonitor 读 CPU 温度需要加载内核驱动，必须提权运行。
#
# 用法：右键本文件 →「使用 PowerShell 运行」（会弹 UAC），或：
#   powershell -ExecutionPolicy Bypass -File .\安装-LHM开机自启-需管理员.ps1

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $root 'tools\LibreHardwareMonitor\LibreHardwareMonitor.exe'
$taskName = 'PurifierLHM'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "需要管理员权限：请右键本文件选「使用 PowerShell 运行」，或从管理员 PowerShell 执行。" -ForegroundColor Red
    Read-Host "按回车关闭"
    exit 1
}
if (-not (Test-Path $exe)) { throw "找不到 $exe，请确认 tools\LibreHardwareMonitor 已解压" }

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction -Execute $exe -WorkingDirectory (Split-Path $exe)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -RunLevel Highest -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description 'LibreHardwareMonitor（管理员，提供 CPU 温度）' | Out-Null

Start-ScheduledTask -TaskName $taskName
Write-Host "已注册：$taskName（登录后自动以管理员启动 LHM）" -ForegroundColor Green
Write-Host "第一次启动 LHM 后，建议在它的 Options 里勾选："
Write-Host "  - Web Server（监听 8085，本脚本默认按它取数据）"
Write-Host "  - Start minimized / Run on startup 可交给任务计划，无需重复"
Read-Host "按回车关闭"

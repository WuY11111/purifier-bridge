param(
    [string]$Out = "shot.png",
    [string]$TitleLike = "*",
    [string[]]$ProcessName = @("python", "pythonw")
)

# 截取指定进程的主窗口（只截我们的窗口，不拍整个桌面）
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class ShotWin {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
}
"@
$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -like $TitleLike } |
        Sort-Object StartTime -Descending | Select-Object -First 1
if (-not $proc) { throw "找不到窗口（$TitleLike）" }
$h = $proc.MainWindowHandle
[ShotWin]::ShowWindow($h, 9) | Out-Null
[ShotWin]::SetForegroundWindow($h) | Out-Null
Start-Sleep -Milliseconds 700
$r = New-Object ShotWin+RECT
[ShotWin]::GetWindowRect($h, [ref]$r) | Out-Null
$w = $r.Right - $r.Left
$hh = $r.Bottom - $r.Top
if ($w -le 10 -or $hh -le 10) { throw "窗口尺寸异常 ${w}x${hh}" }
$bmp = New-Object System.Drawing.Bitmap $w, $hh
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $hh)))
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output "saved $Out ${w}x${hh} ($($proc.MainWindowTitle))"

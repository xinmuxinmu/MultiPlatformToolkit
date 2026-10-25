# ============================================================
#  StarSky 自动签到工具
#  运行环境：Windows 自带 PowerShell 5.1，无需安装任何东西
#  原    理：截取窗口画面 → 识别「签到获得流量」按钮 → 模拟鼠标点击
#  原 因：StarSky 界面是 WebKit 渲染，无法用控件方式定位按钮
# ============================================================
param(
    [switch]$Quiet,      # 静默模式（给定时任务用，不弹提示）
    [switch]$TestClick   # 调试用：即使今天已签到，也强制点一下按钮
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Drawing

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
[StructLayout(LayoutKind.Sequential)]
public struct SkyRect { public int Left, Top, Right, Bottom; }
[StructLayout(LayoutKind.Sequential)]
public struct SkyPoint { public int X, Y; }
public class Sky32 {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out SkyRect r);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out SkyPoint p);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
    [DllImport("user32.dll", EntryPoint = "GetWindowThreadProcessId")] public static extern uint GetWindowThreadProcessIdEx(IntPtr h, out uint pid);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool fAttach);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr SetFocus(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(SkyPoint p);
    [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    public delegate bool EnumProc(IntPtr h, IntPtr l);

    // 有些情况下 MainWindowHandle 取不到（窗口被隐藏到托盘），
    // 这里直接枚举该进程的顶层窗口，取面积最大的那个 WinForms 窗口
    public static IntPtr FindMainWindow(int pid) {
        IntPtr best = IntPtr.Zero;
        int bestArea = 0;
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint wp;
            GetWindowThreadProcessIdEx(h, out wp);
            if (wp != (uint)pid) return true;
            StringBuilder sb = new StringBuilder(256);
            GetClassNameW(h, sb, 256);
            if (sb.ToString().IndexOf("WindowsForms10.Window", StringComparison.Ordinal) < 0) return true;
            SkyRect r;
            if (!GetWindowRect(h, out r)) return true;
            int area = (r.Right - r.Left) * (r.Bottom - r.Top);
            if (area > bestArea) { bestArea = area; best = h; }
            return true;
        }, IntPtr.Zero);
        return best;
    }
}
'@

# 让本进程按物理像素工作，避免高分屏下坐标错位
[void][Sky32]::SetProcessDPIAware()

$script:Root = Split-Path -Parent $MyInvocation.MyCommand.Path
# 统一应用通过 STARSKY_HOME 把配置与日志指向 data/starsky；计划任务未带变量时回退到仓库 data 目录
$script:DataRoot = if ($env:STARSKY_HOME) { $env:STARSKY_HOME } else {
    $repoData = Join-Path $script:Root '..\..\..\data\starsky'
    if (Test-Path (Split-Path -Parent $repoData)) {
        if (-not (Test-Path $repoData)) { New-Item -ItemType Directory -Path $repoData -Force | Out-Null }
        (Resolve-Path $repoData).Path
    } else { $script:Root }
}
if (-not $script:DataRoot) { $script:DataRoot = $script:Root }
$script:IniPath = Join-Path $script:DataRoot '设置.ini'
$script:LogDir = Join-Path $script:DataRoot '日志'
$script:LogFile = Join-Path $script:LogDir '签到日志.txt'

# ---------------- 输出 & 日志 ----------------
function Say($msg, $color = 'Gray') {
    if (-not $Quiet) { Write-Host $msg -ForegroundColor $color }
}
function Write-Log($msg) {
    try {
        if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null }
        $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $msg
        [System.IO.File]::AppendAllText($script:LogFile, $line + "`r`n", (New-Object System.Text.UTF8Encoding($false)))
    } catch {}
}

# ---------------- 管理员权限 ----------------
# StarSky 自身是以管理员权限运行的，Windows 的 UIPI 机制会拦截普通权限程序
# 对它发起的鼠标模拟（表现为：鼠标能移动，但点击毫无反应）。
# 所以本工具必须以管理员权限运行，否则点不动。
$isAdmin = $false
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
} catch {}

if (-not $isAdmin) {
    Say '  [提示] StarSky 是管理员权限运行的，本工具也需要提权，正在申请 ...' 'Yellow'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $PSCommandPath + '"'))
    if ($Quiet) { $argList += '-Quiet' }
    if ($TestClick) { $argList += '-TestClick' }
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList $argList
        exit 0
    } catch {
        Say '  [失败] 提权被取消，无法继续（没有管理员权限点不动 StarSky）' 'Red'
        exit 5
    }
}

# ---------------- 读配置 ----------------
function Read-Ini($path) {
    $ini = @{ 'Global' = @{} }
    $section = 'Global'
    foreach ($line in [System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8)) {
        $l = $line.Trim()
        if ($l -eq '' -or $l.StartsWith(';') -or $l.StartsWith('#')) { continue }
        if ($l -match '^\[(.+)\]$') {
            $section = $Matches[1].Trim()
            if (-not $ini.ContainsKey($section)) { $ini[$section] = @{} }
            continue
        }
        $idx = $l.IndexOf('=')
        if ($idx -gt 0) {
            $k = $l.Substring(0, $idx).Trim()
            $v = $l.Substring($idx + 1).Trim()
            $ini[$section][$k] = $v
        }
    }
    return $ini
}

# ---------------- 自动识别安装位置 ----------------
# 按优先级找 StarSky.exe：配置文件 → 正在运行的进程 → 注册表卸载信息 → 常见安装目录
function Resolve-ExePath($cfgPath) {
    if ($cfgPath) {
        $c = $cfgPath.Trim().Trim('"')
        if ($c -and (Test-Path $c)) { return $c }
    }

    # 1) 已经在运行 → 进程自身路径最准
    foreach ($pn in @('StarSky')) {
        $p = Get-Process -Name $pn -ErrorAction SilentlyContinue | Where-Object { $_.Path } | Select-Object -First 1
        if ($p) {
            try { if (Test-Path $p.Path) { return $p.Path } } catch {}
        }
    }

    # 2) 注册表里的安装信息
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    foreach ($k in $keys) {
        $items = Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like '*StarSky*' }
        foreach ($it in $items) {
            if ($it.InstallLocation) {
                $loc = $it.InstallLocation.Trim().Trim('"').TrimEnd('\')
                $exe = Join-Path $loc 'StarSky.exe'
                if (Test-Path $exe) { return $exe }
            }
            if ($it.DisplayIcon) {
                $ico = ($it.DisplayIcon -split ',')[0].Trim().Trim('"')
                if ($ico -and (Test-Path $ico) -and $ico -like '*.exe') { return $ico }
            }
        }
    }

    # 3) 常见安装目录（含各磁盘根目录下的 Program / Program Files 等）
    $cands = New-Object System.Collections.Generic.List[string]
    foreach ($sub in @('Program\StarSky', 'Program Files\StarSky', 'Program Files (x86)\StarSky', 'Programs\StarSky', 'App\StarSky', 'StarSky')) {
        foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
            if ($root) { $cands.Add((Join-Path $root "$sub\StarSky.exe")) }
        }
        foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
            if ($d.Root -match '^[A-Za-z]:\\$') { $cands.Add((Join-Path $d.Root "$sub\StarSky.exe")) }
        }
    }
    foreach ($c in $cands) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Set-IniValue($path, $key, $value) {
    try {
        $lines = @([System.IO.File]::ReadAllLines($path, [System.Text.Encoding]::UTF8))
        $found = $false
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match ('^\s*' + [regex]::Escape($key) + '\s*=')) {
                $lines[$i] = "$key=$value"
                $found = $true
                break
            }
        }
        if (-not $found) { $lines += "$key=$value" }
        [System.IO.File]::WriteAllText($path, (($lines -join "`r`n") + "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    } catch {}
}

if (-not (Test-Path $script:IniPath)) {
    Say "找不到配置文件：$script:IniPath" 'Red'
    exit 2
}
$cfg = (Read-Ini $script:IniPath)['StarSky']
if ($null -eq $cfg) { $cfg = @{} }

$ExePath   = $cfg['软件路径']
$AutoStart = $cfg['自动启动'];      if (-not $AutoStart) { $AutoStart = '1' }
$CloseAfter = $cfg['签到后关闭软件']; if (-not $CloseAfter) { $CloseAfter = '1' }
$WaitSec = 30
try { if ($cfg['等待超时']) { $WaitSec = [int]$cfg['等待超时'] } } catch {}

# ---------------- 进程 / 窗口 ----------------
function Get-SkyProcess {
    $procs = @(Get-Process -Name 'StarSky' -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { return $null }
    $withWin = @($procs | Where-Object { $_.MainWindowHandle -ne 0 })
    if ($withWin.Count -gt 0) { return $withWin[0] }
    return $procs[0]
}

function Bring-ToFront($h) {
    if ([Sky32]::IsIconic($h)) { [void][Sky32]::ShowWindow($h, 9); Start-Sleep -Milliseconds 500 }
    if ([Sky32]::GetForegroundWindow() -eq $h) { return $true }

    # 附加到前台窗口的输入队列，绕过系统的前台窗口锁定
    $fg = [Sky32]::GetForegroundWindow()
    $tidFg = 0
    if ($fg -ne [IntPtr]::Zero) { $tidFg = [Sky32]::GetWindowThreadProcessId($fg, [IntPtr]::Zero) }
    $tidMe = [Sky32]::GetCurrentThreadId()
    $attached = $false
    if ($tidFg -ne 0 -and $tidFg -ne $tidMe) {
        $attached = [Sky32]::AttachThreadInput($tidMe, $tidFg, $true)
    }
    try {
        [void][Sky32]::ShowWindow($h, 5)          # SW_SHOW
        [void][Sky32]::BringWindowToTop($h)
        [void][Sky32]::SetForegroundWindow($h)
        [void][Sky32]::SetFocus($h)
    } catch {
    } finally {
        if ($attached) { [void][Sky32]::AttachThreadInput($tidMe, $tidFg, $false) }
    }
    Start-Sleep -Milliseconds 400
    if ([Sky32]::GetForegroundWindow() -eq $h) { return $true }

    # 兜底：临时置顶再取消置顶，通常能把窗口抬到最上层
    $SWP = 0x0001 -bor 0x0002 -bor 0x0040     # NOSIZE | NOMOVE | SHOWWINDOW
    [void][Sky32]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, $SWP)   # HWND_TOPMOST
    Start-Sleep -Milliseconds 250
    [void][Sky32]::SetWindowPos($h, [IntPtr](-2), 0, 0, 0, 0, $SWP)   # HWND_NOTOPMOST
    Start-Sleep -Milliseconds 300
    return ([Sky32]::GetForegroundWindow() -eq $h)
}

# 判断某个屏幕坐标上最顶层的窗口是不是 StarSky —— 防止把点击落到别的窗口上
function Test-PointBelongsTo($h, $x, $y) {
    $pt = New-Object SkyPoint
    $pt.X = $x; $pt.Y = $y
    $under = [Sky32]::WindowFromPoint($pt)
    if ($under -eq [IntPtr]::Zero) { return $false }
    $root = [Sky32]::GetAncestor($under, 2)   # GA_ROOT
    return ($root -eq $h)
}

function Get-WindowBitmap($h) {
    try {
        $r = New-Object SkyRect
        if (-not [Sky32]::GetWindowRect($h, [ref]$r)) { return $null }
        $w = $r.Right - $r.Left
        $ht = $r.Bottom - $r.Top
        if ($w -lt 200 -or $ht -lt 200) { return $null }
        $bmp = New-Object System.Drawing.Bitmap($w, $ht)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $ht)))
        $g.Dispose()
        return @{ Bitmap = $bmp; Rect = $r }
    } catch {
        return $null
    }
}

# ---------------- 按钮识别 ----------------
# 在窗口右下区域按行/列统计特征色像素，取最长的连续色带即为按钮
function Find-Button($bmp) {
    $w = $bmp.Width; $h = $bmp.Height
    $x0 = [int]($w * 0.65); $x1 = [int]($w * 0.98)
    $y0 = [int]($h * 0.66); $y1 = [int]($h * 0.89)

    $rect = New-Object System.Drawing.Rectangle(0, 0, $w, $h)
    $data = $bmp.LockBits($rect, [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $stride = $data.Stride
    $bytes = New-Object byte[] ($stride * $h)
    [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $bytes, 0, $bytes.Length)
    $bmp.UnlockBits($data)

    $result = @{ State = 'Unknown'; CX = 0; CY = 0; W = 0; H = 0 }

    # GDI+ 的像素缓冲区行序不固定（可能自底向上），两种行序各扫一遍，
    # 哪种能识别出按钮就用哪种 —— 比单纯采样判断更可靠
    foreach ($flip in @($false, $true)) {
        $rowBlue = New-Object int[] $h; $colBlue = New-Object int[] $w
        $rowGray = New-Object int[] $h; $colGray = New-Object int[] $w

        for ($y = $y0; $y -le $y1; $y += 2) {
            if ($flip) { $base = ($h - 1 - $y) * $stride } else { $base = $y * $stride }
            for ($x = $x0; $x -le $x1; $x += 2) {
                $i = $base + $x * 4
                $b = $bytes[$i]; $g = $bytes[$i + 1]; $r = $bytes[$i + 2]
                # 蓝色「签到获得流量」：实测 (53,124,246) / (77,155,250)
                if ($b -ge 200 -and $g -ge 100 -and $g -le 195 -and $r -le 125 -and ($b - $r) -ge 90) {
                    $rowBlue[$y]++; $colBlue[$x]++
                }
                # 灰色「今日已签到」：实测 (199,200,202)
                elseif ([Math]::Abs($r - 199) -le 8 -and [Math]::Abs($g - 200) -le 8 -and [Math]::Abs($b - 202) -le 8) {
                    $rowGray[$y]++; $colGray[$x]++
                }
            }
        }

        foreach ($pair in @(@('Unsigned', $rowBlue, $colBlue), @('Signed', $rowGray, $colGray))) {
            $state = $pair[0]; $rows = $pair[1]; $cols = $pair[2]

            # 找最长的连续行色带（按同样的步长 2 遍历，否则色带会被奇数行打断）
            $bestS = -1; $bestL = 0; $cs = -1; $cl = 0
            for ($y = $y0; $y -le $y1; $y += 2) {
                if ($rows[$y] -ge 22) {
                    if ($cl -eq 0) { $cs = $y }
                    $cl++
                    if ($cl -gt $bestL) { $bestL = $cl; $bestS = $cs }
                } else { $cl = 0 }
            }
            if ($bestL -lt 18) { continue }   # 至少 36px 高，排除小图标/文字

            $bxS = -1; $bxL = 0; $cxs = -1; $cxl = 0
            for ($x = $x0; $x -le $x1; $x += 2) {
                if ($cols[$x] -ge 12) {
                    if ($cxl -eq 0) { $cxs = $x }
                    $cxl++
                    if ($cxl -gt $bxL) { $bxL = $cxl; $bxS = $cxs }
                } else { $cxl = 0 }
            }
            if ($bxL -lt 30) { continue }     # 至少 60px 宽

            $bw = ($bxL - 1) * 2 + 1
            $bh = ($bestL - 1) * 2 + 1
            if ($bw -gt 500 -or $bh -gt 260) { continue }
            $result.State = $state
            $result.CX = [int]($bxS + $bw / 2)
            $result.CY = [int]($bestS + $bh / 2)
            $result.W = $bw; $result.H = $bh
            break
        }
        if ($result.State -ne 'Unknown') { break }
    }
    return $result
}

function Click-At($x, $y) {
    $old = New-Object SkyPoint
    [void][Sky32]::GetCursorPos([ref]$old)

    [void][Sky32]::SetCursorPos($x, $y)
    Start-Sleep -Milliseconds 180

    # 校验光标是否真的移到了目标点。移不动说明权限不够被系统拦了，
    # 这时绝对不能继续点，否则会点到别的窗口上。
    $now = New-Object SkyPoint
    [void][Sky32]::GetCursorPos([ref]$now)
    if ([Math]::Abs($now.X - $x) -gt 2 -or [Math]::Abs($now.Y - $y) -gt 2) { return $false }

    [Sky32]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)   # 左键按下
    Start-Sleep -Milliseconds 90
    [Sky32]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)   # 左键抬起
    Start-Sleep -Milliseconds 200
    [void][Sky32]::SetCursorPos($old.X, $old.Y)              # 鼠标归位，不打扰用户
    return $true
}

# 发一个 ESC —— 点完签到后如果弹出了提示框，用它把提示框关掉再看状态
function Send-Esc {
    [Sky32]::keybd_event(0x1B, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 60
    [Sky32]::keybd_event(0x1B, 0, 2, [UIntPtr]::Zero)
}

# 关闭 StarSky：先正常关闭，等不到退出再强制结束
function Close-StarSky($proc) {
    if ($null -eq $proc) { return }
    try {
        if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { return }
        [void](Get-Process -Id $proc.Id).CloseMainWindow()
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.ElapsedMilliseconds -lt 4000) {
            Start-Sleep -Milliseconds 200
            if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) { return }
        }
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 400
    } catch {}
}

# ---------------- 主流程 ----------------
$t0 = Get-Date
Say ''
Say '  StarSky 自动签到' 'Cyan'
Say '  ----------------------------------------' 'DarkGray'

# 自动识别安装位置：配置里没填、或填的路径已失效，都能自己找到并写回配置
$ExePath = Resolve-ExePath $ExePath
if ($ExePath -and $ExePath -ne $cfg['软件路径']) {
    Set-IniValue $script:IniPath '软件路径' $ExePath
}

$proc = Get-SkyProcess
$weStarted = $false

if ($null -eq $proc) {
    if ($AutoStart -ne '1') {
        Say '  [跳过] StarSky 未运行，且配置里关闭了自动启动' 'Yellow'
        Write-Log '失败：StarSky 未运行'
        exit 1
    }
    if (-not $ExePath -or -not (Test-Path $ExePath)) {
        Say '  [错误] 自动识别不到 StarSky 的安装位置' 'Red'
        Say '         请打开「设置.ini」把「软件路径」填成实际路径' 'Red'
        Write-Log '失败：找不到 StarSky.exe'
        exit 2
    }
    Say '  [1/3] 没在运行，正在自动打开 ...' 'Gray'
    Say ("         路径：$ExePath") 'DarkGray'
    Start-Process -FilePath $ExePath -WorkingDirectory (Split-Path -Parent $ExePath)
    $weStarted = $true

    $deadline = (Get-Date).AddSeconds($WaitSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
        $proc = Get-SkyProcess
        if ($proc -and $proc.MainWindowHandle -ne 0) { break }
    }
} else {
    Say '  [1/3] StarSky 已在运行' 'Gray'
}

if ($null -eq $proc) {
    Say '  [错误] 启动超时，没等到 StarSky 进程' 'Red'
    Write-Log '失败：启动超时'
    exit 3
}

# 窗口句柄：常规方式取不到时（窗口被隐藏到托盘），直接枚举该进程的顶层窗口
$hwnd = $proc.MainWindowHandle
if ($hwnd -eq 0) { $hwnd = [Sky32]::FindMainWindow($proc.Id) }
if ($hwnd -eq 0) {
    Say '  [错误] 没找到 StarSky 主窗口' 'Red'
    Say '         请先在托盘里把 StarSky 主界面打开，再运行本工具' 'DarkGray'
    Write-Log '失败：无窗口句柄'
    exit 3
}

$fg = Bring-ToFront $hwnd
if (-not $fg) { Say '  [提示] 窗口可能没有切到最前，仍继续尝试' 'DarkYellow' }

Say '  [2/3] 正在识别签到按钮 ...' 'Gray'

# 轮询等待界面渲染出按钮（刚启动时界面是逐步加载的，所以一直轮询而不是死等固定时间）
$btn = $null
$deadline = (Get-Date).AddSeconds($WaitSec)
while ((Get-Date) -lt $deadline) {
    $shot = Get-WindowBitmap $hwnd
    if ($shot) {
        $b = Find-Button $shot.Bitmap
        if ($b.State -ne 'Unknown') { $btn = $b; $btn.Rect = $shot.Rect; $shot.Bitmap.Dispose(); break }
        $shot.Bitmap.Dispose()
    }
    Start-Sleep -Milliseconds 250
}

if ($null -eq $btn) {
    Say '  [失败] 没找到签到按钮' 'Red'
    Say '         常见原因：未登录 / 网络异常 / 界面还在加载 / 被其它窗口遮挡' 'DarkGray'
    $shot = Get-WindowBitmap $hwnd
    if ($shot) {
        if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null }
        $shot.Bitmap.Save((Join-Path $script:LogDir '失败截图.png'), [System.Drawing.Imaging.ImageFormat]::Png)
        $shot.Bitmap.Dispose()
        Say '         已保存现场截图：日志\失败截图.png' 'DarkGray'
    }
    Write-Log '失败：未识别到签到按钮'
    exit 1
}

$clickX = $btn.Rect.Left + $btn.CX
$clickY = $btn.Rect.Top + $btn.CY

if ($btn.State -eq 'Signed' -and -not $TestClick) {
    Say '  [完成] 今天已经签到过了，无需重复签到' 'Green'
    Say ("         耗时 " + [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1) + " 秒") 'DarkGray'
    Write-Log "今日已签到（按钮位置 $clickX,$clickY）"
    if ($CloseAfter -eq '1') {
        Say '         正在关闭 StarSky ...' 'Gray'
        Close-StarSky $proc
    }
    exit 0
}

if ($btn.State -eq 'Signed' -and $TestClick) {
    Say '  [测试] 当前为已签到状态，强制点击一次按钮' 'DarkYellow'
}

Say ("  [3/3] 找到按钮（" + $btn.W + "x" + $btn.H + "），正在点击 ...") 'Gray'
Say ("         坐标：$clickX, $clickY") 'DarkGray'

$ok = $false
$uiChanged = $false     # 点完之后按钮"找不到了" —— 说明界面被提示框盖住了，多半是成功了
for ($try = 1; $try -le 2; $try++) {
    # 每次点击前都重新置前 + 重新识别，防止窗口被挪动/缩放后坐标失效
    [void](Bring-ToFront $hwnd)
    Start-Sleep -Milliseconds 250

    $shot = Get-WindowBitmap $hwnd
    if ($null -eq $shot) { continue }
    $cur = Find-Button $shot.Bitmap
    $rectNow = $shot.Rect
    $shot.Bitmap.Dispose()

    if ($cur.State -eq 'Signed' -and -not $TestClick) { $ok = $true; break }
    if ($cur.State -eq 'Unknown') {
        # 界面已经不是刚才那个样子了（弹窗/遮罩），不要盲目再点一次
        if ($uiChanged) { break }
        continue
    }

    $clickX = $rectNow.Left + $cur.CX
    $clickY = $rectNow.Top + $cur.CY

    # 关键安全校验：这个坐标上最顶层的窗口必须就是 StarSky，否则绝不点击
    if (-not (Test-PointBelongsTo $hwnd $clickX $clickY)) {
        Say '  [已中止] StarSky 窗口被别的窗口挡住了，为避免误点已停止' 'Red'
        Say '           请先让 StarSky 窗口露出来，再重新运行' 'DarkGray'
        Write-Log '失败：窗口被遮挡，未执行点击'
        exit 4
    }

    if (-not (Click-At $clickX $clickY)) {
        Say '  [失败] 鼠标模拟被系统拦截了（StarSky 是管理员权限运行的）' 'Red'
        Say '         请右键「一键签到.bat」→「以管理员身份运行」' 'DarkGray'
        Write-Log '失败：鼠标模拟被系统拦截，请以管理员身份运行'
        exit 5
    }
    Write-Log "已点击签到按钮（坐标 $clickX,$clickY，第 $try 次）"

    # 点完不用死等：每 200ms 看一眼按钮变灰了没有，变了立刻收工
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $unknownStreak = 0
    $escSent = $false
    $lastState = 'Unsigned'
    while ($sw.ElapsedMilliseconds -lt 8000) {
        Start-Sleep -Milliseconds 200
        $shot2 = Get-WindowBitmap $hwnd
        if ($null -eq $shot2) { continue }
        $after = Find-Button $shot2.Bitmap
        $shot2.Bitmap.Dispose()
        $lastState = $after.State

        if ($lastState -eq 'Signed') { $ok = $true; break }
        if ($lastState -eq 'Unknown') {
            $uiChanged = $true
            $unknownStreak++
            # 连续 5 次（约 1 秒）都找不到按钮 → 大概率弹了提示框，按 ESC 关掉再看
            if ($unknownStreak -ge 5 -and -not $escSent) {
                Send-Esc
                $escSent = $true
            }
        } else {
            $unknownStreak = 0
        }
    }
    if ($ok) { break }
    # 最后一次仍读不到按钮 → 界面被提示框盖着，别再盲点；否则说明还是蓝的，重试
    if ($lastState -eq 'Unknown') { break }
    Say "  [重试] 第 $try 次点击后按钮仍可点击，再试一次 ..." 'DarkYellow'
    Start-Sleep -Milliseconds 300
}

if ($ok) {
    Say '  [成功] 签到完成，流量已到账' 'Green'
    Say ("         耗时 " + [Math]::Round(((Get-Date) - $t0).TotalSeconds, 1) + " 秒") 'DarkGray'
    Write-Log '签到成功'
    if ($CloseAfter -eq '1') {
        Say '         正在关闭 StarSky ...' 'Gray'
        Close-StarSky $proc
    }
    exit 0
}

if ($uiChanged -and $lastState -eq 'Unknown') {
    # 点击后界面确实变了（按钮被提示框盖住），但没能读到"已签到"状态。
    # 这不算失败，留一张现场截图方便核对。
    Say '  [完成] 已点击签到，界面已弹出提示（未能自动确认结果）' 'Green'
    Say '         正常就是签成功了；想核对可看：日志\签到后界面.png' 'DarkGray'
    Write-Log '已点击签到，界面已变化但未确认到已签到状态'
    try {
        if (-not (Test-Path $script:LogDir)) { New-Item -ItemType Directory -Path $script:LogDir -Force | Out-Null }
        $s3 = Get-WindowBitmap $hwnd
        if ($s3) {
            $s3.Bitmap.Save((Join-Path $script:LogDir '签到后界面.png'), [System.Drawing.Imaging.ImageFormat]::Png)
            $s3.Bitmap.Dispose()
        }
    } catch {}
    if ($CloseAfter -eq '1') {
        Say '         正在关闭 StarSky ...' 'Gray'
        Close-StarSky $proc
    }
    exit 0
}

Say '  [失败] 点击后状态没有变化' 'Red'
Say '         可能是网络慢、按钮被弹窗挡住，或今天已经签到过' 'DarkGray'
Write-Log '失败：点击后状态未变化'
exit 1

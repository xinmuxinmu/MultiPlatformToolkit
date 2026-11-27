$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcherName = ([char]0x542f) + ([char]0x52a8) + '.bat'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'MultiPlatformToolkit.lnk'
$exePath = Join-Path $desktop 'MultiPlatformToolkit.exe'
$launcher = if (Test-Path -LiteralPath $exePath -PathType Leaf) {
    $exePath
} else {
    Join-Path $root $launcherName
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "Launcher not found: $launcher"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = if ($launcher -eq $exePath) { $desktop } else { $root }
$shortcut.Description = 'Launch MultiPlatformToolkit'
$iconPath = Join-Path $root 'src\launcher\assets\toolkit.ico'
if (Test-Path -LiteralPath $iconPath -PathType Leaf) {
    $shortcut.IconLocation = $iconPath + ',0'
}
$shortcut.WindowStyle = 7
$shortcut.Save()
Write-Output "Shortcut created: $shortcutPath"

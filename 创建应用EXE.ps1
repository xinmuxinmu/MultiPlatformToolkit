$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$sourcePath = Join-Path $root 'src\launcher\Program.cs'
$iconPath = Join-Path $root 'src\launcher\assets\toolkit.ico'
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Launcher source not found: $sourcePath"
}
if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) {
    throw "Launcher icon not found: $iconPath"
}

$desktop = [Environment]::GetFolderPath('Desktop')
$outputName = 'MultiPlatformToolkit.exe'
$outputPath = Join-Path $desktop $outputName
$source = [IO.File]::ReadAllText($sourcePath, [Text.Encoding]::UTF8)
$source = $source.Replace('__PROJECT_ROOT__', $root.Replace('"', '""'))

$buildDir = Join-Path ([IO.Path]::GetTempPath()) ('MultiPlatformToolkit-' + [guid]::NewGuid().ToString('N'))
New-Item -Path $buildDir -ItemType Directory | Out-Null
try {
    $buildSource = Join-Path $buildDir 'Program.cs'
    $buildOutput = Join-Path $buildDir $outputName
    [IO.File]::WriteAllText($buildSource, $source, [Text.Encoding]::UTF8)
    $compiler = Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    }
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
    }
    if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
        throw 'The .NET Framework C# compiler was not found.'
    }
    & $compiler /nologo /target:winexe /optimize+ /reference:System.Windows.Forms.dll "/win32icon:$iconPath" "/out:$buildOutput" $buildSource
    if ($LASTEXITCODE -ne 0) {
        throw "Launcher compilation failed with exit code $LASTEXITCODE"
    }
    Copy-Item -LiteralPath $buildOutput -Destination $outputPath -Force
}
finally {
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    $resolvedBuildDir = [IO.Path]::GetFullPath($buildDir)
    if ($resolvedBuildDir.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedBuildDir -Recurse -Force
    }
}

if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    throw "Launcher executable was not created: $outputPath"
}
Write-Output "Launcher created: $outputPath"

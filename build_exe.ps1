<#
.SYNOPSIS
    Builds the stand-alone "PNUT M&M" application with PyInstaller.

.DESCRIPTION
    Runs the project venv's PyInstaller on mnmparser.spec (windowed, one-dir).  The result is
    dist\PNUT M&M\PNUT M&M.exe plus its support files; config.json and logs\ are created
    next to the exe when it runs (config.PROJECT_ROOT resolves to the exe folder when frozen).

        powershell -ExecutionPolicy Bypass -File build_exe.ps1 [-Clean]

.PARAMETER Clean
    Remove build\ and dist\ before building.
#>
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python" }

if ($Clean) {
    foreach ($dir in @("build", "dist")) {
        $path = Join-Path $root $dir
        if (Test-Path $path) { Remove-Item -Recurse -Force $path }
    }
}

# The icon is drawn with QPainter; make sure the .ico exists for the exe resource.
$icon = Join-Path $root "assets\icon.ico"
if (-not (Test-Path $icon)) {
    Write-Host "Generating $icon ..."
    & $python -m mnmparse.app.icon $icon
    if ($LASTEXITCODE -ne 0) { throw "Icon generation failed ($LASTEXITCODE)" }
}

& $python -m PyInstaller --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the venv: run `"$python -m pip install pyinstaller`""
}

Write-Host "Building with PyInstaller ..."
& $python -m PyInstaller mnmparser.spec --noconfirm
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$exe = Join-Path $root "dist\PNUT M&M\PNUT M&M.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing" }
$size = [math]::Round((Get-ChildItem (Split-Path $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Built: $exe ($size MB in dist\PNUT M&M)"

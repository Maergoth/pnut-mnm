<#
.SYNOPSIS
    Builds the stand-alone "PNUT M&M" application with PyInstaller.

.DESCRIPTION
    Runs the project venv's PyInstaller on mnmparser.spec (windowed, one-dir).  The result is
    dist\PNUT M&M\PNUT M&M.exe plus its support files; config.json and logs\ are created
    next to the exe when it runs (config.PROJECT_ROOT resolves to the exe folder when frozen).

        powershell -ExecutionPolicy Bypass -File build_exe.ps1 [-Clean] [-DistRoot dist]

.PARAMETER Clean
    Remove the build cache before building. Never remove personal data from dist\.

.PARAMETER DistRoot
    Output parent inside this workspace, relative to it or absolute. Defaults to dist.
    Use a new dist\candidates\ folder to preserve a candidate containing personal files.
#>
param(
    [switch]$Clean,
    [string]$DistRoot = "dist"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Resolve-WorkspaceDirectory([string]$value) {
    if ([string]::IsNullOrWhiteSpace($value)) { throw "Output directory cannot be empty" }
    $workspace = [IO.Path]::GetFullPath($root).TrimEnd('\')
    $candidate = if ([IO.Path]::IsPathRooted($value)) { $value } else { Join-Path $workspace $value }
    $candidate = [IO.Path]::GetFullPath($candidate).TrimEnd('\')
    if (-not $candidate.StartsWith($workspace + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Output directory must be inside the workspace: $candidate"
    }
    $part = $candidate
    while ($part) {
        if (Test-Path -LiteralPath $part) {
            $item = Get-Item -LiteralPath $part -Force
            if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw "Output directory has a file or linked ancestor: $part"
            }
        }
        $parent = Split-Path -Parent $part
        if ($parent -eq $part) { break }
        $part = $parent
    }
    return $candidate
}

$distPath = Resolve-WorkspaceDirectory $DistRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw "venv python not found at $python" }

# PyInstaller replaces its output folder. Refuse to discard settings/logs if somebody
# has run that copy of the app; installed copies belong outside dist\.
$outputDir = Join-Path $distPath "PNUT M&M"
if (Test-Path -LiteralPath $outputDir) {
    [void](Resolve-WorkspaceDirectory $outputDir)
    $linked = Get-ChildItem -LiteralPath $outputDir -Recurse -Force | Where-Object {
        $_.Attributes -band [IO.FileAttributes]::ReparsePoint
    } | Select-Object -First 1
    if ($linked) { throw "Build output contains a linked entry: $($linked.FullName)" }
    $personal = Get-ChildItem -LiteralPath $outputDir -Force | Where-Object {
        $_.Name -notin @("PNUT M&M.exe", "_internal")
    }
    if ($personal) {
        throw "Build output contains personal or extra files: $($personal.Name -join ', '). Move that app folder outside dist before rebuilding."
    }
}

if ($Clean) {
    $cachePath = [IO.Path]::GetFullPath((Join-Path $root "build"))
    $expectedCachePath = [IO.Path]::GetFullPath($root).TrimEnd('\') + '\build'
    if ($cachePath -ne $expectedCachePath) { throw "Build cache is outside the project: $cachePath" }
    if (Test-Path -LiteralPath $cachePath) {
        if ((Get-Item -LiteralPath $cachePath).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing to clean a linked build cache: $cachePath"
        }
        Remove-Item -LiteralPath $cachePath -Recurse -Force
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
& $python scripts/generate_legal_notices.py
if ($LASTEXITCODE -ne 0) { throw "License notice generation failed ($LASTEXITCODE)" }
& $python scripts/build_provenance.py
if ($LASTEXITCODE -ne 0) { throw "Build provenance generation failed ($LASTEXITCODE)" }
$expectedBuild = Get-Content -LiteralPath (Join-Path $root "build\build-info.json") -Raw | ConvertFrom-Json
& $python -m PyInstaller mnmparser.spec --noconfirm --distpath $distPath
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$exe = Join-Path $outputDir "PNUT M&M.exe"
if (-not (Test-Path $exe)) { throw "Build finished but $exe is missing" }

# Test the delivered EXE, with no developer Python or Qt installations on its PATH.
# The stdlib-only diagnostic entry point captures a Qt DLL import failure in JSON.
$report = Join-Path $root ("build\startup-smoke-" + [guid]::NewGuid().ToString("N") + ".json")
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.FileName = $exe
$start.Arguments = '--smoke-test --report "' + $report + '"'
$start.WorkingDirectory = Split-Path -Parent $exe
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
$start.EnvironmentVariables["PATH"] = (Join-Path $env:SystemRoot "System32") + ";" + $env:SystemRoot
$start.EnvironmentVariables["QT_QPA_PLATFORM"] = "offscreen"
foreach ($key in @("PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH")) {
    $start.EnvironmentVariables.Remove($key)
}
Write-Host "Checking packaged startup ..."
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $start
try {
    if (-not $process.Start()) { throw "Could not start the packaged startup check" }
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        [void]$process.WaitForExit(3000)
        throw "Packaged startup check timed out. Diagnostic report: $report"
    }
    if (-not (Test-Path -LiteralPath $report)) {
        throw "Packaged startup produced no diagnostic report (exit $($process.ExitCode))"
    }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($process.ExitCode -ne 0 -or $result.ok -ne $true -or $result.frozen -ne $true -or $result.stage -ne "complete") {
        throw "Packaged startup failed (exit $($process.ExitCode)): $($result.error). Diagnostic report: $report"
    }
    if ($result.app_version -ne $expectedBuild.version -or $result.build.version -ne $expectedBuild.version -or
        $result.build.commit -ne $expectedBuild.commit -or $result.build.dirty -ne $expectedBuild.dirty) {
        throw "Packaged executable version/build provenance does not match this build. Diagnostic report: $report"
    }
} finally {
    $process.Dispose()
}
Write-Host "Packaged startup passed: $report"
$size = [math]::Round((Get-ChildItem (Split-Path $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Built: $exe ($size MB in $outputDir)"

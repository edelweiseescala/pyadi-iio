# Copyright (C) 2026 Analog Devices, Inc.
#
# SPDX short identifier: ADIBSD
<#
.SYNOPSIS
  Freezes admt4000_bridge.py into a standalone bridge.exe (PyInstaller) and
  bundles libiio's native DLLs next to it, so the Flutter app can spawn it
  on a machine with no Python or libiio installed.

.DESCRIPTION
  Output lands in windows\bridge\ inside the Flutter project, where the
  Windows CMake build picks it up and copies it alongside
  admt4000_dashboard.exe (see windows/CMakeLists.txt).

  libiio's native runtime (libiio.dll, libiio1.dll, libserialport.dll,
  libusb-1.0.dll, libxml2.dll, libzstd.dll) is NOT something PyInstaller's
  import analysis can discover -- the Python `iio` module loads it via
  ctypes.CDLL('libiio.dll') at runtime, not a normal import, so PyInstaller
  never sees that dependency. These must be copied in explicitly. Default
  source is C:\Windows\System32 (where the official libiio Windows
  installer places them); override -LibiioDir if yours live elsewhere.

.PARAMETER LibiioDir
  Directory containing the 6 libiio runtime DLLs. Default: C:\Windows\System32.

.EXAMPLE
  .\scripts\build_bridge.ps1
.EXAMPLE
  .\scripts\build_bridge.ps1 -LibiioDir "C:\Program Files\libiio\bin"
#>
param(
    [string]$LibiioDir = "$env:WINDIR\System32"
)

$ErrorActionPreference = "Stop"

$DashboardDir = Split-Path -Parent $PSScriptRoot
$Admt4000Dir = Split-Path -Parent $DashboardDir
$ExamplesDir = Split-Path -Parent $Admt4000Dir
$RepoRoot = Split-Path -Parent $ExamplesDir
$BridgeScript = Join-Path $Admt4000Dir "admt4000_bridge.py"
$OutDir = Join-Path $DashboardDir "windows\bridge"
$WorkDir = Join-Path $Admt4000Dir "build_bridge_work"
$DistDir = Join-Path $WorkDir "dist"

# The 6 DLLs the official libiio Windows installer drops together (confirmed
# by matching file timestamps in a known-good install). bridge.exe's own
# directory is searched by the OS loader before PATH, so dropping these next
# to bridge.exe is enough -- no installer needed on the target machine.
$LibiioDlls = @(
    "libiio.dll",
    "libiio1.dll",
    "libserialport.dll",
    "libusb-1.0.dll",
    "libxml2.dll",
    "libzstd.dll"
)

Write-Host "Repo root:      $RepoRoot"
Write-Host "Bridge script:  $BridgeScript"
Write-Host "Output dir:     $OutDir"
Write-Host ""

if (-not (Test-Path $BridgeScript)) {
    throw "Bridge script not found: $BridgeScript"
}

# --- 1. Run PyInstaller ---------------------------------------------------
Write-Host "Building bridge.exe with PyInstaller..."
pyinstaller `
    --onefile `
    --name bridge `
    --distpath $DistDir `
    --workpath (Join-Path $WorkDir "pyinstaller-build") `
    --specpath $WorkDir `
    --paths $RepoRoot `
    --paths $Admt4000Dir `
    --collect-submodules adi `
    --hidden-import admt4000_app `
    --console `
    $BridgeScript

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed (exit code $LASTEXITCODE)"
}

$BuiltExe = Join-Path $DistDir "bridge.exe"
if (-not (Test-Path $BuiltExe)) {
    throw "Expected PyInstaller output not found: $BuiltExe"
}

# --- 2. Assemble windows\bridge\ ------------------------------------------
if (Test-Path $OutDir) {
    Remove-Item $OutDir -Recurse -Force
}
New-Item -ItemType Directory -Path $OutDir | Out-Null

Copy-Item $BuiltExe $OutDir
Write-Host "Copied bridge.exe -> $OutDir"

foreach ($dll in $LibiioDlls) {
    $src = Join-Path $LibiioDir $dll
    if (-not (Test-Path $src)) {
        throw "Missing libiio DLL: $src (pass -LibiioDir if libiio is installed elsewhere)"
    }
    Copy-Item $src $OutDir
}
Write-Host "Copied $($LibiioDlls.Count) libiio runtime DLLs -> $OutDir"

# --- 3. Clean up PyInstaller intermediates ---------------------------------
Remove-Item $WorkDir -Recurse -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done. Contents of $OutDir :"
Get-ChildItem $OutDir | Select-Object Name, Length
Write-Host ""
Write-Host "Next: flutter build windows --release (picks this up via windows/CMakeLists.txt)"

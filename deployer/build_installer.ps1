# build_installer.ps1 - package dist\ARGUS.exe into a Windows installer.
# Pure ASCII (same PS 5.1 encoding trap as build.ps1).
# Run from deployer\:  powershell -ExecutionPolicy Bypass -File build_installer.ps1
#
# Prereqs:
#   1. dist\ARGUS.exe exists (run build.ps1 first)
#   2. Inno Setup 6 installed (winget install JRSoftware.InnoSetup)
#   3. installer\redist\MicrosoftEdgeWebView2Setup.exe present (evergreen
#      bootstrapper, ~2 MB) - bundled so offline-ish machines still get WebView2
#
# Output: dist\installer\ARGUS-Setup-<version>.exe

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path "dist\ARGUS.exe")) {
    throw "dist\ARGUS.exe not found - run build.ps1 first."
}
if (-not (Test-Path "installer\redist\MicrosoftEdgeWebView2Setup.exe")) {
    throw "installer\redist\MicrosoftEdgeWebView2Setup.exe missing - download the evergreen bootstrapper from https://developer.microsoft.com/microsoft-edge/webview2/ first."
}

$iscc = @(
    "$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) {
    throw "Inno Setup 6 not found - install with: winget install JRSoftware.InnoSetup"
}

& $iscc "installer\argus.iss"
if ($LASTEXITCODE -ne 0) { throw "ISCC failed with exit code $LASTEXITCODE" }

Write-Host "`nDone -> dist\installer\ (ARGUS-Setup-*.exe)"

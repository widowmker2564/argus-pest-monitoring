# build.ps1 - package the ARGUS deployer into a single Windows .exe.
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less
# files as ANSI; a UTF-8 em-dash decodes into a smart quote and breaks the
# parser (bit us 2026-07-17).
# Run from deployer/:  powershell -ExecutionPolicy Bypass -File build.ps1
#
# Produces dist\ARGUS.exe. Ship it alongside MicrosoftEdgeWebView2Setup.exe
# (the ~2 MB evergreen bootstrapper) in case the target lacks the WebView2 runtime.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# ---- 1. deps ----
# Pillow is a runtime dep of training.py (PyInstaller collects it via the import)
python -m pip install --upgrade pyinstaller boto3 pywebview keyring Pillow | Out-Null

# ---- 2. prebuild the Pillow Lambda layer ONCE (a frozen exe cannot pip-build it) ----
# deploy.py's stage_layer publishes layer\fyp-pillow.zip when present.
$layerDir = Join-Path $PSScriptRoot "layer"
$zipPath  = Join-Path $layerDir "fyp-pillow.zip"
if (-not (Test-Path $zipPath)) {
    Write-Host "Prebuilding Pillow 12.2.0 layer zip ..."
    $stage = Join-Path $env:TEMP "argus-layer\python"
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    python -m pip install --platform manylinux2014_x86_64 --implementation cp `
        --python-version 3.12 --only-binary=:all: --target $stage "Pillow==12.2.0"
    if (-not (Get-ChildItem "$stage\PIL\_imaging*.so" -ErrorAction SilentlyContinue)) {
        throw "Pillow layer build is missing PIL/_imaging*.so - aborting."
    }
    New-Item -ItemType Directory -Force -Path $layerDir | Out-Null
    Compress-Archive -Path (Join-Path $env:TEMP "argus-layer\python") -DestinationPath $zipPath -Force
    Write-Host "Wrote $zipPath"
}

# ---- 3. package ----
# Windows --add-data separator is ';'. asset_dir() expects web/ at _MEIPASS\web,
# so pack it as "web;web" (NOT "web;.").
# python -m PyInstaller: the bare 'pyinstaller' exe lives in a Scripts dir
# that is not reliably on PATH (failed 2026-07-17); the module form always is.
# App icon (assets\make_icon.py generates the candidates; B = liquid glass is
# the shipping one, Runzhe's pick 2026-07-21) + version resource so
# Explorer/Task Manager show real metadata.
python -m PyInstaller app.py --onefile --noconsole --name ARGUS `
    --icon "assets\argus_B.ico" `
    --version-file "assets\version_info.txt" `
    --add-data "web;web" `
    --add-data "..\web\dashboard_v4;web\dashboard_v4" `
    --add-data "audit;audit" `
    --add-data "legal;legal" `
    --add-data "layer;layer" `
    --add-data "..\lambda;lambda" `
    --exclude-module PyQt5 --exclude-module PyQt6 `
    --exclude-module PySide2 --exclude-module PySide6 --exclude-module gi `
    --hidden-import keyring.backends.Windows `
    --collect-data botocore --collect-data boto3

Write-Host "`nDone -> dist\ARGUS.exe"
Write-Host "Ship MicrosoftEdgeWebView2Setup.exe next to it for machines without WebView2."

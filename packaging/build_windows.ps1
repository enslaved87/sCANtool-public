# Build dist/sCANtool/sCANtool.exe (onedir). Run from repo root or here.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip failed" }

python -m PyInstaller --noconfirm --clean --distpath dist --workpath build `
    "$Root\packaging\scantool_public.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = Join-Path $Root "dist\sCANtool\sCANtool.exe"
if (-not (Test-Path $exe)) { throw "missing $exe" }
Write-Host "built $exe"

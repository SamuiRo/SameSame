<#
.SYNOPSIS
  Builds the SameSame Windows release: installer, no-install zip, and checksums.

.DESCRIPTION
  1. Creates a clean build virtualenv in build\venv (never the dev .venv, so a
     stray dev package cannot end up in the release).
  2. Freezes the app with PyInstaller into build\dist\SameSame.
  3. Smoke-tests the frozen CLI.
  4. Writes to dist\:
       SameSame-<ver>-win64-setup.exe   installer (per user, no admin prompt)
       SameSame-<ver>-win64.zip         same files, unzip and run
       SHA256SUMS-<ver>.txt

  Requires Python 3.11+ on PATH (or -Python) and Inno Setup 6
  (winget install JRSoftware.InnoSetup).

.EXAMPLE
  .\tools\build_windows.ps1
#>
[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$SkipInstaller,
    [switch]$Reuse   # keep build\venv between runs to save a few minutes
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Invoke-Checked([string]$What, [scriptblock]$Block) {
    Write-Host "==> $What" -ForegroundColor Cyan
    # Native tools log to stderr; under Windows PowerShell 5.1 with "Stop" that
    # becomes a terminating error once output is redirected. Judge by exit code.
    $ErrorActionPreference = "Continue"
    & $Block
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

$Version = (Select-String -Path "dedupe\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "SameSame $Version" -ForegroundColor Green

$BuildDir = Join-Path $Root "build"
$Venv = Join-Path $BuildDir "venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$FrozenDir = Join-Path $BuildDir "dist\SameSame"
$OutDir = Join-Path $Root "dist"

if (-not $Reuse -and (Test-Path $Venv)) { Remove-Item -Recurse -Force $Venv }
if (-not (Test-Path $VenvPython)) {
    Invoke-Checked "Create build virtualenv" { & $Python -m venv $Venv }
}
Invoke-Checked "Install SameSame and PyInstaller" {
    & $VenvPython -m pip install --disable-pip-version-check --quiet --upgrade pip
    & $VenvPython -m pip install --disable-pip-version-check --quiet ".[gui,ai]" "pyinstaller>=6.11,<7"
}

foreach ($dir in @((Join-Path $BuildDir "dist"), (Join-Path $BuildDir "work"))) {
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir }
}
Invoke-Checked "Freeze with PyInstaller" {
    & $VenvPython -m PyInstaller packaging\windows\samesame.spec --noconfirm --clean `
        --distpath (Join-Path $BuildDir "dist") --workpath (Join-Path $BuildDir "work")
}

Write-Host "==> Smoke test" -ForegroundColor Cyan
$Reported = (& (Join-Path $FrozenDir "samesame-cli.exe") --version).Trim()
if ($Reported -ne "samesame $Version") { throw "Frozen CLI reported '$Reported', expected 'samesame $Version'" }
if (-not (Test-Path (Join-Path $FrozenDir "SameSame.exe"))) { throw "SameSame.exe missing from the build" }
if (-not (Test-Path (Join-Path $FrozenDir "_internal\dedupe\gui\assets\samesame.ico"))) { throw "GUI icon missing from the build" }
Write-Host "    $Reported OK"

New-Item -ItemType Directory -Force $OutDir | Out-Null
$Zip = Join-Path $OutDir "SameSame-$Version-win64.zip"
$Setup = Join-Path $OutDir "SameSame-$Version-win64-setup.exe"
foreach ($file in @($Zip, $Setup)) { if (Test-Path $file) { Remove-Item -Force $file } }

Write-Host "==> Zip" -ForegroundColor Cyan
Compress-Archive -Path $FrozenDir -DestinationPath $Zip -CompressionLevel Optimal

$Artifacts = @($Zip)
if (-not $SkipInstaller) {
    $Iscc = @(
        (Get-Command iscc -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $Iscc) { throw "Inno Setup 6 not found. Install it with: winget install JRSoftware.InnoSetup (or pass -SkipInstaller)" }
    Invoke-Checked "Build installer" {
        & $Iscc /Q "/DAppVersion=$Version" "/DSourceDir=$FrozenDir" "/DOutputDir=$OutDir" packaging\windows\samesame.iss
    }
    $Artifacts = @($Setup) + $Artifacts
}

$Sums = Join-Path $OutDir "SHA256SUMS-$Version.txt"
$Artifacts | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash -Algorithm SHA256 $_).Hash.ToLower(), (Split-Path $_ -Leaf)
} | Set-Content -Encoding ascii $Sums

Write-Host ""
Write-Host "Done:" -ForegroundColor Green
($Artifacts + $Sums) | ForEach-Object { "  {0}  ({1:N1} MB)" -f (Split-Path $_ -Leaf), ((Get-Item $_).Length / 1MB) }

<#
.SYNOPSIS
    Build a Windows executable for QuantsMind-Agent.

.DESCRIPTION
    Runs PyInstaller in one-directory mode and verifies that the built
    application reports its configuration.

    One directory is used rather than one file: extraction on every launch
    costs seconds and makes antivirus scanning unreliable. An installer
    framework is deliberately out of scope for v0.1 -- see
    docs/source/installation.rst.

.PARAMETER SkipInstall
    Do not install the packaging extra; assume it is already present.

.EXAMPLE
    scripts\package.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

Push-Location $repoRoot
try {
    if (-not $SkipInstall) {
        Write-Host "Installing the packaging extra..." -ForegroundColor Cyan
        & $python -m pip install -e '.[packaging]'
        if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
    }

    Write-Host "Building the Windows bundle..." -ForegroundColor Cyan
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name 'QuantsMind-Agent' `
        --paths (Join-Path $repoRoot 'src') `
        --collect-submodules quantsmind_agent `
        --hidden-import quantsmind_agent.__main__ `
        (Join-Path $repoRoot 'scripts\pyinstaller_entry.py')

    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed (exit $LASTEXITCODE)" }

    $exe = Join-Path $repoRoot 'dist\QuantsMind-Agent\QuantsMind-Agent.exe'
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "PyInstaller reported success but $exe is missing."
    }

    Write-Host "Verifying the bundle..." -ForegroundColor Cyan
    & $exe --check-config | Out-Host
    $verifyExit = $LASTEXITCODE
    if ($verifyExit -gt 1) {
        throw "The bundled executable failed to start (exit $verifyExit)."
    }

    Write-Host ""
    Write-Host "Built: $exe" -ForegroundColor Green
    Write-Host "Ship the whole dist\QuantsMind-Agent directory, not the .exe alone."
    Write-Host ""
    Write-Host "A green build is NOT the same as acceptance. GUI rendering and"
    Write-Host "real-LLM behaviour are verified manually -- see"
    Write-Host "docs\source\testing\acceptance-v0.1.rst."
}
finally {
    Pop-Location
}

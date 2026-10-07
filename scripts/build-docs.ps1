<#
.SYNOPSIS
    Build the QuantsMind-Agent HTML documentation (strict).

.DESCRIPTION
    Builds docs\build\html\index.html from the reStructuredText sources in
    docs\source. Any Sphinx warning fails the build, so documentation errors
    cannot slip through.

.PARAMETER AllowWarnings
    Build without failing on warnings.

.PARAMETER Clean
    Remove docs\build before building.

.EXAMPLE
    scripts\build-docs.ps1
    scripts\build-docs.ps1 -Clean
#>
[CmdletBinding()]
param(
    [switch]$AllowWarnings,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

Push-Location $repoRoot
try {
    $arguments = @('scripts\build_docs.py')
    if ($AllowWarnings) { $arguments += '--allow-warnings' }
    if ($Clean) { $arguments += '--clean' }

    Write-Host "Building QuantsMind-Agent documentation..." -ForegroundColor Cyan
    & $python @arguments
    $exitCode = $LASTEXITCODE

    if ($exitCode -ne 0) {
        Write-Host "Documentation build FAILED (exit $exitCode)." -ForegroundColor Red
        exit $exitCode
    }

    $index = Join-Path $repoRoot 'docs\build\html\index.html'
    if (-not (Test-Path -LiteralPath $index)) {
        Write-Host "Documentation build reported success but index.html is missing." -ForegroundColor Red
        exit 1
    }

    Write-Host "Documentation built: $index" -ForegroundColor Green
    exit 0
}
finally {
    Pop-Location
}

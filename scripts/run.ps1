<#
.SYNOPSIS
    Launch QuantsMind-Agent from source.

.DESCRIPTION
    Runs the application with the repository's src directory on PYTHONPATH, so
    no editable install is required. Useful for trying a change without
    reinstalling.

.PARAMETER Workspace
    Project folder to open on startup.

.PARAMETER Debug
    Enable verbose logging.

.EXAMPLE
    scripts\run.ps1
    scripts\run.ps1 -Workspace C:\code\my-project -Debug
#>
[CmdletBinding()]
param(
    [string]$Workspace = '',
    [switch]$Debug
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = 'python'
}

$env:PYTHONPATH = Join-Path $repoRoot 'src'

$arguments = @('-m', 'quantsmind_agent')
if ($Workspace) { $arguments += @('--workspace', $Workspace) }
if ($Debug) { $arguments += '--debug' }

Push-Location $repoRoot
try {
    & $python @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}

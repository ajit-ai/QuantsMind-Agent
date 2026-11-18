<#
.SYNOPSIS
    Verify Authenticode signatures and timestamps on Windows release artifacts.

.DESCRIPTION
    This script performs read-only verification. It does not sign, repair, copy
    or replace an artifact. Every path must have a valid, timestamped signature;
    pass -ExpectedThumbprint to require a specific certificate or
    -ExpectedSubject to require the certificate's exact common name. Use
    -AllowUnsigned only for an explicitly non-release inspection.

.EXAMPLE
    powershell -File scripts/verify_windows_signature.ps1 `
        -Path artifacts\v1.0.0\windows\msi\QuantsMindAgent-1.0.0-x64.msi `
        -ExpectedThumbprint $env:QM_SIGNING_CERT_SHA1
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string[]]$Path,
    [string]$ExpectedThumbprint = $env:QM_SIGNING_CERT_SHA1,
    [string]$ExpectedSubject = '',
    [switch]$RequireTimestamp = $true,
    [switch]$AllowUnsigned
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$results = New-Object System.Collections.ArrayList
function Check($name, $ok, $detail = '') {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    [void]$results.Add([pscustomobject]@{ Name = $name; Status = $status; Detail = $detail })
    Write-Host ("[{0}] {1}{2}" -f $status, $name, $(if ($detail) { " -- $detail" } else { '' }))
}

if ($ExpectedThumbprint) {
    $normalized = ($ExpectedThumbprint -replace '[\s:]', '').ToUpperInvariant()
    if ($normalized -notmatch '^[0-9A-F]{40}$') {
        throw 'ExpectedThumbprint must be a 40-character SHA-1 thumbprint'
    }
} else {
    $normalized = $null
}

foreach ($item in $Path) {
    $resolved = if ([IO.Path]::IsPathRooted($item)) { $item } else { Join-Path $repoRoot $item }
    Check 'artifact exists' (Test-Path -LiteralPath $resolved -PathType Leaf) $resolved
    if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { continue }

    $signature = Get-AuthenticodeSignature -LiteralPath $resolved
    if ($AllowUnsigned -and $signature.Status -eq 'NotSigned') {
        Write-Host "[SKIP] $resolved is explicitly allowed to be unsigned"
        continue
    }
    Check 'signature status is Valid' ($signature.Status -eq 'Valid') "$($signature.Status)"
    if ($signature.SignerCertificate) {
        Check 'signer thumbprint matches' `
            (-not $normalized -or $signature.SignerCertificate.Thumbprint -eq $normalized) `
            $signature.SignerCertificate.Thumbprint
        Check 'signer subject is present' `
            (-not [string]::IsNullOrWhiteSpace($signature.SignerCertificate.Subject)) `
            $signature.SignerCertificate.Subject
        if ($ExpectedSubject) {
            $commonName = $signature.SignerCertificate.GetNameInfo(
                [Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false
            )
            Check 'signer common name matches' ($commonName -eq $ExpectedSubject) $commonName
        }
    } else {
        Check 'signer certificate is present' $false 'no signer certificate'
    }
    if ($RequireTimestamp) {
        Check 'signature is timestamped' `
            ($null -ne $signature.TimeStamperCertificate) `
            $(if ($signature.TimeStamperCertificate) { $signature.TimeStamperCertificate.Subject } else { 'no timestamp' })
    }
}

$failures = @($results | Where-Object { $_.Status -eq 'FAIL' })
Write-Host ''
Write-Host ("{0}/{1} signature checks passed" -f ($results.Count - $failures.Count), $results.Count)
if ($failures.Count) { exit 1 }
exit 0

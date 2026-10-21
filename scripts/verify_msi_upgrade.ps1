<#
.SYNOPSIS
    Plan or execute a destructive MSI upgrade, rollback and uninstall matrix.

.DESCRIPTION
    This harness is fail-closed. It validates both artifacts, versions, product
    identities and signatures before touching the machine. Without -Execute it
    performs preflight only and changes nothing. Execution additionally requires
    -AuthorizeDestructiveInstall and aborts if QuantsMind-Agent is already
    installed.

    The executed matrix is:

    1. install the older MSI
    2. upgrade in place to the newer MSI
    3. uninstall the newer product
    4. reinstall the older MSI as the application rollback
    5. uninstall the older product

    A real v0.9.0 -> v1.0.0 run needs an authorized v1.0.0 MSI and a disposable
    Windows host or VM. This script does not fabricate either.

.PARAMETER FromMsi
    The older, already qualified MSI.

.PARAMETER ToMsi
    The newer MSI to install over it.

.PARAMETER EvidenceDir
    Directory for JSON evidence and MSI logs. Required with -Execute.

.PARAMETER Execute
    Perform the installs. Without this switch the command is preflight-only.

.PARAMETER AuthorizeDestructiveInstall
    Explicit acknowledgement that this host may be modified.

.PARAMETER RequireSignedTarget
    Require a valid, timestamped signature on the newer MSI.

.EXAMPLE
    powershell -File scripts/verify_msi_upgrade.ps1 `
        -FromMsi artifacts\v0.9.0\windows\msi\QuantsMindAgent-0.9.0-x64.msi `
        -ToMsi artifacts\candidates\v1.0.0\<commit>\windows\msi\QuantsMindAgent-1.0.0-x64.msi
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$FromMsi,
    [Parameter(Mandatory = $true)][string]$ToMsi,
    [string]$EvidenceDir = '',
    [switch]$Execute,
    [switch]$AuthorizeDestructiveInstall,
    [switch]$RequireSignedTarget,
    [string]$ExpectedThumbprint = $env:QM_SIGNING_CERT_SHA1
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($FromMsi)) { $FromMsi = Join-Path $repoRoot $FromMsi }
if (-not [IO.Path]::IsPathRooted($ToMsi)) { $ToMsi = Join-Path $repoRoot $ToMsi }
if ($EvidenceDir -and -not [IO.Path]::IsPathRooted($EvidenceDir)) {
    $EvidenceDir = Join-Path $repoRoot $EvidenceDir
}

$results = New-Object System.Collections.ArrayList
function Check($name, $ok, $detail = '') {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    [void]$results.Add([pscustomobject]@{ Name = $name; Status = $status; Detail = $detail })
    Write-Host ("[{0}] {1}{2}" -f $status, $name, $(if ($detail) { " -- $detail" } else { '' }))
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Get-MsiProperty([string]$Msi, [string]$Name) {
    $installer = New-Object -ComObject WindowsInstaller.Installer
    $database = $installer.GetType().InvokeMember(
        'OpenDatabase', [Reflection.BindingFlags]::InvokeMethod, $null, $installer, @($Msi, 0)
    )
    $view = $database.GetType().InvokeMember(
        'OpenView', [Reflection.BindingFlags]::InvokeMethod, $null, $database,
        @("SELECT Value FROM Property WHERE Property='$Name'")
    )
    [void]$view.GetType().InvokeMember(
        'Execute', [Reflection.BindingFlags]::InvokeMethod, $null, $view, $null
    )
    $record = $view.GetType().InvokeMember(
        'Fetch', [Reflection.BindingFlags]::InvokeMethod, $null, $view, $null
    )
    if (-not $record) { throw "MSI $Msi has no $Name property" }
    return [string]$record.GetType().InvokeMember(
        'StringData', [Reflection.BindingFlags]::GetProperty, $null, $record, @(1)
    )
}

function Get-QuantsMindArpEntries {
    $roots = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $entries = foreach ($root in $roots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | ForEach-Object {
            $item = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            if ($item.DisplayName -eq 'QuantsMind-Agent') { $item }
        }
    }
    return @($entries)
}

function Get-ProfileFingerprint([string]$Root) {
    if (-not (Test-Path -LiteralPath $Root)) { return 'absent' }
    $parts = Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        ForEach-Object {
            $file = $_
            try {
                "$($file.FullName.Substring($Root.Length))|$((Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256 -ErrorAction Stop).Hash)"
            } catch {
                "$($file.FullName.Substring($Root.Length))|locked|$($file.Length)|$($file.LastWriteTimeUtc.Ticks)"
            }
        }
    return ($parts -join ';')
}

function Invoke-Msi([string]$Arguments, [string]$LogPath) {
    $work = Start-Process -FilePath 'msiexec.exe' -ArgumentList $Arguments -Wait -PassThru -NoNewWindow
    return [pscustomobject]@{ Code = $work.ExitCode; Log = $LogPath }
}

$fromVersion = $null
$toVersion = $null
$fromProductCode = $null
$toProductCode = $null
$fromUpgradeCode = $null
$toUpgradeCode = $null

Check 'older MSI exists' (Test-Path -LiteralPath $FromMsi -PathType Leaf) $FromMsi
Check 'newer MSI exists' (Test-Path -LiteralPath $ToMsi -PathType Leaf) $ToMsi

if ((Test-Path -LiteralPath $FromMsi -PathType Leaf) -and (Test-Path -LiteralPath $ToMsi -PathType Leaf)) {
    try {
        $fromVersion = Get-MsiProperty $FromMsi 'ProductVersion'
        $toVersion = Get-MsiProperty $ToMsi 'ProductVersion'
        $fromProductCode = Get-MsiProperty $FromMsi 'ProductCode'
        $toProductCode = Get-MsiProperty $ToMsi 'ProductCode'
        $fromUpgradeCode = Get-MsiProperty $FromMsi 'UpgradeCode'
        $toUpgradeCode = Get-MsiProperty $ToMsi 'UpgradeCode'
    } catch {
        Check 'MSI metadata is readable' $false $_.Exception.Message
    }

    if ($fromVersion -and $toVersion) {
        Check 'upgrade target is newer' ([version]$toVersion -gt [version]$fromVersion) `
            "$fromVersion -> $toVersion"
        Check 'ProductCodes differ' ($fromProductCode -ne $toProductCode) `
            "$fromProductCode -> $toProductCode"
        Check 'UpgradeCode is stable' ($fromUpgradeCode -eq $toUpgradeCode) `
            "$fromUpgradeCode / $toUpgradeCode"
    }
}

if ($RequireSignedTarget) {
    $verifier = Join-Path $PSScriptRoot 'verify_windows_signature.ps1'
    if (-not $ExpectedThumbprint) {
        Check 'newer MSI signature verification' $false `
            'ExpectedThumbprint or QM_SIGNING_CERT_SHA1 is required'
    } elseif (Test-Path -LiteralPath $verifier -PathType Leaf) {
        & powershell -NoProfile -NonInteractive -File $verifier `
            -Path $ToMsi -ExpectedThumbprint $ExpectedThumbprint
        Check 'newer MSI signature verification' ($LASTEXITCODE -eq 0) "exit=$LASTEXITCODE"
    } else {
        Check 'newer MSI signature verification' $false 'verify_windows_signature.ps1 is missing'
    }
}

$existing = Get-QuantsMindArpEntries
Check 'no existing QuantsMind-Agent installation' ($existing.Count -eq 0) `
    $(if ($existing.Count) { "$($existing.Count) installed entr(ies)" } else { 'clean host' })

$failures = @($results | Where-Object { $_.Status -eq 'FAIL' })
if ($failures.Count) {
    Write-Host "`npreflight failed; nothing was installed" -ForegroundColor Red
    exit 1
}

$plan = @(
    'install older MSI',
    'upgrade in place to newer MSI',
    'uninstall newer product',
    'reinstall older MSI as application rollback',
    'uninstall older product'
)

if (-not $Execute) {
    Write-Host "`npreflight passed; planned matrix:"
    $plan | ForEach-Object { Write-Host "  - $_" }
    Write-Host 'no installs were performed; pass -Execute -AuthorizeDestructiveInstall to run'
    exit 0
}

if (-not $AuthorizeDestructiveInstall) {
    Write-Host 'refusing to modify this host without -AuthorizeDestructiveInstall' -ForegroundColor Red
    exit 2
}
if (-not $EvidenceDir) {
    Write-Host '-EvidenceDir is required with -Execute' -ForegroundColor Red
    exit 2
}
New-Item -ItemType Directory -Force -Path $EvidenceDir | Out-Null

$installDir = Join-Path $env:LOCALAPPDATA 'Programs\QuantsMindAgent'
$userDataDir = Join-Path $env:LOCALAPPDATA 'QuantsMind-Agent'
$beforeProfile = Get-ProfileFingerprint $userDataDir
$profileUnchanged = $false
$summary = [ordered]@{
    started_utc = [DateTime]::UtcNow.ToString('o')
    host = $env:COMPUTERNAME
    from_msi = $FromMsi
    to_msi = $ToMsi
    from_version = $fromVersion
    to_version = $toVersion
    from_product_code = $fromProductCode
    to_product_code = $toProductCode
    from_sha256 = Get-Sha256 $FromMsi
    to_sha256 = Get-Sha256 $ToMsi
    steps = @()
}

function Record-Step($Name, $Code, $Log) {
    $entry = [ordered]@{ step = $Name; exit_code = $Code; log = $Log }
    $summary.steps += $entry
    Check $Name ($Code -eq 0 -or $Code -eq 3010) "exit=$Code log=$Log"
}

try {
    $log = Join-Path $EvidenceDir '01-install-older.log'
    $result = Invoke-Msi "/i `"$FromMsi`" /qn /norestart /l*v `"$log`"" $log
    Record-Step 'install older MSI' $result.Code $log
    if ($result.Code -notin @(0, 3010)) { throw 'older MSI install failed' }

    $log = Join-Path $EvidenceDir '02-upgrade-to-newer.log'
    $result = Invoke-Msi "/i `"$ToMsi`" /qn /norestart /l*v `"$log`"" $log
    Record-Step 'upgrade to newer MSI' $result.Code $log
    if ($result.Code -notin @(0, 3010)) { throw 'upgrade failed' }

    $afterUpgrade = Get-QuantsMindArpEntries
    Check 'upgrade leaves one installed entry' ($afterUpgrade.Count -eq 1) "$($afterUpgrade.Count)"
    Check 'installed version is the newer version' `
        ($afterUpgrade.Count -eq 1 -and $afterUpgrade[0].DisplayVersion -eq $toVersion) `
        $(if ($afterUpgrade.Count) { $afterUpgrade[0].DisplayVersion } else { 'none' })
    Check 'install directory exists after upgrade' (Test-Path -LiteralPath $installDir) $installDir
    Check 'upgraded executable exists' `
        (Test-Path -LiteralPath (Join-Path $installDir 'QuantsMindAgent.exe')) `
        (Join-Path $installDir 'QuantsMindAgent.exe')

    $log = Join-Path $EvidenceDir '03-uninstall-newer.log'
    $result = Invoke-Msi "/x `"$ToMsi`" /qn /norestart /l*v `"$log`"" $log
    Record-Step 'uninstall newer MSI' $result.Code $log
    if ($result.Code -notin @(0, 3010)) { throw 'newer MSI uninstall failed' }

    $log = Join-Path $EvidenceDir '04-reinstall-older.log'
    $result = Invoke-Msi "/i `"$FromMsi`" /qn /norestart /l*v `"$log`"" $log
    Record-Step 'reinstall older MSI' $result.Code $log
    if ($result.Code -notin @(0, 3010)) { throw 'rollback reinstall failed' }

    $afterRollback = Get-QuantsMindArpEntries
    Check 'rollback installs the older version' `
        ($afterRollback.Count -eq 1 -and $afterRollback[0].DisplayVersion -eq $fromVersion) `
        $(if ($afterRollback.Count) { $afterRollback[0].DisplayVersion } else { 'none' })

    $log = Join-Path $EvidenceDir '05-uninstall-older.log'
    $result = Invoke-Msi "/x `"$FromMsi`" /qn /norestart /l*v `"$log`"" $log
    Record-Step 'uninstall older MSI' $result.Code $log
    if ($result.Code -notin @(0, 3010)) { throw 'older MSI uninstall failed' }

    $final = Get-QuantsMindArpEntries
    Check 'uninstall removes the installed entry' ($final.Count -eq 0) "$($final.Count)"
    Check 'uninstall removes the install directory' `
        (-not (Test-Path -LiteralPath $installDir)) $installDir
    $afterProfile = Get-ProfileFingerprint $userDataDir
    $profileUnchanged = ($beforeProfile -eq $afterProfile)
    Check 'real user data is byte-identical through the matrix' `
        $profileUnchanged `
        $(if ($profileUnchanged) { 'byte-identical' } else { 'CHANGED' })
} catch {
    Check 'matrix completed without an exception' $false $_.Exception.Message
} finally {
    $summary.finished_utc = [DateTime]::UtcNow.ToString('o')
    $summary.profile_unchanged = $profileUnchanged
    $summary.results = $results
    $summaryPath = Join-Path $EvidenceDir 'upgrade-rollback-evidence.json'
    $summary | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
    Write-Host "`nevidence: $summaryPath"
}

$failures = @($results | Where-Object { $_.Status -eq 'FAIL' })
if ($failures.Count) {
    Write-Host ("{0}/{1} matrix checks failed" -f $failures.Count, $results.Count) -ForegroundColor Red
    exit 1
}
Write-Host ("{0}/{1} matrix checks passed" -f $results.Count, $results.Count)
exit 0







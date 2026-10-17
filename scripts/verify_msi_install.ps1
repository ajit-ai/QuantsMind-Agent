<#
.SYNOPSIS
    Install, launch, re-install and uninstall a QuantsMind-Agent MSI, and report
    what actually happened.

.DESCRIPTION
    scripts\build_msi.py produces the package. Producing it is not the same as
    proving it works: an installer can build cleanly and still install into the
    wrong place, forget its shortcuts, leave the application unable to start, or
    take the user's configuration and projects with it on uninstall. Each of
    those is a defect only someone can find by doing it, so this does it.

    The checks, in order:

    1. the real user profile is fingerprinted before anything happens
    2. silent install, then: install directory, file count, executable,
       Start Menu shortcut, Desktop shortcut, Add/Remove Programs entry
    3. launch the *installed* executable with an isolated temporary config
       directory and workspace, confirm a real titled window appears, close it
       through the window, and confirm no process is left behind
    4. install the same package again: an upgrade or repair must not duplicate
       the Add/Remove Programs entry or duplicate files
    5. uninstall, then: install directory gone, shortcuts gone, Add/Remove
       Programs entry gone
    6. the real user profile is fingerprinted again and must be byte-identical

    That last pair is the one worth reading. Configuration, credentials, logs
    and projects live outside the installation directory, and an installer that
    "cleans up" a directory it does not own will quietly delete a user's work.
    This measures it rather than trusting the file table.

.PARAMETER Msi
    The MSI to test.

.PARAMETER InstallDir
    Override the expected per-user install directory.

.PARAMETER Timeout
    Seconds to wait for the application window.

.EXAMPLE
    scripts\verify_msi_install.ps1 -Msi artifacts\v0.2.0\windows\msi\QuantsMindAgent-0.2.0-x64.msi
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Msi,
    [string]$InstallDir = "$env:LOCALAPPDATA\Programs\QuantsMindAgent",
    [int]$Timeout = 90
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
if (-not [IO.Path]::IsPathRooted($Msi)) { $Msi = Join-Path $repoRoot $Msi }
if (-not (Test-Path -LiteralPath $Msi)) { throw "No such MSI: $Msi" }

$results = New-Object System.Collections.ArrayList
function Check($name, $ok, $detail = '') {
    $status = if ($ok) { 'PASS' } else { 'FAIL' }
    [void]$results.Add([pscustomobject]@{ Name = $name; Status = $status; Detail = $detail })
    Write-Host ("[{0}] {1}{2}" -f $status, $name, $(if ($detail) { " -- $detail" } else { '' }))
}

$work = Join-Path ([IO.Path]::GetTempPath()) ("qm-msiverify-" + [Guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$configDir = Join-Path $work 'profile'
$workspace = Join-Path $work 'verify workspace'
New-Item -ItemType Directory -Force -Path $configDir, $workspace | Out-Null
Set-Content -Path (Join-Path $workspace 'hello.py') -Value "print('ok')`n" -Encoding UTF8

# The real profile is never used by this script, and never written to. It is
# fingerprinted so that claim is measured instead of asserted.
$realProfile = Join-Path $env:LOCALAPPDATA 'QuantsMind-Agent'
function ProfileFingerprint {
    if (-not (Test-Path -LiteralPath $realProfile)) { return 'absent' }
    $parts = Get-ChildItem -LiteralPath $realProfile -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object FullName |
        ForEach-Object {
            # $_ inside catch is the error record, not the file, so the file is
            # captured before the try.
            $file = $_
            try {
                "$($file.FullName.Substring($realProfile.Length))|$((Get-FileHash $file.FullName -Algorithm SHA256 -ErrorAction Stop).Hash)"
            } catch {
                "$($file.FullName.Substring($realProfile.Length))|locked|$($file.Length)|$($file.LastWriteTimeUtc.Ticks)"
            }
        }
    return ($parts -join ';')
}
$before = ProfileFingerprint

$startMenuLink = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\QuantsMind-Agent.lnk'
$desktopLink = Join-Path ([Environment]::GetFolderPath('Desktop')) 'QuantsMind-Agent.lnk'
$uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall'

function Invoke-MsiExec([string]$ArgumentString, [string]$LogName) {
    # msiexec does not use the standard C runtime argument parsing, so paths are
    # quoted into one argument string rather than passed as an argv list. Passing
    # an array makes the quotation marks part of the path and msiexec rejects the
    # whole command line with 1639.
    $log = Join-Path $work $LogName
    $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $ArgumentString -Wait -PassThru `
        -RedirectStandardOutput (Join-Path $work "$LogName.out") `
        -RedirectStandardError (Join-Path $work "$LogName.err")
    return [pscustomobject]@{ Code = $process.ExitCode; Log = $log }
}

function Get-ArpEntry {
    Get-ChildItem -Path $uninstallKey -ErrorAction SilentlyContinue |
        ForEach-Object {
            $props = Get-ItemProperty -Path $_.PSPath -ErrorAction SilentlyContinue
            if ($props.DisplayName -eq 'QuantsMind-Agent') {
                [pscustomobject]@{
                    Key = $_.PSChildName
                    DisplayName = $props.DisplayName
                    DisplayVersion = $props.DisplayVersion
                    InstallLocation = $props.InstallLocation
                    Publisher = $props.Publisher
                }
            }
        }
}

Write-Host "MSI            : $Msi"
Write-Host "install dir    : $InstallDir"
Write-Host "real profile   : $realProfile (read-only fingerprint)"
Write-Host "isolation      : $work`n"

# -- 1. install ------------------------------------------------------------
Write-Host '--- install ---'
$install = Invoke-MsiExec "/i `"$Msi`" /qn /norestart /l*v `"$work\install.log`"" 'install.log'
# 0 = success, 3010 = success, restart required
Check 'silent install succeeds' ($install.Code -eq 0 -or $install.Code -eq 3010) "exit=$($install.Code) log=$($install.Log)"

$installed = Test-Path -LiteralPath $InstallDir
Check 'install directory created' $installed $InstallDir
$installedFiles = if ($installed) { @(Get-ChildItem -LiteralPath $InstallDir -Recurse -File) } else { @() }
Check 'application files installed' ($installedFiles.Count -gt 100) "$($installedFiles.Count) files"
$exe = Join-Path $InstallDir 'QuantsMindAgent.exe'
Check 'executable present' (Test-Path -LiteralPath $exe) $exe
Check 'Qt runtime bundled' (Test-Path -LiteralPath (Join-Path $InstallDir '_internal')) '_internal'
Check 'Start Menu shortcut created' (Test-Path -LiteralPath $startMenuLink) $startMenuLink
Check 'Desktop shortcut created' (Test-Path -LiteralPath $desktopLink) $desktopLink

$arp = @(Get-ArpEntry)
Check 'listed in installed apps (per-user)' ($arp.Count -eq 1) `
    $(if ($arp.Count) { "$($arp[0].DisplayName) $($arp[0].DisplayVersion) at $($arp[0].InstallLocation)" } else { 'no ARP entry' })
Check 'Add/Remove Programs reports version 0.2.0' ($arp.Count -eq 1 -and $arp[0].DisplayVersion -eq '0.2.0') `
    $(if ($arp.Count) { $arp[0].DisplayVersion } else { 'n/a' })

# -- 2. launch the installed application ----------------------------------
Write-Host "`n--- launch (isolated profile and workspace) ---"
if (-not (Test-Path -LiteralPath $exe)) {
    # A failed install must not abort the run before the uninstall step, or the
    # machine is left with whatever partial state the installer managed to write.
    Check 'installed application starts and stays running' $false "no executable at $exe"
    Check 'installed application opens a real window' $false 'not attempted: nothing was installed'
} else {
    $appLog = Join-Path $work 'app-stdout.log'
    $process = Start-Process -FilePath $exe `
        -ArgumentList '--workspace', ('"' + $workspace + '"'), '--config-dir', ('"' + $configDir + '"') `
        -RedirectStandardOutput $appLog -RedirectStandardError (Join-Path $work 'app-stderr.log') `
        -PassThru

    $title = ''
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        if ($process.HasExited) { break }
        $process.Refresh()
        if ($process.MainWindowHandle -ne 0 -and $process.MainWindowTitle) {
            $title = $process.MainWindowTitle
            break
        }
    }
    Check 'installed application starts and stays running' (-not $process.HasExited) `
        $(if ($process.HasExited) { "exited with $($process.ExitCode)" } else { "pid=$($process.Id)" })
    Check 'installed application opens a real window' ($title -like 'QuantsMind-Agent*') "title='$title'"
    Check 'no traceback in application output' `
        (-not (Select-String -Path (Join-Path $work 'app-stderr.log') -Pattern 'Traceback \(most recent call last\)' -Quiet -ErrorAction SilentlyContinue)) `
        ''

    $launchedPid = 0
    $launchedPid = $process.Id
    if (-not $process.HasExited) {
        $closed = $process.CloseMainWindow()
        if (-not $process.WaitForExit(30000)) { $process.Kill(); $process.WaitForExit(10000) }
        Check 'closes cleanly when the window is closed' $closed "exit=$($process.ExitCode)"
    }
    Start-Sleep -Seconds 2
    # Only the process this script started is a leak. Another copy the user is
    # already running is theirs, and reporting it as our failure would be wrong.
    $stillThere = Get-Process -Id $launchedPid -ErrorAction SilentlyContinue
    Check 'no background process survives shutdown' ($null -eq $stillThere) "pid=$launchedPid"
}

# -- 3. install the same package again ------------------------------------
Write-Host "`n--- reinstall (upgrade/repair path) ---"
$again = Invoke-MsiExec "/i `"$Msi`" /qn /norestart /l*v `"$work\reinstall.log`"" 'reinstall.log'
Check 'reinstall succeeds' ($again.Code -eq 0 -or $again.Code -eq 3010) "exit=$($again.Code)"
$arpAgain = @(Get-ArpEntry)
Check 'reinstall does not duplicate the installed-apps entry' ($arpAgain.Count -eq 1) "$($arpAgain.Count) entries"
$filesAgain = @(Get-ChildItem -LiteralPath $InstallDir -Recurse -File -ErrorAction SilentlyContinue)
Check 'reinstall does not duplicate application files' `
    ($filesAgain.Count -eq $installedFiles.Count) "$($filesAgain.Count) vs $($installedFiles.Count)"

# -- 4. uninstall ---------------------------------------------------------
Write-Host "`n--- uninstall ---"
$remove = Invoke-MsiExec "/x `"$Msi`" /qn /norestart /l*v `"$work\uninstall.log`"" 'uninstall.log'
Check 'uninstall succeeds' ($remove.Code -eq 0 -or $remove.Code -eq 3010) "exit=$($remove.Code)"
Check 'install directory removed' (-not (Test-Path -LiteralPath $InstallDir)) $InstallDir
Check 'Start Menu shortcut removed' (-not (Test-Path -LiteralPath $startMenuLink)) ''
Check 'Desktop shortcut removed' (-not (Test-Path -LiteralPath $desktopLink)) ''
Check 'installed-apps entry removed' (@(Get-ArpEntry).Count -eq 0) ''

# -- 5. user data untouched ----------------------------------------------
$after = ProfileFingerprint
Check 'real user profile untouched by install and uninstall' ($before -eq $after) `
    $(if ($before -eq $after) { 'byte-identical' } else { "CHANGED: before=$before after=$after" })

$failures = @($results | Where-Object { $_.Status -eq 'FAIL' })
Write-Host ''
Write-Host ("{0}/{1} MSI install verification checks passed" -f ($results.Count - $failures.Count), $results.Count)
if ($failures.Count) {
    Write-Host ('failing: ' + (($failures | ForEach-Object { $_.Name }) -join ', '))
    Write-Host "logs kept in $work"
    exit 1
}
Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "note: this verifies the installer and the installed application."
Write-Host "      It does not exercise the approval dialog, which needs a live model turn."
exit 0
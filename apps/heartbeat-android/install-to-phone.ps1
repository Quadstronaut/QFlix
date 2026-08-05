#requires -Version 5.1
<#
.SYNOPSIS
    Build (if needed) and install QFlix Admin to a connected Android device.

.DESCRIPTION
    Task 7 of the QFlix Admin plan could not complete because the phone was
    unplugged mid-run. Everything else is done and committed; this is the one
    step that needs the physical device.

    applicationId is com.qflix.admin, which differs from the old Heartbeat app
    (com.qflix.heartbeat). Android therefore installs this ALONGSIDE the old
    app rather than upgrading it - intended, so the old app keeps working
    against its own SSH key until you retire it.

.EXAMPLE
    .\install-to-phone.ps1
#>
[CmdletBinding()]
param([switch]$Rebuild)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$apk = Join-Path $here 'app\build\outputs\apk\debug\app-debug.apk'

Write-Host '==> checking for a connected device' -ForegroundColor Cyan
$devices = (& adb devices) | Select-Object -Skip 1 | Where-Object { $_ -match '\sdevice$' }
if (-not $devices) {
    Write-Host 'No device found.' -ForegroundColor Red
    Write-Host '  1. Plug the phone in over USB.'
    Write-Host '  2. Unlock it. If prompted, tap "Allow USB debugging".'
    Write-Host '  3. Re-run this script.'
    Write-Host ''
    Write-Host '  If it still does not appear:  adb kill-server; adb start-server; adb devices'
    exit 1
}
Write-Host "  found: $($devices -join ', ')" -ForegroundColor Green

if ($Rebuild -or -not (Test-Path $apk)) {
    Write-Host '==> building debug APK' -ForegroundColor Cyan
    & .\gradlew.bat assembleDebug --console=plain
    if ($LASTEXITCODE -ne 0) { Write-Host 'build failed' -ForegroundColor Red; exit 1 }
}

$size = '{0:N1} MB' -f ((Get-Item $apk).Length / 1MB)
Write-Host "==> installing $size" -ForegroundColor Cyan
& adb install -r $apk
if ($LASTEXITCODE -ne 0) { Write-Host 'install failed' -ForegroundColor Red; exit 1 }

Write-Host '==> launching' -ForegroundColor Cyan
& adb shell monkey -p com.qflix.admin -c android.intent.category.LAUNCHER 1 | Out-Null
Start-Sleep -Seconds 3

# A launch that crashes still "succeeds" from monkey's point of view, so read
# the crash buffer rather than trusting the exit code.
$crash = & adb logcat -d -s AndroidRuntime:E -t 80
if ($crash -match 'com\.qflix\.admin') {
    Write-Host 'LAUNCHED BUT CRASHED - trace follows:' -ForegroundColor Red
    $crash | Where-Object { $_ -match 'AndroidRuntime|qflix' } | Select-Object -Last 30
    exit 1
}

Write-Host ''
Write-Host 'QFlix Admin installed and launched cleanly.' -ForegroundColor Green
Write-Host ''
Write-Host 'NOT YET PROVISIONED. The app has no SSH key, so every screen will'
Write-Host 'report a transport failure until one is loaded. The bundle is at:'
Write-Host '  .admin-key\qflix-admin        (private key)'
Write-Host '  .admin-key\known_host         (host pin)'
Write-Host ''
Write-Host 'App-private storage is not adb-writable without root, so an in-app'
Write-Host 'import screen is the real fix - that is tracked follow-up work, not'
Write-Host 'something to work around by relaxing where the key is stored.'

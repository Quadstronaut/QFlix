<#
.SYNOPSIS
  Install QFlix hourly collector: B:\QFlix\data\ tree + Windows Task Scheduler entry.

.PARAMETER Install
  Create everything (idempotent).

.PARAMETER Uninstall
  Remove Task Scheduler entry. Leaves B:\QFlix\data\ in place.
#>

[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$DataRoot   = "B:\QFlix\data"
$TaskFolder = "\Archangel\QFlix"
$TaskName   = "Hourly Collect"
$TaskPath   = "$TaskFolder\$TaskName"
$RepoRoot   = (Resolve-Path "$PSScriptRoot\..\..").Path
$CollectPS1 = Join-Path $RepoRoot "scripts\local\qflix-collect.ps1"

function Ensure-Dirs {
    foreach ($sub in @("snapshots", "logs", "events", "runs")) {
        New-Item -ItemType Directory -Path (Join-Path $DataRoot $sub) -Force | Out-Null
    }
    Write-Host "Created: $DataRoot tree"
}

function Register-Task {
    if (-not (Test-Path $CollectPS1)) {
        throw "Collector not found at $CollectPS1"
    }
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$CollectPS1`""
    # Anchor at the next top-of-hour and repeat every hour indefinitely.
    # -StartWhenAvailable catches up if PC was off at trigger time.
    $now = Get-Date
    $nextHour = $now.Date.AddHours($now.Hour + 1)
    $trigger = New-ScheduledTaskTrigger -Once -At $nextHour `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration ([TimeSpan]::MaxValue)
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive
    Register-ScheduledTask -TaskName $TaskName -TaskPath $TaskFolder `
        -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Host "Registered: $TaskPath (first run at $nextHour, hourly thereafter)"
}

function Unregister-Task {
    Get-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $TaskName -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    Write-Host "Removed: $TaskPath"
}

if ($Install) {
    Ensure-Dirs
    Register-Task
    Write-Host ""
    Write-Host "Done. To trigger immediately: Start-ScheduledTask -TaskPath '$TaskFolder\' -TaskName '$TaskName'"
} elseif ($Uninstall) {
    Unregister-Task
} else {
    Write-Host "Usage: $($MyInvocation.MyCommand.Name) -Install | -Uninstall"
}

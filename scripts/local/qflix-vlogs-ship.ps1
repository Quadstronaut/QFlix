<#
.SYNOPSIS
  Ship structured logs from the seedbox into the workstation-local VictoriaLogs.

.DESCRIPTION
  Runs every 5 minutes via Task Scheduler.
  1. SSH-pulls JSON from ~/scripts/mcp/logs.py --emit-json --app all --since 6m
     (one SSH call covers every known app; 1 min overlap accepted for resilience).
  2. Converts each parsed line to VictoriaLogs JSON-lines format.
  3. POSTs to http://127.0.0.1:9428/insert/jsonline with stream fields (host, app).

  Designed to fail silent on transient errors (VictoriaLogs not yet up after boot,
  SSH tunnel cycling) so the 5-min scheduler doesn't email failures on every run.
#>

[CmdletBinding()]
param(
    [string]$Since      = '6m',
    [int]$Tail          = 5000,
    [string]$VLogsUrl   = 'http://127.0.0.1:9428',
    [string]$SshHostFile,
    [string]$LogFile
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
if (-not $SshHostFile) {
    $SshHostFile = Join-Path $RepoRoot "secrets\seedbox.ssh-host"
}
if (-not $LogFile) {
    $LogFile = "B:\QFlix\data\logs\vlogs-ship.log"
    New-Item -ItemType Directory -Force -Path (Split-Path $LogFile -Parent) | Out-Null
}

function Write-ShipLog {
    param([string]$msg)
    $ts = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
    "$ts $msg" | Out-File -FilePath $LogFile -Append -Encoding utf8
}

function Read-Trimmed {
    param([string]$path)
    if (-not (Test-Path $path)) { return $null }
    return (Get-Content -Path $path -Raw).Trim()
}

try {
    $sshHost = Read-Trimmed $SshHostFile
    if (-not $sshHost) {
        Write-ShipLog "skip: seedbox.ssh-host missing"
        exit 0
    }

    $remote = "python3 ~/scripts/mcp/logs.py --emit-json --app all --since $Since --tail $Tail"
    $sshArgs = @(
        '-o','BatchMode=yes',
        '-o','ConnectTimeout=10',
        '-o','ServerAliveInterval=15',
        "quadstronaut@$sshHost",
        $remote
    )
    $json = & ssh @sshArgs 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-ShipLog "skip: ssh failed (exit $LASTEXITCODE)"
        exit 0
    }
    if (-not $json) {
        Write-ShipLog "skip: empty ssh stdout"
        exit 0
    }

    $result = $json | ConvertFrom-Json
    $body = New-Object System.Text.StringBuilder
    $count = 0
    foreach ($prop in $result.PSObject.Properties) {
        $appName = $prop.Name
        $appData = $prop.Value
        if (-not $appData.lines) { continue }
        foreach ($line in $appData.lines) {
            if (-not $line.message) { continue }
            $obj = [ordered]@{
                _msg        = [string]$line.message
                _time       = [string]$line.ts
                level       = [string]$line.level
                app         = $appName
                source_file = [string]$line.source_file
                host        = 'seedbox'
            }
            [void]$body.AppendLine(($obj | ConvertTo-Json -Compress))
            $count++
        }
    }

    if ($count -eq 0) {
        Write-ShipLog "ok: 0 lines (nothing to ship)"
        exit 0
    }

    $uri = "$VLogsUrl/insert/jsonline?_stream_fields=host,app&_time_field=_time&_msg_field=_msg"
    try {
        Invoke-RestMethod -Uri $uri -Method Post `
            -Body $body.ToString() `
            -ContentType 'application/stream+json' `
            -TimeoutSec 15 | Out-Null
        Write-ShipLog "ok: shipped $count lines"
    } catch {
        Write-ShipLog "skip: vlogs POST failed - $($_.Exception.Message)"
        exit 0
    }
} catch {
    Write-ShipLog "error: $($_.Exception.Message)"
    exit 0
}

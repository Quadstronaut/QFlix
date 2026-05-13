<#
.SYNOPSIS
  Hourly QFlix farm snapshot collector. Run by Windows Task Scheduler.

.DESCRIPTION
  1. Ensures \Archangel\Manitoba SSH Tunnel task is running (port 42014 reachable).
  2. Acquires single-instance lock at B:\QFlix\data\.collect.lock.
  3. SSH-invokes ~/scripts/mcp/collect.py and writes snapshot.
  4. SSH-invokes ~/scripts/mcp/logs.py and appends to per-app daily logs.
  5. Walks last 3 snapshots; updates B:\QFlix\data\stale-state.json.
  6. For new candidates, SSH-invokes ~/scripts/mcp/unstick.py.
  7. Posts Discord summary (no @ping). Pushes to Kuma.
  8. Writes B:\QFlix\data\last-collect.json. Prunes retention.
#>

$ErrorActionPreference = "Stop"
$DataRoot = "B:\QFlix\data"
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$Secrets  = Join-Path $RepoRoot "secrets"
$LockFile = Join-Path $DataRoot ".collect.lock"
$TunnelTask = "\Archangel\Manitoba SSH Tunnel"
$TunnelPort = 42014
$MaxActionsPerDay = 10

# PS 5.1 compat: Get-Date -AsUTC is PS 7+ only.
function Get-UtcNow { [DateTime]::UtcNow }

function Read-Secret {
    param([string]$name)
    $p = Join-Path $Secrets $name
    if (-not (Test-Path $p)) { return "" }
    return (Get-Content -Path $p -Raw).Trim()
}

function Post-Discord {
    param([string]$message, [string]$title = "QFlix Collect", [int]$color = 0x3498DB)
    $hook = Read-Secret "discord-webhook.url"
    if (-not $hook) { return }
    $body = @{
        embeds = @(@{ title = $title; description = $message; color = $color })
    } | ConvertTo-Json -Depth 4 -Compress
    try { Invoke-RestMethod -Uri $hook -Method Post -ContentType "application/json" -Body $body | Out-Null } catch {}
}

function Push-Kuma {
    param([string]$monitor, [string]$msg = "OK")
    $tokenJson = Read-Secret "kuma-push-tokens.json"
    if (-not $tokenJson) { return $false }
    $tokens = $tokenJson | ConvertFrom-Json
    $token = $tokens.$monitor
    if (-not $token) { return $false }
    $kumaHost = Read-Secret "uptimekuma.host"
    if (-not $kumaHost) { $kumaHost = "kuma.seedbox.example.com" }
    $url = "https://$kumaHost/api/push/$token?status=up&msg=" + [uri]::EscapeDataString($msg) + "&ping=0"
    try { Invoke-RestMethod -Uri $url -TimeoutSec 10 | Out-Null; return $true } catch { return $false }
}

function Ensure-Tunnel {
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port $TunnelPort -WarningAction SilentlyContinue -InformationLevel Quiet) {
        return $true
    }
    Write-Host "Tunnel down - attempting restart"
    try {
        Start-ScheduledTask -TaskName $TunnelTask -ErrorAction Stop
    } catch {
        Post-Discord "Tunnel restart failed: $_" "QFlix Collect ERROR" 0xE74C3C
        return $false
    }
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-NetConnection -ComputerName 127.0.0.1 -Port $TunnelPort -WarningAction SilentlyContinue -InformationLevel Quiet) {
            return $true
        }
    }
    Post-Discord "Tunnel did not come up after 30s" "QFlix Collect ERROR" 0xE74C3C
    return $false
}

function Acquire-Lock {
    if (Test-Path $LockFile) {
        $pidStr = (Get-Content $LockFile).Trim()
        try {
            Get-Process -Id $pidStr -ErrorAction Stop | Out-Null
            Write-Host "Prior collect still running (PID=$pidStr). Exiting."
            exit 1
        } catch {
            Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
        }
    }
    New-Item -Path $LockFile -ItemType File -Force | Out-Null
    Set-Content -Path $LockFile -Value $PID -Encoding ascii
}

function Release-Lock {
    Remove-Item $LockFile -Force -ErrorAction SilentlyContinue
}

function Invoke-SSH {
    param([string]$remoteCmd, [int]$timeoutSec = 90)
    $sshHost = Read-Secret "seedbox.ssh-host"
    if (-not $sshHost) { throw "secrets/seedbox.ssh-host missing" }
    $outFile = Join-Path $env:TEMP "qflix-ssh.out"
    $errFile = Join-Path $env:TEMP "qflix-ssh.err"
    $proc = Start-Process -FilePath "ssh" `
        -ArgumentList @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "quadstronaut@$sshHost", $remoteCmd) `
        -PassThru -RedirectStandardOutput $outFile `
        -RedirectStandardError  $errFile -WindowStyle Hidden -NoNewWindow
    if (-not $proc.WaitForExit($timeoutSec * 1000)) {
        $proc.Kill()
        throw "SSH timeout after $timeoutSec s"
    }
    $stdout = ""; $stderr = ""
    if (Test-Path $outFile) { $stdout = Get-Content $outFile -Raw -ErrorAction SilentlyContinue }
    if (Test-Path $errFile) { $stderr = Get-Content $errFile -Raw -ErrorAction SilentlyContinue }
    return [PSCustomObject]@{
        ExitCode = $proc.ExitCode
        Stdout   = $stdout
        Stderr   = $stderr
    }
}

function Collect-Snapshot {
    $r = Invoke-SSH "python3 ~/scripts/mcp/collect.py --emit-json --include qbit,arrs,seerr,plex"
    if ($r.ExitCode -ne 0) { throw "collect.py exit=$($r.ExitCode): $($r.Stderr)" }
    $now = Get-UtcNow
    $dir = Join-Path $DataRoot ("snapshots\{0:yyyy-MM-dd}" -f $now)
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $path = Join-Path $dir ("{0:HH}.json" -f $now)
    [System.IO.File]::WriteAllText("$path.tmp", $r.Stdout)
    Move-Item -Path "$path.tmp" -Destination $path -Force
    return $path
}

function Collect-Logs {
    $r = Invoke-SSH "python3 ~/scripts/mcp/logs.py --app all --since 1h --tail 2000 --emit-json" 60
    if ($r.ExitCode -ne 0) { return $false }
    try {
        $payload = $r.Stdout | ConvertFrom-Json
    } catch { return $false }
    $today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
    $logsDir = Join-Path $DataRoot "logs\$today"
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
    foreach ($prop in $payload.PSObject.Properties) {
        $appName = $prop.Name
        $entry   = $prop.Value
        if (-not $entry.lines) { continue }
        $logFile = Join-Path $logsDir "$appName.log"
        $lines = $entry.lines | ForEach-Object { ($_ | ConvertTo-Json -Compress) }
        Add-Content -Path $logFile -Value $lines -Encoding utf8
    }
    return $true
}

function Update-StaleState {
    # State is kept as a plain hashtable (mutable) for PS 5.1 compatibility,
    # since `ConvertFrom-Json -AsHashtable` is PS 7+. We hand-convert on load.
    $stateFile = Join-Path $DataRoot "stale-state.json"
    $hashes = @{}
    if (Test-Path $stateFile) {
        try {
            $loaded = Get-Content $stateFile -Raw | ConvertFrom-Json
            foreach ($prop in $loaded.hashes.PSObject.Properties) {
                $entry = @{}
                foreach ($p in $prop.Value.PSObject.Properties) {
                    $entry[$p.Name] = $p.Value
                }
                $hashes[$prop.Name] = $entry
            }
        } catch {}
    }

    # Load current + 2 prior snapshots
    $allFiles = Get-ChildItem -Path (Join-Path $DataRoot "snapshots") -Recurse -Filter "*.json" `
        | Sort-Object FullName | Select-Object -Last 3
    if ($allFiles.Count -lt 3) {
        $out = @{ hashes = $hashes; updated_at = ([DateTime]::UtcNow.ToString("o")) }
        ($out | ConvertTo-Json -Depth 6) | Out-File -FilePath $stateFile -Encoding utf8
        return @()
    }
    $snaps = $allFiles | ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json }

    # Build hash -> [samples] over the 3 snapshots
    $hashSamples = @{}
    foreach ($s in $snaps) {
        foreach ($t in $s.qbit.torrents) {
            if (-not $hashSamples.ContainsKey($t.hash)) { $hashSamples[$t.hash] = @() }
            $hashSamples[$t.hash] += [PSCustomObject]@{
                downloaded = $t.downloaded_bytes
                state      = $t.state
                progress   = $t.progress
                dlspeed    = $t.dl_speed_bytes_s
            }
        }
    }

    $candidates = @()
    foreach ($h in @($hashSamples.Keys)) {
        $samples = $hashSamples[$h]
        if ($samples.Count -lt 3) { continue }
        $delta = $samples[-1].downloaded - $samples[0].downloaded
        if ($delta -ne 0) {
            if ($hashes.ContainsKey($h)) { $hashes.Remove($h) }
            continue
        }
        $latest = $samples[-1]
        if ($latest.progress -ge 1.0) { continue }
        $rule = $null
        if ($latest.state -eq "stalledDL") { $rule = "stalledDL" }
        elseif ($latest.state -eq "downloading" -and $latest.dlspeed -lt 10000) { $rule = "dead-slow" }
        if (-not $rule) { continue }

        if (-not $hashes.ContainsKey($h)) {
            $hashes[$h] = @{
                first_zero_movement_at = ([DateTime]::UtcNow.ToString("o"))
                consecutive_zero_hours = 3
                last_progress          = $latest.progress
                rule_matched           = $rule
                candidate_for_unstick  = $true
                acted_on_at            = $null
            }
        } else {
            $hashes[$h].consecutive_zero_hours = 3
            $hashes[$h].rule_matched = $rule
            $hashes[$h].candidate_for_unstick = $true
            $hashes[$h].last_progress = $latest.progress
        }
        if (-not $hashes[$h].acted_on_at) {
            $candidates += $h
        }
    }
    $out = @{ hashes = $hashes; updated_at = ([DateTime]::UtcNow.ToString("o")) }
    ($out | ConvertTo-Json -Depth 6) | Out-File -FilePath $stateFile -Encoding utf8
    return $candidates
}

function Count-TodaysActions {
    $today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
    $f = Join-Path $DataRoot "events\$today.jsonl"
    if (-not (Test-Path $f)) { return 0 }
    return (Get-Content $f | Where-Object { $_.Trim() -ne "" }).Count
}

function Act-On-Candidates {
    param([string[]]$candidates)
    $acted = @()
    $count = Count-TodaysActions
    foreach ($h in $candidates) {
        if ($count -ge $MaxActionsPerDay) { break }
        $r = Invoke-SSH "python3 ~/scripts/mcp/unstick.py --emit-json --hash $h --reason '3h-zero-movement'" 60
        if ($r.ExitCode -ne 0) {
            Write-Host "Unstick failed for $h : $($r.Stderr)"
            continue
        }
        try { $result = $r.Stdout | ConvertFrom-Json } catch { continue }
        $today = [DateTime]::UtcNow.ToString("yyyy-MM-dd")
        $eventDir = Join-Path $DataRoot "events"
        New-Item -ItemType Directory -Path $eventDir -Force | Out-Null
        $line = @{
            ts = ([DateTime]::UtcNow.ToString("o")); action = "unstick"
            hash = $h; result = $result.status; via = "qflix-collect.ps1"
        } | ConvertTo-Json -Compress
        Add-Content -Path (Join-Path $eventDir "$today.jsonl") -Value $line -Encoding utf8
        $acted += $h
        $count++
    }
    return $acted
}

function Prune-Retention {
    $now = [DateTime]::UtcNow
    Get-ChildItem (Join-Path $DataRoot "snapshots") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt $now.AddDays(-30) } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
    Get-ChildItem (Join-Path $DataRoot "logs") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt $now.AddDays(-7) } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
    Get-ChildItem (Join-Path $DataRoot "events") -File -Filter "*.jsonl" -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt $now.AddDays(-365) } |
        ForEach-Object { Remove-Item $_.FullName -Force }
    Get-ChildItem (Join-Path $DataRoot "runs") -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTimeUtc -lt $now.AddDays(-7) } |
        ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
}

# --- Main flow -------------------------------------------------------------

$started = Get-UtcNow
$exitCode = 0
$transcriptDir = Join-Path $DataRoot ("runs\{0:yyyy-MM-dd}" -f $started)
New-Item -ItemType Directory -Path $transcriptDir -Force | Out-Null
$transcript = Join-Path $transcriptDir ("{0:HH-mm-ss}.log" -f $started)
Start-Transcript -Path $transcript -Force | Out-Null

try {
    if (-not (Ensure-Tunnel)) { exit 2 }
    Acquire-Lock
    $snapPath = Collect-Snapshot
    Collect-Logs | Out-Null
    $candidates = Update-StaleState
    $acted = @()
    if ($candidates.Count -gt 0) {
        $acted = Act-On-Candidates $candidates
    }
    Prune-Retention
    $duration = ((Get-UtcNow) - $started).TotalSeconds
    $snap = Get-Content $snapPath -Raw | ConvertFrom-Json
    $tcount = ($snap.qbit.torrents | Measure-Object).Count
    $msg = "Snapshot {0:HH}.json: $tcount torrents, $($candidates.Count) stale candidates, $($acted.Count) actions" -f $started
    Post-Discord $msg
    Push-Kuma "QFlix Collect (workstation)" $msg | Out-Null

    @{
        ts = ($started.ToString("o"))
        exit_code = 0
        duration_s = [math]::Round($duration, 2)
        snapshot_path = $snapPath
        torrent_count = $tcount
        candidates = $candidates.Count
        actions = $acted.Count
    } | ConvertTo-Json | Out-File -FilePath (Join-Path $DataRoot "last-collect.json") -Encoding utf8
} catch {
    $exitCode = 1
    $err = "$_"
    Post-Discord "Collect failed: $err" "QFlix Collect ERROR" 0xE74C3C
    @{
        ts = ($started.ToString("o"))
        exit_code = 1
        error = $err
    } | ConvertTo-Json | Out-File -FilePath (Join-Path $DataRoot "last-collect.json") -Encoding utf8
} finally {
    Release-Lock
    Stop-Transcript | Out-Null
}

exit $exitCode

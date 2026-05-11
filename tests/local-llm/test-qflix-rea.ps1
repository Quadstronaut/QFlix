#Requires -Version 5.1
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Script:Pass = 0
$Script:Fail = 0
$Script:Failures = @()

function Assert-Equal {
    param($Expected, $Actual, [string]$Name)
    if ($Expected -is [array] -or $Actual -is [array]) {
        $eJson = $Expected | ConvertTo-Json -Depth 20 -Compress
        $aJson = $Actual   | ConvertTo-Json -Depth 20 -Compress
        if ($eJson -eq $aJson) { $Script:Pass++; Write-Host "  PASS  $Name" -F Green; return }
    } elseif ($Expected -eq $Actual) {
        $Script:Pass++; Write-Host "  PASS  $Name" -F Green; return
    }
    $Script:Fail++
    $Script:Failures += "$Name`n    expected: $Expected`n    actual:   $Actual"
    Write-Host "  FAIL  $Name" -F Red
    Write-Host "    expected: $Expected" -F DarkGray
    Write-Host "    actual:   $Actual"  -F DarkGray
}

function Assert-True  { param([bool]$Cond,[string]$Name) Assert-Equal $true  $Cond $Name }
function Assert-False { param([bool]$Cond,[string]$Name) Assert-Equal $false $Cond $Name }

function Test-Case {
    param([string]$Name,[scriptblock]$Block)
    Write-Host "`n[$Name]" -F Cyan
    try { & $Block }
    catch {
        $Script:Fail++
        $Script:Failures += "$Name (exception)`n    $($_.Exception.Message)"
        Write-Host "  EXCEPTION $($_.Exception.Message)" -F Red
    }
}

# Dot-source the script under test so its functions are available.
# Setting $script:DotSourceMode tells the script not to run Invoke-Main.
$script:DotSourceMode = $true
$repoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$scriptPath = Join-Path $repoRoot 'scripts/local-llm/qflix-rea.ps1'
if (-not (Test-Path $scriptPath)) {
    Write-Host "ERROR: script not found at $scriptPath" -F Red
    Write-Host "       (this is expected before Task 2 lands)" -F Yellow
    exit 1
}
. $scriptPath

# --- Sentinel ---
Test-Case 'script dot-sources without executing main' { Assert-True $true 'no-op sentinel' }

# --- Task 2: state I/O ---
Test-Case 'Get-StateDir returns APPDATA path and creates dir' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        $d = Get-StateDir
        Assert-Equal (Join-Path $env:APPDATA 'qflix-rea') $d 'path is APPDATA\qflix-rea'
        Assert-True (Test-Path $d) 'dir was created'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Read-State returns defaults when file absent' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        $s = Read-State
        Assert-Equal '' $s.last_heartbeat_date 'default last_heartbeat_date'
        Assert-Equal '' $s.last_ollama_dead_ping 'default last_ollama_dead_ping'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Write-State then Read-State roundtrips' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        Write-State @{ last_heartbeat_date = '2026-05-11'; last_ollama_dead_ping = '' }
        $s = Read-State
        Assert-Equal '2026-05-11' $s.last_heartbeat_date 'persisted heartbeat date'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

# --- Task 3: lock + audit log ---
Test-Case 'Acquire-Lock returns a stream then blocks second acquire' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        $a = Acquire-Lock
        Assert-True ($a -ne $null) 'first acquire returns stream'
        $b = Acquire-Lock
        Assert-True ($b -eq $null) 'second acquire returns null'
        $a.Dispose()
        $c = Acquire-Lock
        Assert-True ($c -ne $null) 'third acquire after first dispose succeeds'
        $c.Dispose()
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

# --- Task 4: model discovery ---
Test-Case 'Filter-OllamaListOutput filters by include + exclude regex' {
    $mockOutput = @"
NAME                       ID              SIZE      MODIFIED
qwen3-coder:30b            06c1097efce0    18 GB     3 days ago
qwen3-vl:8b                901cae732162    6.1 GB    3 days ago
qwen3:8b                   500a1f067a9f    5.2 GB    3 days ago
qwen2.5-coder:1.5b-base    02e0f2817a89    986 MB    3 days ago
bge-m3:latest              790764642607    1.2 GB    3 days ago
qwen2.5-coder:7b           dae161e27b0e    4.7 GB    2 weeks ago
mistral:7b                 0000000000aa    4.0 GB    1 week ago
"@
    $models = Filter-OllamaListOutput -RawText $mockOutput
    Assert-Equal @('qwen3-coder:30b','qwen3:8b','qwen2.5-coder:7b') $models 'expected 3 code-capable models'
}

Test-Case 'Filter-OllamaListOutput handles empty input' {
    $models = Filter-OllamaListOutput -RawText ''
    Assert-Equal 0 @($models).Count 'empty input yields empty array'
}

Test-Case 'Filter-OllamaListOutput skips header-only output' {
    $models = Filter-OllamaListOutput -RawText "NAME    ID    SIZE    MODIFIED`n"
    Assert-Equal 0 @($models).Count 'header-only input yields empty array'
}

Test-Case 'Write-AuditLog appends line and rotates at 10MB' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        Write-AuditLog 'first line'
        Write-AuditLog 'second line'
        $logPath = Join-Path (Get-StateDir) 'audit.log'
        $lines = @(Get-Content -LiteralPath $logPath)
        Assert-Equal 2 $lines.Count 'two lines persisted'
        Assert-True ($lines[0] -match 'first line') 'first line present'

        # Force rotation: write a big chunk then a final line
        $big = 'x' * (11MB)
        Set-Content -LiteralPath $logPath -Value $big -Encoding UTF8 -NoNewline
        Write-AuditLog 'after rotation'
        Assert-True (Test-Path "$logPath.1") 'rotated file exists'
        $newLines = @(Get-Content -LiteralPath $logPath)
        Assert-Equal 1 $newLines.Count 'new log has only post-rotation line'
        Assert-True ($newLines[0] -match 'after rotation') 'post-rotation content correct'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

# Summary
Write-Host "`n========================================" -F White
Write-Host "  $Script:Pass passed, $Script:Fail failed" -F $(if($Script:Fail){'Red'}else{'Green'})
Write-Host "========================================" -F White
if ($Script:Fail -gt 0) {
    foreach ($f in $Script:Failures) { Write-Host "  - $f" -F Red }
    exit 1
}
exit 0

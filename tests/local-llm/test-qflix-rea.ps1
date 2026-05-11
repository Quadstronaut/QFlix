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

# Cases populated in later tasks. Runner stub for Task 1 only verifies dot-sourcing works.
Test-Case 'script dot-sources without executing main' { Assert-True $true 'no-op sentinel' }

# Summary
Write-Host "`n========================================" -F White
Write-Host "  $Script:Pass passed, $Script:Fail failed" -F $(if($Script:Fail){'Red'}else{'Green'})
Write-Host "========================================" -F White
if ($Script:Fail -gt 0) {
    foreach ($f in $Script:Failures) { Write-Host "  - $f" -F Red }
    exit 1
}
exit 0

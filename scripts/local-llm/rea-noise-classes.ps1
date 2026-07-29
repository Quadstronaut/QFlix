# rea-noise-classes.ps1 — load the REA noise policy from git.
#
# WHY THIS FILE IS TRACKED AND qflix-rea.ps1 IS NOT
# -------------------------------------------------
# qflix-rea.ps1 is gitignored (.gitignore:55): operator-local workstation
# tooling, public repo. That made it invisible to CI — and it is the ALERTING
# LAYER. The fix is not to un-ignore a 56KB script; it is to move the POLICY
# into git and leave the plumbing outside. This loader is that seam.
#
# USAGE from qflix-rea.ps1 (replaces the inline $Script:NoiseFindingRules table):
#
#     . (Join-Path $PSScriptRoot 'rea-noise-classes.ps1')
#     $Script:NoiseFindingRules = Get-ReaNoiseRules
#     $Script:DeadmanReasons    = Get-ReaDeadmanReasons
#
# Until that edit is made, the yaml and the ps1 hold the policy twice and
# defect class C-07 compares them on every run (cross-check layer 2 — it only
# runs where the ps1 exists, which is the operator's workstation, and the skip
# is COUNTED in CI, never silent). After the edit there is exactly one copy and
# the comparison becomes trivially true.
#
# No YAML module dependency: PowerShell 5.1 has no ConvertFrom-Yaml, and adding
# powershell-yaml would put a package manager in the alert path. The parser
# below handles exactly the shape manifest/rea-noise-classes.yaml uses and
# THROWS on anything else rather than returning a partial rule table — a
# half-loaded noise table would silence real findings, which is strictly worse
# than not starting.

Set-StrictMode -Version 2.0

function Get-ReaNoiseClassesPath {
    $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
    Join-Path $repoRoot 'manifest/rea-noise-classes.yaml'
}

function ConvertFrom-ReaSingleQuoted {
    # YAML single-quoted scalar: backslashes are literal (which is why every rx
    # is written that way) and '' is an escaped quote.
    param([string]$Raw)
    $t = $Raw.Trim()
    if ($t.StartsWith("'") -and $t.EndsWith("'") -and $t.Length -ge 2) {
        return $t.Substring(1, $t.Length - 2).Replace("''", "'")
    }
    return $t
}

function Get-ReaNoiseRules {
    <#
      .SYNOPSIS
      The enforcement table, read from manifest/rea-noise-classes.yaml.
      .OUTPUTS
      An array of hashtables with keys id / rx / field, in file order — the
      same shape the inline table had, so Test-IsNoiseFinding is unchanged.
    #>
    param([string]$Path = (Get-ReaNoiseClassesPath))

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "rea-noise-classes.yaml not found at $Path - refusing to run with an empty noise table"
    }
    $lines = Get-Content -LiteralPath $Path -Encoding UTF8
    $rules = @()
    $cur = $null
    $inClasses = $false
    foreach ($line in $lines) {
        if ($line -match '^classes:\s*$')        { $inClasses = $true;  continue }
        if ($line -match '^[a-z_]+:' -and $line -notmatch '^classes:') { $inClasses = $false }
        if (-not $inClasses) { continue }

        if ($line -match "^\s*-\s+id:\s*(\S+)\s*$") {
            if ($null -ne $cur) { $rules += $cur }
            $cur = @{ id = $Matches[1]; rx = $null; field = $null }
            continue
        }
        if ($null -eq $cur) { continue }
        if ($line -match "^\s+rx:\s*(.+?)\s*$") {
            $cur.rx = ConvertFrom-ReaSingleQuoted $Matches[1]
            continue
        }
        if ($line -match "^\s+field:\s*(.+?)\s*$") {
            $v = $Matches[1].Trim()
            if ($v -ne 'null' -and $v -ne '~' -and $v -ne '') { $cur.field = $v }
            continue
        }
    }
    if ($null -ne $cur) { $rules += $cur }

    if ($rules.Count -eq 0) {
        throw "parsed 0 noise rules from $Path - refusing to run with an empty noise table"
    }
    foreach ($r in $rules) {
        if (-not $r.rx) { throw "noise class '$($r.id)' has no rx in $Path" }
        # Fail at LOAD time, not at match time: a bad regex discovered mid-run
        # would suppress nothing and page the operator with a stack trace.
        try { [void][regex]::new($r.rx) }
        catch { throw "noise class '$($r.id)' has an uncompilable rx: $($_.Exception.Message)" }
    }
    return $rules
}

function Get-ReaDeadmanReasons {
    param([string]$Path = (Get-ReaNoiseClassesPath))
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "rea-noise-classes.yaml not found at $Path"
    }
    $reasons = @()
    $in = $false
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        if ($line -match '^deadman_reasons:\s*$') { $in = $true; continue }
        if ($in) {
            if ($line -match '^\s*-\s+(\S+)\s*$') { $reasons += $Matches[1]; continue }
            if ($line.Trim() -ne '') { break }
        }
    }
    if ($reasons.Count -eq 0) { throw "parsed 0 deadman reasons from $Path" }
    return $reasons
}

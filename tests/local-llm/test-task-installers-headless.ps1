#!/usr/bin/env pwsh
# test-task-installers-headless.ps1 — no scheduled task this repo installs may
# flash a console window.
#
# THE BUG THIS GUARDS (2026-09-27, operator-reported):
#   A Windows scheduled task with an Interactive principal ("run only when the
#   user is logged on") whose action is `-Execute powershell.exe` gets a console
#   window ALLOCATED AND DRAWN by the OS before the process parses its own
#   arguments. -WindowStyle Hidden hides it a beat later. Net effect: a visible
#   flash that steals keyboard focus, on every run — hourly, for REA and the S2
#   backup — landing mid-game, mid-stream, mid-recording.
#
#   The live tasks on this workstation were repaired by hand (Archangel DEVIL-1,
#   commit 9039a52) to `conhost.exe --headless "<powershell.exe>" <args>`, which
#   attaches the child to a console that is never drawn. But the INSTALLERS in
#   this repo still emitted the old action, so running any of them silently undid
#   the repair. That is the shape this file exists to make impossible: a fix that
#   lives only in live state, with the code that recreates the fault still shipped.
#
# WHY A REPO-WIDE SCAN AND NOT THREE ASSERTIONS:
#   The next installer somebody adds must be caught too. This enumerates every
#   .ps1 under scripts/ that registers a task and adjudicates all of them, so the
#   guard grows with the repo instead of naming a fixed list that rots.
#
# Runs on the hosted Linux runner: pure text analysis, no Task Scheduler calls
# and no Windows paths resolved. scripts/local-llm/qflix-rea.ps1 is gitignored
# (audit-scope S2) so it is simply absent in CI; that SKIP is loud, per R4.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Script:Pass = 0
$Script:Fail = 0
$Script:Skip = 0
$Script:Failures = @()

function Assert-True {
    param([bool]$Cond, [string]$Name)
    if ($Cond) { $Script:Pass++; Write-Host "  PASS  $Name" }
    else { $Script:Fail++; $Script:Failures += $Name; Write-Host "  FAIL  $Name" }
}
function Test-Case {
    param([string]$Name, [scriptblock]$Block)
    Write-Host ''
    Write-Host "[$Name]"
    try { & $Block }
    catch {
        $Script:Fail++
        $Script:Failures += "$Name (threw: $($_.Exception.Message))"
        Write-Host "  FAIL  $Name  threw: $($_.Exception.Message)"
    }
}

$repoRoot   = (Resolve-Path (Join-Path (Join-Path $PSScriptRoot '..') '..')).Path
$scriptsDir = Join-Path $repoRoot 'scripts'

function ConvertTo-NativeRel {
    param([Parameter(Mandatory)][string]$Rel)
    $Rel -replace '/', [IO.Path]::DirectorySeparatorChar
}

# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------

# A file is an INSTALLER if it creates a task by either route. Both are covered
# because the repo has used both: the ScheduledTasks module and raw schtasks.
$RegistersTask = 'Register-ScheduledTask|schtasks\s+/Create'

# Every -Execute value, and every schtasks /TR command line.
$ExecuteArg = [regex]'-Execute\s+(?<v>\([^)]*\)|"[^"]*"|''[^'']*''|\$[\w:]+(?:\.\w+)*)'
$SchtasksTr = [regex]'/TR\s+(?<v>"(?:[^"])*"|`"(?:[^`])*`")'

function Resolve-ActionValue {
    <#
      One hop of variable resolution, and no more. `-Execute $d.Execute` is the
      idiom here (a pure definition function returns the hashtable), so a scan
      that stopped at the literal token would adjudicate the string '$d.Execute'
      and pass anything. Resolving further would be guesswork; an unresolvable
      value is reported as such rather than assumed innocent.
    #>
    param([Parameter(Mandatory)][string]$Value, [Parameter(Mandatory)][string]$Text)
    if ($Value -match '^\$[\w:]+\.(?<prop>\w+)$') {
        $prop = $Matches['prop']
        $m = [regex]::Match($Text, "(?m)^\s*$prop\s*=\s*(?<rhs>.+)$")
        if ($m.Success) { return $m.Groups['rhs'].Value }
        return $Value
    }
    if ($Value -match '^\$(?<name>\w+)$') {
        $name = $Matches['name']
        $m = [regex]::Match($Text, "(?m)^\s*\`$$name\s*=\s*(?<rhs>.+)$")
        if ($m.Success) { return $m.Groups['rhs'].Value }
        return $Value
    }
    return $Value
}

function Get-InstallerFile {
    # Excludes vendored trees: a dependency's own installer is not ours to police.
    Get-ChildItem -Path $scriptsDir -Filter '*.ps1' -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '[\\/](\.venv|node_modules|site-packages)[\\/]' } |
        Where-Object { (Get-Content -LiteralPath $_.FullName -Raw) -match $RegistersTask }
}

$installers = @(Get-InstallerFile)

Test-Case 'the scan finds the installers it is supposed to adjudicate' {
    # A scan that silently matches nothing is exactly the failure mode this whole
    # file was written about, so assert the population is non-empty and name it.
    Assert-True ($installers.Count -ge 1) 'at least one task installer was found'
    foreach ($f in $installers) {
        Write-Host "        subject: $($f.FullName.Substring($repoRoot.Length + 1))"
    }
}

Test-Case 'no installer launches the interpreter directly (console flash)' {
    foreach ($f in $installers) {
        $rel  = $f.FullName.Substring($repoRoot.Length + 1)
        $text = Get-Content -LiteralPath $f.FullName -Raw

        $values = @()
        foreach ($m in $ExecuteArg.Matches($text)) { $values += $m.Groups['v'].Value }
        foreach ($m in $SchtasksTr.Matches($text)) { $values += $m.Groups['v'].Value }

        Assert-True ($values.Count -ge 1) "$rel : an -Execute or /TR value was actually located"

        foreach ($v in $values) {
            $resolved = Resolve-ActionValue -Value $v -Text $text
            # Checking FOR conhost rather than forbidding powershell.exe is
            # deliberate: the correct wrapped form still names powershell.exe,
            # as it must — it is the thing being wrapped.
            Assert-True ($resolved -match 'conhost') `
                "$rel : the task action launches conhost, not the interpreter -- $v"
        }

        Assert-True ($text -match '--headless') "$rel : conhost is invoked with --headless"
        # The tradeoff has to travel with the code, because it silently changes
        # what the Task Scheduler UI means: see the note in the repo's handoff.
        Assert-True ($text -match 'Last Run Result') `
            "$rel : records that Last Run Result now reports conhost's exit, not the script's"
    }
}

Test-Case 'the definition functions survive an unset WINDIR' {
    # The hosted runner has no $env:WINDIR. A definition function that emitted
    # "\System32\conhost.exe" there would satisfy the substring check above and
    # still register a broken action on any machine where WINDIR was unset.
    foreach ($rel in @('local-llm/backup-untracked.ps1', 'local-llm/ollama-recover.ps1', 'local/install-qflix-collect.ps1')) {
        $path = Join-Path $scriptsDir (ConvertTo-NativeRel $rel)
        if (-not (Test-Path -LiteralPath $path)) {
            $Script:Skip++
            Write-Host "  SKIP  $rel absent"
            continue
        }
        $text = Get-Content -LiteralPath $path -Raw
        Assert-True ($text -match "\`$env:WINDIR\s*\}\s*else\s*\{\s*'C:\\WINDOWS'\s*\}") `
            "$rel : falls back to a literal Windows root when WINDIR is unset"
    }
}

Test-Case 'REA installs the task where the task actually lives' {
    # The installer said \Archangel\QFlix-LLM\ while the live task has always sat
    # at \QFlix-LLM\. Reinstalling would have created a SECOND hourly REA rather
    # than replacing the first. Subject is S2 (gitignored) -> absent in CI.
    $rea = Join-Path $scriptsDir (ConvertTo-NativeRel 'local-llm/qflix-rea.ps1')
    if (-not (Test-Path -LiteralPath $rea)) {
        $Script:Skip++
        Write-Host '  SKIP  scripts/local-llm/qflix-rea.ps1 is untracked by design (audit-scope S2).'
        Write-Host '        This block did NOT run on this runner. Residual R4.'
        return
    }
    $text = Get-Content -LiteralPath $rea -Raw
    Assert-True ($text -match "(?m)^\`$Script:TaskFolderPath\s*=\s*'\\QFlix-LLM\\'") `
        'TaskFolderPath matches the live task path'
    Assert-True ($text -match "LegacyTaskFolderPath\s*=\s*'\\Archangel\\QFlix-LLM\\'") `
        'the drifted path is remembered so it can be swept'
    Assert-True ($text -match 'Remove-ReaTask -Folder \$Script:LegacyTaskFolderPath') `
        'install sweeps the drifted copy instead of leaving two hourly audits'

    # All three live triggers, not just the logon one the old installer wrote.
    Assert-True ($text -match 'New-ScheduledTaskTrigger -AtLogOn')  'logon trigger registered'
    Assert-True ($text -match 'MSFT_TaskSessionStateChangeTrigger') 'session-lock trigger registered'
    Assert-True ($text -match 'RepeatMinutes\s*=\s*60')             'hourly repetition registered'
}

Write-Host ''
Write-Host "PASS $Script:Pass  FAIL $Script:Fail  SKIP $Script:Skip"
if ($Script:Fail -gt 0) {
    Write-Host ''
    Write-Host 'FAILURES:'
    $Script:Failures | ForEach-Object { Write-Host "  - $_" }
    exit 1
}
exit 0

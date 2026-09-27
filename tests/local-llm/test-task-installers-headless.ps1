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
# and no Windows paths resolved. Every subject including qflix-rea.ps1 is
# tracked as of 2026-09-27, so nothing here is expected to skip in CI; the skip
# paths below are kept for a partial checkout and say so loudly when they fire.

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

function Get-IsolatedFunction {
    <#
      Lift one function out of an installer by AST and make it callable here,
      WITHOUT dot-sourcing the file (these scripts run work at load time, and
      one of them is 160KB). Only valid for the pure definition functions.
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name)
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$null, [ref]$null)
    $fn = $ast.Find({
        param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $Name
    }, $true)
    if (-not $fn) { return $null }
    . ([scriptblock]::Create($fn.Extent.Text))
    return (Get-Command $Name -CommandType Function -ErrorAction SilentlyContinue)
}

Test-Case 'the definition functions RUN, and produce absolute paths with no WINDIR' {
    # This block is executable, not a pattern match, because a pattern match is
    # what missed the bug it now guards: the first cut of this fix built paths
    # with `Join-Path $win 'System32\conhost.exe'`, and pwsh on Linux throws
    # "A drive with the name 'C' does not exist" the moment a Windows drive
    # qualifier reaches Join-Path. Every regex here passed; CI failed.
    #
    # So: unset WINDIR, actually CALL each definition function, and assert the
    # value it returns. The hosted runner is the hostile case and also the one
    # that matters, since a function that cannot run there is not pure.
    $subjects = @(
        @{ Rel = 'local-llm/backup-untracked.ps1';  Fn = 'Get-BackupTaskDefinition';  Arg = @{ ScriptPath = '/x/s.ps1' } },
        @{ Rel = 'local-llm/ollama-recover.ps1';    Fn = 'Get-RecoverTaskDefinition'; Arg = @{ ScriptPath = '/x/s.ps1' } },
        @{ Rel = 'local-llm/qflix-rea.ps1';         Fn = 'Get-HeadlessPsAction';      Arg = @{ Arguments  = '-File "/x/s.ps1"' } }
    )
    $saved = $env:WINDIR
    try {
        $env:WINDIR = ''
        foreach ($s in $subjects) {
            $path = Join-Path $scriptsDir (ConvertTo-NativeRel $s.Rel)
            if (-not (Test-Path -LiteralPath $path)) {
                $Script:Skip++
                Write-Host "  SKIP  $($s.Rel) absent -- every subject is tracked, so this is a partial checkout, not by design"
                continue
            }
            $cmd = Get-IsolatedFunction -Path $path -Name $s.Fn
            Assert-True ($null -ne $cmd) "$($s.Rel) : $($s.Fn) is present and parseable"
            if (-not $cmd) { continue }

            $splat = $s.Arg
            $d = & $cmd @splat
            Assert-True ($d.Execute -match '^[A-Za-z]:\\') `
                "$($s.Rel) : Execute is an absolute Windows path with no WINDIR -- $($d.Execute)"
            Assert-True ($d.Execute -like '*conhost.exe') "$($s.Rel) : Execute is conhost"
            Assert-True ($d.Argument -match '^--headless "[A-Za-z]:\\.*powershell\.exe" ') `
                "$($s.Rel) : Argument wraps an absolute powershell.exe -- $($d.Argument)"
        }
    } finally { $env:WINDIR = $saved }

    # install-qflix-collect.ps1 builds its action inline rather than in a pure
    # function, so it cannot be called here. Hold it to the static rule and say
    # so, rather than quietly covering two of three subjects.
    $collect = Join-Path $scriptsDir (ConvertTo-NativeRel 'local/install-qflix-collect.ps1')
    if (Test-Path -LiteralPath $collect) {
        $text = Get-Content -LiteralPath $collect -Raw
        Assert-True ($text -match "\`$env:WINDIR\s*\}\s*else\s*\{\s*'C:\\WINDOWS'\s*\}") `
            'local/install-qflix-collect.ps1 : falls back to a literal Windows root (static check only)'
    }
}

Test-Case 'no installer hands a Windows drive qualifier to Join-Path' {
    # The defect class above, checked everywhere at once: Join-Path validates the
    # drive qualifier against the CURRENT host's PSDrives, so any Windows path
    # built with it is a landmine for the Linux runner.
    foreach ($f in $installers) {
        $rel  = $f.FullName.Substring($repoRoot.Length + 1)
        $text = Get-Content -LiteralPath $f.FullName -Raw
        Assert-True ($text -notmatch 'Join-Path\s+\$win\b') `
            "$rel : the Windows root is concatenated, not Join-Path'd"
    }
}

Test-Case 'REA installs the task where the task actually lives' {
    # The installer said \Archangel\QFlix-LLM\ while the live task has always sat
    # at \QFlix-LLM\. Reinstalling would have created a SECOND hourly REA rather
    # than replacing the first.
    $rea = Join-Path $scriptsDir (ConvertTo-NativeRel 'local-llm/qflix-rea.ps1')
    if (-not (Test-Path -LiteralPath $rea)) {
        $Script:Skip++
        Write-Host '  SKIP  scripts/local-llm/qflix-rea.ps1 absent. It has been TRACKED since'
        Write-Host '        2026-09-27, so this is a partial checkout, not the old R4 skip.'
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

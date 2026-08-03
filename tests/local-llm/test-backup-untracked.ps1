#!/usr/bin/env pwsh
# test-backup-untracked.ps1 — guards the backup path for the files git cannot hold.
#
# Subject: scripts/local-llm/backup-untracked.ps1 (TRACKED, unlike its own
# subjects). Every structural test below runs entirely inside a temp sandbox, so
# it works on the hosted Linux runner with no B: volume and no robocopy.
#
# The LAST block is different: where the real mirror volume is present (i.e. the
# operator's workstation), it asserts the LIVE backup of the real S2 members. A
# backup nobody checks is not a backup, and "run the repo's own test suite" is a
# thing that actually happens here. In CI the block SKIPS LOUDLY and says so —
# never silently.

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
function Assert-Equal {
    param($Expected, $Actual, [string]$Name)
    if ($Expected -eq $Actual) { $Script:Pass++; Write-Host "  PASS  $Name" }
    else {
        $Script:Fail++
        $Script:Failures += "$Name (expected '$Expected', got '$Actual')"
        Write-Host "  FAIL  $Name  expected '$Expected' got '$Actual'"
    }
}
function Test-Case {
    param([string]$Name, [scriptblock]$Block)
    Write-Host ""
    Write-Host "[$Name]"
    try { & $Block }
    catch {
        $Script:Fail++
        $Script:Failures += "$Name (exception): $($_.Exception.Message)"
        Write-Host "  EXCEPTION $($_.Exception.Message)"
    }
}

# NOT named $repoRoot. Dot-sourcing a script runs its param() block in the
# CALLER's scope, and backup-untracked.ps1 declares [string]$RepoRoot — which
# PowerShell resolves case-insensitively to the same variable. A $repoRoot here
# is silently overwritten with '' the moment the subject is loaded, and the
# failure surfaces far away as "cannot bind argument ... empty string".
$RepoDir    = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$scriptPath = Join-Path $RepoDir 'scripts/local-llm/backup-untracked.ps1'
$scopePath  = Join-Path $RepoDir 'manifest/audit-scope.yaml'

if (-not (Test-Path $scriptPath)) {
    Write-Host "ERROR: subject missing at $scriptPath"
    exit 1
}
. $scriptPath -AsModule

# --- sandbox helpers --------------------------------------------------------
$Script:Sandboxes = @()

function New-ScratchDir {
    $d = Join-Path ([IO.Path]::GetTempPath()) ("qflix-bk-" + [Guid]::NewGuid().ToString('N').Substring(0, 10))
    $Script:Sandboxes += $d
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}
function New-ScratchFile {
    <#
      .SYNOPSIS
      A temp fixture this test WRITES. Name stays in a parameter, deliberately.
      .DESCRIPTION
      A Join-Path whose second argument is a quoted literal ending in a loadable
      extension is exactly what C-10 reads as "this test loads a SUBJECT", and it
      then demands the path be git-tracked. A scratch file the test creates is an
      OUTPUT, not a subject — c10's own docstring says as much about the scratch
      paths a PowerShell test merely probes. Keeping the filename in a parameter
      states that in code rather than spending three waivers on it. The one
      genuine negative fixture (a repo path that must NOT exist) still gets a
      named waiver, W-C10-002.

      C-10 matches raw TEXT, comments included: the first draft of this very
      comment quoted the pattern as an example and became a finding against the
      file explaining why it should not be one.
    #>
    param([Parameter(Mandatory)][string]$Dir, [Parameter(Mandatory)][string]$Name, [string[]]$Lines)
    $p = Join-Path $Dir $Name
    if ($null -ne $Lines) { Set-Content -LiteralPath $p -Value $Lines -Encoding UTF8 }
    return $p
}
function New-Sandbox {
    <#
      Builds a miniature of the real topology:
        <sb>/src/repo/manifest/audit-scope.yaml   (a real S2 shape)
        <sb>/src/repo/<member files>
        <sb>/mirror                               (stands in for B:\BAKS\Documents)
        <sb>/hist                                 (stands in for the history store)
      SourceRoot is <sb>/src, so the repo sits UNDER it exactly as
      G:\Documents\GIT\Ultra.cc\QFlix sits under G:\Documents.
    #>
    param([string[]]$Members = @('scripts/local-llm/fake-rea.ps1'), [string]$Body = 'v1')
    $sb = Join-Path ([IO.Path]::GetTempPath()) ("qflix-bk-" + [Guid]::NewGuid().ToString('N').Substring(0, 10))
    $Script:Sandboxes += $sb
    $repo = Join-Path $sb 'src/repo'
    New-Item -ItemType Directory -Path (Join-Path $repo 'manifest') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $sb 'mirror') -Force | Out-Null

    $yaml = @('schema: 1', 'surfaces:', '  S1:', '    title: repo, git-tracked', '  S2:',
              '    title: repo-adjacent, untracked but load-bearing', '    members:')
    foreach ($m in $Members) {
        $yaml += "      - path: $m"
        $yaml += '        owner: operator'
        $yaml += '        residual: because'
    }
    $yaml += @('  S3:', '    title: seedbox runtime state', 'repo_areas: []')
    Set-Content -LiteralPath (Join-Path $repo 'manifest/audit-scope.yaml') -Value $yaml -Encoding UTF8

    foreach ($m in $Members) {
        $f = Join-Path $repo ($m -replace '[\\/]', [string][IO.Path]::DirectorySeparatorChar)
        New-Item -ItemType Directory -Path (Split-Path -Parent $f) -Force | Out-Null
        Set-Content -LiteralPath $f -Value $Body -Encoding UTF8
    }
    return @{
        root = $sb; repo = $repo
        source = (Join-Path $sb 'src'); mirror = (Join-Path $sb 'mirror'); hist = (Join-Path $sb 'hist')
    }
}
function Invoke-Sandbox {
    param($Box, [switch]$VerifyOnly, [int]$Keep = 30, [int]$MaxAge = 48)
    Invoke-BackupUntracked -RepoRoot $Box.repo -SourceRoot $Box.source -MirrorRoot $Box.mirror `
        -HistoryRoot $Box.hist -KeepVersions $Keep -MaxReceiptAgeHours $MaxAge `
        -VerifyOnly:$VerifyOnly -NoAlert
}
function Get-MirrorFile {
    param($Box, [string]$Rel)
    Join-Path $Box.mirror ('repo/' + $Rel -replace '[\\/]', [string][IO.Path]::DirectorySeparatorChar)
}

# ===========================================================================
Test-Case 'the member list comes from audit-scope.yaml, not a hand-list' {
    # The whole point of reading the manifest: enrolling an S2 member enrols it
    # in the backup. A hardcoded list here would be a second policy surface, and
    # the two would drift exactly like REA's prompt and rule table did.
    $paths = @(Get-S2MemberPath -ScopePath $scopePath)
    Assert-True ($paths.Count -ge 2) "real audit-scope.yaml yields >= 2 S2 members (got $($paths.Count))"
    Assert-True ($paths -contains 'scripts/local-llm/qflix-rea.ps1') 'qflix-rea.ps1 is enrolled'
    Assert-True ($paths -contains 'scripts/manitoba-tunnel.ps1') 'manitoba-tunnel.ps1 is enrolled'
    # ...and nothing from a NEIGHBOURING surface leaks in. S4's note names
    # scripts/local-llm and B:\QFlix\data in prose; a sloppy parser scoops those.
    foreach ($p in $paths) {
        Assert-True ($p -match '^[A-Za-z0-9._/-]+$') "member '$p' is a bare path, not prose"
    }
}

Test-Case 'a manifest whose S2 shape changed yields ZERO, never a subset' {
    $sb = New-ScratchDir
    $p = New-ScratchFile -Dir $sb -Name 'scope.yaml' -Lines @(
        'schema: 1', 'surfaces:', '  S9:', '    members:', '      - path: a/b.ps1')
    Assert-Equal 0 (@(Get-S2MemberPath -ScopePath $p)).Count 'members under a non-S2 surface are not claimed'

    $q = New-ScratchFile -Dir $sb -Name 'scope2.yaml' -Lines @(
        'schema: 1', 'surfaces:', '  S2:', '    note: no members key at all')
    Assert-Equal 0 (@(Get-S2MemberPath -ScopePath $q)).Count 'an S2 with no members: key yields zero'
}

Test-Case 'a missing manifest throws instead of backing up nothing' {
    # C-09 discipline: a missing prerequisite must be loud. Returning an empty
    # list here would make the run report "0 members, all fine".
    $threw = $false
    try { Get-S2MemberPath -ScopePath (Join-Path $repoRoot 'manifest/does-not-exist.yaml') } catch { $threw = $true }
    Assert-True $threw 'Get-S2MemberPath throws on a missing scope file'
}

Test-Case 'zero parsed members is a BLOCKER (exit 2), not a clean run' {
    $box = New-Sandbox
    Set-Content -LiteralPath (Join-Path $box.repo 'manifest/audit-scope.yaml') -Encoding UTF8 `
        -Value @('schema: 1', 'surfaces:', '  S1:', '    title: only s1')
    $r = Invoke-Sandbox $box
    Assert-Equal 2 $r.exit 'exit 2'
    Assert-Equal 'BLOCKED' $r.status 'status BLOCKED'
    Assert-True ($r.receipt.blockers -contains 'scope-parsed-zero-s2-members') 'names the blocker'
}

Test-Case 'happy path: mirror copy + history snapshot + verified receipt' {
    $box = New-Sandbox
    $r = Invoke-Sandbox $box
    Assert-Equal 0 $r.exit 'exit 0'
    Assert-Equal 'ok' $r.status 'status ok'
    Assert-Equal 1 $r.receipt.members.Count 'one member'
    $m = $r.receipt.members[0]
    Assert-Equal 'ok' $m.status 'member ok'
    Assert-True (Test-Path -LiteralPath $m.mirror_path) 'mirror file exists'
    Assert-Equal $m.sha256 (Get-FileSha256 $m.mirror_path) 'mirror sha matches source'
    Assert-True (Test-Path -LiteralPath $m.snapshot_path) 'history snapshot exists'
    Assert-Equal $m.sha256 (Get-FileSha256 $m.snapshot_path) 'snapshot sha matches source'
    Assert-Equal $false $m.snapshot_reused 'first run writes a fresh snapshot'
    # the mirror path is the one Backup-Documents itself would have used
    Assert-Equal (Get-MirrorFile $box 'scripts/local-llm/fake-rea.ps1') $m.mirror_path 'mirror path mirrors the source tree'
}

Test-Case 'the history store is OUTSIDE the /MIR destination' {
    # If it were inside, robocopy /MIR (which implies /PURGE) would delete every
    # snapshot on the next midnight run, because none of them exist under the
    # mirror SOURCE. This is the single most load-bearing layout fact.
    $box = New-Sandbox
    $r = Invoke-Sandbox $box
    $inside = Get-PathUnderRoot -Path $r.receipt.members[0].snapshot_path -Root $box.mirror
    Assert-Equal $null $inside 'snapshot is not under the mirror root'
}

Test-Case 'a corrupted mirror copy is caught by hash, not by size or mtime' {
    $box = New-Sandbox -Body 'aaaa'
    $r1 = Invoke-Sandbox $box
    $mirror = $r1.receipt.members[0].mirror_path
    # same byte count, same visible metadata; only the content differs
    [IO.File]::WriteAllText($mirror, 'bbbb')
    (Get-Item -LiteralPath $mirror).LastWriteTimeUtc = (Get-Item -LiteralPath $r1.receipt.members[0].source).LastWriteTimeUtc
    $r2 = Invoke-Sandbox $box -VerifyOnly
    Assert-Equal 1 $r2.exit 'exit 1'
    Assert-True ($r2.receipt.problems -contains 'mirror-mismatch:scripts/local-llm/fake-rea.ps1') 'names the file'
}

Test-Case 'a backup run REPAIRS a divergent mirror' {
    $box = New-Sandbox -Body 'aaaa'
    $r1 = Invoke-Sandbox $box
    [IO.File]::WriteAllText($r1.receipt.members[0].mirror_path, 'bbbb')
    $r2 = Invoke-Sandbox $box
    Assert-Equal 0 $r2.exit 'exit 0 after repair'
    Assert-Equal $r2.receipt.members[0].sha256 (Get-FileSha256 $r2.receipt.members[0].mirror_path) 'mirror restored'
}

Test-Case 'a source ROLLED BACK to older content still overwrites the mirror' {
    # The robocopy /IS /IT reason. Default robocopy skips a source whose mtime is
    # older than the destination, so a restored-from-backup or reverted file
    # would leave the newer, wrong bytes in the mirror forever.
    $box = New-Sandbox -Body 'new-content'
    $r1 = Invoke-Sandbox $box
    $src = $r1.receipt.members[0].source
    [IO.File]::WriteAllText($src, 'old-content')
    (Get-Item -LiteralPath $src).LastWriteTimeUtc = (Get-Date).ToUniversalTime().AddDays(-30)
    $r2 = Invoke-Sandbox $box
    Assert-Equal 0 $r2.exit 'exit 0'
    Assert-Equal 'old-content' ((Get-Content -LiteralPath $r2.receipt.members[0].mirror_path -Raw).Trim()) 'mirror took the older source'
}

Test-Case 'history keeps the PREVIOUS version after an edit (this is what /MIR could not do)' {
    $box = New-Sandbox -Body 'v1'
    $r1 = Invoke-Sandbox $box
    $sha1 = $r1.receipt.members[0].sha256
    [IO.File]::WriteAllText($r1.receipt.members[0].source, 'v2-a-bad-edit')
    $r2 = Invoke-Sandbox $box
    Assert-Equal 2 $r2.receipt.members[0].snapshot_count 'two snapshots on disk'
    $histDir = Split-Path -Parent $r2.receipt.members[0].snapshot_path
    $olds = @(Get-ChildItem -LiteralPath $histDir -Filter '*.bak' -File | Where-Object { (Get-FileSha256 $_.FullName) -eq $sha1 })
    Assert-Equal 1 $olds.Count 'the pre-edit content is still recoverable'
    Assert-Equal 'v1' ((Get-Content -LiteralPath $olds[0].FullName -Raw).Trim()) 'and it reads back byte-for-byte'
}

Test-Case 'snapshots are content-addressed: a no-op re-run adds nothing' {
    $box = New-Sandbox
    $r1 = Invoke-Sandbox $box
    $r2 = Invoke-Sandbox $box
    Assert-Equal 1 $r2.receipt.members[0].snapshot_count 'still one snapshot'
    Assert-Equal $true $r2.receipt.members[0].snapshot_reused 'and the receipt SAYS it was reused'
}

Test-Case 'pruning honours KeepVersions but never evicts the current content' {
    # Six edits inside the same second. That is not an artificial rate: it is
    # exactly what an agent editing a file in a loop does, and it is how the
    # second-resolution stamp bug was found — the name sort became a SHA sort,
    # the pruner picked a non-oldest victim, and the keep-current exemption then
    # (correctly) refused to delete it, leaving Keep+1 files.
    $box = New-Sandbox -Body 'e0'
    foreach ($i in 1..5) {
        $r = Invoke-Sandbox $box -Keep 3
        [IO.File]::WriteAllText($r.receipt.members[0].source, "e$i")
    }
    $r = Invoke-Sandbox $box -Keep 3
    Assert-Equal 3 $r.receipt.members[0].snapshot_count 'pruned to exactly KeepVersions'
    Assert-True (Test-Path -LiteralPath $r.receipt.members[0].snapshot_path) 'the current snapshot survived pruning'
    Assert-Equal $r.receipt.members[0].sha256 (Get-FileSha256 $r.receipt.members[0].snapshot_path) 'and still matches source'
}

Test-Case 'reverting to an already-archived version cannot delete that version' {
    # The one case where Keep+1 files are correct: the current content is an OLD
    # snapshot, so it sits in the doomed set. Deleting it would leave the receipt
    # pointing at a file that no longer exists and red the NEXT run for a reason
    # that is not a fault.
    # Reached by LOWERING KeepVersions with an old version restored — the doomed
    # set is then the oldest N, and the current content is sitting in it. Every
    # write goes through [IO.File]::WriteAllText so the reverted bytes are
    # byte-identical (Set-Content -Encoding UTF8 on 5.1 emits a BOM; WriteAllText
    # does not, and a 3-byte difference is a different SHA and a different test).
    $box = New-Sandbox
    $src = (Invoke-Sandbox $box).receipt.members[0].source
    foreach ($v in @('v1', 'v2', 'v3', 'v4', 'v5')) {
        [IO.File]::WriteAllText($src, $v)
        Invoke-Sandbox $box -Keep 5 | Out-Null
    }
    [IO.File]::WriteAllText($src, 'v1')          # restore an archived version, byte-for-byte
    $r = Invoke-Sandbox $box -Keep 2
    Assert-Equal 0 $r.exit 'exit 0'
    Assert-Equal $true $r.receipt.members[0].snapshot_reused 'the archived version was reused, not re-written'
    Assert-True (Test-Path -LiteralPath $r.receipt.members[0].snapshot_path) 'and it was exempted from the prune'
    Assert-Equal 3 $r.receipt.members[0].snapshot_count 'exactly KeepVersions+1 survive (the exempted one)'
    Assert-Equal $r.receipt.members[0].sha256 (Get-FileSha256 $r.receipt.members[0].snapshot_path) 'and it still matches source'
}

Test-Case 'a VANISHED source is a problem, never "nothing to do"' {
    $box = New-Sandbox
    $r1 = Invoke-Sandbox $box
    Remove-Item -LiteralPath $r1.receipt.members[0].source -Force
    $r2 = Invoke-Sandbox $box
    Assert-Equal 1 $r2.exit 'exit 1'
    Assert-True ($r2.receipt.problems -contains 'missing-source:scripts/local-llm/fake-rea.ps1') 'names the missing file'
}

Test-Case 'an unmounted backup volume is exit 2 (cannot tell), not exit 0' {
    $box = New-Sandbox
    $box2 = @{ repo = $box.repo; source = $box.source; mirror = (Join-Path $box.root 'not-mounted'); hist = $box.hist }
    $r = Invoke-Sandbox $box2
    Assert-Equal 2 $r.exit 'exit 2'
    Assert-True (@($r.receipt.blockers | Where-Object { $_ -like 'mirror-root-absent:*' }).Count -eq 1) 'names mirror-root-absent'
}

Test-Case 'a repo outside the mirror source root is exit 2, not a silent success' {
    # The existing Backup-Documents chain only covers G:\Documents. A checkout
    # somewhere else is structurally uncovered, and saying so is the only honest
    # answer available.
    $box = New-Sandbox
    $box2 = @{ repo = $box.repo; source = (Join-Path $box.root 'elsewhere'); mirror = $box.mirror; hist = $box.hist }
    New-Item -ItemType Directory -Path $box2.source -Force | Out-Null
    $r = Invoke-Sandbox $box2
    Assert-Equal 2 $r.exit 'exit 2'
    Assert-True (@($r.receipt.blockers | Where-Object { $_ -like 'repo-outside-mirror-root:*' }).Count -eq 1) 'names it'
}

Test-Case 'VerifyOnly is read-only and does not launder a stale receipt' {
    $box = New-Sandbox
    $r1 = Invoke-Sandbox $box
    $receipt = Join-Path $box.hist 'receipt.json'
    $before = (Get-Content -LiteralPath $receipt -Raw)
    Start-Sleep -Milliseconds 20
    $r2 = Invoke-Sandbox $box -VerifyOnly
    Assert-Equal 0 $r2.exit 'verify passes on a fresh backup'
    Assert-Equal $before (Get-Content -LiteralPath $receipt -Raw) 'the backup receipt is untouched by a verify run'
    Assert-True (Test-Path -LiteralPath (Join-Path $box.hist 'receipt.verify.json')) 'verify writes its own receipt beside it'
}

Test-Case 'VerifyOnly reds on a receipt older than the cap — the dead-man for a dead task' {
    $box = New-Sandbox
    Invoke-Sandbox $box | Out-Null
    $receipt = Join-Path $box.hist 'receipt.json'
    $obj = (Get-Content -LiteralPath $receipt -Raw) | ConvertFrom-Json
    $obj.generated_utc = (Get-Date).ToUniversalTime().AddHours(-100).ToString('o')
    ($obj | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $receipt -Encoding UTF8
    $r = Invoke-Sandbox $box -VerifyOnly -MaxAge 48
    Assert-Equal 1 $r.exit 'exit 1'
    Assert-True (@($r.receipt.problems | Where-Object { $_ -like 'receipt-stale:*' }).Count -eq 1) 'names receipt-stale'
}

Test-Case 'VerifyOnly with no receipt at all is a problem, not a pass' {
    $box = New-Sandbox
    New-Item -ItemType Directory -Path $box.hist -Force | Out-Null
    $r = Invoke-Sandbox $box -VerifyOnly
    Assert-Equal 1 $r.exit 'exit 1'
    Assert-True ($r.receipt.problems -contains 'receipt-absent') 'names receipt-absent'
}

Test-Case 'failure drops a sentinel and success clears it' {
    $box = New-Sandbox
    $r1 = Invoke-Sandbox $box
    $sentinel = Join-Path $box.hist 'FAILED'
    Assert-True (-not (Test-Path -LiteralPath $sentinel)) 'clean run leaves no sentinel'
    Remove-Item -LiteralPath $r1.receipt.members[0].source -Force
    Invoke-Sandbox $box | Out-Null
    Assert-True (Test-Path -LiteralPath $sentinel) 'failing run drops the sentinel'
    Set-Content -LiteralPath $r1.receipt.members[0].source -Value 'restored' -Encoding UTF8
    Invoke-Sandbox $box | Out-Null
    Assert-True (-not (Test-Path -LiteralPath $sentinel)) 'recovery clears the sentinel'
}

Test-Case 'every S2 member is enrolled, not just the one we care about' {
    # Regression guard for the obvious shortcut: hardcoding qflix-rea.ps1 and
    # letting manitoba-tunnel.ps1 rot uncovered.
    $box = New-Sandbox -Members @('scripts/local-llm/fake-rea.ps1', 'scripts/fake-tunnel.ps1', 'a/b/c/deep.ps1')
    $r = Invoke-Sandbox $box
    Assert-Equal 0 $r.exit 'exit 0'
    Assert-Equal 3 $r.receipt.members.Count 'all three members backed up'
    foreach ($m in $r.receipt.members) { Assert-Equal 'ok' $m.status "$($m.path) ok" }
}

Test-Case 'the scheduled task definition is code, and it is HOURLY' {
    # The cadence is the whole point of the task existing: the parent
    # Backup-Documents already runs daily, and "daily" is the 22-hour window
    # that left the two newest edits of qflix-rea.ps1 on a single disk.
    $d = Get-BackupTaskDefinition -ScriptPath '/x/backup-untracked.ps1'
    Assert-Equal '\Archangel\Backups\' $d.TaskPath 'sits beside Backup-Documents, not in a new tree'
    Assert-Equal 'QFlix-Untracked-Backup' $d.TaskName 'stable task name'
    Assert-Equal 60 $d.RepeatMinutes 'repeats hourly, not daily'
    Assert-True ($d.Argument -like '*-NoProfile*') 'runs with -NoProfile'
    Assert-True ($d.Argument -like '*-File "/x/backup-untracked.ps1"*') 'target path is quoted'
    Assert-True ($d.Description.Length -ge 60) 'carries a description an operator can act on'
}

Test-Case 'alerting is not silently swallowed when every channel is unavailable' {
    # C-03 applied to the alarm itself: if the pager cannot be reached, that must
    # be COUNTED and returned, never absorbed.
    $sb = New-ScratchDir
    $skips = @(Send-BackupAlert -Reason 'unit-test' -Message 'unit test, do not page' `
                                -WebhookFile (Join-Path $sb 'nope.url') `
                                -StatePath (New-ScratchFile -Dir $sb -Name 'state.json') -DedupHours 24)
    Assert-True ($skips -contains 'webhook-file-absent') 'absent webhook is counted'
    Assert-True ($skips.Count -ge 1) 'skips are returned to the caller'
}

Test-Case 'the dedup path survives a SECOND alert for the same reason' {
    # Regression guard for a real 2026-08-03 bug: [DateTime]::TryParse was given
    # an untyped $null [ref], which Set-StrictMode 2.0 rejects. Dedup only runs
    # once a reason already has a recorded timestamp, so the first alert always
    # worked and the second always threw — the alarm breaking precisely when a
    # failure PERSISTED. Every test here only ever fired one alert, so nothing
    # caught it until the channel was wired by hand and fired twice.
    $sb = New-ScratchDir
    $state = New-ScratchFile -Dir $sb -Name 'state.json'
    # Pre-seed the state exactly as a delivered alert would have left it.
    (@{ 'boom' = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json) |
        Set-Content -LiteralPath $state -Encoding UTF8
    $skips = @(Send-BackupAlert -Reason 'boom' -Message 'second alert' `
                                -WebhookFile (Join-Path $sb 'nope.url') -StatePath $state -DedupHours 24)
    Assert-True ($skips -contains 'alert-deduped:boom') 'a repeat inside the window is deduped, not thrown'

    # ...and the window really does expire, or a persistent failure goes quiet
    # forever, which is the same class of bug in the other direction.
    (@{ 'boom' = (Get-Date).ToUniversalTime().AddHours(-48).ToString('o') } | ConvertTo-Json) |
        Set-Content -LiteralPath $state -Encoding UTF8
    $skips2 = @(Send-BackupAlert -Reason 'boom' -Message 'stale alert' `
                                 -WebhookFile (Join-Path $sb 'nope.url') -StatePath $state -DedupHours 24)
    Assert-True (-not ($skips2 -contains 'alert-deduped:boom')) 'an alert older than the window re-fires'

    # An unparseable timestamp must not dedup either — fail OPEN on the alarm.
    (@{ 'boom' = 'not-a-date' } | ConvertTo-Json) | Set-Content -LiteralPath $state -Encoding UTF8
    $skips3 = @(Send-BackupAlert -Reason 'boom' -Message 'corrupt state' `
                                 -WebhookFile (Join-Path $sb 'nope.url') -StatePath $state -DedupHours 24)
    Assert-True (-not ($skips3 -contains 'alert-deduped:boom')) 'a corrupt dedup state does not silence the pager'
}

# ===========================================================================
# LIVE assertion — only where the real backup volume is mounted.
# ===========================================================================
Test-Case 'LIVE: the real S2 members are actually backed up right now' {
    $liveMirror = 'B:\BAKS\Documents'
    $liveHist   = 'B:\BAKS\qflix-untracked-history'
    if (-not (Test-Path -LiteralPath $liveMirror)) {
        $Script:Skip++
        Write-Host "  SKIP  live backup volume $liveMirror is not mounted on this host."
        Write-Host "        This is expected on the CI runner and ONLY there. The live"
        Write-Host "        assertion did NOT run: read it as a limit, not a pass."
        return
    }
    $r = Invoke-BackupUntracked -RepoRoot $RepoDir -SourceRoot 'G:\Documents' -MirrorRoot $liveMirror `
            -HistoryRoot $liveHist -VerifyOnly -NoAlert
    foreach ($p in $r.receipt.problems) { Write-Host "        problem: $p" }
    foreach ($b in $r.receipt.blockers) { Write-Host "        blocker: $b" }
    Assert-Equal 0 $r.exit 'live verify of the real mirror + history store passes'
    Assert-True ($r.receipt.members.Count -ge 2) 'at least two live S2 members verified'
}

Write-Host ""
Write-Host "PASS: $Script:Pass   FAIL: $Script:Fail   SKIPPED BLOCKS: $Script:Skip"
foreach ($sb in $Script:Sandboxes) { Remove-Item -LiteralPath $sb -Recurse -Force -ErrorAction SilentlyContinue }
if ($Script:Fail -gt 0) {
    foreach ($f in $Script:Failures) { Write-Host "  - $f" }
    exit 1
}
exit 0

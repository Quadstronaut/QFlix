#Requires -Version 5.1
<#
.SYNOPSIS
  Back up and VERIFY the gitignored-but-load-bearing workstation files
  (audit-scope.yaml surface S2), on top of the existing Backup-Documents chain.

.DESCRIPTION
  ==========================================================================
  WHY THIS EXISTS
  ==========================================================================
  scripts/local-llm/qflix-rea.ps1 is the ALERTING LAYER for the whole stack
  and it is gitignored (.gitignore:55) because it is operator-local tooling
  and the repo is public. git is therefore not its backup, by construction.

  A backup DID already exist. It is the `\Archangel\Backups\Backup-Documents`
  scheduled task:

      robocopy.exe "G:\Documents" "B:\BAKS\Documents" /MIR /XD wf_*
                   /XF worktree-wf_* /R:1 /W:1 /MT:8 /NP /NFL /NDL
                   /LOG+:B:\BAKS\backup-documents.log

  chained to `\Archangel\Backups\Archive-B-to-D` (B:\BAKS -> D:\BAKS, additive,
  with oldest-first eviction below 10% free). This script does NOT replace
  either. It extends them, because the chain had three holes and nobody would
  ever have found out:

    H1  NOBODY CHECKED IT.  Measured 2026-08-03, before this script existed:
          source  84383 bytes  sha 61677A93...  mtime 2026-08-03 02:11
          B:\BAKS 78467 bytes  sha F9A623BA...  mtime 2026-08-02 22:37
          D:\BAKS 78467 bytes  sha F9A623BA...  mtime 2026-08-02 22:37
        The two most recent edits to the single most critical unversioned file
        in the system were in NO backup, and every surface reported success.
        Backup-Documents' own LastTaskResult was 3 (a robocopy success code).
        A backup nobody verifies is a belief, not a backup.

    H2  UP TO 24h OF LAG.  The mirror runs daily at 00:00. Anything written
        after it lives on exactly one disk until the next midnight.

    H3  A MIRROR IS NOT HISTORY.  /MIR propagates a bad edit; the D: tier is
        additive for DELETES but still overwrites on EDIT. So "restore the
        version from before that change" was not a thing you could do. This is
        the sentence residual R4 has carried since 2026-07-29.

  ==========================================================================
  WHAT IT DOES
  ==========================================================================
  1. Reads the file list from manifest/audit-scope.yaml surface S2. NOT from a
     hand-list here. That manifest is already the repo's declaration of "the
     untracked things the system depends on"; enrolling a new S2 member
     therefore enrols it in the backup with no second policy surface to drift
     (the failure mode named in memory `rea-noise-enforcement`).
  2. Copies each member into the SAME mirror tree Backup-Documents writes, at
     the path Backup-Documents would have used. Closes H2: the copy is
     immediate, and the daily /MIR run is a no-op over an already-correct file.
  3. Appends a CONTENT-ADDRESSED snapshot to a history store that lives OUTSIDE
     the /MIR destination (see HISTORY STORE below). Closes H3.
  4. VERIFIES by SHA256 -- source vs mirror vs the snapshot that claims to hold
     this content -- and writes a receipt. Closes H1.
  5. On any problem: posts to Discord (the operator's real pager, the same one
     REA uses), writes a Windows event-log Error, drops a FAILED sentinel, and
     exits non-zero.

  ==========================================================================
  HISTORY STORE -- WHY IT IS NOT UNDER B:\BAKS\Documents
  ==========================================================================
  Because Backup-Documents runs `/MIR`, and /MIR implies /PURGE. ANY file under
  B:\BAKS\Documents that does not exist under G:\Documents is deleted on the
  next midnight run. A version history written there would be silently emptied
  every night and would look fine in between. It therefore lives at
  B:\BAKS\qflix-untracked-history\, a sibling of the mirror destination and
  outside its purge scope.

  ==========================================================================
  EXIT CODES  (rule 5 -- empty-because-clean must differ from empty-because-broken)
  ==========================================================================
    0  every S2 member is backed up and byte-identical in the mirror and in the
       history store.
    1  DIVERGENT / MISSING. The backup is wrong and this script is certain.
    2  CANNOT TELL. The manifest is unreadable or parses to zero members, the
       repo is not under the mirror source root, or the backup volume is not
       mounted. Deliberately distinct from 1: "the backup is broken" and "I
       cannot see the backup medium" demand different operator actions.
       Fails CLOSED -- never silently green.

  ==========================================================================
  HONEST LIMITS
  ==========================================================================
  L1 THE ALARM DIES WITH THE SCRIPT. Every loud channel here (Discord, event
     log, exit code, sentinel) is emitted BY this script, so a disabled task
     alerts nobody. That is the same turtle as R-WORKSTATION-SCHEDULED-TASKS and it is registered
     there rather than papered over. Two things bound it: `-VerifyOnly` fails
     on a receipt older than -MaxReceiptAgeHours, so the staleness is visible
     to anything that asks; and tests/local-llm/test-backup-untracked.ps1
     asserts the LIVE backup whenever the mirror volume is present, so running
     the repo's own test suite on the workstation surfaces it.
  L2 IT PROVES BYTES, NOT CORRECTNESS. A verified backup of a broken edit is a
     verified backup of a broken edit. What H3's history store buys is the
     ability to go back; it does not judge which version was good.
  L3 CONTENT-ADDRESSED DEDUP MEANS NO SNAPSHOT ON A NO-OP RUN. Re-running with
     an unchanged source adds nothing -- by design, so hourly runs do not mint
     8,760 identical copies a year. `snapshot_reused: true` in the receipt says
     so explicitly rather than implying a fresh write.
  L4 THE VERSION HISTORY LIVES ON ONE DRIVE. The current-version MIRROR reaches
     two (B: here, D: via the existing Archive-B-to-D task at 01:00), but the
     snapshot store is B:-only, because Archive-B-to-D copies exactly
     B:\BAKS\{Documents,Downloads} and extending it would mean editing a script
     outside this repo that backs up everything else on the machine. Losing B:
     costs the history, not the backup. Stated rather than implied.
#>

[CmdletBinding()]
param(
    # Dot-source without executing, so tests can exercise the functions.
    [switch]$AsModule,

    # Verify only: no copies, no snapshots, no pruning. Also enforces receipt
    # freshness, which plain backup mode cannot (it has just written it).
    [switch]$VerifyOnly,

    [string]$RepoRoot,

    # The /MIR pair from \Archangel\Backups\Backup-Documents, verbatim. These
    # are parameters and not constants precisely so the tests can drive the
    # whole thing over temp directories.
    [string]$SourceRoot  = 'G:\Documents',
    [string]$MirrorRoot  = 'B:\BAKS\Documents',

    # Sibling of MirrorRoot -- see HISTORY STORE above. Never inside it.
    [string]$HistoryRoot = 'B:\BAKS\qflix-untracked-history',

    [string]$ReceiptPath,
    [string]$LogPath,
    [string]$StatePath,

    [int]$KeepVersions = 30,
    [int]$MaxReceiptAgeHours = 48,

    # Per-reason Discord dedup, same shape as REA's dead-man dedup.
    [int]$AlertDedupHours = 24,

    [switch]$NoAlert,
    [string]$WebhookFile,

    # Register (or re-register) the Windows scheduled task, then exit.
    [switch]$InstallTask
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Script:BackupSchema = 1

# ---------------------------------------------------------------------------
# manifest/audit-scope.yaml -> surface S2 member paths
# ---------------------------------------------------------------------------
# No ConvertFrom-Yaml in PowerShell 5.1 and no powershell-yaml dependency: the
# same reasoning rea-noise-classes.ps1 wrote down -- a package manager in the
# backup path is a new failure mode bought for nothing. This parser handles
# exactly the shape audit-scope.yaml uses and returns an EMPTY list rather than
# a partial one on anything else, and an empty list is a hard exit 2 upstream.
# A half-parsed member list would back up a subset and report success, which is
# the H1 failure again in a new costume.
function Get-S2MemberPath {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$ScopePath)

    if (-not (Test-Path -LiteralPath $ScopePath)) {
        throw "audit-scope.yaml not found at $ScopePath - refusing to guess which files are load-bearing"
    }

    $paths = @()
    $inSurfaces = $false
    $inS2 = $false
    $inMembers = $false

    foreach ($line in (Get-Content -LiteralPath $ScopePath -Encoding UTF8)) {
        $line = $line -replace "`r$", ''
        if ($line -match '^\s*#') { continue }

        # Top-level key: only `surfaces:` opens the region we care about.
        if ($line -match '^[A-Za-z_][A-Za-z0-9_]*:') {
            $inSurfaces = ($line -match '^surfaces:\s*$')
            $inS2 = $false
            $inMembers = $false
            continue
        }
        if (-not $inSurfaces) { continue }

        # Surface keys sit at exactly two spaces of indent: `  S1:` .. `  S5:`.
        if ($line -match '^\s{2}\S') {
            $inS2 = ($line -match '^\s{2}S2:\s*$')
            $inMembers = $false
            continue
        }
        if (-not $inS2) { continue }

        if ($line -match '^\s{4}members:\s*$') { $inMembers = $true; continue }
        # Any other key at the members: indent closes the members list.
        if ($line -match '^\s{4}\S' -and $line -notmatch '^\s{4}members:\s*$') { $inMembers = $false; continue }
        if (-not $inMembers) { continue }

        if ($line -match '^\s*-\s+path:\s*(\S+)\s*$') {
            $p = $Matches[1].Trim("'").Trim('"')
            if ($p) { $paths += $p }
        }
    }
    return $paths
}

# ---------------------------------------------------------------------------
# path plumbing
# ---------------------------------------------------------------------------
# Separator-normalise to whatever the running platform uses. The manifest
# writes POSIX separators; Windows is the production host; the CI runner that
# executes tests/local-llm/** is Linux. Hardcoding '\' would make the test suite
# unrunnable in the very CI job C-10 exists to enforce.
function ConvertTo-NativePath {
    param([Parameter(Mandatory)][string]$Path)
    ($Path -replace '[\\/]', [string][IO.Path]::DirectorySeparatorChar)
}

function Get-PathUnderRoot {
    <#
      .SYNOPSIS
      The portion of $Path below $Root, or $null when $Path is not under $Root.
      .DESCRIPTION
      Case-insensitive (Windows) and separator-normalised. Returning $null
      rather than throwing lets the caller report `repo-outside-mirror-root` as
      a distinct exit-2 condition: it means the EXISTING mirror chain
      structurally cannot cover this file, which is a different fact from "the
      copy is wrong".
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Root)
    $sep = [string][IO.Path]::DirectorySeparatorChar
    $p = (ConvertTo-NativePath $Path).TrimEnd($sep)
    $r = (ConvertTo-NativePath $Root).TrimEnd($sep)
    if ($p.Length -le $r.Length) { return $null }
    if (-not $p.Substring(0, $r.Length + 1).Equals($r + $sep, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    return $p.Substring($r.Length + 1)
}

function Get-FileSha256 {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToUpperInvariant()
}

function ConvertTo-HistoryKey {
    # scripts/local-llm/qflix-rea.ps1 -> scripts__local-llm__qflix-rea.ps1
    param([Parameter(Mandatory)][string]$RelPath)
    ($RelPath -replace '[\\/]', '__')
}

# ---------------------------------------------------------------------------
# transport -- robocopy, the same tool the parent task uses
# ---------------------------------------------------------------------------
# /IS /IT force the copy even when robocopy judges the pair "same" or "tweaked".
# Without them a source that was ROLLED BACK (older mtime, different bytes) is
# skipped and the mirror keeps the newer, wrong file -- silently. The hash check
# afterwards is what actually decides; robocopy's exit code is only a hint, so a
# transport failure falls through to one Copy-Item retry rather than aborting.
function Copy-ToMirror {
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)

    $dstDir = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }

    # robocopy is Windows-only. On the Linux CI runner that executes
    # tests/local-llm/**, fall back to Copy-Item rather than throwing: the
    # transport is an implementation detail, the SHA256 check below is the
    # contract. rc = -1 means "robocopy was not available", which is a fact
    # worth being able to read back, not a failure.
    $rc = -1
    if (Get-Command robocopy.exe -ErrorAction SilentlyContinue) {
        $srcDir = Split-Path -Parent $Source
        $name = Split-Path -Leaf $Source
        & robocopy.exe $srcDir $dstDir $name /IS /IT /COPY:DAT /R:1 /W:1 /NP /NFL /NDL /NJH /NJS | Out-Null
        $rc = $LASTEXITCODE
    }

    # Verify, then retry once through a different code path. robocopy exit codes
    # >= 8 are failures, but even rc < 8 has been observed to leave a stale file
    # on odd filesystems, so the hash is the arbiter either way.
    if ((Get-FileSha256 $Destination) -ne (Get-FileSha256 $Source)) {
        Copy-Item -LiteralPath $Source -Destination $Destination -Force
    }
    return $rc
}

# ---------------------------------------------------------------------------
# history store
# ---------------------------------------------------------------------------
function Add-HistorySnapshot {
    <#
      .SYNOPSIS
      Content-addressed snapshot. Returns @{ path; reused }.
      .DESCRIPTION
      Named <utc-stamp>-<sha12>.bak. The stamp sorts lexically, so pruning is a
      name sort; the sha makes re-runs idempotent. `reused` is surfaced in the
      receipt so a no-op run cannot be mistaken for a fresh write (limit L3).
    #>
    param(
        [Parameter(Mandatory)][string]$Source,
        [Parameter(Mandatory)][string]$HistoryDir,
        [Parameter(Mandatory)][string]$Sha256
    )
    if (-not (Test-Path -LiteralPath $HistoryDir)) { New-Item -ItemType Directory -Path $HistoryDir -Force | Out-Null }
    $short = $Sha256.Substring(0, 12)
    $existing = @(Get-ChildItem -LiteralPath $HistoryDir -Filter "*-$short.bak" -File -ErrorAction SilentlyContinue |
                  Sort-Object Name)
    if ($existing.Count -gt 0) {
        return @{ path = $existing[-1].FullName; reused = $true }
    }
    # MILLISECOND precision, not seconds. Pruning is a lexical name sort, so a
    # second-resolution stamp makes several edits inside the same second sort by
    # their SHA — i.e. arbitrarily — and the "oldest" snapshot the pruner picks
    # is then not the oldest. Caught by the KeepVersions test, which saw 4 files
    # survive a Keep=3 prune because the current content landed in the doomed
    # set and the keep-current exemption (correctly) refused to delete it.
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd'T'HHmmssfff'Z'")
    $dest = Join-Path $HistoryDir "$stamp-$short.bak"
    Copy-Item -LiteralPath $Source -Destination $dest -Force
    return @{ path = $dest; reused = $false }
}

function Remove-OldSnapshot {
    <#
      .SYNOPSIS
      Keep the newest $Keep snapshots, and ALWAYS keep the one holding $KeepSha.
      .DESCRIPTION
      The current content is exempt from pruning on purpose: with Keep small and
      a burst of edits, a plain "newest N" rule can evict the snapshot the
      receipt just pointed at, which would fail verification on the next run for
      a reason that has nothing to do with a real fault.
      Returns the number of files removed (counted, never silent -- rule 4).
    #>
    param(
        [Parameter(Mandatory)][string]$HistoryDir,
        [Parameter(Mandatory)][int]$Keep,
        [string]$KeepSha
    )
    if ($Keep -le 0) { return 0 }
    if (-not (Test-Path -LiteralPath $HistoryDir)) { return 0 }
    $all = @(Get-ChildItem -LiteralPath $HistoryDir -Filter '*.bak' -File -ErrorAction SilentlyContinue |
             Sort-Object Name)
    if ($all.Count -le $Keep) { return 0 }
    $keepShort = if ($KeepSha) { $KeepSha.Substring(0, 12) } else { '' }
    $doomed = @($all[0..($all.Count - $Keep - 1)])
    $removed = 0
    foreach ($f in $doomed) {
        if ($keepShort -and $f.Name.EndsWith("-$keepShort.bak")) { continue }
        Remove-Item -LiteralPath $f.FullName -Force
        $removed++
    }
    return $removed
}

# ---------------------------------------------------------------------------
# alerting
# ---------------------------------------------------------------------------
function Send-BackupAlert {
    <#
      .SYNOPSIS
      Discord + Windows event log, with per-reason dedup.
      .DESCRIPTION
      Reuses the webhook REA already pages through rather than inventing a
      channel. Returns a list of SKIP labels for anything that could not be
      delivered -- a swallowed alert failure is the exact class C-03 exists for.
    #>
    param(
        [Parameter(Mandatory)][string]$Reason,
        [Parameter(Mandatory)][string]$Message,
        [string]$WebhookFile,
        [string]$StatePath,
        [int]$DedupHours = 24
    )
    $skips = @()

    # dedup
    $state = @{}
    if ($StatePath -and (Test-Path -LiteralPath $StatePath)) {
        try {
            $raw = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8
            if ($raw.Trim()) {
                $obj = $raw | ConvertFrom-Json
                foreach ($p in $obj.PSObject.Properties) { $state[$p.Name] = $p.Value }
            }
        } catch { $skips += 'alert-state-unreadable' }
    }
    if ($state.ContainsKey($Reason)) {
        # MUST be a typed [DateTime], not $null. Under Set-StrictMode 2.0 an
        # untyped [ref] cannot bind TryParse's out-parameter and this THROWS —
        # which, because dedup only runs on the SECOND alert for a reason, means
        # the alarm would have crashed exactly when a failure persisted. Found
        # 2026-08-03 by firing the wiring test twice.
        [DateTime]$last = [DateTime]::MinValue
        if ([DateTime]::TryParse([string]$state[$Reason], [ref]$last)) {
            if (((Get-Date).ToUniversalTime() - $last.ToUniversalTime()).TotalHours -lt $DedupHours) {
                return @("alert-deduped:$Reason")
            }
        }
    }

    $delivered = $false
    if ($WebhookFile -and (Test-Path -LiteralPath $WebhookFile)) {
        $url = (Get-Content -LiteralPath $WebhookFile -Raw -Encoding UTF8).Trim()
        if ($url) {
            try {
                $body = @{ content = $Message } | ConvertTo-Json -Compress
                Invoke-RestMethod -Uri $url -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 20 | Out-Null
                $delivered = $true
            } catch { $skips += 'discord-post-failed' }
        } else { $skips += 'webhook-file-empty' }
    } else { $skips += 'webhook-file-absent' }

    # Windows event log: best effort. Registering a source needs admin, so a
    # failure here is COUNTED, not fatal -- Discord is the primary channel.
    try {
        if (-not [System.Diagnostics.EventLog]::SourceExists('QFlix-Backup')) {
            New-EventLog -LogName Application -Source 'QFlix-Backup' -ErrorAction Stop
        }
        Write-EventLog -LogName Application -Source 'QFlix-Backup' -EntryType Error `
                       -EventId 1001 -Message $Message -ErrorAction Stop
        $delivered = $true
    } catch { $skips += 'eventlog-unavailable' }

    if ($delivered -and $StatePath) {
        try {
            $state[$Reason] = (Get-Date).ToUniversalTime().ToString('o')
            $dir = Split-Path -Parent $StatePath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            ($state | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $StatePath -Encoding UTF8
        } catch { $skips += 'alert-state-unwritable' }
    }
    if (-not $delivered) { $skips += 'alert-undelivered-on-every-channel' }
    return $skips
}

# ---------------------------------------------------------------------------
# schedule, as code
# ---------------------------------------------------------------------------
# The schedule is otherwise pure live registry state -- residual R-WORKSTATION-SCHEDULED-TASKS, the
# same row that already covers REA's own task. It cannot be made offline-
# enumerable, but it CAN be made reproducible: `-InstallTask` rebuilds it from
# this file, so a machine rebuild is a command rather than an act of memory.
function Get-BackupTaskDefinition {
    <#
      .SYNOPSIS
      The task's identity and command line, as data. Pure -- no registry, no
      platform calls -- so the CI runner can assert it.
      .DESCRIPTION
      HOURLY, not daily. The parent Backup-Documents runs at 00:00, which was
      hole H2: an edit at 02:11 sat on exactly one disk for 22 hours. Two files
      totalling ~87 KB, and an unchanged source writes no snapshot at all (L3),
      so the cost of an hourly cadence is a hash of 87 KB.
    #>
    param([Parameter(Mandatory)][string]$ScriptPath)
    @{
        TaskPath = '\Archangel\Backups\'
        TaskName = 'QFlix-Untracked-Backup'
        Execute  = 'powershell.exe'
        Argument = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $ScriptPath + '"'
        RepeatMinutes = 60
        Description = 'Backs up and SHA256-verifies the audit-scope S2 files (gitignored operator ' +
                      'tooling) into the Backup-Documents mirror plus a version history. Exit 1 = ' +
                      'divergent, exit 2 = cannot tell. Source: scripts/local-llm/backup-untracked.ps1'
    }
}

function Install-BackupTask {
    param([Parameter(Mandatory)][string]$ScriptPath)
    $d = Get-BackupTaskDefinition -ScriptPath $ScriptPath
    $action = New-ScheduledTaskAction -Execute $d.Execute -Argument $d.Argument
    $daily = New-ScheduledTaskTrigger -Daily -At '00:05'
    $daily.Repetition = (New-ScheduledTaskTrigger -Once -At '00:05' `
        -RepetitionInterval (New-TimeSpan -Minutes $d.RepeatMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    $logon = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId ("$env:USERDOMAIN\$env:USERNAME") -LogonType Interactive
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15)
    Register-ScheduledTask -TaskPath $d.TaskPath -TaskName $d.TaskName -Action $action `
        -Trigger @($daily, $logon) -Principal $principal -Settings $settings `
        -Description $d.Description -Force | Out-Null
    return $d
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
function Invoke-BackupUntracked {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RepoRoot,
        [Parameter(Mandatory)][string]$SourceRoot,
        [Parameter(Mandatory)][string]$MirrorRoot,
        [Parameter(Mandatory)][string]$HistoryRoot,
        [string]$ReceiptPath,
        [string]$LogPath,
        [string]$StatePath,
        [int]$KeepVersions = 30,
        [int]$MaxReceiptAgeHours = 48,
        [int]$AlertDedupHours = 24,
        [switch]$VerifyOnly,
        [switch]$NoAlert,
        [string]$WebhookFile
    )

    if (-not $ReceiptPath) { $ReceiptPath = Join-Path $HistoryRoot 'receipt.json' }
    if (-not $LogPath)     { $LogPath     = Join-Path $HistoryRoot 'backup-untracked.log' }
    if (-not $StatePath)   { $StatePath   = Join-Path $HistoryRoot 'alert-state.json' }
    if (-not $WebhookFile) { $WebhookFile = Join-Path $RepoRoot 'secrets/discord-webhook.url' }
    $sentinel = Join-Path $HistoryRoot 'FAILED'

    $problems = @()      # exit 1 -- the backup is wrong
    $blockers = @()      # exit 2 -- cannot tell
    $skips = @()
    $members = @()

    # --- policy surface ----------------------------------------------------
    $scopePath = Join-Path $RepoRoot 'manifest/audit-scope.yaml'
    $paths = @()
    try { $paths = @(Get-S2MemberPath -ScopePath $scopePath) }
    catch { $blockers += "scope-unreadable:$($_.Exception.Message)" }

    if ($blockers.Count -eq 0 -and $paths.Count -eq 0) {
        # The silent-subset failure. If the manifest's shape ever changes, this
        # script must stop, not back up nothing and report a clean run.
        $blockers += 'scope-parsed-zero-s2-members'
    }

    # --- the medium --------------------------------------------------------
    if ($blockers.Count -eq 0 -and -not (Test-Path -LiteralPath $MirrorRoot)) {
        $blockers += "mirror-root-absent:$MirrorRoot"
    }

    if ($blockers.Count -eq 0) {
        foreach ($rel in $paths) {
            $src = Join-Path $RepoRoot (ConvertTo-NativePath $rel)
            $rec = [ordered]@{
                path = $rel; source = $src; sha256 = $null; bytes = $null
                source_mtime_utc = $null; mirror_path = $null; mirror_sha256 = $null
                snapshot_path = $null; snapshot_reused = $null; snapshot_count = 0
                pruned = 0; status = 'unknown'
            }

            if (-not (Test-Path -LiteralPath $src -PathType Leaf)) {
                # The catastrophic case, and the one worth paging for: the
                # unversioned load-bearing file is GONE from its only tracked-by-
                # nothing home. Never treated as "nothing to back up".
                $rec.status = 'missing-source'
                $problems += "missing-source:$rel"
                $members += $rec
                continue
            }

            $item = Get-Item -LiteralPath $src
            $rec.sha256 = Get-FileSha256 $src
            $rec.bytes = $item.Length
            $rec.source_mtime_utc = $item.LastWriteTimeUtc.ToString('o')

            $under = Get-PathUnderRoot -Path $src -Root $SourceRoot
            if (-not $under) {
                $rec.status = 'outside-mirror-source-root'
                $blockers += "repo-outside-mirror-root:$rel"
                $members += $rec
                continue
            }
            $dst = Join-Path $MirrorRoot $under
            $rec.mirror_path = $dst

            if (-not $VerifyOnly) {
                try { [void](Copy-ToMirror -Source $src -Destination $dst) }
                catch { $skips += "mirror-copy-threw:$rel" }
            }
            $rec.mirror_sha256 = Get-FileSha256 $dst
            if ($rec.mirror_sha256 -ne $rec.sha256) {
                $rec.status = 'mirror-mismatch'
                $problems += "mirror-mismatch:$rel"
                $members += $rec
                continue
            }

            $histDir = Join-Path $HistoryRoot (ConvertTo-HistoryKey $rel)
            if (-not $VerifyOnly) {
                try {
                    $snap = Add-HistorySnapshot -Source $src -HistoryDir $histDir -Sha256 $rec.sha256
                    $rec.snapshot_path = $snap.path
                    $rec.snapshot_reused = $snap.reused
                    $rec.pruned = Remove-OldSnapshot -HistoryDir $histDir -Keep $KeepVersions -KeepSha $rec.sha256
                } catch { $skips += "snapshot-threw:$rel" }
            } else {
                $short = $rec.sha256.Substring(0, 12)
                $hit = @(Get-ChildItem -LiteralPath $histDir -Filter "*-$short.bak" -File -ErrorAction SilentlyContinue |
                         Sort-Object Name)
                if ($hit.Count -gt 0) { $rec.snapshot_path = $hit[-1].FullName; $rec.snapshot_reused = $true }
            }

            $rec.snapshot_count = @(Get-ChildItem -LiteralPath $histDir -Filter '*.bak' -File -ErrorAction SilentlyContinue).Count

            if (-not $rec.snapshot_path) {
                $rec.status = 'no-history-snapshot'
                $problems += "no-history-snapshot:$rel"
                $members += $rec
                continue
            }
            if ((Get-FileSha256 $rec.snapshot_path) -ne $rec.sha256) {
                $rec.status = 'snapshot-mismatch'
                $problems += "snapshot-mismatch:$rel"
                $members += $rec
                continue
            }

            $rec.status = 'ok'
            $members += $rec
        }
    }

    # --- receipt freshness (the -VerifyOnly dead-man, limit L1) -------------
    $receiptAgeH = $null
    if ($VerifyOnly -and $blockers.Count -eq 0) {
        if (-not (Test-Path -LiteralPath $ReceiptPath)) {
            $problems += 'receipt-absent'
        } else {
            try {
                $prev = (Get-Content -LiteralPath $ReceiptPath -Raw -Encoding UTF8) | ConvertFrom-Json
                $gen = [DateTime]::Parse($prev.generated_utc).ToUniversalTime()
                $receiptAgeH = [math]::Round(((Get-Date).ToUniversalTime() - $gen).TotalHours, 1)
                if ($receiptAgeH -gt $MaxReceiptAgeHours) {
                    $problems += "receipt-stale:${receiptAgeH}h>cap=${MaxReceiptAgeHours}h"
                }
            } catch { $problems += 'receipt-unparseable' }
        }
    }

    $status = 'ok'
    $exit = 0
    if ($problems.Count -gt 0) { $status = 'FAILED'; $exit = 1 }
    if ($blockers.Count -gt 0) { $status = 'BLOCKED'; $exit = 2 }

    $receipt = [ordered]@{
        schema = $Script:BackupSchema
        generated_utc = (Get-Date).ToUniversalTime().ToString('o')
        mode = if ($VerifyOnly) { 'verify' } else { 'backup' }
        status = $status
        repo_root = $RepoRoot
        source_root = $SourceRoot
        mirror_root = $MirrorRoot
        history_root = $HistoryRoot
        keep_versions = $KeepVersions
        receipt_age_hours = $receiptAgeH
        members = $members
        problems = $problems
        blockers = $blockers
        skips = $skips
    }

    # A verify run must NEVER overwrite the backup receipt -- that would reset
    # the very age it is checking, turning the dead-man into a self-licking
    # clock. It writes beside it instead.
    $outReceipt = if ($VerifyOnly) { [IO.Path]::ChangeExtension($ReceiptPath, '.verify.json') } else { $ReceiptPath }
    try {
        $dir = Split-Path -Parent $outReceipt
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        ($receipt | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $outReceipt -Encoding UTF8
    } catch { $skips += 'receipt-unwritable' }

    $summary = ('{0} mode={1} members={2} problems={3} blockers={4} skips={5}' -f
                $status, $receipt.mode, $members.Count, $problems.Count, $blockers.Count, $skips.Count)
    try {
        $dir = Split-Path -Parent $LogPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value (
            '{0}  {1}' -f (Get-Date).ToUniversalTime().ToString('o'), $summary)
    } catch { $skips += 'log-unwritable' }

    # --- loud ---------------------------------------------------------------
    if ($exit -ne 0) {
        try { Set-Content -LiteralPath $sentinel -Encoding UTF8 -Value $summary } catch { $skips += 'sentinel-unwritable' }
        if (-not $NoAlert) {
            $reason = if ($blockers.Count -gt 0) { $blockers[0] -replace ':.*$', '' } else { $problems[0] -replace ':.*$', '' }
            $detail = (($problems + $blockers) -join ', ')
            $msg = ":rotating_light: **QFlix untracked-file backup $status** - the gitignored operator tooling is NOT safely backed up.`n``$detail```nreceipt: $outReceipt"
            # A bug in the ALARM must not swallow the FINDING. Everything above
            # this line has already landed on disk (receipt, log, sentinel) and
            # $exit is already set, so the worst case degrades to a silent-but-
            # recorded failure instead of an exception that loses both.
            try {
                $skips += @(Send-BackupAlert -Reason $reason -Message $msg -WebhookFile $WebhookFile `
                                             -StatePath $StatePath -DedupHours $AlertDedupHours)
            } catch { $skips += "alerting-threw:$($_.Exception.Message -replace '[\r\n]+', ' ')" }
        } else { $skips += 'alerting-disabled-by-flag' }
    } else {
        if (Test-Path -LiteralPath $sentinel) { Remove-Item -LiteralPath $sentinel -Force }
    }

    return @{ exit = $exit; status = $status; receipt = $receipt; receipt_path = $outReceipt; summary = $summary }
}

# ---------------------------------------------------------------------------
if (-not $AsModule) {
    if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }
    if ($InstallTask) {
        $d = Install-BackupTask -ScriptPath (Join-Path $PSScriptRoot 'backup-untracked.ps1')
        Write-Host ("registered {0}{1} every {2} min + AtLogOn" -f $d.TaskPath, $d.TaskName, $d.RepeatMinutes)
        exit 0
    }
    $r = Invoke-BackupUntracked -RepoRoot $RepoRoot -SourceRoot $SourceRoot -MirrorRoot $MirrorRoot `
            -HistoryRoot $HistoryRoot -ReceiptPath $ReceiptPath -LogPath $LogPath -StatePath $StatePath `
            -KeepVersions $KeepVersions -MaxReceiptAgeHours $MaxReceiptAgeHours `
            -AlertDedupHours $AlertDedupHours -VerifyOnly:$VerifyOnly -NoAlert:$NoAlert -WebhookFile $WebhookFile
    Write-Host $r.summary
    foreach ($m in $r.receipt.members) {
        Write-Host ('  {0,-22} {1}  {2} bytes  sha={3}' -f $m.status, $m.path, $m.bytes, $m.sha256)
    }
    foreach ($p in $r.receipt.problems) { Write-Host "  PROBLEM $p" }
    foreach ($b in $r.receipt.blockers) { Write-Host "  BLOCKER $b" }
    foreach ($s in $r.receipt.skips)    { Write-Host "  skip    $s" }
    exit $r.exit
}

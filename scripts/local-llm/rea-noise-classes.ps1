# rea-noise-classes.ps1 - the REA noise policy, LOADED from git and MIRRORED back.
#
# WHY THIS FILE IS TRACKED AND qflix-rea.ps1 IS NOT
# -------------------------------------------------
# qflix-rea.ps1 is gitignored (.gitignore:55): operator-local workstation
# tooling, public repo. That made it invisible to CI - and it is the ALERTING
# LAYER. The fix is not to un-ignore a 114KB script; it is to move the POLICY
# into git and leave the plumbing outside. This loader is that seam.
#
# WHY THE LOADER ALSO WRITES (added 2026-08-19)
# ---------------------------------------------
# Loading was not enough, because nothing made the operator-local copies FOLLOW.
# The same defect shipped three times - 2026-07-29, 2026-08-06 and 2026-08-19 -
# every time by editing one policy surface and not the others. There were FOUR:
#
#   S-a  manifest/rea-noise-classes.yaml `classes`         (rx, prompt_clause)  git
#   S-b  manifest/rea-noise-classes.yaml `prompt_segments` (marker)             git
#   S-c  qflix-rea.ps1 $Script:NoiseFindingRules literal table               untracked
#   S-d  qflix-rea.ps1 Get-SystemPrompt "NEVER report" sentence              untracked
#
# On 2026-08-19 S-a/S-b carried 27 classes and S-c/S-d carried 25. Audit detector
# C-07 went red, report_digest became host-dependent (the ps1 exists only on the
# workstation), and - the part that actually costs money - both new classes were
# INERT: REA kept paging the operator on log lines the policy said to ignore.
#
# S-c and S-d are now DERIVED, never authored:
#
#   Get-ReaNoiseRules     hands qflix-rea.ps1 the LIVE table straight out of the
#                         yaml. A class added to the yaml alone is enforcing on
#                         the very next REA run, with no ps1 edit at all.
#   Sync-ReaNoiseMirror   rewrites the ps1's GENERATED text mirror of that table
#                         and appends any never-report clause the prompt is
#                         missing, so C-07's text-level cross-check (which reads
#                         the ps1 as TEXT, not by running it) goes clean without
#                         a human splicing anything by hand.
#
# Two surfaces remain, both in git, and C-07 bijects them offline on every run.
# The mirror is deliberately INERT - it lives inside a PowerShell block comment,
# so it can never diverge in BEHAVIOUR from the table REA actually uses; the
# worst it can do is be stale for one run, and the sync then fixes it.
#
# HONEST LIMIT: the sync repairs the ps1's rule table byte-for-byte, but it can
# only STUB the prompt prose (marker + prompt_clause). Prose that explains WHY a
# class is benign, and names the sibling shape that must still page, is written
# by a human. A stub keeps REA correct and C-07 green; it does not make the ask
# to the models as good as a hand-written clause. Improve the stub in place.
#
# No YAML module dependency: PowerShell 5.1 has no ConvertFrom-Yaml, and adding
# powershell-yaml would put a package manager in the alert path. The parser
# below handles exactly the shape manifest/rea-noise-classes.yaml uses and
# THROWS on anything else rather than returning a partial rule table - a
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

function ConvertFrom-ReaYamlScalar {
    <#
      .SYNOPSIS
      One flow scalar, in any of the three quoting styles this file uses.
      .DESCRIPTION
      Single-quoted (backslashes literal, '' escapes a quote) is how every rx is
      written. Double-quoted (backslash escapes) is how several markers are
      written, e.g. the plex-metadata-agent one, which embeds \" around a quoted
      log phrase. Bare is used for short values. Getting this wrong is not a
      cosmetic bug: a marker that un-escapes wrong will never be found in the
      prompt, and Sync-ReaNoiseMirror would then append a duplicate stub forever.
    #>
    param([string]$Raw)
    $t = $Raw.Trim()
    if ($t.Length -ge 2 -and $t.StartsWith("'") -and $t.EndsWith("'")) {
        return $t.Substring(1, $t.Length - 2).Replace("''", "'")
    }
    if ($t.Length -ge 2 -and $t.StartsWith('"') -and $t.EndsWith('"')) {
        $body = $t.Substring(1, $t.Length - 2)
        $sb = New-Object System.Text.StringBuilder
        $i = 0
        while ($i -lt $body.Length) {
            $ch = $body[$i]
            if ($ch -eq '\' -and ($i + 1) -lt $body.Length) {
                $n = $body[$i + 1]
                if     ($n -eq '"')  { [void]$sb.Append('"');  $i += 2 }
                elseif ($n -eq '\')  { [void]$sb.Append('\');  $i += 2 }
                elseif ($n -eq 'n')  { [void]$sb.Append("`n"); $i += 2 }
                elseif ($n -eq 't')  { [void]$sb.Append("`t"); $i += 2 }
                else                 { [void]$sb.Append($ch);  $i += 1 }
            } else {
                [void]$sb.Append($ch)
                $i += 1
            }
        }
        return $sb.ToString()
    }
    return $t
}

function Read-ReaPolicy {
    <#
      .SYNOPSIS
      The whole tracked policy: header markers, classes, prompt segments,
      deadman reasons. One pass, one parser, so the class table and the prompt
      table can never be read by two subtly different readers.
      .OUTPUTS
      Hashtable with keys: start_marker, stop_marker, source_script, classes,
      segments, deadman.
    #>
    param([string]$Path = (Get-ReaNoiseClassesPath))

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "rea-noise-classes.yaml not found at $Path - refusing to run with an empty noise table"
    }
    $lines = Get-Content -LiteralPath $Path -Encoding UTF8

    $policy = @{
        start_marker  = $null
        stop_marker   = $null
        source_script = $null
        classes       = @()
        segments      = @()
        deadman       = @()
    }
    $section = 'header'
    $cur     = $null
    $seg     = $null
    # StrictMode 2.0 throws on an uninitialized variable, and these are only
    # assigned when a list item is seen. Default to the file's own indentation.
    $keyPad  = '    '
    $segPad  = '    '

    foreach ($line in $lines) {
        # A top-level key ends whatever block we were in. Comment lines and
        # blank lines never do - the `why:` folded blocks are full of prose.
        if ($line -match '^([a-z_]+):\s*(.*)$') {
            $key = $Matches[1]
            $val = $Matches[2]
            if ($null -ne $cur) { $policy.classes  += $cur; $cur = $null }
            if ($null -ne $seg) { $policy.segments += $seg; $seg = $null }
            if     ($key -eq 'classes')          { $section = 'classes'; continue }
            elseif ($key -eq 'prompt_segments')  { $section = 'segments'; continue }
            elseif ($key -eq 'deadman_reasons')  { $section = 'deadman'; continue }
            else {
                $section = 'header'
                if ($key -eq 'prompt_start_marker') { $policy.start_marker  = ConvertFrom-ReaYamlScalar $val }
                if ($key -eq 'prompt_stop_marker')  { $policy.stop_marker   = ConvertFrom-ReaYamlScalar $val }
                if ($key -eq 'source_script')       { $policy.source_script = ConvertFrom-ReaYamlScalar $val }
                continue
            }
        }

        # KEY INDENT IS LOAD-BEARING, not tidiness. `\s+` here is a real bug: the
        # `why:` folded blocks are prose, and arr-release-rejected-unknown-title's
        # `why` contains the sentence-start "field: null this rx runs against
        # signature+summary+excerpt JOINED, so the" on its own line. A `^\s+field:`
        # match ate it and set field='null this rx runs against ...', which would
        # have made Test-IsNoiseFinding look up a property no finding has - the
        # rule would then have suppressed NOTHING, silently, forever. Caught
        # 2026-08-19 by C-07's byte comparison the first time this parser fed the
        # generated mirror. Sibling keys sit exactly two columns right of the "-",
        # folded prose sits further right, so pin the column.
        if ($section -eq 'classes') {
            if ($line -match '^(\s*)-\s+id:\s*(\S+)\s*$') {
                if ($null -ne $cur) { $policy.classes += $cur }
                $keyPad = ' ' * ($Matches[1].Length + 2)
                $cur = @{ id = $Matches[2]; rx = $null; field = $null; prompt_clause = $null }
                continue
            }
            if ($null -eq $cur) { continue }
            if ($line -match ('^' + $keyPad + 'rx:\s*(.+?)\s*$')) {
                $cur.rx = ConvertFrom-ReaSingleQuoted $Matches[1]
                continue
            }
            if ($line -match ('^' + $keyPad + 'field:\s*(.+?)\s*$')) {
                $v = $Matches[1].Trim()
                if ($v -ne 'null' -and $v -ne '~' -and $v -ne '') { $cur.field = $v }
                continue
            }
            if ($line -match ('^' + $keyPad + 'prompt_clause:\s*(.+?)\s*$')) {
                $cur.prompt_clause = ConvertFrom-ReaYamlScalar $Matches[1]
                continue
            }
            continue
        }

        if ($section -eq 'segments') {
            if ($line -match '^(\s*)-\s+index:\s*(\d+)\s*$') {
                if ($null -ne $seg) { $policy.segments += $seg }
                $segPad = ' ' * ($Matches[1].Length + 2)
                $seg = @{ index = [int]$Matches[2]; marker = $null; classes = @() }
                continue
            }
            if ($null -eq $seg) { continue }
            if ($line -match ('^' + $segPad + 'marker:\s*(.+?)\s*$')) {
                $seg.marker = ConvertFrom-ReaYamlScalar $Matches[1]
                continue
            }
            if ($line -match ('^' + $segPad + 'classes:\s*\[(.*)\]\s*$')) {
                $seg.classes = @($Matches[1].Split(',') |
                    ForEach-Object { $_.Trim() } |
                    Where-Object { $_ -ne '' })
                continue
            }
            continue
        }

        if ($section -eq 'deadman') {
            if ($line -match '^\s*-\s+(\S+)\s*$') { $policy.deadman += $Matches[1] }
            continue
        }
    }
    if ($null -ne $cur) { $policy.classes  += $cur }
    if ($null -ne $seg) { $policy.segments += $seg }

    if ($policy.classes.Count -eq 0) {
        throw "parsed 0 noise rules from $Path - refusing to run with an empty noise table"
    }
    foreach ($r in $policy.classes) {
        if (-not $r.rx) { throw "noise class '$($r.id)' has no rx in $Path" }
        # Fail at LOAD time, not at match time: a bad regex discovered mid-run
        # would suppress nothing and page the operator with a stack trace.
        try { [void][regex]::new($r.rx) }
        catch { throw "noise class '$($r.id)' has an uncompilable rx: $($_.Exception.Message)" }
    }
    return $policy
}

function Get-ReaNoiseRules {
    <#
      .SYNOPSIS
      The enforcement table, read from manifest/rea-noise-classes.yaml.
      .OUTPUTS
      An array of hashtables with keys id / rx / field, in file order - the
      same shape the inline table had, so Test-IsNoiseFinding is unchanged.
    #>
    param([string]$Path = (Get-ReaNoiseClassesPath))
    $policy = Read-ReaPolicy -Path $Path
    $out = @()
    foreach ($c in $policy.classes) {
        $out += @{ id = $c.id; rx = $c.rx; field = $c.field }
    }
    return $out
}

function Get-ReaPromptSegments {
    <#
      .SYNOPSIS
      The prompt's ';'-delimited never-report segments, with the classes each
      one claims. This is the half of the policy the ENFORCEMENT table cannot
      express: what the models are actually asked to ignore.
    #>
    param([string]$Path = (Get-ReaNoiseClassesPath))
    (Read-ReaPolicy -Path $Path).segments
}

function Get-ReaDeadmanReasons {
    param([string]$Path = (Get-ReaNoiseClassesPath))
    $reasons = (Read-ReaPolicy -Path $Path).deadman
    if ($reasons.Count -eq 0) { throw "parsed 0 deadman reasons from $Path" }
    return $reasons
}

function Format-ReaNoiseRulesLiteral {
    <#
      .SYNOPSIS
      Render the rule table as the exact PowerShell literal the audit's C-07
      parser reads out of qflix-rea.ps1.
      .DESCRIPTION
      The shape is load-bearing, not cosmetic. lib/audit/detectors/
      c07_rea_prompt_rule_bijection.py::parse_ps1_rules finds the block by the
      literal string "$Script:NoiseFindingRules = @(", ends it at the first
      "\n)\n", splits entries on "@{ id = ", and reads `rx` / `field` per entry.
      Emit LF here; Sync-ReaNoiseMirror converts to the target file's endings.
    #>
    param([object[]]$Rules)
    $sb = New-Object System.Text.StringBuilder
    [void]$sb.Append("`$Script:NoiseFindingRules = @(`n")
    foreach ($r in $Rules) {
        [void]$sb.Append("    @{ id = '" + $r.id.Replace("'", "''") + "'`n")
        if ($r.field) {
            [void]$sb.Append("       field = '" + $r.field.Replace("'", "''") + "'`n")
        }
        [void]$sb.Append("       rx = '" + $r.rx.Replace("'", "''") + "' }`n")
    }
    [void]$sb.Append(")`n")
    return $sb.ToString()
}

function Get-ReaMissingPromptSegments {
    <#
      .SYNOPSIS
      Which yaml prompt segments the given ps1 text does NOT already say.
      .DESCRIPTION
      A segment counts as missing if its marker is absent from the never-report
      sentence, OR if any class it claims has a prompt_clause that is absent.
      Both are C-07 drift conditions ("segment N marker missing from prompt",
      "prompt_clause for <id> is not literal prompt text") and both are repaired
      the same way: say the thing.
    #>
    param(
        [string]$Ps1Text,
        [hashtable]$Policy
    )
    $i = $Ps1Text.IndexOf($Policy.start_marker)
    if ($i -lt 0) { throw "never-report sentence start marker not found in the ps1" }
    $j = $Ps1Text.IndexOf($Policy.stop_marker, $i + 1)
    if ($j -lt 0) { throw "never-report sentence stop marker not found in the ps1" }
    $prompt = $Ps1Text.Substring($i, $j - $i)

    $byId = @{}
    foreach ($c in $Policy.classes) { $byId[$c.id] = $c }

    $missing = @()
    foreach ($s in $Policy.segments) {
        $gap = $false
        if (-not $s.marker) { continue }
        if ($prompt.IndexOf($s.marker) -lt 0) { $gap = $true }
        foreach ($cid in $s.classes) {
            if (-not $byId.ContainsKey($cid)) { continue }
            $clause = $byId[$cid].prompt_clause
            if ($clause -and $prompt.IndexOf($clause) -lt 0) { $gap = $true }
        }
        if ($gap) { $missing += $s }
    }
    return $missing
}

function Format-ReaPromptStub {
    <#
      .SYNOPSIS
      One ';'-delimited clause for a segment the prompt does not yet carry.
      .DESCRIPTION
      Marker first (C-07 requires every ';'-chunk to contain a declared marker),
      then any prompt_clause the marker does not already contain. The result may
      contain NO ';' of its own - the detector splits on it - so this throws
      rather than writing a clause that would split into an unclaimed chunk.
    #>
    param(
        [hashtable]$Segment,
        [hashtable]$Policy
    )
    $byId = @{}
    foreach ($c in $Policy.classes) { $byId[$c.id] = $c }
    $parts = @($Segment.marker)
    foreach ($cid in $Segment.classes) {
        if (-not $byId.ContainsKey($cid)) { continue }
        $clause = $byId[$cid].prompt_clause
        if ($clause -and $Segment.marker.IndexOf($clause) -lt 0 -and ($parts -notcontains $clause)) {
            $parts += $clause
        }
    }
    $stub = ($parts -join ' - ')
    if ($stub.Contains(';')) {
        throw "prompt stub for segment $($Segment.index) contains ';', which would split into an unclaimed chunk: $stub"
    }
    return $stub
}

function Sync-ReaNoiseMirror {
    <#
      .SYNOPSIS
      Make the operator-local qflix-rea.ps1 agree with the tracked policy,
      without a human editing it.
      .DESCRIPTION
      Two repairs, both idempotent and both write-only-on-change:
        1. The GENERATED NOISE-TABLE MIRROR block is re-rendered from the yaml.
           It sits inside a PowerShell block comment: it is TEXT for the audit's
           C-07 cross-check and is never executed, so it cannot drift in
           behaviour from Get-ReaNoiseRules, only in bytes - for at most one run.
        2. Any yaml prompt segment the never-report sentence does not carry is
           appended to it as a stub clause, immediately before the stop marker.
      .PARAMETER WhatIfOnly
      Compute and report, write nothing.
      .OUTPUTS
      Hashtable: changed, table_synced, clauses_added, reason.
    #>
    param(
        # Defaults off THIS file's location, never off the caller's. A function's
        # $PSScriptRoot is the directory of the script that DEFINED it, so this
        # resolves to the real qflix-rea.ps1 even when the caller was dot-sourced
        # by a test harness - where $PSCommandPath would have pointed at the
        # harness and aimed a rewrite at the wrong 100KB file.
        [string]$Ps1Path = (Join-Path $PSScriptRoot 'qflix-rea.ps1'),
        [string]$Path = (Get-ReaNoiseClassesPath),
        [switch]$WhatIfOnly
    )
    $result = @{ changed = $false; table_synced = $false; clauses_added = @(); reason = 'ok' }
    if (-not (Test-Path -LiteralPath $Ps1Path)) {
        $result.reason = 'ps1-absent'
        return $result
    }
    $policy = Read-ReaPolicy -Path $Path
    $orig   = [System.IO.File]::ReadAllText($Ps1Path)
    $text   = $orig
    $crlf   = $text.Contains("`r`n")

    $beginMark = '# BEGIN GENERATED NOISE-TABLE MIRROR'
    $endMark   = '# END GENERATED NOISE-TABLE MIRROR'
    $b = $text.IndexOf($beginMark)
    $e = $text.IndexOf($endMark)
    if ($b -lt 0 -or $e -lt $b) {
        # Refuse rather than guess where the table lives. A sync that writes to
        # the wrong offset in a 114KB operator-local script is unrecoverable
        # from origin - the file is gitignored.
        $result.reason = 'no-mirror-markers'
        return $result
    }

    # ---- repair 1: the generated table mirror -----------------------------
    $literal = Format-ReaNoiseRulesLiteral -Rules $policy.classes
    $block = $beginMark + " (regenerated by rea-noise-classes.ps1::Sync-ReaNoiseMirror - DO NOT EDIT)`n" +
             "<#`n" + $literal + "#>`n"
    $tail = $text.Substring($e)
    $head = $text.Substring(0, $b)
    $wanted = $head + $block + $tail
    if ($crlf) { $wanted = $wanted.Replace("`r`n", "`n").Replace("`n", "`r`n") }
    if ($wanted -ne $text) { $text = $wanted; $result.table_synced = $true }

    # ---- repair 2: never-report clauses the prompt does not carry ---------
    # @() is load-bearing: PowerShell unrolls a zero- or one-element array on
    # return, so an unwrapped $missing is $null when nothing is missing and
    # $missing.Count then throws under StrictMode 2.0 - inside the try/catch in
    # Invoke-Main that would have silently disabled repair 2 forever.
    $missing = @(Get-ReaMissingPromptSegments -Ps1Text $text -Policy $policy)
    if ($missing.Count -gt 0) {
        $i = $text.IndexOf($policy.start_marker)
        $j = $text.IndexOf($policy.stop_marker, $i + 1)
        $before = $text.Substring(0, $j).TrimEnd()
        # The sentence ends "...still report it). CONVERSELY," - drop that final
        # period so the appended clauses join the SAME sentence rather than
        # starting an orphan one the ';' split would mis-chunk.
        if ($before.EndsWith('.')) { $before = $before.Substring(0, $before.Length - 1) }
        $add = ''
        foreach ($s in $missing) {
            $stub = Format-ReaPromptStub -Segment $s -Policy $policy
            $add += '; ' + $stub
            $result.clauses_added += $s.index
        }
        $text = $before + $add + '. ' + $text.Substring($j)
    }

    if ($text -eq $orig) { return $result }

    # ---- sanity gate before touching an unrecoverable file ---------------
    if ($text.Length -lt ($orig.Length * 0.5)) {
        throw "Sync-ReaNoiseMirror refused: rewrite would shrink $Ps1Path from $($orig.Length) to $($text.Length) bytes"
    }
    foreach ($needle in @($beginMark, $endMark, $policy.start_marker, $policy.stop_marker)) {
        if ($text.IndexOf($needle) -lt 0) {
            throw "Sync-ReaNoiseMirror refused: rewrite lost the marker '$needle'"
        }
    }
    $result.changed = $true
    if ($WhatIfOnly) { return $result }

    # Preserve the file's encoding. qflix-rea.ps1 is BOM-less UTF-8 and a BOM
    # would be a silent, invisible diff on a file nothing in git can restore.
    $bytes = [System.IO.File]::ReadAllBytes($Ps1Path)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    $enc = New-Object System.Text.UTF8Encoding($hasBom)
    $tmp = "$Ps1Path.sync.tmp"
    [System.IO.File]::WriteAllText($tmp, $text, $enc)
    Move-Item -LiteralPath $tmp -Destination $Ps1Path -Force
    return $result
}

#!/usr/bin/env pwsh
# test-rea-noise-classes.ps1 — guards the POLICY seam, in CI, with no ps1 present.
#
# tests/local-llm/test-qflix-rea.ps1 cannot run on a hosted runner: its subject
# (scripts/local-llm/qflix-rea.ps1) is an audit-scope S2 member and is not in
# git. This file tests the half that IS in git — the tracked loader and the
# tracked policy — so the pwsh CI job proves something real on every push
# instead of only on the operator's workstation.
#
# What it does NOT prove: that qflix-rea.ps1 actually calls the loader. That is
# residual R4, and defect class C-07's cross-check layer covers it wherever the
# file exists.

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Script:Pass = 0
$Script:Fail = 0
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

# $env:TEMP is a WINDOWS variable; under pwsh on ubuntu-latest it is undefined,
# so Join-Path bound null and THREW in the two acceptance tests for the
# single-source noise policy - which therefore never actually executed in CI
# (council finding, arbiter-verified 2026-08-26). GetTempPath() works on both.
$tmpRoot    = if ($env:TEMP) { $env:TEMP } else { [IO.Path]::GetTempPath() }
$repoRoot   = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$loaderPath = Join-Path $repoRoot 'scripts/local-llm/rea-noise-classes.ps1'
$yamlPath   = Join-Path $repoRoot 'manifest/rea-noise-classes.yaml'

if (-not (Test-Path $loaderPath)) {
    Write-Host "ERROR: tracked loader missing at $loaderPath"
    exit 1
}
. $loaderPath

Test-Case 'policy file is present and tracked' {
    Assert-True (Test-Path $yamlPath) 'manifest/rea-noise-classes.yaml exists'
}

Test-Case 'loader returns the full rule table' {
    $rules = Get-ReaNoiseRules -Path $yamlPath
    Assert-True ($rules.Count -ge 10) "at least 10 noise rules parsed (got $($rules.Count))"
    $ids = @($rules | ForEach-Object { $_.id })
    foreach ($want in @(
        'plex-client-abort-stream-write', 'plex-nat-pmp-upnp',
        'tdarr-express-undefined-includes', 'tdarr-worker-not-a-function',
        'tdarr-wasm-oom', 'mediainfo-failure', 'plex-post-reap-scan',
        'external-indexer-5xx-html', 'indexer-severity-field-echo',
        'bare-stack-continuation')) {
        Assert-True ($ids -contains $want) "rule '$want' present"
    }
}

Test-Case 'every rx compiles as a .NET regex' {
    foreach ($r in (Get-ReaNoiseRules -Path $yamlPath)) {
        $ok = $true
        try { [void][regex]::new($r.rx) } catch { $ok = $false }
        Assert-True $ok "rx for '$($r.id)' compiles"
    }
}

Test-Case 'rx escaping survived the YAML round-trip' {
    $rules = Get-ReaNoiseRules -Path $yamlPath
    $tdarr = $rules | Where-Object { $_.id -eq 'tdarr-express-undefined-includes' }
    # YAML doubles the inner quotes; the loader must undouble them or this rule
    # never matches the real Tdarr log line. The parens stay REGEX-ESCAPED
    # (\( ... \)), so assert on the quoted token, not on a literal paren run.
    Assert-True ($tdarr.rx -like "*'includes'*")  "single quotes un-doubled"
    Assert-True (-not ($tdarr.rx -like "*''*"))   "no doubled quotes left over"
    Assert-True ($tdarr.rx -like '*\(reading*')   "regex escaping preserved"
    $sev = $rules | Where-Object { $_.id -eq 'indexer-severity-field-echo' }
    Assert-True ($sev.rx -like '*"severity"*') 'double quotes preserved'
}

Test-Case 'the field-scoped rule keeps its field' {
    $rules = Get-ReaNoiseRules -Path $yamlPath
    $bare  = $rules | Where-Object { $_.id -eq 'bare-stack-continuation' }
    Assert-Equal 'excerpt' $bare.field "bare-stack-continuation is scoped to the excerpt field"
    $plex = $rules | Where-Object { $_.id -eq 'plex-nat-pmp-upnp' }
    Assert-Equal $null $plex.field 'unscoped rules carry a null field'
}

Test-Case 'rules actually suppress the log lines they were written for' {
    $rules = @{}
    foreach ($r in (Get-ReaNoiseRules -Path $yamlPath)) { $rules[$r.id] = $r.rx }
    $cases = @(
        @{ id = 'tdarr-express-undefined-includes'
           hay = "TypeError: Cannot read properties of undefined (reading 'includes')" },
        @{ id = 'tdarr-wasm-oom'
           hay = 'WebAssembly.instantiate(): Out of memory: wasm memory' },
        @{ id = 'plex-post-reap-scan'
           hay = 'Failed to create parent iterator for /data/TV/Fargo' },
        @{ id = 'indexer-severity-field-echo'
           hay = '{"severity": "error", "message": "no results"}' },
        @{ id = 'plex-client-abort-stream-write'
           hay = 'Caught exception trying to stream file: write: protocol is shutdown (SSL routines)' },
        @{ id = 'tdarr-handbrake-binary-test'
           hay = '[2026-08-16T01:00:05.302] [ERROR] Tdarr_Node - Binary test 1: handbrakePath not working' },
        @{ id = 'seerr-plex-scan-tvdbid-collision'
           hay = 'SQLITE_CONSTRAINT: UNIQUE constraint failed: media.tvdbId' },
        @{ id = 'buildarr-unsupported-plex-notification'
           hay = "buildarr_radarr.config.settings.notifications [WARNING] <radarr> (main) Unsupported remote notification connection 'Plex Media Server' with implementation 'PlexServer', ignoring" },
        # The four 2026-08-18 classes, quoted verbatim from the 2026-08-17 run
        # that paged on them.
        @{ id = 'plex-network-service-shutdown'
           hay = 'Aug 17, 2026 11:01:30.579 [140110926211896] ERROR - Network Service: Error in advertiser handle read: 125 (Operation canceled) socket=-1' },
        @{ id = 'bazarr-signalr-reconnect'
           hay = '2026-08-17 13:07:56,782 - root (7f72ea5fc700) :  ERROR (signalr_client:159) - BAZARR SignalR client for Sonarr connection as been lost. Trying to reconnect...' },
        @{ id = 'arr-indexer-unavailable-backoff'
           hay = '<error code="429" description="Indexer is disabled till 08/18/2026 02:27:55 due to recent failures." />' },
        @{ id = 'external-indexer-5xx-html'
           hay = 'error code: 522' }
    )
    foreach ($c in $cases) {
        Assert-True ($c.hay -match $rules[$c.id]) "'$($c.id)' matches its canonical log line"
    }
    # ...and a genuine fault is NOT suppressed by any of them. This is the
    # direction that matters: an over-broad rule silences real pages.
    $real = 'Unknown system error -122: Disk quota exceeded while writing /data/Movies'
    foreach ($id in $rules.Keys) {
        if ($id -eq 'bare-stack-continuation') { continue }  # excerpt-scoped, tested above
        Assert-True (-not ($real -match $rules[$id])) "'$id' does NOT suppress a disk-quota fault"
    }
    # Near-miss guards for the 2026-08-16 classes: the sibling shapes that ARE
    # real faults must not match.
    Assert-True (-not ('Binary test 2: ffmpegPath not working' -match $rules['tdarr-handbrake-binary-test'])) "'tdarr-handbrake-binary-test' does NOT suppress an ffmpegPath failure"
    Assert-True (-not ('SQLITE_CONSTRAINT: UNIQUE constraint failed: user.email' -match $rules['seerr-plex-scan-tvdbid-collision'])) "'seerr-plex-scan-tvdbid-collision' does NOT suppress another column"
    # ...and the BUNDLED shapes (benign line + real sibling in one excerpt)
    # must not match either - the sibling negative lookahead is the guard.
    $bundledTdarr = "Binary test 1: handbrakePath not working`nBinary test 2: ffmpegPath not working"
    Assert-True (-not ($bundledTdarr -match $rules['tdarr-handbrake-binary-test'])) "'tdarr-handbrake-binary-test' does NOT suppress a bundled ffmpegPath failure"
    $bundledSeerr = "UNIQUE constraint failed: media.tvdbId`nUNIQUE constraint failed: user.email"
    Assert-True (-not ($bundledSeerr -match $rules['seerr-plex-scan-tvdbid-collision'])) "'seerr-plex-scan-tvdbid-collision' does NOT suppress a bundled different-column failure"
    Assert-True (-not ("Unsupported remote notification connection 'Discord' with implementation 'DiscordWebhook', ignoring" -match $rules['buildarr-unsupported-plex-notification'])) "'buildarr-unsupported-plex-notification' does NOT suppress another implementation"

    # ---- near-miss guards for the 2026-08-18 classes ---------------------
    # Each of these is the SIBLING SHAPE that is a real fault. If a rule ever
    # widens far enough to eat one of them, it has stopped being narrow.

    # An advertiser error that is NOT a cancellation is a bind/connect failure,
    # not a teardown. This is the whole reason the rule requires the cancel
    # token rather than just the advertiser marker.
    Assert-True (-not ('ERROR - Network Service: Error in advertiser handle read: 98 (Address already in use) socket=7' -match $rules['plex-network-service-shutdown'])) "'plex-network-service-shutdown' does NOT suppress an EADDRINUSE advertiser failure"
    # A model's own prose summary must not be able to complete the match on its
    # own - that is what field='excerpt' is for, and this asserts the rule text
    # would not match a summary-only haystack either.
    Assert-True (-not ('Network service advertiser handle read failure' -match $rules['plex-network-service-shutdown'])) "'plex-network-service-shutdown' does NOT fire on a model summary alone"

    # Bazarr's typo is the anchor. A model that paraphrases it into correct
    # English is writing prose, not quoting the log, and must not suppress.
    Assert-True (-not ('BAZARR SignalR client for Radarr connection has been lost' -match $rules['bazarr-signalr-reconnect'])) "'bazarr-signalr-reconnect' does NOT fire on the paraphrased 'has been lost'"

    # A bare rate-limit with no Prowlarr backoff description could be one of
    # OUR services throttling, which is reportable.
    Assert-True (-not ('HTTP Error - Res: HTTP/1.1 [GET] http://127.0.0.1:17024/prowlarr/api: 429.TooManyRequests' -match $rules['arr-indexer-unavailable-backoff'])) "'arr-indexer-unavailable-backoff' does NOT suppress a bare 429 with no backoff description"

    # The Cloudflare bare-body alternative is LINE-ANCHORED. Our own logs put a
    # timestamp or level token first, so a genuine 5xx from one of our services
    # can never present as a bare body line.
    Assert-True (-not ('2026-08-18 04:00:00 ERROR qflix-dash returned error code: 522 to the client' -match $rules['external-indexer-5xx-html'])) "'external-indexer-5xx-html' does NOT suppress our own service quoting an error code mid-line"
}

Test-Case 'deadman reasons load' {
    $reasons = Get-ReaDeadmanReasons -Path $yamlPath
    Assert-Equal 5 $reasons.Count 'five deadman reasons'
    Assert-True ($reasons -contains 'all_models_noop') 'all_models_noop present'
}

Test-Case 'loader refuses to run with a missing policy file' {
    # C-09 discipline applied to the loader itself: a missing prerequisite must
    # THROW, never return an empty rule table (which would silently disable
    # every suppression and page the operator at 2am with known noise).
    $threw = $false
    try { Get-ReaNoiseRules -Path (Join-Path $repoRoot 'manifest/does-not-exist.yaml') }
    catch { $threw = $true }
    Assert-True $threw 'throws instead of returning an empty table'
}

Test-Case 'the prompt half of the policy parses too' {
    # Added 2026-08-19. The loader used to read only id/rx/field, so the prompt
    # segments - the half that says what the MODELS are asked to ignore - were
    # readable by pytest and by nothing on this side. Sync-ReaNoiseMirror needs
    # them to repair the never-report sentence without a human.
    $policy = Read-ReaPolicy -Path $yamlPath
    Assert-True ($policy.classes.Count -ge 27)  "at least 27 classes (got $($policy.classes.Count))"
    Assert-True ($policy.segments.Count -ge 23) "at least 23 prompt segments (got $($policy.segments.Count))"
    Assert-True ($policy.start_marker -eq 'NON-ACTIONABLE NOISE you must NEVER report') 'start marker parsed'
    Assert-True ($policy.stop_marker  -eq 'CONVERSELY') 'stop marker parsed'
    $ids = @($policy.classes | ForEach-Object { $_.id })
    foreach ($s in $policy.segments) {
        Assert-True ([string]::IsNullOrEmpty($s.marker) -eq $false) "segment $($s.index) has a marker"
        Assert-True (-not $s.marker.Contains(';')) "segment $($s.index) marker carries no ';'"
        foreach ($cid in $s.classes) {
            Assert-True ($ids -contains $cid) "segment $($s.index) claims a real class '$cid'"
        }
    }
    foreach ($c in $policy.classes) {
        Assert-True ([string]::IsNullOrEmpty($c.prompt_clause) -eq $false) "class '$($c.id)' has a prompt_clause"
    }
}

Test-Case 'why-prose cannot masquerade as a class key' {
    # THE 2026-08-19 PARSER BUG, pinned. arr-release-rejected-unknown-title's
    # `why:` block contains the prose line "field: null this rx runs against
    # signature+summary+excerpt JOINED, so the". A `^\s+field:` match ate it and
    # set field='null this rx runs against ...' - Test-IsNoiseFinding would then
    # look up a property no finding has, and the rule would suppress NOTHING,
    # silently, forever. Sibling keys sit at a fixed column; folded prose does
    # not. Found the first time the parser fed C-07's byte-level comparison.
    $policy = Read-ReaPolicy -Path $yamlPath
    $rej = $policy.classes | Where-Object { $_.id -eq 'arr-release-rejected-unknown-title' }
    Assert-Equal $null $rej.field 'arr-release-rejected-unknown-title field stays null'
    $scoped = $policy.classes | Where-Object { $_.id -eq 'bare-stack-continuation' }
    Assert-Equal 'excerpt' $scoped.field 'a genuinely scoped class still parses its field'
    foreach ($c in $policy.classes) {
        $ok = ($null -eq $c.field) -or ($c.field -in @('excerpt', 'signature', 'summary'))
        Assert-True $ok "class '$($c.id)' field is a real finding field (got '$($c.field)')"
    }
}

Test-Case 'double-quoted YAML scalars round-trip' {
    # Several markers are double-quoted because they embed a quoted log phrase,
    # e.g. the plex-metadata-agent one. A wrong un-escape makes the marker
    # unfindable in the prompt, and Sync-ReaNoiseMirror would then append the
    # same stub on every single run.
    Assert-Equal 'a "quoted" phrase' (ConvertFrom-ReaYamlScalar '"a \"quoted\" phrase"') 'escaped double quotes'
    Assert-Equal "it's fine" (ConvertFrom-ReaYamlScalar "'it''s fine'") 'doubled single quotes'
    Assert-Equal 'bare value' (ConvertFrom-ReaYamlScalar 'bare value') 'bare scalar'
    $policy = Read-ReaPolicy -Path $yamlPath
    $seg9 = $policy.segments | Where-Object { $_.index -eq 9 }
    Assert-True ($seg9.marker.Contains('"Unable to find metadata agent provider for identifier"')) 'segment 9 marker un-escaped'
}

Test-Case 'the rendered literal is the shape C-07 parses' {
    # lib/audit/detectors/c07_rea_prompt_rule_bijection.py::parse_ps1_rules finds
    # the block by the literal "$Script:NoiseFindingRules = @(" and ends it at the
    # first "\n)\n". Get either wrong and the audit reads ZERO rules and reports
    # drift against a table that is in fact correct.
    $policy  = Read-ReaPolicy -Path $yamlPath
    $literal = Format-ReaNoiseRulesLiteral -Rules $policy.classes
    Assert-True ($literal.StartsWith('$Script:NoiseFindingRules = @(' + "`n")) 'opens with the exact variable assignment'
    Assert-True ($literal.EndsWith("`n)`n")) 'closes with the exact terminator'
    $entries = ([regex]::Matches($literal, '@\{ id = ')).Count
    Assert-Equal $policy.classes.Count $entries 'one entry per class'
    Assert-True ($literal.Contains("''includes''")) 'inner single quotes are re-doubled for PowerShell'
    Assert-True ($literal.Contains("       field = 'excerpt'")) 'scoped classes carry their field'
    # The terminator must not appear early, or parse_ps1_rules truncates.
    Assert-Equal ($literal.Length - 3) $literal.IndexOf("`n)`n") 'no premature block terminator'
}

Test-Case 'the mirror sync refuses to guess' {
    # The subject is a 114KB gitignored operator-local script: a write to the
    # wrong offset is unrecoverable from origin. Both no-op paths must return a
    # NAMED reason rather than throwing or, worse, writing.
    $absent = Sync-ReaNoiseMirror -Ps1Path (Join-Path $tmpRoot 'rea-does-not-exist.ps1') -Path $yamlPath
    Assert-Equal 'ps1-absent' $absent.reason 'a missing subject is named, not fatal'
    Assert-Equal $false $absent.changed 'a missing subject changes nothing'

    $tmp = Join-Path $tmpRoot ('rea-nomarkers-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        [System.IO.File]::WriteAllText($tmp, "# no mirror markers here`n")
        $r = Sync-ReaNoiseMirror -Ps1Path $tmp -Path $yamlPath
        Assert-Equal 'no-mirror-markers' $r.reason 'a subject without the markers is named, not guessed at'
        Assert-Equal $false $r.changed 'a subject without the markers is left alone'
        Assert-Equal "# no mirror markers here`n" ([System.IO.File]::ReadAllText($tmp)) 'the file is byte-identical afterwards'
    } finally { if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force } }
}

Test-Case 'a yaml-only class reaches both surfaces with no ps1 edit' {
    # THE ACCEPTANCE TEST for the 2026-08-19 change. Adding a class to
    # manifest/rea-noise-classes.yaml and to NOTHING ELSE must leave the ps1
    # agreeing with it - rule table and never-report sentence both - after one
    # sync. Synthetic subject so this runs in CI where qflix-rea.ps1 is absent.
    $policy = Read-ReaPolicy -Path $yamlPath
    $tmp = Join-Path $tmpRoot ('rea-synth-' + [guid]::NewGuid().ToString('N') + '.ps1')
    try {
        $body = "# synthetic subject`n" +
                '# BEGIN GENERATED NOISE-TABLE MIRROR' + "`n" +
                '# END GENERATED NOISE-TABLE MIRROR' + "`n" +
                $policy.start_marker + ' (external, cosmetic, or expected): nothing yet. ' +
                $policy.stop_marker + " disk-quota failures ARE real.`n"
        [System.IO.File]::WriteAllText($tmp, $body)

        $first = Sync-ReaNoiseMirror -Ps1Path $tmp -Path $yamlPath
        Assert-Equal $true $first.changed 'the first sync repairs the subject'
        Assert-Equal $true $first.table_synced 'the rule table is written'
        Assert-Equal $policy.segments.Count $first.clauses_added.Count 'every missing clause is appended'

        $after = [System.IO.File]::ReadAllText($tmp)
        $entries = ([regex]::Matches($after, '@\{ id = ')).Count
        Assert-Equal $policy.classes.Count $entries 'the mirror carries every class'
        Assert-True ($after.Contains('$Script:NoiseFindingRules = @(')) 'C-07 can find the table'
        $missing = @(Get-ReaMissingPromptSegments -Ps1Text $after -Policy $policy)
        Assert-Equal 0 $missing.Count 'no prompt segment is left unsaid'

        # Idempotence is what makes this safe to call on every REA run: a second
        # sync must be a pure no-op, or the script would rewrite itself hourly.
        $second = Sync-ReaNoiseMirror -Ps1Path $tmp -Path $yamlPath
        Assert-Equal $false $second.changed 'the second sync changes nothing'
        Assert-Equal $after ([System.IO.File]::ReadAllText($tmp)) 'the file is byte-identical after a second sync'
    } finally { if (Test-Path $tmp) { Remove-Item -LiteralPath $tmp -Force } }
}

Write-Host ""
Write-Host "PASS: $Script:Pass   FAIL: $Script:Fail"
if ($Script:Fail -gt 0) {
    foreach ($f in $Script:Failures) { Write-Host "  - $f" }
    exit 1
}
exit 0

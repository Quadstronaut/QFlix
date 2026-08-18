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

Write-Host ""
Write-Host "PASS: $Script:Pass   FAIL: $Script:Fail"
if ($Script:Fail -gt 0) {
    foreach ($f in $Script:Failures) { Write-Host "  - $f" }
    exit 1
}
exit 0

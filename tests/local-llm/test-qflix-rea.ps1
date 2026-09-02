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

# --- Task 5: tunnel + ollama gates ---
Test-Case 'Wait-ForTunnel returns false fast when port not listening' {
    $start = Get-Date
    $result = Wait-ForTunnel -Port 1 -MaxSec 3 -PollSec 1
    $elapsed = ((Get-Date) - $start).TotalSeconds
    Assert-False $result 'returned false'
    Assert-True ($elapsed -lt 5) 'gave up within timeout'
}

Test-Case 'Wait-ForTunnel returns true when port opens during wait' {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = $listener.LocalEndpoint.Port
    try {
        $result = Wait-ForTunnel -Port $port -MaxSec 5 -PollSec 1
        Assert-True $result 'returned true'
    } finally { $listener.Stop() }
}

Test-Case 'Test-OllamaHealth returns false when unreachable' {
    $prev = $Script:OllamaBase
    $Script:OllamaBase = 'http://127.0.0.1:1'
    try {
        Assert-False (Test-OllamaHealth) 'unreachable returns false'
    } finally { $Script:OllamaBase = $prev }
}

# --- Task 6: remote heredoc ---
Test-Case 'Get-RemoteHeredoc returns bash with all 7 section markers' {
    $h = Get-RemoteHeredoc
    foreach ($k in @('arr_logs','journal_errors','cron_mail','maint_state','nginx_errors','plex_errors','kuma_red')) {
        Assert-True ($h -match $k) "section $k present"
    }
    Assert-False ($h -match "`r") 'no CRLF in heredoc'
}

Test-Case 'Get-RemoteHeredoc references seedbox-correct paths' {
    $h = Get-RemoteHeredoc
    Assert-True ($h -match 'for app in sonarr sonarr2') '*arr iteration list'
    Assert-True ($h -match '~/\.apps/\$app/logs/\*\.txt') '*arr log glob'
    Assert-True ($h -match 'journalctl --user -p err') 'journalctl invocation'
    Assert-True ($h -match '/var/spool/mail/quadstronaut') 'cron mail spool'
    Assert-True ($h -match 'uptimekuma/kuma\.db') 'kuma sqlite path'
}

Test-Case 'Get-RemoteHeredoc templates the section cap' {
    $h = Get-RemoteHeredoc
    Assert-True ($h -match "SECTION_CAP=$($Script:SectionByteCap)") 'cap value substituted'
}

# --- Task 7: JSON extractor + blob decoder ---
Test-Case 'Extract-JsonArray parses bare empty array' {
    $txt = Get-Content -Raw "$PSScriptRoot/fixtures/model-clean.txt"
    $arr = Extract-JsonArray $txt
    Assert-True ($arr -is [array]) 'parsed (is array)'
    Assert-Equal 0 @($arr).Count 'empty array'
}

Test-Case 'Extract-JsonArray finds array inside prose with fences' {
    $txt = Get-Content -Raw "$PSScriptRoot/fixtures/model-noisy.txt"
    $arr = Extract-JsonArray $txt
    Assert-True ($arr -is [array]) 'parsed (is array)'
    Assert-Equal 1 @($arr).Count 'one finding'
    Assert-Equal 'heartbeat:xdg-runtime-unset' (@($arr)[0].signature) 'signature extracted'
}

Test-Case 'Extract-JsonArray parses dirty single-line array' {
    $txt = Get-Content -Raw "$PSScriptRoot/fixtures/model-dirty.txt"
    $arr = Extract-JsonArray $txt
    Assert-True ($arr -is [array]) 'parsed (is array)'
    Assert-Equal 1 @($arr).Count 'one finding'
    Assert-Equal 'buildarr' (@($arr)[0].app) 'app extracted'
}

Test-Case 'Extract-JsonArray returns null on garbage' {
    $arr = Extract-JsonArray 'I am sorry, I cannot help with that.'
    Assert-True ($null -eq $arr) 'no array returns null'
}

Test-Case 'Invoke-Model pins an explicit context window and output budget' {
    # 2026-08-18: no num_ctx meant the server-default window, and a storm-fat
    # blob silently truncated the PROMPT from the top - amputating the JSON
    # format instructions - so all 3 models answered prose and 22 consecutive
    # hourly runs graded all_models_noop. The exact option values are pinned
    # because "some options" is not the guard; the WINDOW is.
    $src = Get-Content -Raw $ScriptPath
    Assert-True ($src -match 'num_ctx\s*=\s*24576') 'num_ctx pinned at 24576'
    Assert-True ($src -match 'num_predict\s*=\s*3072') 'num_predict raised to 3072'
    # 420s: at 24576 ctx the larger models prefill slower; 240s graded storm
    # runs models=1/3 with two timeouts (measured 2026-08-18).
    Assert-True ($src -match '\$Script:ModelTimeoutSec = 420') 'model timeout holds the big models on a fat blob'
}

Test-Case 'system prompt caps the findings count' {
    $p = Get-SystemPrompt
    Assert-True ($p.Contains('AT MOST 10 findings')) 'findings cap present'
    Assert-True ($p.Contains('Return ONLY a JSON array')) 'JSON-only instruction retained'
}

Test-Case 'Extract-JsonArray salvages a num_predict-truncated array' {
    # The storm shape: complete findings, then the token budget cuts the last
    # element mid-string. The complete leading elements must survive.
    $trunc = '[{"signature":"a","severity":"error"},{"signature":"b","severity":"warning"},{"signature":"c","sev'
    $r = Extract-JsonArray $trunc
    Assert-True ($null -ne $r) 'truncated array parsed'
    Assert-Equal 2 (@($r).Count) 'both complete elements recovered, half element dropped'
    Assert-Equal 'b' (@($r)[1].signature) 'second element intact'
}

Test-Case 'Extract-JsonArray salvage does not invent structure from garbage' {
    Assert-True ($null -eq (Extract-JsonArray '[this is not json at all')) 'unclosed prose stays null'
    Assert-True ($null -eq (Extract-JsonArray 'no array here')) 'no bracket stays null'
}

Test-Case 'Extract-JsonArray skips a prose bracket prefix and finds the real array' {
    # COUNCIL 2026-08-18 (reviewer-reproduced live): IndexOf('[') anchored on
    # the FIRST bracket, so a "[INFO]"-style prose prefix made the real
    # findings unreachable. Every '[' is a candidate now.
    $t = '[INFO] scanning logs... result: [{"signature":"a","severity":"error"}]'
    $r = Extract-JsonArray $t
    Assert-True ($null -ne $r) 'parsed despite prose bracket prefix'
    Assert-Equal 1 (@($r).Count) 'one finding'
    Assert-Equal 'a' (@($r)[0].signature) 'the real array won, not [INFO]'
}

Test-Case 'Extract-JsonArray prefers the object array over a decorative empty one' {
    $t = '[] and here are the findings: [{"signature":"b","severity":"warning"}]'
    $r = Extract-JsonArray $t
    Assert-Equal 1 (@($r).Count) 'object array preferred over leading []'
    Assert-Equal 'b' (@($r)[0].signature) 'correct element'
}

Test-Case 'Extract-JsonArray still returns a bare [] for a clean run' {
    $r = Extract-JsonArray 'all clean: []'
    Assert-True ($null -ne $r) 'parsed'
    Assert-Equal 0 (@($r).Count) 'empty means clean, not null'
}

Test-Case 'Extract-JsonArray salvage cut is quote-aware' {
    # The old salvage cut at LastIndexOf('}'), which lands INSIDE the string
    # value here and produced an unparseable candidate - whole batch lost.
    $t = '[{"signature":"a","severity":"error"},{"signature":"b","excerpt":"brace } inside","severity":"warn'
    $r = Extract-JsonArray $t
    Assert-True ($null -ne $r) 'salvaged'
    Assert-Equal 1 (@($r).Count) 'only the complete element survives'
    Assert-Equal 'a' (@($r)[0].signature) 'complete element intact'
    Assert-True $Script:LastExtractSalvaged 'salvage is flagged for the audit log'
}

Test-Case 'Extract-JsonArray clean parse resets the salvage flag' {
    $null = Extract-JsonArray '[{"signature":"a","severity":"error"},{"signature":"b","sev'
    Assert-True $Script:LastExtractSalvaged 'salvage sets the flag'
    $null = Extract-JsonArray '[{"signature":"c"}]'
    Assert-False $Script:LastExtractSalvaged 'a clean parse clears it'
}

Test-Case 'findings cap is enforced in code, not just asked for in the prompt' {
    # COUNCIL 2026-08-18: the 10-finding cap was prompt text only. Pin the
    # call-site enforcement + its audit line.
    $src = Get-Content -Raw $scriptPath
    Assert-True ($src -match '\@\(\$arr\)\.Count -gt 10') 'overflow check present'
    Assert-True ($src.Contains('Select-Object -First 10')) 'cap enforced'
    Assert-True ($src.Contains('overflow model=')) 'overflow is audit-logged'
    Assert-True ($src.Contains('salvaged model=')) 'salvage is audit-logged'
}

Test-Case 'Extract-JsonArray handles nested brackets and strings with brackets' {
    $arr = Extract-JsonArray 'preamble [{"x":"a [b] c","y":[1,2,3]}] postamble'
    Assert-True ($arr -is [array]) 'parsed (is array)'
    Assert-Equal 1 @($arr).Count 'one element'
}

Test-Case 'ConvertFrom-FetchedBlob base64-decodes sources' {
    $a = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes('hello world'))
    $b = [Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes("multi`nline"))
    $json = @"
{"fetched_at":"2026-05-11T00:00:00Z","host":"h","sources":{"arr_logs":"$a","journal_errors":"$b","cron_mail":"","maint_state":"","nginx_errors":"","plex_errors":"","kuma_red":""}}
"@
    $r = ConvertFrom-FetchedBlob -Json $json
    Assert-Equal 'hello world' $r.sources.arr_logs 'arr_logs decoded'
    Assert-Equal "multi`nline" $r.sources.journal_errors 'journal_errors decoded'
    Assert-Equal '' $r.sources.cron_mail 'empty section stays empty'
}

# --- Task 8: consensus grouping ---
Test-Case 'Get-Consensus groups by signature, max severity wins' {
    $findings = @(
        @{ time='2026-05-11T04:30:11Z'; app='buildarr'; file='journal:buildarr.service'; severity='warning'; summary='short'; excerpt='excerpt-A'; signature='buildarr:pydantic'; _model='qwen3:8b' },
        @{ time='2026-05-11T04:30:11Z'; app='buildarr'; file='journal:buildarr.service'; severity='error';   summary='a longer summary about the same issue'; excerpt='excerpt-B (longer)'; signature='buildarr:pydantic'; _model='qwen3-coder:30b' },
        @{ time='2026-05-11T05:00:00Z'; app='nginx';    file='nginx/error.log'; severity='error'; summary='502'; excerpt='upstream'; signature='nginx:502'; _model='qwen3-coder:30b' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 2 $groups.Count 'two groups'
    $bg = $groups | Where-Object { $_.signature -eq 'buildarr:pydantic' } | Select-Object -First 1
    Assert-Equal 'error' $bg.severity 'severity escalated to error'
    Assert-Equal 2 @($bg.models_flagged).Count 'two models flagged'
    Assert-Equal 'a longer summary about the same issue' $bg.summary 'longest summary kept'
    Assert-Equal 'excerpt-B (longer)' $bg.excerpt 'longest excerpt kept'
}

Test-Case 'Get-Consensus normalizes signature case and whitespace' {
    $findings = @(
        @{ time='t'; app='a'; file='f'; severity='error'; summary='s'; excerpt='e'; signature='Foo:Bar';   _model='m1' },
        @{ time='t'; app='a'; file='f'; severity='error'; summary='s'; excerpt='e'; signature=' foo:bar '; _model='m2' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 1 $groups.Count 'collapsed to one group'
}

Test-Case 'Get-Consensus drops findings with empty signature' {
    $findings = @(
        @{ time='t'; app='a'; file='f'; severity='error'; summary='s'; excerpt='e'; signature=''; _model='m1' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 0 $groups.Count 'no groups for empty signature'
}

Test-Case 'Get-Consensus sorts severity desc then time asc' {
    $findings = @(
        @{ time='2026-05-11T10:00:00Z'; app='a'; file='f'; severity='warning'; summary='s'; excerpt='e'; signature='w1'; _model='m1' },
        @{ time='2026-05-11T09:00:00Z'; app='a'; file='f'; severity='error';   summary='s'; excerpt='e'; signature='e1'; _model='m1' },
        @{ time='2026-05-11T08:00:00Z'; app='a'; file='f'; severity='error';   summary='s'; excerpt='e'; signature='e2'; _model='m1' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 'e2' $groups[0].signature 'earliest error first'
    Assert-Equal 'e1' $groups[1].signature 'later error second'
    Assert-Equal 'w1' $groups[2].signature 'warning last'
}

Test-Case 'Get-Consensus merges invented-signature variants of ONE excerpt (2026-08-25 4-for-1)' {
    # The 2026-08-25 12:08 alert: ONE sonarr log line paged as FOUR fields
    # because each model minted its own signature for it. Two of the four also
    # trimmed the timestamp off the excerpt, so the merge must survive both
    # exact-duplicate keys and contained keys.
    $line  = '2026-08-25 15:18:06.6|Error|DiscordProxy|Unable to post payload NzbDrone.Core.Notifications.Discord.Payloads.DiscordPayload'
    $short = 'Unable to post payload NzbDrone.Core.Notifications.Discord.Payloads.DiscordPayload'
    $findings = @(
        @{ time='t1'; app='sonarr'; file='f'; severity='error'; summary='DiscordProxy unable to post payload'; excerpt=$line;  signature='sonarr:discord-proxy-post-failure';   _model='qwen2.5-coder:7b' },
        @{ time='t1'; app='sonarr'; file='f'; severity='error'; summary='Discord notification failure';        excerpt=$short; signature='sonarr:discord-notification-failure'; _model='qwen3:8b' },
        @{ time='t1'; app='sonarr'; file='f'; severity='error'; summary='Discord payload posting failed';      excerpt=$line;  signature='sonarr:discord-proxy-failure';       _model='qwen3-coder:30b' },
        @{ time='t1'; app='sonarr'; file='f'; severity='error'; summary='Discord notification failure';        excerpt=$short; signature='sonarr:discord-notification-failure-2'; _model='qwen3:8b' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 1 $groups.Count 'four signatures, one line, ONE group'
    Assert-Equal 3 @($groups[0].models_flagged).Count 'all three models credited'
    Assert-Equal $line $groups[0].excerpt 'longest excerpt kept'
}

Test-Case 'Get-Consensus excerpt merge does NOT collapse short or distinct excerpts' {
    # <40-char normalized keys never merge (two real findings can share a short
    # phrase), and two long-but-different lines stay separate.
    $findings = @(
        @{ time='t'; app='a'; file='f'; severity='error'; summary='s'; excerpt='connection refused'; signature='a:one'; _model='m1' },
        @{ time='t'; app='b'; file='f'; severity='error'; summary='s'; excerpt='connection refused'; signature='b:two'; _model='m1' },
        @{ time='t'; app='c'; file='f'; severity='error'; summary='s'; excerpt='2026-08-22 03:38:11.9|Error|ServerSideNotificationService|Failed to retrieve notifications'; signature='c:three'; _model='m1' },
        @{ time='t'; app='d'; file='f'; severity='error'; summary='s'; excerpt='2026-08-25 14:49:03 ERROR root BAZARR unable to sync subtitles: /path/x.en.srt'; signature='d:four'; _model='m1' }
    )
    $groups = @(Get-Consensus -Findings $findings)
    Assert-Equal 4 $groups.Count 'no false merges'
}

# --- Task 9: Discord payload builders ---
Test-Case 'New-DiscordErrorPayload mentions operator + builds embed fields' {
    $groups = @(
        [pscustomobject]@{ signature='buildarr:pydantic'; time='2026-05-11T04:30:11Z'; app='buildarr'; file='journal:buildarr.service'; severity='error'; summary='Pydantic err'; excerpt='trace...'; models_flagged=@('qwen3-coder:30b','qwen3:8b') }
    )
    $p = New-DiscordErrorPayload -Groups $groups -OperatorId '123' -ModelCount 3 -DurationSec 47
    Assert-Equal '<@123>' $p.content 'operator mention'
    Assert-Equal @('123') $p.allowed_mentions.users 'allowed_mentions users'
    Assert-Equal 1 @($p.embeds).Count 'one embed'
    Assert-Equal 15158332 $p.embeds[0].color 'red color'
    Assert-True ($p.embeds[0].title -match 'QFlix REA') 'title shape'
    Assert-Equal 1 @($p.embeds[0].fields).Count 'one field per group'
    Assert-True ($p.embeds[0].fields[0].value -match 'qwen3-coder:30b') 'model name in field value'
    Assert-True ($p.embeds[0].fields[0].value -match '2/3') 'consensus fraction'
}

Test-Case 'New-DiscordErrorPayload clamps long excerpt to 300 chars' {
    $longExcerpt = 'x' * 500
    $groups = @(
        [pscustomobject]@{ signature='s'; time='t'; app='a'; file='f'; severity='error'; summary='s'; excerpt=$longExcerpt; models_flagged=@('m') }
    )
    $p = New-DiscordErrorPayload -Groups $groups -OperatorId '1' -ModelCount 1 -DurationSec 1
    Assert-True ($p.embeds[0].fields[0].value.Length -le 1024) 'field value clamped to <=1024'
}

Test-Case 'New-DiscordHeartbeatPayload has no content mention' {
    $p = New-DiscordHeartbeatPayload -ModelCount 3
    Assert-Equal '' $p.content 'no mention'
    Assert-Equal 3066993 $p.embeds[0].color 'green'
    Assert-True ($p.embeds[0].title -match 'clean') 'title says clean'
    Assert-False ($p.embeds[0].description -match 'held') 'no held line when SoloCount=0'
}

Test-Case 'heartbeat surfaces the consensus-floor remainder without paging (2026-08-26)' {
    # Operator directive: single-model findings never ping. They are counted
    # on the heartbeat instead — visible in daily review, zero pages. Every
    # hallucinated page in the 2026-08 storm was flagged by exactly one model,
    # so this floor alone would have silenced all of them.
    $p = New-DiscordHeartbeatPayload -ModelCount 3 -SoloCount 2
    Assert-Equal '' $p.content 'still no mention — a held finding must not ping'
    Assert-True ($p.embeds[0].description -match '2 single-model finding') 'held count on the heartbeat'
    Assert-True ($p.embeds[0].description -match 'audit log') 'points at the audit log'
}

Test-Case 'New-DiscordDeadmanPayload pings operator and uses orange' {
    $p = New-DiscordDeadmanPayload -OperatorId '123'
    Assert-Equal '<@123>' $p.content 'operator mention'
    Assert-Equal 16753920 $p.embeds[0].color 'orange'
    Assert-True ($p.embeds[0].title -match 'Ollama') 'mentions Ollama'
}

# --- Task 10: Ollama + Discord I/O ---
Test-Case 'Invoke-Model returns null on unreachable endpoint' {
    $prev = $Script:OllamaBase
    $Script:OllamaBase = 'http://127.0.0.1:1'
    try {
        $r = Invoke-Model -Model 'doesnt-matter' -Prompt 'x' -SystemPrompt 'y' -TimeoutSec 3
        Assert-True ($null -eq $r) 'returns null on connect failure'
    } finally { $Script:OllamaBase = $prev }
}

Test-Case 'Send-Discord returns false on bad URL' {
    $r = Send-Discord -WebhookUrl 'http://127.0.0.1:1/fake' -Payload @{ content='x' }
    Assert-False $r 'failed POST returns false'
}

# --- Task 11: prompt helpers ---
Test-Case 'Get-FieldOrEmpty returns empty for missing field on pscustomobject' {
    $obj = [pscustomobject]@{ a = 'x' }
    Assert-Equal 'x' (Get-FieldOrEmpty $obj 'a') 'present field'
    Assert-Equal '' (Get-FieldOrEmpty $obj 'b') 'missing field returns empty'
    Assert-Equal '' (Get-FieldOrEmpty $null 'a') 'null object returns empty'
}

Test-Case 'Get-FieldOrEmpty handles hashtables too' {
    $h = @{ a = 1 }
    Assert-Equal '1' (Get-FieldOrEmpty $h 'a') 'hashtable present'
    Assert-Equal '' (Get-FieldOrEmpty $h 'b') 'hashtable missing'
}

Test-Case 'Get-RepoRoot resolves to actual repo root with secrets/' {
    $r = Get-RepoRoot
    Assert-True (Test-Path (Join-Path $r 'secrets')) 'secrets/ exists at returned root'
    Assert-True (Test-Path (Join-Path $r 'scripts/local-llm/qflix-rea.ps1')) 'script path consistent'
}

Test-Case 'Build-UserPrompt embeds the blob' {
    $up = Build-UserPrompt -BlobJson '{"x":1}'
    Assert-True ($up -match '"x":1') 'blob embedded'
    Assert-True ($up -match 'JSON array of findings') 'instruction present'
}

Test-Case 'Get-SystemPrompt mentions Manitoba and JSON schema' {
    $sp = Get-SystemPrompt
    Assert-True ($sp -match 'Manitoba') 'mentions Manitoba'
    Assert-True ($sp -match 'signature') 'mentions signature key'
    Assert-True ($sp -match 'severity') 'mentions severity key'
}

Test-Case 'Read-Secret returns null when file absent' {
    $tmp = Join-Path $env:TEMP "qflix-rea-secrets-$(Get-Random)"
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    try {
        $r = Read-Secret -RepoRoot $tmp -Name 'nonexistent.url'
        Assert-True ($null -eq $r) 'null for missing file'
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

Test-Case 'Read-Secret returns trimmed contents when present' {
    $tmp = Join-Path $env:TEMP "qflix-rea-secrets-$(Get-Random)"
    New-Item -ItemType Directory -Path (Join-Path $tmp 'secrets') -Force | Out-Null
    try {
        Set-Content -LiteralPath (Join-Path $tmp 'secrets/test.id') -Value "  abc123  `n" -Encoding UTF8 -NoNewline
        $r = Read-Secret -RepoRoot $tmp -Name 'test.id'
        Assert-Equal 'abc123' $r 'value trimmed'
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
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

# --- 2026-07-28: deterministic noise suppression + dead-source pattern fixes ---
# Regression cover for the REA alert of 2026-07-28, which paged the operator with
# two findings that were BOTH known-benign noise, one of which the system prompt
# already forbade. Prompt text is advisory; these tests pin the enforcement.

Test-Case 'Test-IsNoiseFinding suppresses the Plex client-abort stream write' {
    # Verbatim shape of the 2026-07-28 false positive: the tell-tale phrasing sits
    # in the excerpt, while the signature only says "ssl-protocol-shutdown".
    $f = @{
        signature = 'plex:ssl-protocol-shutdown'
        summary   = 'Plex encountered SSL protocol shutdown while streaming media files'
        excerpt   = 'Caught exception trying to stream file: /config/Transcode/Sessions/plex-transcode-liuzjq8/init-stream1.m4s: write: protocol is shutdown (SSL routines) [asio.ssl:167772367]'
    }
    Assert-Equal 'plex-client-abort-stream-write' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses on signature alone when excerpt is empty' {
    $f = @{ signature = 'plex:ssl-protocol-shutdown'; summary = ''; excerpt = '' }
    Assert-Equal 'plex-client-abort-stream-write' (Test-IsNoiseFinding $f) 'signature-only match'
}

Test-Case 'Test-IsNoiseFinding suppresses the tdarr undefined-includes TypeError' {
    # The prompt already banned this class and qwen3-coder:30b reported it anyway.
    $f = @{
        signature = 'tdarr:undefined-includes-error'
        summary   = 'Tdarr server experiencing unhandled undefined property errors during API requests'
        excerpt   = "TypeError: Cannot read properties of undefined (reading 'includes') at /home28/quadstronaut/.apps/tdarr/Tdarr_Server/srcug/api/servers.js:1:3117"
    }
    Assert-Equal 'tdarr-express-undefined-includes' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses tdarr worker-not-a-function' {
    $f = @{ signature = 'tdarr:worker-fn'; summary = 'worker2 handler is not a function'; excerpt = '' }
    Assert-Equal 'tdarr-worker-not-a-function' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses Plex NAT-PMP/UPnP chatter' {
    $f = @{ signature = 'plex:natpmp'; summary = 'NAT-PMP port mapping not supported by gateway'; excerpt = '' }
    Assert-Equal 'plex-nat-pmp-upnp' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding does NOT suppress the real faults these rules sit next to' {
    # The whole risk of a suppression layer is over-reach. These are the actionable
    # errors found in the SAME log files as the suppressed classes on 2026-07-28 -
    # if a rule ever starts eating one of them, this test goes red first.
    # NOTE: the WASM/MediaInfo OOM pairing that used to live in this list was
    # itself ruled permanently unfixable + canary-tracked on 2026-07-28 (see the
    # 2026-07-29 suppression tests below) - it now belongs on the "gets
    # suppressed" side, not here.
    $quota = @{
        signature = 'tdarr:log-write-edquot'
        summary   = 'log4js cannot write server log: disk quota exceeded'
        excerpt   = "log4js.fileAppender - Writing to file Tdarr_Server_Log.txt, error happened [Error: Unknown system error -122, write]"
    }
    Assert-Equal $null (Test-IsNoiseFinding $quota) 'EDQUOT write failure survives'

    $plexReal = @{
        signature = 'plex:db-corrupt'
        summary   = 'Plex database is corrupt and the server will not start'
        excerpt   = 'ERROR - Database corruption detected, sqlite3: database disk image is malformed'
    }
    Assert-Equal $null (Test-IsNoiseFinding $plexReal) 'genuine Plex fault survives'
}

Test-Case 'Test-IsNoiseFinding returns null for an empty finding' {
    Assert-Equal $null (Test-IsNoiseFinding @{ signature=''; summary=''; excerpt='' }) 'empty -> null'
    Assert-Equal $null (Test-IsNoiseFinding $null) 'null -> null'
}

# --- 2026-07-29: WASM/MediaInfo + post-reap noise rules (REA 02:51 false page) ---
# The REA auditor paged on 2026-07-29 with 4 findings, all verified noise. Two of
# the three root causes are code-level suppression gaps closed here (the third,
# the prompt/enforcement de-conflict + line-level staleness, is covered below).

Test-Case 'Test-IsNoiseFinding suppresses the tdarr WASM/MediaInfo out-of-memory (ruled unfixable 2026-07-28)' {
    $f = @{
        signature = 'tdarr:wasm-oom'
        summary   = 'Tdarr MediaInfo probe failing: WebAssembly out of memory'
        excerpt   = '[ERROR] Tdarr_Server - [RangeError: WebAssembly.instantiate(): Out of memory: wasm memory]'
    }
    Assert-Equal 'tdarr-wasm-oom' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses the wasm-memory-exhausted signature shape' {
    $f = @{ signature = 'tdarr:wasm-memory-exhausted'; summary = ''; excerpt = '' }
    Assert-Equal 'tdarr-wasm-oom' (Test-IsNoiseFinding $f) 'signature-only match'
}

Test-Case 'Test-IsNoiseFinding suppresses "Error running MediaInfo"' {
    $f = @{ signature = 'tdarr:mediainfo'; summary = 'Error running MediaInfo on file'; excerpt = '' }
    Assert-Equal 'mediainfo-failure' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses Plex post-reap "Failed to create parent iterator" chatter' {
    # Fargo was reaped 2026-07-22 (arrId=225); this is Plex noticing the series
    # directory it was about to scan is gone, not a fault.
    $f = @{ signature = 'plex:scan-error'; summary = 'Plex library scan reported an error'; excerpt = 'Failed to create parent iterator for /data/media/tv/Fargo' }
    Assert-Equal 'plex-post-reap-scan' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses the bazarr ratelimit finding AS MODELS EMIT IT (no URL)' {
    # The 2026-08-13 page, verbatim shape: norm() had rewritten the URL to <url>
    # upstream and the model truncated its excerpt at "rate limit exceeded", so
    # the old URL-anchored rx could never match any field. The rule must catch
    # the updater marker + rate-limit token WITHOUT seeing the releases URL.
    $f = @{ signature = 'bazarr:github-rate-limit'
            summary   = 'GitHub API rate limit exceeded for Bazarr release check'
            excerpt   = 'Error trying to get releases from Github. Http error. 403 Client Error: rate limit exceeded' }
    Assert-Equal 'bazarr-github-release-check-ratelimit' (Test-IsNoiseFinding $f) 'truncated shape suppressed'
}

Test-Case 'Test-IsNoiseFinding still suppresses the full-URL ratelimit shape (vlogs path)' {
    $f = @{ signature = ''
            summary   = 'Bazarr update check rate limited'
            excerpt   = 'requests.exceptions.HTTPError: 403 Client Error: rate limit exceeded for url: https://api.github.com/repos/morpheus65535/Bazarr/releases?per_page=100' }
    Assert-Equal 'bazarr-github-release-check-ratelimit' (Test-IsNoiseFinding $f) 'URL shape still suppressed'
}

Test-Case 'a 403 against any OTHER GitHub endpoint still pages' {
    # Guarantee carried over from the original URL anchor: QFlix's own
    # api.github.com callers must never be silenced by the Bazarr class.
    $f = @{ signature = 'lifecycle:github-403'
            summary   = 'release resolver hit GitHub rate limit'
            excerpt   = '403 rate limit exceeded for url: https://api.github.com/repos/Sonarr/Sonarr/releases' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'no updater marker => not suppressed'
}

Test-Case 'a Bazarr update-check ConnectionError still pages' {
    # Deliberately uncovered (yaml note): only the rate-limit shape is provably
    # external; a persistent connection failure is a network fault worth seeing.
    $f = @{ signature = 'bazarr:github-release-check'
            summary   = 'Bazarr could not reach GitHub for release check'
            excerpt   = 'Error trying to get releases from Github. Connection Error.' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'no rate-limit token => not suppressed'
}

Test-Case 'a speculative rate-limit SUMMARY cannot suppress a ConnectionError excerpt' {
    # The v1 (2026-08-13) reshape ran on the combined hay, so a model summary
    # guessing "possible rate limiting" completed the token pair for a
    # ConnectionError whose excerpt had no rate-limit token at all - defeating
    # the ConnectionError-still-pages guarantee. field='excerpt' closes it.
    $f = @{ signature = 'bazarr:github-unreachable'
            summary   = 'Bazarr updater cannot reach GitHub, possible rate limiting or outage'
            excerpt   = 'Error trying to get releases from Github. Connection Error. requests.exceptions.ConnectionError: Max retries exceeded with url: <url>' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'prose-poisoned summary => not suppressed'
}

Test-Case 'the 220-char arr_logs cut of the updater line IS suppressed (Http error marker)' {
    # cut -c1-220 amputates "rate limit exceeded" (byte ~464 post-norm) from
    # Bazarr's one-line traceback, so the only token pair an arr_logs excerpt
    # can carry is the updater marker + "Http error." - check_update.py logs
    # that sentence for HTTPError ONLY (Connection/Timeout have their own).
    $f = @{ signature = 'bazarr:github-release-check'
            summary   = 'Bazarr release check failed'
            excerpt   = '[/home/quadstronaut/.apps/bazarr/log/bazarr.log]       1 2026-08-14 00:50:17|ERROR   |root  |Error trying to get releases from Github. Http error.|<Traceback (most recent call last):' }
    Assert-Equal 'bazarr-github-release-check-ratelimit' (Test-IsNoiseFinding $f) 'cut-220 arr_logs shape suppressed'
}

Test-Case 'an ARRAY-shaped excerpt keeps its line boundaries (no cross-element borrow)' {
    # Models sometimes emit excerpt as a JSON array of lines. A [string] cast
    # space-joins them into ONE line, silently defeating the (?m)^-no-(?s)
    # one-line constraint (proven 2026-08-14). Get-FieldOrEmpty must join
    # arrays with a newline so a benign updater element cannot borrow a
    # rate-limit token from a REAL 429 element.
    $f = @{ signature = 'bazarr:providers'
            summary   = 'Bazarr provider errors'
            excerpt   = @('Error trying to get releases from Github. Connection Error.',
                          'opensubtitles.com: 429 rate limit reached, all providers throttled') }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'array elements stay separate lines => not suppressed'
}

Test-Case 'a benign updater line cannot borrow a rate-limit token from ANOTHER excerpt line' {
    # (?m)^ with no (?s): both tokens must share one line. A real provider
    # throttle (opensubtitles 429) next to a benign updater line must page.
    $f = @{ signature = 'bazarr:providers'
            summary   = 'Bazarr provider errors'
            excerpt   = "Error trying to get releases from Github. Connection Error.`nopensubtitles.com: 429 rate limit reached, all providers throttled" }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'cross-line token join => not suppressed'
}

Test-Case 'marker in summary plus a FOREIGN rate-limit excerpt is not suppressed' {
    # Cross-field join: v1 let a marker-phrased summary pair with an unrelated
    # indexer rate-limit in the excerpt. field='excerpt' means the summary is
    # never consulted, and the excerpt alone carries no updater marker.
    $f = @{ signature = 'prowlarr:rate-limit'
            summary   = 'Bazarr: repeated failures trying to get releases from Github'
            excerpt   = 'Rate limit exceeded for indexer prowlarr-nzbgeek' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'cross-field join => not suppressed'
}

Test-Case 'every noise rule has an id and a compilable regex' {
    Assert-True ($Script:NoiseFindingRules.Count -ge 4) 'at least the four known classes'
    foreach ($r in $Script:NoiseFindingRules) {
        Assert-True ([bool]$r.id) "rule has an id"
        # Throws if the pattern is malformed - a broken rule must fail loudly here,
        # not silently match nothing in production.
        [void][regex]::new($r.rx)
    }
}

Test-Case 'plex_errors source matches the real PMS log level format' {
    # PMS logs "<ts> [<thread-id>] ERROR - <msg>": the brackets hold the THREAD ID,
    # so the old grep '\[ERROR\]' matched zero lines and the source was dead.
    $h = Get-RemoteHeredoc
    Assert-True  ($h -match 'grep -a " ERROR - "') 'greps the dash-delimited level'
    Assert-False ($h -match 'grep -h "\\\[ERROR\\\]" "\$f"') 'no bracketed-level grep on PMS logs'
}

Test-Case 'plex_errors source drops the benign high-volume classes' {
    # Measured live 2026-07-28: 1204 fresh ERROR lines -> 22 after this filter, so
    # real Plex faults now fit inside SECTION_CAP instead of being crowded out.
    $h = Get-RemoteHeredoc
    Assert-True ($h -match 'Caught exception trying to stream file\.\*protocol is shutdown') 'client-abort writes filtered'
    Assert-True ($h -match 'CreditsDetectionManager') 'credits-detection chatter filtered'
    Assert-True ($h -match 'Unknown metadata type: folder') 'folder-type chatter filtered'
}

Test-Case 'tdarr source strips stack-trace continuations and reads the timestamped logs' {
    $h = Get-RemoteHeredoc
    # The continuation strip now runs AFTER tailnew's per-line [path] prefix,
    # so it anchors on "] " plus the line's own original indentation. Two
    # whitespace chars minimum (tailnew's own separator + at least one of the
    # line's) so a real line beginning "at " cannot be eaten.
    Assert-True ($h.Contains('grep -vE "^\[[^]]+\][[:space:]][[:space:]]+at "')) 'express stack continuations stripped'
    Assert-True ($h -match 'Tdarr_Server_Log\.txt')            'timestamped server log included'
    Assert-True ($h -match 'Tdarr_Node_Log\.txt')              'timestamped node log included'
    Assert-True ($h -match 'uniq -c')                          'repeated faults collapsed, not truncated away'
}

# --- 2026-08-25: the undated-source structural fix ---------------------------
# The page that forced this: "tdarr:permission-denied", EACCES mkdir on
# '/tdarr-workDir-node-baY4PcyP1-worker-gloomy-goa-ts-1779348004454'. That epoch
# is 2026-05-21T07:20:04Z - THREE MONTHS old - and it shipped because node.err
# carries ZERO dated lines (0/694 measured), so every line filter is a provable
# no-op on it and the only surviving gate was file mtime, which was 1.4 days
# old over a tail window spanning 94 days. No noise class can fix that; the
# collector has to stop shipping the bytes.
Test-Case 'undated .err streams ship on the WATERMARK basis, not the mtime gate' {
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('tailnew() {'))      'tailnew helper defined'
    Assert-True ($h.Contains('export -f tailnew')) 'tailnew exported to bash -c children'
    Assert-True ($h.Contains('export REA_OFFSETS')) 'offsets path exported to bash -c children'
    # The three ship-nothing paths, each pinned: first sight, no growth, and
    # the mtime floor. Dropping any one of them re-opens the defect.
    Assert-True ($h.Contains('[ -n "$prev" ] || continue'))          'first sight ships nothing'
    Assert-True ($h.Contains('[ "$size" -gt "$prev" ] || continue')) 'no appended bytes ships nothing'
    Assert-True ($h -match 'find "\$f" -mtime -"\$FRESH_DAYS" -print -quit 2>/dev/null\)" \] \|\| continue')  'mtime floor kept as the fallback basis'
    Assert-True ($h.Contains('[ "$size" -lt "$prev" ] && prev=0'))   'rotation resets the watermark'
    # Both undated sources are actually WIRED to it - a helper defined and
    # used nowhere is the half-fix this pin exists to catch.
    Assert-True ($h.Contains('tailnew 900 ~/.apps/tdarr/logs/server.err ~/.apps/tdarr/logs/node.err')) 'tdarr .err half on the watermark'
    Assert-True ($h.Contains("collect kometa bash -c 'tailnew 1200 ~/.apps/kometa/logs/kometa.err'"))  'kometa.err on the watermark'
    Assert-False ($h.Contains('tailfresh 80 ~/.apps/tdarr/logs/server.err'))  'tdarr .err no longer rides the mtime gate'
    Assert-False ($h.Contains('tailfresh 120 ~/.apps/kometa/logs/kometa.err')) 'kometa no longer rides the mtime gate'
    # Per-line [path] prefix, not a header: byte truncation can orphan a line
    # from a header, never from its own prefix.
    Assert-True ($h.Contains('printf ''%s\n'' "$new" | sed -e "s#^#[$f] #"')) 'watermark output is per-line [path] prefixed'
}

Test-Case 'the tdarr DATED half runs first so the byte cap cannot starve it' {
    # Measured 2026-08-25: the .err half was 2934 bytes against a 3000-byte cap,
    # so the FRESH_CUTOFF-filtered Tdarr_*_Log.txt half got 66 bytes and shipped
    # a truncated header with ZERO log lines - deterministically, every run.
    # collect_cap caps with head -c (oldest bytes win), so list order IS the
    # budget priority.
    $h = Get-RemoteHeredoc
    $iDated = $h.IndexOf('Tdarr_Server_Log.txt')
    $iErr   = $h.IndexOf('tailnew 900 ~/.apps/tdarr/logs/server.err')
    Assert-True ($iDated -gt 0 -and $iErr -gt 0) 'both tdarr legs present'
    Assert-True ($iDated -lt $iErr)              'dated leg precedes the undated leg'
}

Test-Case 'the two dated-but-unfiltered sources now carry the line filter' {
    # nginx ("2026/08/20 04:31:00 [error]") and sabnzbd ("2026-08-22
    # 04:00:31,123::INFO::") both date every line and both were still gated by
    # file mtime alone - sabnzbd's tail-150 window spanned 2.7 days.
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('tailfresh 200 ~/.apps/nginx/logs/error.log | freshlines'))    'nginx line-filtered'
    Assert-True ($h.Contains('tailfresh 150 ~/.apps/sabnzbd/logs/sabnzbd.log | freshlines')) 'sabnzbd line-filtered'
}

Test-Case 'every section declares a freshness basis, and an undeclared one withholds content' {
    # The law: freshness is established at COLLECTION time or the section ships
    # nothing. A 15th source added without a declaration must not silently
    # inherit "unbounded" - it must be a NAMED configuration error.
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('src_basis() {')) 'declaration table exists'
    Assert-True ($h.Contains('# collector-error: section=%s declares no freshness basis')) 'undeclared section is a named error'
    Assert-True ($h.Contains('case "$(src_basis "$k")" in')) 'the assembly loop CONSULTS the table'
    # mtime alone must never be a legal declaration - it is the exact gate that
    # failed, and admitting it would re-legalise the defect.
    Assert-True ($h -match 'line\|watermark\|query\|line\+watermark\)') 'legal bases are line/watermark/query only'
    foreach ($k in @('arr_logs','journal_errors','cron_mail','maint_state','nginx_errors','plex_errors',
                     'kuma_red','sabnzbd','tdarr','kometa','config_sync','app_extra','reaper_log','vlogs')) {
        Assert-True ($h -match ("(?m)^\s*(?:[a-z_0-9|]*\|)?" + [regex]::Escape($k) + "(?:\|[a-z_0-9|]*)?\)\s+echo ")) "section $k declares a basis"
    }
}

Test-Case 'the section cap truncates on a LINE boundary, never mid-header' {
    # The 2026-08-25 page's `file` field was the fragment
    # "===== /home/.../Tdarr_Server_Log.txt (ERROR" - a header with zero lines
    # under it, produced by a byte cut landing inside a header.
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('head -c "$((cap + 1))"')) 'reads cap+1 so truncation is DETECTABLE'
    $lineSafeCut = 'head -c "$cap" "$raw" | sed -e ''$d'''
    Assert-True ($h.Contains($lineSafeCut)) 'drops the trailing partial line when truncated'
    Assert-True ($h.Contains('collect_cap "$name" "$SECTION_CAP" "$@"')) 'collect delegates to the one capped path'
    Assert-False ($h.Contains('( "$@" 2>&1 || true ) | head -c "$SECTION_CAP" | base64')) 'no un-line-safe cap path remains'
}

# --- 2026-08-25: `file` comes from the COLLECTOR, never the model ------------
Test-Case 'Resolve-FindingFile anchors on the excerpt, not on the model file field' {
    # The exact 2026-08-25 shape: the excerpt is a REAL node.err line, and the
    # model named Tdarr_Server_Log.txt (the last header-shaped token in the
    # window). Provenance must beat the model's guess.
    $blob = [pscustomobject]@{
        fetched_at = '2026-08-25T05:00:09Z'
        host       = 'manitoba'
        sources    = [pscustomobject]@{
            tdarr = @(
                '===== /home/quadstronaut/.apps/tdarr/logs/server.err ====='
                "TypeError: Cannot read properties of undefined (reading 'includes')"
                '===== /home/quadstronaut/.apps/tdarr/logs/node.err ====='
                "  path: '/tdarr-workDir-node-baY4PcyP1-worker-gloomy-goa-ts-1779348004454'"
                '===== /home/quadstronaut/.apps/tdarr/logs/Tdarr_Server_Log.txt (ERROR'
            ) -join "`n"
        }
    }
    $idx = Get-CollectorPathIndex -DecodedBlob $blob
    $got = Resolve-FindingFile -Index $idx `
        -ModelFile '/home/quadstronaut/.apps/tdarr/logs/Tdarr_Server_Log.txt' `
        -Excerpt   "path: '/tdarr-workDir-node-baY4PcyP1-worker-gloomy-goa-ts-1779348004454'"
    Assert-Equal '/home/quadstronaut/.apps/tdarr/logs/node.err' $got 'excerpt provenance beats the model-named path'
}

Test-Case 'Resolve-FindingFile refuses a path the collector never emitted' {
    $blob = [pscustomobject]@{
        sources = [pscustomobject]@{ plex_errors = '[Plex Media Server.log] Aug 25, 2026 01:02:03 ERROR - widget exploded in an unmistakable way' }
    }
    $idx = Get-CollectorPathIndex -DecodedBlob $blob
    # Invented path + paraphrased excerpt: nothing to anchor on, nothing on the
    # allowlist. 'unattributed' is the honest answer; a made-up path is not.
    Assert-Equal 'unattributed' (Resolve-FindingFile -Index $idx -ModelFile '/var/log/plex/plex.log' -Excerpt 'plex threw an error about a widget') 'invented path rejected'
    # A real shipped line resolves to the collector's own [logfile] tag.
    Assert-Equal 'Plex Media Server.log' (Resolve-FindingFile -Index $idx -ModelFile '' -Excerpt 'ERROR - widget exploded in an unmistakable way') 'per-line tag is the provenance'
    # A model naming the right file by basename gets ratified, not rejected.
    Assert-Equal 'Plex Media Server.log' (Resolve-FindingFile -Index $idx -ModelFile '/somewhere/Plex Media Server.log' -Excerpt 'too short') 'basename ratified against the allowlist'
}

Test-Case 'Get-CollectorPathIndex ignores bracket tokens that are not paths' {
    $blob = [pscustomobject]@{
        sources = [pscustomobject]@{ tdarr = "[2026-08-23] [ERROR] worker died unexpectedly during transcode`n[Req#77] whatever" }
    }
    $idx = Get-CollectorPathIndex -DecodedBlob $blob
    Assert-False ($idx.tokens.ContainsKey('2026-08-23')) 'a date is not provenance'
    Assert-False ($idx.tokens.ContainsKey('ERROR'))      'a level is not provenance'
    Assert-True  ($idx.tokens.ContainsKey('source:tdarr')) 'the section itself is always a legal token'
    Assert-Equal 'source:tdarr' (Resolve-FindingFile -Index $idx -ModelFile '' -Excerpt '[2026-08-23] [ERROR] worker died unexpectedly during transcode') 'a shipped line with no path token belongs to its SECTION, not to a bracket token'
    # An excerpt too short to anchor is unattributed rather than guessed at -
    # 24 chars is the collision floor and it fails toward honesty.
    Assert-Equal 'unattributed' (Resolve-FindingFile -Index $idx -ModelFile '' -Excerpt 'worker died') 'sub-floor excerpt is not guessed'
}

Test-Case 'the prompt no longer instructs a fetched_at timestamp fallback' {
    # Get-SystemPrompt used to say "fall back to fetched_at", which for an
    # UNDATED line is an instruction to stamp it NOW - and Test-IsStaleFinding
    # then grades ($ref - $t).TotalDays == 0 as FRESH. The prompt was laundering
    # every undated source past the only remaining freshness check.
    $sp = Get-SystemPrompt
    Assert-False ($sp.Contains('fall back to fetched_at')) 'fetched_at fallback removed'
    Assert-True  ($sp -match '(?i)never substitute fetched_at') 'and explicitly forbidden'
}

Test-Case 'stale-line collectors compare each line date against FRESH_CUTOFF' {
    # 2026-08-13: a 5-day-old tdarr xhr burst and 4-day-old bazarr2 SignalR
    # blips paged because tailfresh only filters whole files by MTIME and the
    # model-side time field fails open. The tdarr [ERROR] grep and the bazarr
    # .err/.log tail loop must both carry the deterministic bash-side date
    # filter, and the cutoff must derive from FRESH_DAYS (single source).
    # Every assertion here is EXACT on purpose (adversarial review 2026-08-14
    # showed a bare invocation-count survived comparison flips, swapped substr
    # offsets, a dropped export, and pipeline reordering):
    $h = Get-RemoteHeredoc
    Assert-True ($h -match 'FRESH_CUTOFF=\$\(date -d "-\$FRESH_DAYS days" \+%F\)') 'cutoff derived from FRESH_DAYS'
    # Both awk sites run inside bash -c CHILDREN: without the export the
    # children expand $FRESH_CUTOFF to "" and `d >= ""` is true for every
    # line - both filters become silent no-ops with this suite green.
    Assert-True ($h.Contains('export FRESH_CUTOFF')) 'cutoff exported to bash -c children'
    Assert-True ($h.Contains('export FRESH_DAYS'))   'FRESH_DAYS exported (tailfresh children)'
    # Full awk program pinned per collector: keep-direction (d >= c), the
    # site-specific substr offset (bazarr date at col 1; tdarr skips its
    # leading "[" via col 2), and the continuation-inheritance BEGIN{keep=1}.
    # Bazarr: inheritance filter over the WHOLE file, trimmed to 120 AFTER. It
    # was a `tail -n 360` pre-window until 2026-09-02, when a 2026-08-18
    # traceback was measured shipping as current for fifteen days: the dated
    # header governing it sat 81 lines ABOVE the start of that window, so awk
    # opened at keep=1 and never saw a date. An EOF-relative window cannot
    # guarantee it contains the dated line that governs the lines inside it.
    # Tdarr: NO
    # inheritance - it runs post-grep where adjacent lines are weeks apart and
    # the only undated lines are interleave-corrupted REAL errors that must
    # pass (fail open), not inherit a stale verdict (fail closed).
    $bazarrSite = 'freshtail "$f" 120'
    # 2026-08-25: tdarr site moved off the fixed col-2 substr to match() so a
    # date NOT at column 2 (interleave-spliced lines) still gets a verdict;
    # undated lines still pass through (plain fail-open, no inheritance).
    $tdarrSite  = 'awk -v c="$FRESH_CUTOFF" "{ if (match(\$0, /\[[0-9]{4}-[0-9]{2}-[0-9]{2}T/)) { d=substr(\$0, RSTART+1, 10); if (d < c) next } print }"'
    Assert-True ($h.Contains($bazarrSite)) 'bazarr scans the WHOLE file, emits the last 120'
    Assert-False ($h.Contains('tail -n 360 "$f"')) 'the 360-line pre-window that leaked a 15-day-old traceback is gone'
    Assert-True ($h.Contains($tdarrSite))  'tdarr filter verbatim (match-anchored date, plain fail-open)'
    # arr Error/Fatal grep (2026-08-15: expired July indexer-backoff line paged
    # from a sparse-error file - whole-file grep needs the same date floor).
    $arrSite = 'grep -aE "\|(Error|Fatal)\|" "$f" | awk -v c="$FRESH_CUTOFF" "{ d=substr(\$0,1,10); if (d ~ /^[0-9]{4}-/ && d < c) next; print }" | tail -n 8'
    Assert-True ($h.Contains($arrSite)) 'arr Error/Fatal filter verbatim, before tail -8'
    # app_extra (2026-08-18: a 2026-06-22 Gemini 429 paged as a live fault from
    # a code path that no longer EXISTS in the deployed tree). These files are
    # append-only and some are written WEEKLY, so the mtime gate calls the file
    # fresh while `tail -n 120` reaches back months - qflix-newsletter.err is
    # 843 lines covering ten weekly runs and its tail-120 window spanned
    # 2026-06-15 to 2026-08-17. Factored as a REUSABLE `freshlines` filter
    # rather than a fourth copy of the awk, because this section has two loops
    # and eight files. Pinned three ways: the function must exist, be exported
    # to the bash -c child (without the export the child has no such command and
    # the pipeline dies), and be wired into BOTH loops - a filter defined but
    # piped into only one loop is exactly the half-fix this pin exists to catch.
    Assert-True ($h.Contains('freshlines() {'))     'freshlines helper defined'
    Assert-True ($h.Contains('export -f freshlines')) 'freshlines exported to bash -c children'
    # Inheritance (BEGIN{keep=1}) so a stale multi-line traceback goes with its
    # dated header instead of surviving headless - the Gemini finding's own
    # shape. gsub accepts listmonk's YYYY/MM/DD alongside ISO dates.
    Assert-True ($h -match 'BEGIN \{ keep = 1 \}')                    'freshlines inherits across undated continuation lines'
    Assert-True ($h.Contains('gsub("/", "-", d)'))                     'freshlines accepts slash-separated dates (listmonk)'
    $extraLoop1 = 'tail -n 120 "$f" | freshlines | grep -aiE "error|exception|fail|traceback"'
    $extraLoop2 = 'T=$(tail -n 120 "$f" | freshlines | grep -aiE "error|exception|fail|traceback" || true)'
    Assert-True ($h.Contains($extraLoop1)) 'app_extra original loop filters lines before the error grep'
    Assert-True ($h.Contains($extraLoop2)) 'app_extra widened loop filters lines before the error grep'
    # plex_errors (2026-08-16: a 3.5-day-old Aug-12 EAE burst paged on Aug 15 -
    # PMS rotates weekly, so the mtime gate alone admits up to 7 days of lines,
    # and the model omitted `time` so the ps1 backstop failed open). PMS leads
    # with a month-name date awk cannot compare lexically, so the fresh window
    # is an enumerated alternation of FRESH_DAYS+1 zero-padded literal dates
    # ("Aug 01, 2026" - verified zero-padded across every rotated log). The
    # filter must sit BETWEEN the ERROR grep and the [logfile] sed prefix so
    # the date stays at column 1, and unmatched date-shapes must pass (fail
    # open).
    $plexFreshDef = 'PLEX_FRESH=$(for i in $(seq 0 "$FRESH_DAYS"); do date -d "-$i days" "+%b %d, %Y"; done | paste -sd"|" -)'
    Assert-True ($h.Contains($plexFreshDef)) 'plex fresh-date alternation derived from FRESH_DAYS'
    # Empty-alternation guard (adversarial review 2026-08-16): PLEX_FRESH=""
    # would make the rx "^() ", which matches NO real PMS line - every dated
    # ERROR silently dropped, fail CLOSED. An empty awk dynamic regex matches
    # everything, so an empty PLEX_RX must disable the drop branch instead.
    $plexGuard = 'PLEX_RX=""; [ -n "$PLEX_FRESH" ] && PLEX_RX="^($PLEX_FRESH) "'
    Assert-True ($h.Contains($plexGuard)) 'plex empty-alternation fail-open guard present'
    $plexSite = 'grep -a " ERROR - " "$f" | awk -v rx="$PLEX_RX" "{ if ($0 ~ /^[A-Z][a-z]{2} [0-9]{2}, [0-9]{4} /) { if ($0 !~ rx) next } print }" | sed -e "s#^#[${f##*/}] #"'
    Assert-True ($h.Contains($plexSite.Replace('$0','\$0'))) 'plex filter verbatim (between ERROR grep and sed prefix, date-shape fail-open)'
    # config_sync (2026-08-16: buildarr.err is append-only with a fresh mtime
    # every run - plain tailfresh shipped a 60-line window spanning 14 days
    # and Aug-02 buildarr warnings paged. Bazarr-style inheritance awk over a
    # 3x pre-window, trimmed to the 60-line budget AFTER, so a stale header
    # just above the cut still sheds its undated traceback body.)
    $cfgSite = 'freshtail "$f" 60'
    Assert-True ($h.Contains($cfgSite)) 'config_sync scans the WHOLE file, emits the last 60'
    Assert-False ($h.Contains('tail -n 180 "$f"')) 'the 180-line pre-window is gone too'
    # Both legs go through the ONE helper now rather than carrying verbatim
    # copies of its awk - a copy is what let the two sites drift to different
    # (and differently-wrong) window sizes in the first place.
    Assert-False ($h -match 'tail -n [0-9]+ "\$f" \| awk -v c="\$FRESH_CUTOFF" "BEGIN\{keep=1\}') 'no inline copy of the inheritance awk survives'
    Assert-False ($h.Contains("collect config_sync bash -c 'tailfresh")) 'config_sync no longer ships a raw tailfresh window'
    Assert-True ($h.Contains('freshtail() {'))     'freshtail helper defined'
    Assert-True ($h.Contains('export -f freshtail')) 'freshtail exported to bash -c children'
    Assert-False ($h -match 'freshlines < "\$f"') 'neither leg emits the whole file any more'
    # Tdarr ordering: the filter must sit BETWEEN the [ERROR] grep and
    # tail -n 400, so stale lines cannot eat the 400-line window.
    Assert-True ($h -match 'grep -a "\\\[ERROR\\\]" "\$f"[\s\S]*?awk -v c="\$FRESH_CUTOFF"[\s\S]*?tail -n 400') 'tdarr filter before the 400-line window'
}

# --- 2026-08-20: cron_mail was the last collector with NO line-date filter ---
# The 13-issue page that morning led with "cron:permission-denied", flagged 2/3 -
# its highest-confidence finding. The underlying mail was dated 2026-08-09 08:05
# and fcb756b restored the exec bit at 08:10 the same morning; the class had been
# dead for twelve days. /var/spool/mail/quadstronaut is append-only and nothing
# rotates it (439 KB, 432 messages, 428 of them stale), so the only thing gating
# it was tailfresh's file-mtime check - and four benign 2026-08-18 setlocale
# warnings kept the mtime fresh enough to re-ship the entire window. Measured:
# `tail -n 500` spanned 2026-08-09 to 2026-08-18 and carried 18 Permission-denied
# lines; mboxfresh drops 428/432 messages and carries 0.
Test-Case 'cron_mail filters the mbox spool per MESSAGE date, not just file mtime' {
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('mboxfresh() {'))       'mboxfresh helper defined'
    Assert-True ($h.Contains('export -f mboxfresh')) 'mboxfresh exported to bash -c children'
    # The raw tailfresh call is the exact shape that shipped the false page.
    Assert-False ($h.Contains("collect cron_mail bash -c 'tailfresh 500")) 'cron_mail no longer ships a raw tailfresh window'
    Assert-True ($h.Contains('mboxfresh < "$f" | tail -n 500'))            'filter runs BEFORE the tail window'
    # Order matters and is not cosmetic: collect() caps with `head -c`, which
    # keeps the FIRST bytes of the window - its oldest end. Tailing first would
    # park the surviving stale lines exactly where the cap lands.
    Assert-False ($h.Contains('tail -n 500 "$f" | mboxfresh'))             'not tail-then-filter (head -c cap keeps the oldest bytes)'
    # Envelope line is both boundary and date, so a stale body cannot outlive
    # its stale header - same inheritance law freshlines uses for tracebacks.
    Assert-True ($h.Contains('/^From [^ ]+ / {'))                          'keys on the mbox envelope line'
    Assert-True ($h -match 'BEGIN \{\s*\r?\n\s*keep = 1')                  'inherits across the message body'
    # awk interval expressions are not portable across mawk/gawk; the year test
    # must stay spelled out rather than {4}.
    Assert-False ($h.Contains('$7 ~ /^[0-9]{4}$/'))                        'no {n} interval expression in the year test'
    # Fail-open law, identical to every other FRESH_CUTOFF site.
    Assert-True ($h -match 'else\s*\r?\n\s*keep = 1')                      'unparseable envelope fails OPEN'
    # Suppression is counted, never silent (journal_errors / plex_errors law).
    Assert-True ($h.Contains('# collector-suppressed: section=cron_mail'))  'suppression is counted and announced'
    Assert-True ($h.Contains('$((ALL-KEPT))'))                              'announces how many messages were dropped'
}

Test-Case 'system prompt protects the real faults it sits beside' {
    $sp = Get-SystemPrompt
    Assert-True ($sp -match 'EDQUOT')                     'quota failures still mentioned'
    Assert-True ($sp -match 'MUST still report')          'disk-quota MUST-report force preserved'
    Assert-True ($sp -match 'protocol is shutdown')       'Plex client-abort listed as noise'
}

# --- 2026-07-29: system-prompt de-conflict (root cause #1 of the 02:51 false page) ---
# The prompt used to ORDER the model to ALWAYS report WASM/MediaInfo OOM, which by
# 2026-07-29 was ruled permanently unfixable + canary-tracked - the prompt was
# fighting the suppression list above. This must flip WITHOUT weakening the
# disk-quota/write-failure half, which is still a genuine, actionable fault class.
Test-Case 'system prompt no longer orders WASM/MediaInfo OOM to be reported, and reclassifies it as known/permanent/canary-tracked' {
    $sp = Get-SystemPrompt
    Assert-True ($sp -match 'Out of memory: wasm memory') 'WASM OOM phrase still present (now under noise/reclassification)'
    Assert-True ($sp -match '(?i)(permanent|unfixable)') 'reclassified as permanent/unfixable'
    Assert-True ($sp -match '(?i)canary')                'reclassified as canary-tracked'
    Assert-True ($sp -match '(?i)do NOT report it')       'explicit do-not-report instruction for WASM/MediaInfo'
}

Test-Case 'system prompt lists Plex post-reap parent-iterator chatter as noise' {
    $sp = Get-SystemPrompt
    Assert-True ($sp -match '(?i)failed to create parent iterator') 'mentioned'
    Assert-True ($sp -match '(?i)reaper')                            'tied to reaper retention, not a fault'
}

# --- 2026-07-29: line-level staleness enforcement (root cause #3 of the 02:51 page) ---
# Freshness used to be gated per FILE mtime only; a fresh file (touched benignly,
# e.g. the newsletter .err file touched every Monday) could still carry weeks-old
# lines that rode along forever because the model's own 3-day rule is advisory.
Test-Case 'Test-IsStaleFinding drops a finding whose time is older than FreshDays before fetched_at' {
    $f = @{ time = '2026-06-22T10:00:00Z' }  # ~37 days before fetched_at below
    Assert-True (Test-IsStaleFinding -Finding $f -FetchedAt '2026-07-29T02:51:00Z') 'stale line dropped'
}

Test-Case 'Test-IsStaleFinding keeps a finding within FreshDays of fetched_at' {
    $f = @{ time = '2026-07-27T10:00:00Z' }  # ~2 days before fetched_at below
    Assert-False (Test-IsStaleFinding -Finding $f -FetchedAt '2026-07-29T02:51:00Z') 'fresh line kept'
}

Test-Case 'Test-IsStaleFinding fails OPEN on a missing time field' {
    Assert-False (Test-IsStaleFinding -Finding @{ time = '' } -FetchedAt '2026-07-29T02:51:00Z') 'missing time kept, not dropped'
    Assert-False (Test-IsStaleFinding -Finding @{ } -FetchedAt '2026-07-29T02:51:00Z') 'absent time field kept, not dropped'
}

Test-Case 'Test-IsStaleFinding fails OPEN on an unparseable time field' {
    $f = @{ time = 'not-a-real-timestamp' }
    Assert-False (Test-IsStaleFinding -Finding $f -FetchedAt '2026-07-29T02:51:00Z') 'garbage time kept, not dropped'
}

Test-Case 'Test-IsStaleFinding does not drop a future timestamp (clock skew)' {
    $f = @{ time = '2026-08-15T00:00:00Z' }  # after fetched_at
    Assert-False (Test-IsStaleFinding -Finding $f -FetchedAt '2026-07-29T02:51:00Z') 'future timestamp kept, not dropped'
}

Test-Case 'Test-IsStaleFinding reuses the shared FreshDays constant (no second hardcoded copy)' {
    $prev = $Script:FreshDays
    try {
        $Script:FreshDays = 100
        $f = @{ time = '2026-06-22T10:00:00Z' }  # ~37 days before - stale under 3, fresh under 100
        Assert-False (Test-IsStaleFinding -Finding $f -FetchedAt '2026-07-29T02:51:00Z') 'threshold follows $Script:FreshDays'
    } finally { $Script:FreshDays = $prev }
}

Test-Case 'Get-RemoteHeredoc templates FRESH_DAYS from the shared constant' {
    $h = Get-RemoteHeredoc
    Assert-True ($h -match "FRESH_DAYS=$($Script:FreshDays)") 'FRESH_DAYS substituted, single source of truth'
}

# ============================================================================
# 2026-07-29 REA audit fixes
# ============================================================================

# --- Fix 1: REA can go permanently dark - five early-return failure paths in
# Invoke-Main used to write only an audit-log line and return, never paging.
# Only ollama_down paged. These pin the new shared Send-DeadmanAlert helper and
# its per-reason 24h dedup keys in state.json.

Test-Case 'DeadmanReasons covers exactly the five non-ollama silent failure paths' {
    Assert-Equal @('tunnel_timeout','no_models','ssh_fail','blob_parse','all_models_noop') $Script:DeadmanReasons 'five reasons'
}

Test-Case 'New-DiscordDeadmanPayload keeps the original Ollama wording when called with no overrides' {
    # Backward-compat pin: the ollama_down caller only ever passes -OperatorId.
    $p = New-DiscordDeadmanPayload -OperatorId '123'
    Assert-Equal '<@123>' $p.content 'operator mention'
    Assert-Equal 16753920 $p.embeds[0].color 'orange'
    Assert-True ($p.embeds[0].title -match 'Ollama') 'default title unchanged'
}

Test-Case 'New-DiscordDeadmanPayload accepts a reason-specific title and description' {
    $p = New-DiscordDeadmanPayload -OperatorId '123' -Title '[WARN] QFlix REA - custom reason' -Description 'custom body'
    Assert-Equal '<@123>' $p.content 'operator mention still present'
    Assert-Equal 16753920 $p.embeds[0].color 'shared deadman color'
    Assert-Equal '[WARN] QFlix REA - custom reason' $p.embeds[0].title 'custom title used'
    Assert-Equal 'custom body' $p.embeds[0].description 'custom description used'
}

Test-Case 'Read-State returns empty dead_ping_<reason> defaults for all five reasons when file absent' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        $s = Read-State
        foreach ($r in $Script:DeadmanReasons) {
            Assert-Equal '' $s["dead_ping_$r"] "default dead_ping_$r"
        }
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Write-State then Read-State roundtrips a dead_ping_<reason> key without disturbing the others' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        Write-State @{ last_heartbeat_date = ''; last_ollama_dead_ping = ''; dead_ping_ssh_fail = '2026-07-29T02:00:00Z' }
        $s = Read-State
        Assert-Equal '2026-07-29T02:00:00Z' $s.dead_ping_ssh_fail 'persisted dead_ping_ssh_fail'
        Assert-Equal '' $s.dead_ping_no_models 'unrelated reason still defaults to empty'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Send-DeadmanAlert pages (dry-run) on first occurrence of a reason' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    $prevDry = $DryRun
    try {
        $DryRun = $true
        Send-DeadmanAlert -Reason 'tunnel_timeout' -Title 't' -Description 'd' -Webhook 'http://example.invalid' -OpId '123'
        $log = Get-Content -Raw -LiteralPath (Join-Path (Get-StateDir) 'audit.log')
        Assert-True ($log -match 'fail reason=tunnel_timeout outcome=dryrun_deadman') 'dry-run deadman logged'
    } finally {
        $DryRun = $prevDry
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Send-DeadmanAlert stays silent within the 24h dedup window for the same reason' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        Write-State @{ last_heartbeat_date = ''; last_ollama_dead_ping = ''; dead_ping_no_models = (Get-Date).ToString('o') }
        Send-DeadmanAlert -Reason 'no_models' -Title 't' -Description 'd' -Webhook 'http://example.invalid' -OpId '123'
        $log = Get-Content -Raw -LiteralPath (Join-Path (Get-StateDir) 'audit.log')
        Assert-True ($log -match 'fail reason=no_models outcome=silent') 'deduped within 24h, no POST attempted'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Send-DeadmanAlert dedup keys are independent per reason' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    $prevDry = $DryRun
    try {
        $DryRun = $true
        # no_models was "just paged" (recent timestamp) - ssh_fail is a DIFFERENT
        # reason and must still page: a stuck tunnel must not mask a model outage.
        Write-State @{ last_heartbeat_date = ''; last_ollama_dead_ping = ''; dead_ping_no_models = (Get-Date).ToString('o') }
        Send-DeadmanAlert -Reason 'ssh_fail' -Title 't' -Description 'd' -Webhook 'http://example.invalid' -OpId '123'
        $log = Get-Content -Raw -LiteralPath (Join-Path (Get-StateDir) 'audit.log')
        Assert-True ($log -match 'fail reason=ssh_fail outcome=dryrun_deadman') 'independent reason still pages'
        Assert-False ($log -match 'reason=ssh_fail outcome=silent') 'not deduped by an unrelated reason''s recent ping'
    } finally {
        $DryRun = $prevDry
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Send-DeadmanAlert is silent (never throws) when webhook/opid are missing' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    try {
        Send-DeadmanAlert -Reason 'blob_parse' -Title 't' -Description 'd' -Webhook '' -OpId ''
        $log = Get-Content -Raw -LiteralPath (Join-Path (Get-StateDir) 'audit.log')
        Assert-True ($log -match 'fail reason=blob_parse outcome=silent') 'no webhook/opid -> silent outcome logged'
    } finally {
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

Test-Case 'Send-DeadmanAlert appends an optional Detail suffix to the audit line' {
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-test-$(Get-Random)"
    $prevDry = $DryRun
    try {
        $DryRun = $true
        Send-DeadmanAlert -Reason 'ssh_fail' -Title 't' -Description 'd' -Webhook 'http://example.invalid' -OpId '123' -Detail 'msg=connection refused'
        $log = Get-Content -Raw -LiteralPath (Join-Path (Get-StateDir) 'audit.log')
        Assert-True ($log -match 'fail reason=ssh_fail outcome=dryrun_deadman msg=connection refused') 'detail suffix present on the audit line'
    } finally {
        $DryRun = $prevDry
        if (Test-Path $env:APPDATA) { Remove-Item $env:APPDATA -Recurse -Force }
    }
}

# --- Fix 2: the vlogs LogsQL query used to carry global term exclusions
# "-PMP -includes" that dropped a real error from ANY of the 13+ aggregated
# apps merely for containing the word "includes", with no audit trace. Widened
# instead of scoped (see the comment above `collect vlogs` for why) - the two
# offending classes are now caught by NoiseFindingRules, which IS logged.

Test-Case 'vlogs query no longer carries the blanket -PMP -includes exclusions' {
    $h = Get-RemoteHeredoc
    # Check the actual query text (the exact adjoining shape from the old query
    # line), not just any mention of the string - the fix comment above
    # `collect vlogs` deliberately quotes "-PMP -includes" for documentation,
    # so a bare substring check would false-fail against its own explanation.
    Assert-False ($h -match 'traceback\) -PMP -includes') 'blanket global-term exclusions removed from the query'
    Assert-True ($h -match 'collect_cap vlogs') 'vlogs source still present'
}

Test-Case 'vlogs query still fetches the core error/fatal/exception/panic/traceback terms' {
    $h = Get-RemoteHeredoc
    Assert-True ($h -match 'error OR fatal OR exception OR panic OR traceback') 'core query terms unchanged'
    Assert-True ($h -match 'fields _time,app,level,_msg') 'field projection includes level'
}

# --- Fix 3: the system prompt already lists three "must NEVER report" classes
# with no corresponding $Script:NoiseFindingRules entry - prompt text alone is
# advisory, exactly the gap this rule table exists to close for every other class.

Test-Case 'Test-IsNoiseFinding suppresses a Cloudflare 5xx error page relayed from an external indexer' {
    $f = @{
        signature = 'prowlarr:indexer-5xx'
        summary   = 'Prowlarr received a 5xx error page from an external indexer'
        excerpt   = 'Cloudflare Error 522: Connection timed out - Ray ID: 7d1f2a3b4c5d6e7f'
    }
    Assert-Equal 'external-indexer-5xx-html' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses nginx default error-page HTML relayed from an upstream proxy' {
    $f = @{ signature = ''; summary = ''; excerpt = '<html><body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>' }
    Assert-Equal 'external-indexer-5xx-html' (Test-IsNoiseFinding $f) 'matched by the same rule id'
}

Test-Case 'Test-IsNoiseFinding suppresses an indexer response merely echoing a severity:error JSON field' {
    $f = @{ signature = ''; summary = ''; excerpt = '{"result":"failure","severity":"error","description":"query too broad"}' }
    Assert-Equal 'indexer-severity-field-echo' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding does NOT suppress plain prose that merely mentions severity and error' {
    # The rule is scoped to the exact quoted JSON key:value shape, not the bare
    # words, so a real narrative sentence using both words still pages.
    $f = @{ signature = ''; summary = 'the severity of this error is high'; excerpt = '' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'prose mention is not the quoted JSON shape'
}

Test-Case 'Test-IsNoiseFinding suppresses a bare stack-trace continuation with no root error' {
    $f = @{
        signature = 'bazarr:unhandled'
        summary   = 'Bazarr logged a stack trace continuation with no visible root cause'
        excerpt   = "   at Foo.Bar() in /src/Foo.cs:line 42`n   at Baz.Qux()`n--- End of inner exception stack trace ---"
    }
    Assert-Equal 'bare-stack-continuation' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'Test-IsNoiseFinding does NOT suppress a stack trace that carries its own root exception header' {
    $f = @{
        signature = 'bazarr:nullref'
        summary   = 'Bazarr crashed with a null reference exception'
        excerpt   = "System.NullReferenceException: Object reference not set to an instance of an object.`n   at Foo.Bar() in /src/Foo.cs:line 42"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'real exception header survives - continuation rule must not eat it'
}

Test-Case 'Test-IsNoiseFinding does NOT suppress a continuation-shaped excerpt that also carries an ERROR log line' {
    $f = @{
        signature = ''
        summary   = ''
        excerpt   = "[ERROR] something genuinely broke`n   at Foo.Bar()"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'an ERROR-prefixed line blocks the continuation-only suppression'
}

Test-Case 'Test-IsNoiseFinding continuation rule is scoped to the excerpt field, not summary/signature' {
    # A model's own prose summary will almost always contain the word
    # "error"/"exception" regardless of what the raw excerpt shows - if this
    # rule read the combined haystack it would never fire. It must also never
    # fire on an empty excerpt.
    $f = @{ signature = 'x:error'; summary = 'an unhandled exception occurred somewhere'; excerpt = '' }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'empty excerpt never triggers the continuation rule'
}

Test-Case 'every noise rule (including field-scoped ones) has an id and a compilable regex' {
    Assert-True ($Script:NoiseFindingRules.Count -ge 9) 'nine known classes now (6 prior + 3 added 2026-07-29)'
    foreach ($r in $Script:NoiseFindingRules) {
        Assert-True ([bool]$r.id) "rule has an id"
        [void][regex]::new($r.rx)
    }
}

# --- 2026-08-16: the two classes behind the 2026-08-15 twelve-field page ---
# (four root causes; kometa + plex were config/staleness fixes, these two are
# permanent-benign and get rules). Both are enrolled in
# manifest/rea-noise-classes.yaml (segments 15 + 16) - C-07 pins parity.

Test-Case 'Test-IsNoiseFinding suppresses the tdarr handbrakePath binary self-test' {
    # Verbatim 2026-08-15 shape: fires daily at node start; HandBrakeCLI is
    # absent + uninstallable on this slot (ruled 2026-07-28) and nothing here
    # ever invokes it.
    $f = @{
        signature = 'tdarr:ffmpeg-binary-failure'
        summary   = 'FFmpeg binary test failure'
        excerpt   = '[2026-08-16T01:00:05.302] [ERROR] Tdarr_Node - Binary test 1: handbrakePath not working'
    }
    Assert-Equal 'tdarr-handbrake-binary-test' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'a tdarr ffmpegPath binary failure still pages' {
    # ffmpeg is the binary every transcode and health check actually uses -
    # its self-test failing is a REAL fault the handbrake rule must never eat.
    $f = @{
        signature = 'tdarr:ffmpeg-binary-failure'
        summary   = 'FFmpeg binary test failure'
        excerpt   = '[2026-08-16T01:00:05.302] [ERROR] Tdarr_Node - Binary test 2: ffmpegPath not working'
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'ffmpegPath failure survives'
}

Test-Case 'Test-IsNoiseFinding suppresses the seerr media.tvdbId scan collision' {
    # Verbatim 2026-08-15 shape: Plex item 7693 "Monster (2022)" carries three
    # tmdb guids resolving to one tvdbId; media row 47 already exists and is
    # available, so the sibling-guid insert fails once per full scan.
    $f = @{
        signature = 'seerr:sqlite-unique-failed'
        summary   = 'SQLITE UNIQUE constraint failed'
        excerpt   = '.366Z [error][Plex Scan]: Failed to process Plex media {"errorMessage":"SQLITE_CONSTRAINT: UNIQUE constraint failed: media.tvdbId","title":"Monster (2022)"}'
    }
    Assert-Equal 'seerr-plex-scan-tvdbid-collision' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'a seerr UNIQUE constraint on any OTHER column still pages' {
    # The rule is anchored to the media.tvdbId column; a constraint failure on
    # a different column/table is a genuine schema or data fault.
    $f = @{
        signature = 'seerr:sqlite-unique-failed'
        summary   = 'SQLITE UNIQUE constraint failed'
        excerpt   = 'SQLITE_CONSTRAINT: UNIQUE constraint failed: user.email'
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'different-column constraint survives'
}

Test-Case 'an excerpt carrying BOTH the handbrake line and a REAL binary failure pages' {
    # Adversarial review 2026-08-16: both binary tests fire from the SAME
    # startup self-test and the tdarr collector uniq-collapses them into one
    # section, so a real ffmpegPath failure lands NEXT TO the benign handbrake
    # line - a model bundling both into one excerpt must not be eaten whole.
    $f = @{
        signature = 'tdarr:binary-tests-failing'
        summary   = 'Tdarr node binary tests failing'
        excerpt   = "[2026-08-16T01:00:05.302] [ERROR] Tdarr_Node - Binary test 1: handbrakePath not working`n[2026-08-16T01:00:05.400] [ERROR] Tdarr_Node - Binary test 2: ffmpegPath not working"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'bundled real binary failure survives'
}

Test-Case 'an excerpt carrying BOTH the tvdbId line and another-column constraint pages' {
    $f = @{
        signature = 'seerr:sqlite-unique-failed'
        summary   = 'Multiple SQLITE UNIQUE constraint failures'
        excerpt   = "SQLITE_CONSTRAINT: UNIQUE constraint failed: media.tvdbId`nSQLITE_CONSTRAINT: UNIQUE constraint failed: user.email"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'bundled different-column constraint survives'
}

Test-Case 'Test-IsNoiseFinding suppresses the MediaInfo child stderr/result fragments' {
    # Third face of the wasm-oom event: the dead child JSON stderr split by
    # the per-line grep. Both shapes, verbatim from the 2026-08-16 blob.
    $f = @{
        signature = 'tdarr:result-error'
        summary   = 'Tdarr server logging error result objects'
        excerpt   = "      2 [2026-08-16] [ERROR] Tdarr_Server - stderr:  {`n      2 [2026-08-16] [ERROR] Tdarr_Server - { result: 'error', error: {} }"
    }
    Assert-Equal 'tdarr-mediainfo-result-fragment' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'a Tdarr stderr line CARRYING content still pages' {
    # The empty shapes are the fingerprint; a stderr with an actual payload
    # (or a result with a non-empty error) is a different, real fault.
    $f = @{
        signature = 'tdarr:stderr'
        summary   = 'Tdarr server stderr output'
        excerpt   = "      1 [2026-08-16] [ERROR] Tdarr_Server - stderr:  { fatal: disk quota exceeded }"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'content-bearing stderr survives'
}

Test-Case 'Test-IsNoiseFinding suppresses the daily buildarr PlexServer notification warning' {
    # Fires 4x every 04:30 run since at least 2026-08-02; upstream plugin
    # limitation, connection left untouched and working. Surfaced only when
    # config_sync gained its FRESH_CUTOFF filter and this became the sole
    # fresh content of the section.
    $f = @{
        signature = 'buildarr:unsupported-notification'
        summary   = 'Buildarr cannot manage the Plex notification connection'
        excerpt   = "2026-08-16 04:30:08,776 buildarr:2912293 buildarr_radarr.config.settings.notifications [WARNING] <radarr> (main) Unsupported remote notification connection 'Plex Media Server' with implementation 'PlexServer', ignoring"
    }
    Assert-Equal 'buildarr-unsupported-plex-notification' (Test-IsNoiseFinding $f) 'matched by rule id'
}

Test-Case 'a buildarr unsupported-connection warning for any OTHER implementation still pages' {
    $f = @{
        signature = 'buildarr:unsupported-notification'
        summary   = 'Buildarr cannot manage a notification connection'
        excerpt   = "2026-08-16 04:30:08,776 buildarr:2912293 [WARNING] <radarr> (main) Unsupported remote notification connection 'Discord' with implementation 'DiscordWebhook', ignoring"
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'different implementation survives'
}

Test-Case 'the 2026-08-16 rules are excerpt-scoped: a prose-only match cannot suppress' {
    # A model summary naming the benign phrase while the excerpt shows a
    # DIFFERENT tdarr fault must page - same prose-poisoning law as the
    # bazarr-ratelimit and bare-stack-continuation rules.
    $f = @{
        signature = 'tdarr:log-write-edquot'
        summary   = 'tdarr errors including handbrakePath not working and quota failures'
        excerpt   = 'log4js.fileAppender - Writing to file Tdarr_Server_Log.txt, error happened [Error: Unknown system error -122, write]'
    }
    Assert-Equal $null (Test-IsNoiseFinding $f) 'summary-only phrase does not suppress an EDQUOT excerpt'
}

Test-Case 'journal_errors drops RESOLVED failed-to-start lines, counted, systemd-verified' {
    # 2026-08-16: two "Failed to start manitoba-maint-canary-deploy-drift"
    # lines paged for a unit that had been green again within the hour - the
    # 24h journal window replays every maintenance blip for a day. A start
    # failure is current only while systemd holds the unit failed, so the
    # collector asks systemd per matched unit and drops resolved lines with
    # a census. Everything else passes untouched (fail open).
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('grep -oE "Failed to start [A-Za-z0-9@._-]+\.(service|timer|socket|mount|path)"')) 'failed-to-start shape matched verbatim'
    Assert-True ($h.Contains('[ "$(systemctl --user is-failed "$u" 2>/dev/null)" != "failed" ]')) 'systemd is the arbiter of CURRENT failure'
    Assert-True ($h.Contains('# collector-suppressed: section=journal_errors n=$ND resolved failed-to-start lines (unit not in failed state at fetch time)')) 'suppression counted, never silent'
}

Test-Case 'remote heredoc is syntactically valid bash (bash -n)' {
    # 2026-08-16: an apostrophe added to a COMMENT inside the single-quoted
    # plex_errors bash -c body terminated the quote and killed the whole
    # remote fetch (fail reason=ssh_fail, line-247 syntax error). Substring
    # pins cannot catch quoting damage; a real bash parse can. GIT BASH
    # EXPLICITLY, never `Get-Command bash`: on this box that resolves to
    # System32 bash.exe (WSL), which cannot read C:/ paths and fails 127
    # with the suite green-looking-red. Candidate list, not one pinned
    # path: the operator workstation runs scoop-managed Git, so the
    # Program Files pin silently skipped this test on the ONE machine the
    # ps1 actually runs on (caught 2026-08-16). Skips (counted) only when
    # no candidate exists.
    $bashExe = @(
        (Join-Path $env:USERPROFILE 'scoop\apps\git\current\bin\bash.exe'),
        (Join-Path $env:ProgramFiles 'Git\bin\bash.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $bashExe) {
        Assert-True $true 'Git Bash not installed - syntax check skipped'
    } else {
        $h = Get-RemoteHeredoc
        $tmp = Join-Path $env:TEMP "rea-heredoc-$(Get-Random).sh"
        # WriteAllText default is UTF-8 WITHOUT BOM - the same bytes
        # Invoke-RemoteFetch now streams.
        [System.IO.File]::WriteAllText($tmp, $h)
        # Git Bash mangles backslashed Windows paths; forward slashes work in
        # both worlds. Native stderr under EAP=Stop becomes a terminating
        # NativeCommandError in PS 5.1, so relax it around the invocation -
        # the assertion is on the exit code, not the stream.
        $tmpBash = $tmp -replace '\\', '/'
        $prevEap = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $bashExe -n $tmpBash 2>&1 | Out-Null
            Assert-Equal 0 $LASTEXITCODE 'bash -n exits 0 on the generated heredoc'
        } finally {
            $ErrorActionPreference = $prevEap
            Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        }
    }
}

Test-Case 'app_extra covers the file-only apps, originals first, per-line capped' {
    # 2026-08-16 source audit: listmonk/upgradinatorr/stream-stats log to files,
    # never to the journal, and are absent from VictoriaLogs - before this they
    # were reachable by NO source. The three original files must stay FIRST
    # (collect caps with head -c, oldest bytes win, so list order is budget
    # priority), and both loops must carry the per-line cut cap so one long
    # JSON/traceback line cannot eat the section.
    # The kavita/komga/calibre-web pins that shipped with this case were removed
    # the same day, when the books stack was decommissioned 2026-08-16: asserting
    # a collector reads a log directory that no longer exists is a false pin.
    $h = Get-RemoteHeredoc
    foreach ($p in @('listmonk/logs/listmonk.log', 'listmonk/logs/sync.log',
                     'upgradinatorr/logs/*.log',
                     'stream-stats/logs/kill_stream.log')) {
        Assert-True ($h.Contains($p)) "app_extra lists $p"
    }
    Assert-True ($h.IndexOf('qflix-dash/logs/app.log') -lt $h.IndexOf('listmonk/logs/listmonk.log')) 'original files precede the 2026-08-16 additions'
    Assert-True ($h.Contains('| grep -aiE "error|exception|fail|traceback" | cut -c1-200 | tail -n 40')) 'original loop per-line capped'
    Assert-True ($h.Contains('printf "%s\n" "$T" | cut -c1-200 | tail -n 20')) 'new loop per-line capped'
}

Test-Case 'unpackerr is level-anchored, not word-matched (2026-08-26 queue-chatter flood)' {
    # unpackerr logs a stats line EVERY MINUTE containing "0 failed" — the
    # generic word grep read every one as an error hit the day the log came
    # back alive (dead since 2026-05-22, resurrected 2026-08-26). Its own
    # levels are bracketed, so the collector must anchor on [ERROR]/[WARN].
    $h = Get-RemoteHeredoc
    Assert-True ($h.Contains('grep -aE "\[(ERROR|WARN)\]"')) 'unpackerr grep anchors on the level token'
    Assert-False ($h.Contains('unpackerr/unpackerr.log ~/.apps')) 'unpackerr is out of the word-match loop'
    $blk = $h.Substring($h.IndexOf('unpackerr SEPARATELY'))
    Assert-True ($blk.IndexOf('grep -aiE') -lt 0 -or $blk.IndexOf('grep -aiE') -gt $blk.IndexOf('=====')) 'no case-insensitive word grep on the unpackerr block'
}

Test-Case 'system prompt carries the 2026-08-16 classes with their still-report carve-outs' {
    $sp = Get-SystemPrompt
    Assert-True ($sp.Contains('Binary test N: handbrakePath not working')) 'handbrake clause present'
    Assert-True ($sp.Contains('ffmpegPath not working')) 'ffmpeg carve-out present'
    Assert-True ($sp.Contains('UNIQUE constraint failed: media.tvdbId')) 'seerr clause present'
    Assert-True ($sp -match '(?i)other seerr sqlite_constraint') 'other-column carve-out present'
}

# --- 2026-08-25: the 12:08 seven-field false page ---
# All seven fields were noise: 2x *arr cloud-news poll timeout (prowlarr+radarr,
# same upstream blip, same minute), 4x ONE sonarr Discord post failure (webhook
# verified alive by GET minutes later), 1x bazarr single-file subsync failure.
# Fixtures are the EXACT excerpts from that alert, post-norm.
Test-Case 'Test-IsNoiseFinding suppresses the 2026-08-25 seven-field page' {
    $cloudNews = @{
        signature = 'prowlarr:notification-failure'
        summary   = 'Failed to retrieve notifications'
        excerpt   = '[2026-08-22 03:38:11.9|Error|ServerSideNotificationService|Failed to retrieve notifications'
    }
    Assert-Equal 'arr-cloud-news-fetch-timeout' (Test-IsNoiseFinding $cloudNews) 'cloud-news poll timeout suppressed'

    $discordFull = @{
        signature = 'sonarr:discord-proxy-post-failure'
        summary   = 'DiscordProxy unable to post payload'
        excerpt   = '2026-08-25 15:18:06.6|Error|DiscordProxy|Unable to post payload NzbDrone.Core.Notifications.Discord.Payloads.DiscordPayload'
    }
    Assert-Equal 'arr-discord-notify-post-failure' (Test-IsNoiseFinding $discordFull) 'DiscordProxy line suppressed'

    $discordTrimmed = @{
        signature = 'sonarr:discord-notification-failure'
        summary   = 'Discord notification failure'
        excerpt   = 'Unable to post payload NzbDrone.Core.Notifications.Discord.Payloads.DiscordPayload'
    }
    Assert-Equal 'arr-discord-notify-post-failure' (Test-IsNoiseFinding $discordTrimmed) 'timestamp-trimmed variant suppressed'

    $discordWarn = @{
        signature = 'sonarr:discord-webhook'
        summary   = 'Unable to send notification'
        excerpt   = '2026-08-25 15:18:07.8|Warn|NotificationService|Unable to send OnImportComplete notification to: Discord Webhook'
    }
    Assert-Equal 'arr-discord-notify-post-failure' (Test-IsNoiseFinding $discordWarn) 'paired Warn line suppressed'

    $subsync = @{
        signature = 'bazarr:subtitle-sync-failure'
        summary   = 'BAZARR unable to sync subtitles'
        excerpt   = '[2026-08-25 14:49:03]ERROR    |root    |BAZARR unable to sync subtitles: /home/quadstronaut/media/Movies/Lucky Strike (2026) {tmdb-1594914}/Lucky Strike (2026) WEBRip-1080p.en.srt'
    }
    Assert-Equal 'bazarr-subsync-single-file' (Test-IsNoiseFinding $subsync) 'single-file subsync failure suppressed'

    # The live dry-run variant that BEAT the first rx: the model trimmed the
    # leading "BAZARR" off the excerpt. Rx must match the model form.
    $subsyncTrimmed = @{
        signature = 'bazarr:subtitle-sync-failure'
        summary   = 'Subtitle sync failure'
        excerpt   = 'unable to sync subtitles: /home/quadstronaut/media/Movies/Lucky Strike (2026) {tmdb-1594914}/Lucky Strike (2026) WEBRip-1080p.en.srt'
    }
    Assert-Equal 'bazarr-subsync-single-file' (Test-IsNoiseFinding $subsyncTrimmed) 'BAZARR-trimmed variant suppressed'
}

Test-Case 'Test-IsNoiseFinding suppresses the 2026-08-25 dry-run residue (one blip, four lines)' {
    # The first live dry-run after the seven-field fix still paged 4 fields;
    # three were NEW one-offs from the SAME 2026-08-22 03:38-03:39 network blip.
    # Fixtures are the exact model-emitted excerpts from that run.
    $flare = @{
        signature = 'prowlarr:proxy-validation-failure'
        summary   = 'FlareSolverr proxy validation failed'
        excerpt   = '[2026-08-22 03:39:17.0|Error|FlareSolverr|Proxy validation failed'
    }
    Assert-Equal 'prowlarr-flaresolverr-validation-transient' (Test-IsNoiseFinding $flare) 'FlareSolverr one-off suppressed'

    $update = @{
        signature = 'radarr2:app-check-update-error'
        summary   = 'CommandExecutor error occurred while executing task ApplicationCheckUpdate'
        excerpt   = '[2026-08-22 03:38:18.1|Error|CommandExecutor|Error occurred while executing task ApplicationCheckUpdate'
    }
    Assert-Equal 'arr-update-check-failure' (Test-IsNoiseFinding $update) 'self-update check failure suppressed'

    # Run 3 residue, same blip: the OTHER *arrs spell the task differently, and
    # CheckHealth joined in. The class owns the shape, allowlisted by task.
    $updateAlt = @{
        signature = 'prowlarr:update-check-failure'
        summary   = 'CommandExecutor error occurred while executing task ApplicationUpdateCheck'
        excerpt   = '[2026-08-22 03:38:12.4|Error|CommandExecutor|Error occurred while executing task ApplicationUpdateCheck'
    }
    Assert-Equal 'arr-update-check-failure' (Test-IsNoiseFinding $updateAlt) 'ApplicationUpdateCheck spelling suppressed'
    $checkHealth = @{
        signature = 'radarr:health-check-failure'
        summary   = 'CommandExecutor error occurred while executing task CheckHealth'
        excerpt   = '[2026-08-22 03:40:23.1|Error|CommandExecutor|Error occurred while executing task CheckHealth'
    }
    Assert-Equal 'arr-update-check-failure' (Test-IsNoiseFinding $checkHealth) 'CheckHealth task failure suppressed'
    # A LOCAL task failing must still page - the allowlist is the guard.
    $backup = @{
        signature = 'radarr:backup-failure'
        summary   = 'CommandExecutor error occurred while executing task Backup'
        excerpt   = '[2026-08-25 03:00:00.0|Error|CommandExecutor|Error occurred while executing task Backup'
    }
    Assert-Equal $null (Test-IsNoiseFinding $backup) 'Backup task failure survives'

    $watchlist = @{
        signature = 'seerr:plex-tv-503-error'
        summary   = 'Plex.TV Metadata API 503'
        excerpt   = 'Failed to retrieve watchlist items {"errorMessage":"Request failed with status code 503"}'
    }
    Assert-Equal 'seerr-plextv-watchlist-5xx' (Test-IsNoiseFinding $watchlist) 'plex.tv watchlist 5xx suppressed'

    # The auth failure on the same fetch is OURS and must still page.
    $watchlist401 = @{
        signature = 'seerr:plex-tv-401'
        summary   = 'watchlist auth failure'
        excerpt   = 'Failed to retrieve watchlist items {"errorMessage":"Request failed with status code 401"}'
    }
    Assert-Equal $null (Test-IsNoiseFinding $watchlist401) 'watchlist 401 survives'

    # Run 2 residue: qwen2.5-coder:7b read the reaper SUCCESS summary as
    # "failed to delete any items". Model form is ASCII hyphen, raw log em dash.
    $reaperOk = @{
        signature = 'reaper:no-deletions'
        summary   = 'qflix-reaper failed to delete any items'
        excerpt   = 'SUCCESS - 0 deleted, 0 GB reclaimed across 0 libraries'
    }
    Assert-Equal 'reaper-success-line-misread' (Test-IsNoiseFinding $reaperOk) 'SUCCESS line misread suppressed (hyphen)'
    $reaperOkRaw = @{
        signature = 'reaper:no-deletions'
        summary   = 'reaper deleted nothing'
        excerpt   = ('2026-08-25T05:14:32Z [qflix-reaper] SUCCESS ' + [char]0x2014 + ' 0 deleted, 0 GB reclaimed across 0 libraries')
    }
    Assert-Equal 'reaper-success-line-misread' (Test-IsNoiseFinding $reaperOkRaw) 'SUCCESS line misread suppressed (em dash)'

    # A real reaper error sharing the excerpt declines the rule and pages.
    $reaperReal = @{
        signature = 'reaper:plex-delete-fail'
        summary   = 'reaper cannot delete'
        excerpt   = "ERROR - Plex delete returned 500 for item 123`nSUCCESS - 0 deleted, 0 GB reclaimed across 0 libraries"
    }
    Assert-Equal $null (Test-IsNoiseFinding $reaperReal) 'SUCCESS line next to a real error still pages'
}

Test-Case '2026-08-25 rules do NOT eat the real faults next door' {
    # A DIFFERENT logger failing to retrieve something is not the cloud-news poll.
    $realNotif = @{
        signature = 'sonarr:db-notification-read'
        summary   = 'notification read failure'
        excerpt   = '2026-08-25 10:00:00.0|Error|NotificationRepository|Failed to retrieve notification definitions from database'
    }
    Assert-Equal $null (Test-IsNoiseFinding $realNotif) 'non-cloud retrieve failure survives'

    # A Kuma/operator-webhook delivery failure is a REAL alerting break -
    # the rule anchors on the *arr notifier strings, not "discord" generally.
    $realWebhook = @{
        signature = 'kuma:discord-notify-fail'
        summary   = 'Kuma failed to notify Discord'
        excerpt   = 'ERROR - Discord notification returned 401 Unauthorized for monitor QFlix Reaper'
    }
    Assert-Equal $null (Test-IsNoiseFinding $realWebhook) 'operator alert-path failure survives'

    # Bazarr failing to DOWNLOAD subtitles is delivery, not polish.
    $realBazarr = @{
        signature = 'bazarr:download-fail'
        summary   = 'subtitle download failed'
        excerpt   = '[2026-08-25 14:49:03]ERROR    |root    |BAZARR unable to download subtitles for episode: provider error'
    }
    Assert-Equal $null (Test-IsNoiseFinding $realBazarr) 'download failure survives'
}

# --- 2026-08-27: the 04:07 credits-detection page (investigated, proven benign) ---
Test-Case 'Test-IsNoiseFinding suppresses "Job failed: Video does not exist" and ONLY that Job-failed variant' {
    # 51/51 occurrences over 3+ weeks co-occur within seconds with a
    # missing-file scan line for the same title (reaper delete or arr
    # replace mid-scan). The exact excerpt from the 2026-08-27 04:07 alert:
    $gone = @{
        signature = 'plex:credits-detection-failed'
        summary   = 'CreditsDetectionManager job failed due to video not existing'
        excerpt   = '[2026-08-27 05:08:21.965] ERROR - [CreditsDetectionManager] Job failed: Video does not exist'
    }
    Assert-Equal 'plex-credits-job-video-missing' (Test-IsNoiseFinding $gone) 'video-does-not-exist variant suppressed'

    # The UNPROVEN Job-failed variants must still page - the 2026-08-03
    # carve-out stands for them.
    $scanner = @{
        signature = 'plex:credits-scanner-failed'
        summary   = 'credits scanner job failed'
        excerpt   = '[2026-08-27 05:08:21.965] ERROR - [CreditsDetectionManager] Job failed: Scanner job failed'
    }
    Assert-Equal $null (Test-IsNoiseFinding $scanner) 'Scanner job failed still pages'
    $mism = @{
        signature = 'plex:credits-mismatch'
        summary   = 'mis-matching media items'
        excerpt   = '[2026-08-27 05:08:21.965] ERROR - [CreditsDetectionManager] Mis-matching media items detected'
    }
    Assert-Equal $null (Test-IsNoiseFinding $mism) 'Mis-matching media items still pages'
}

# ---------------------------------------------------------------------------
# 2026-09-02: cross-run page ledger.
#
# Get-Consensus dedups WITHIN a run; nothing dedupped ACROSS runs. With REA on
# an hourly timer and FreshDays=3, one log line could page 72 times. Measured
# that day: 45 Discord pages in 24h from ~8 distinct causes, including a single
# transient listmonk postgres blip that was the last line of a 52-line log and
# therefore sat in the tail re-paging every hour.
# ---------------------------------------------------------------------------

function New-TestGroup {
    param([string]$Sig, [string]$Key)
    [pscustomobject]@{
        signature = $Sig; page_key = $Key; app = 'plex'; file = 'x'
        severity = 'error'; time = '2026-09-02T00:00:00Z'
        summary = 's'; excerpt = 'e'; models_flagged = @('a','b')
    }
}

function Use-TempReaState {
    param([scriptblock]$Block)
    $prev = $env:APPDATA
    $env:APPDATA = Join-Path $env:TEMP "qflix-rea-ledger-$(Get-Random)"
    try { & $Block } finally {
        try { Remove-Item -Recurse -Force $env:APPDATA -ErrorAction SilentlyContinue } catch {}
        $env:APPDATA = $prev
    }
}

Test-Case 'Get-Consensus emits page_key, the collector-anchored cross-run identity' {
    # signature is model-authored and re-invented hourly; the excerpt key is not.
    $f = @{ signature='bazarr:connection-refused'; severity='error'; app='bazarr'; file='f'
            time='2026-09-02T00:00:00Z'; summary='s'
            excerpt='urllib3.exceptions.NewConnectionError: HTTPConnection(host=127.0.0.1, port=17003): Connection refused'
            _model='m1' }
    $g = @(Get-Consensus -Findings @([pscustomobject]$f))
    Assert-Equal 1 $g.Count 'one group'
    Assert-True ($g[0].PSObject.Properties.Name -contains 'page_key') 'page_key emitted'
    Assert-True ([string]$g[0].page_key -notmatch '[0-9]') 'digits stripped so timestamps collapse'
}

Test-Case 'two runs, same underlying line: the second does not page' {
    Use-TempReaState {
        $g = @(New-TestGroup 'plex:file-not-found' 'plex error opening input server returned not found')
        $r1 = Select-DuePageGroups -Groups $g
        Assert-Equal 1 @($r1.Due).Count   'first run pages'
        Assert-Equal 0 @($r1.Muted).Count 'nothing muted on first sight'

        # Same line, model invents a DIFFERENT signature (the real 2026-09-02
        # pattern: file-not-found / missing-source-file / transcode-404 ...).
        $g2 = @(New-TestGroup 'plex:missing-source-file' 'plex error opening input server returned not found')
        $r2 = Select-DuePageGroups -Groups $g2
        Assert-Equal 0 @($r2.Due).Count   'second run does NOT page'
        Assert-Equal 1 @($r2.Muted).Count 'second run muted by excerpt key, not signature'
    }
}

Test-Case 'twenty-four hourly runs of one unchanged fault page exactly once' {
    Use-TempReaState {
        $paged = 0
        for ($i = 0; $i -lt 24; $i++) {
            $r = Select-DuePageGroups -Groups @(New-TestGroup "sig-$i" 'one unchanged underlying log line here')
            $paged += @($r.Due).Count
        }
        Assert-Equal 1 $paged '24 runs -> 1 page (was 24)'
    }
}

Test-Case 'the cooldown expires so a still-broken fault is re-surfaced' {
    Use-TempReaState {
        $key = 'a fault that is still broken tomorrow morning'
        $null = Select-DuePageGroups -Groups @(New-TestGroup 's' $key)
        # Backdate the stamp past the cooldown.
        $led = Read-PageLedger
        $led[$key] = $led[$key] - ($Script:PageCooldownHours * 3600) - 60
        Write-PageLedger $led
        $r = Select-DuePageGroups -Groups @(New-TestGroup 's' $key)
        Assert-Equal 1 @($r.Due).Count 'pages again after the cooldown - silence-forever is the opposite failure'
    }
}

Test-Case 'the ledger is per-key: one muted finding cannot mute a different one' {
    Use-TempReaState {
        $null = Select-DuePageGroups -Groups @(New-TestGroup 'a' 'first distinct underlying log line aaaaaa')
        $r = Select-DuePageGroups -Groups @(
            (New-TestGroup 'a' 'first distinct underlying log line aaaaaa'),
            (New-TestGroup 'b' 'second distinct underlying log line bbbbb'))
        Assert-Equal 1 @($r.Due).Count   'the new one pages'
        Assert-Equal 1 @($r.Muted).Count 'the repeat is muted'
        Assert-Equal 'b' @($r.Due)[0].signature 'the RIGHT one pages'
    }
}

Test-Case 'the ledger survives across processes (file, not memory)' {
    Use-TempReaState {
        $key = 'a line that must stay muted across a restart'
        $null = Select-DuePageGroups -Groups @(New-TestGroup 's' $key)
        Assert-True (Test-Path (Get-PageLedgerPath)) 'ledger written to disk'
        # Read-PageLedger is the whole of the cross-process state; a fresh
        # process sees exactly this.
        Assert-True ((Read-PageLedger).ContainsKey($key)) 'key persisted'
        $r = Select-DuePageGroups -Groups @(New-TestGroup 's' $key)
        Assert-Equal 0 @($r.Due).Count 'still muted after a simulated restart'
    }
}

Test-Case 'a group with no page_key pages rather than being silently swallowed' {
    Use-TempReaState {
        $g = [pscustomobject]@{ signature='no-key'; app='x'; file='f'; severity='error'
                                time='t'; summary='s'; excerpt='e'; models_flagged=@('a','b') }
        $r = Select-DuePageGroups -Groups @($g)
        Assert-Equal 1 @($r.Due).Count 'no identity to dedup on => it pages'
    }
}

Test-Case 'a corrupt ledger FAILS OPEN and pages' {
    Use-TempReaState {
        $null = Select-DuePageGroups -Groups @(New-TestGroup 's' 'some underlying line that was already paged')
        Set-Content -LiteralPath (Get-PageLedgerPath) -Value '{not json at all' -Encoding UTF8
        $r = Select-DuePageGroups -Groups @(New-TestGroup 's' 'some underlying line that was already paged')
        Assert-Equal 1 @($r.Due).Count 'a broken suppressor must never eat an alert'
    }
}

Test-Case 'the ledger prunes entries it can no longer mute with' {
    Use-TempReaState {
        $old = 'an ancient line nobody will ever see again'
        $null = Select-DuePageGroups -Groups @(New-TestGroup 's' $old)
        $led = Read-PageLedger
        $led[$old] = $led[$old] - (3 * $Script:PageCooldownHours * 3600)
        Write-PageLedger $led
        $null = Select-DuePageGroups -Groups @(New-TestGroup 't' 'a completely different current log line')
        Assert-False ((Read-PageLedger).ContainsKey($old)) 'stale entry pruned'
    }
}

Test-Case 'a DryRun reads the ledger but never stamps it' {
    # A dry run posts nothing. If it stamped, it would mute the next REAL page
    # for 24h and the operator would lose an alert to a diagnostic command they
    # ran themselves. Muting is only ever paid for by a ping that went out.
    Use-TempReaState {
        $k = 'a line seen only by an operator dry run today'
        $r = Select-DuePageGroups -Groups @(New-TestGroup 's' $k) -Stamp:$false
        Assert-Equal 1 @($r.Due).Count 'dry run still classifies it as due'
        Assert-False ((Read-PageLedger).ContainsKey($k)) 'but wrote no stamp'
        $r2 = Select-DuePageGroups -Groups @(New-TestGroup 's' $k)
        Assert-Equal 1 @($r2.Due).Count 'so the next REAL run still pages'
    }
}

Test-Case 'Invoke-Main passes -Stamp:(-not $DryRun) through to the ledger' {
    # Shape pin: the wiring is the whole point of the switch, and a switch
    # defined but never passed is exactly the half-fix this catches.
    $src = Get-Content -Raw -LiteralPath (Join-Path (Get-RepoRoot) 'scripts/local-llm/qflix-rea.ps1')
    Assert-True ($src.Contains('Select-DuePageGroups -Groups $errorGroups -Stamp:(-not $DryRun)')) 'DryRun threaded into the ledger call'
}

Test-Case 'the heartbeat does not call a run with muted repeats clean' {
    $p = New-DiscordHeartbeatPayload -ModelCount 3 -SoloCount 0 -MutedCount 2
    Assert-True ($p.embeds[0].title -notlike '*clean*') 'still-broken is not clean'
    Assert-True ($p.embeds[0].description -like '*2 repeat finding(s) muted*') 'muted count is stated'
    Assert-Equal '' $p.content 'a muted repeat must still not ping'
    $q = New-DiscordHeartbeatPayload -ModelCount 3 -SoloCount 0 -MutedCount 0
    Assert-True ($q.embeds[0].title -like '*clean*') 'a genuinely clean run still says clean'
}


function Get-GitBashExe {
    # The SAME resolution the `bash -n` pin uses. Plain `bash` on this
    # workstation is WSL, whose /mnt view and line-ending handling produce
    # phantom failures (measured 2026-08-25, 245 of them); Git Bash is the
    # shell whose semantics actually match the seedbox.
    @(
        (Join-Path $env:USERPROFILE 'scoop\apps\git\current\bin\bash.exe'),
        (Join-Path $env:ProgramFiles 'Git\bin\bash.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe')
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Invoke-FreshlinesProbe {
    <#
      Runs the SHIPPED freshlines helper - extracted from the generated heredoc,
      so this can never drift from the implementation - over a synthetic log
      carrying the measured 2026-09-02 bazarr2.err geometry: one dated header,
      400 undated filler lines (more than the old 360-line pre-window), then the
      traceback. Returns the surviving traceback count as a string.
    #>
    param([string]$HeaderDate)
    $bashExe = Get-GitBashExe
    if (-not $bashExe) { return $null }

    $tmp = Join-Path $env:TEMP "rea-freshlines-$(Get-Random).log"
    $sf  = Join-Path $env:TEMP "rea-freshlines-$(Get-Random).sh"
    $sb  = New-Object System.Text.StringBuilder
    [void]$sb.Append("$HeaderDate 06:39:05,437 - root : ERROR (signalr_client:159) - BAZARR SignalR client connection lost`n")
    for ($i = 0; $i -lt 400; $i++) { [void]$sb.Append("    undated filler continuation line $i`n") }
    [void]$sb.Append("urllib3.exceptions.NewConnectionError: HTTPConnection(host=127.0.0.1, port=17003): Connection refused`n")
    [System.IO.File]::WriteAllText($tmp, $sb.ToString())

    $h  = Get-RemoteHeredoc
    $i0 = $h.IndexOf('freshlines() {')
    $i1 = $h.IndexOf('export -f freshlines')
    if ($i0 -lt 0 -or $i1 -le $i0) { throw 'freshlines body not found in the generated heredoc' }
    $fn = $h.Substring($i0, $i1 - $i0)

    $logBash = $tmp -replace '\\', '/'
    [System.IO.File]::WriteAllText($sf,
        "FRESH_CUTOFF=2026-08-30`n$fn`nfreshlines < '$logBash' | grep -c 'NewConnectionError'`n")

    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        return (& $bashExe ($sf -replace '\\', '/') 2>&1 | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEap
        Remove-Item -Force $tmp, $sf -ErrorAction SilentlyContinue
    }
}

function Invoke-FreshtailProbe {
    <#
      Runs the SHIPPED freshtail (extracted from the generated heredoc) over a
      synthetic file with the measured buildarr.err shape:

        300  ANCIENTHEAD  undated, above ANY dated line, far outside the window
          1  STALEDATED   header older than the cutoff
        200  STALEBODY    undated, inherits the stale verdict
          1  FRESHDATED   current

      Window 60, so the ancient head is well outside it - which is the real
      file's geometry (1,383 lines, ~40 of them dated).
    #>
    $bashExe = Get-GitBashExe
    if (-not $bashExe) { return $null }

    $tmp = Join-Path $env:TEMP "rea-freshtail-$(Get-Random).log"
    $sf  = Join-Path $env:TEMP "rea-freshtail-$(Get-Random).sh"
    $sb  = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt 300; $i++) { [void]$sb.Append("  ANCIENTHEAD undated traceback frame $i`n") }
    [void]$sb.Append("2026-08-01 04:30:25,036 buildarr [WARNING] STALEDATED something old`n")
    for ($i = 0; $i -lt 200; $i++) { [void]$sb.Append("    STALEBODY continuation $i`n") }
    [void]$sb.Append("2026-09-02 04:31:00,349 buildarr [WARNING] FRESHDATED something current`n")
    [System.IO.File]::WriteAllText($tmp, $sb.ToString())

    $h  = Get-RemoteHeredoc
    $i0 = $h.IndexOf('freshtail() {')
    $i1 = $h.IndexOf('export -f freshtail')
    if ($i0 -lt 0 -or $i1 -le $i0) { throw 'freshtail body not found in the generated heredoc' }
    $fn = $h.Substring($i0, $i1 - $i0)

    $logBash = $tmp -replace '\\', '/'
    $body = @(
        'FRESH_CUTOFF=2026-08-30'
        $fn
        "OUT=`$(freshtail '$logBash' 60)"
        'printf "%s|%s|%s|%s\n" "$(printf ''%s'' "$OUT" | grep -c ANCIENTHEAD)" "$(printf ''%s'' "$OUT" | grep -c FRESHDATED)" "$(printf ''%s'' "$OUT" | grep -c STALEDATED)" "$(printf ''%s'' "$OUT" | grep -c STALEBODY)"'
    ) -join "`n"
    [System.IO.File]::WriteAllText($sf, $body + "`n")

    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $out = (& $bashExe ($sf -replace '\\', '/') 2>&1 | Out-String).Trim()
        $q = $out -split '\|'
        return [pscustomobject]@{ AncientKept=$q[0]; FreshKept=$q[1]; StaleKept=$q[2]; StaleBodyKept=$q[3]; Raw=$out }
    } finally {
        $ErrorActionPreference = $prevEap
        Remove-Item -Force $tmp, $sf -ErrorAction SilentlyContinue
    }
}

function Invoke-FreshtailFailOpenProbe {
    <#
      The mirror law. A file with NO dated line anywhere must emit its window
      whole - freshlines and freshtail both fail OPEN on undated content, and
      freshtail bounding the EMIT must not quietly turn that into fail-closed.
    #>
    $bashExe = Get-GitBashExe
    if (-not $bashExe) { return $null }
    $tmp = Join-Path $env:TEMP "rea-freshtail-fo-$(Get-Random).log"
    $sf  = Join-Path $env:TEMP "rea-freshtail-fo-$(Get-Random).sh"
    $sb  = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt 100; $i++) { [void]$sb.Append("  UNDATED supervisor echo line $i`n") }
    [System.IO.File]::WriteAllText($tmp, $sb.ToString())

    $h  = Get-RemoteHeredoc
    $i0 = $h.IndexOf('freshtail() {'); $i1 = $h.IndexOf('export -f freshtail')
    $fn = $h.Substring($i0, $i1 - $i0)
    $logBash = $tmp -replace '\\', '/'
    [System.IO.File]::WriteAllText($sf,
        ("FRESH_CUTOFF=2026-08-30`n$fn`nfreshtail '$logBash' 60 | grep -c UNDATED`n"))
    $prevEap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        return (& $bashExe ($sf -replace '\\', '/') 2>&1 | Out-String).Trim()
    } finally {
        $ErrorActionPreference = $prevEap
        Remove-Item -Force $tmp, $sf -ErrorAction SilentlyContinue
    }
}

Test-Case 'BEHAVIOURAL: freshtail scans the whole file but never emits ancient leading undated lines' {
    # buildarr.err, measured 2026-09-02: 1,383 lines, only ~40 of them dated.
    # freshlines passes undated lines above the first dated line (a deliberate
    # fail-open law), so `freshlines < file | tail -n 60` returned a long
    # UNDATED traceback from the HEAD of the file, whose ~100-char lines then
    # ate the whole 3000-byte section budget and starved the current dated
    # warnings out of it. Scanning whole is right; emitting whole is not.
    if (-not (Get-GitBashExe)) { Assert-True $true 'Git Bash not installed'; return }
    $r = Invoke-FreshtailProbe
    Assert-Equal '0' $r.AncientKept   'the ancient undated head, outside the window, is NOT emitted'
    Assert-Equal '1' $r.FreshKept     'the current dated line IS emitted'
    Assert-Equal '0' $r.StaleKept     'a stale-dated header is dropped'
    Assert-Equal '0' $r.StaleBodyKept 'and its undated body is dropped WITH it (inheritance survives the window)'
}

Test-Case 'BEHAVIOURAL: freshtail still fails OPEN on a wholly undated file' {
    # bazarr2.log is supervisor echoes with no dates at all, and rides the
    # fail-open path whole BY DESIGN. Bounding the emit must not become a
    # fail-closed filter that silently drops every undated source.
    if (-not (Get-GitBashExe)) { Assert-True $true 'Git Bash not installed'; return }
    Assert-Equal '60' (Invoke-FreshtailFailOpenProbe) 'a fully undated file emits its whole window'
}


Test-Case 'BEHAVIOURAL: freshlines drops a traceback whose dated header is outside any EOF-relative window' {
    # Reproduces the 2026-09-02 bazarr2.err geometry exactly: a stale dated
    # header, then >360 lines of undated filler, then the traceback. Under the
    # old `tail -n 360` pre-window awk opened mid-filler at keep=1 and shipped
    # the traceback as current. Reading the file whole, the header governs it.
    if (-not (Get-GitBashExe)) { Assert-True $true 'Git Bash not installed - the shape pins above still cover this'; return }
    Assert-Equal '0' (Invoke-FreshlinesProbe -HeaderDate '2026-08-18') 'stale traceback dropped, header 400+ lines up'
}

Test-Case 'BEHAVIOURAL: freshlines keeps the same traceback when its header is fresh' {
    # The mirror assertion. A filter that dropped everything would pass the test
    # above; this is what stops that from being the fix.
    if (-not (Get-GitBashExe)) { Assert-True $true 'Git Bash not installed'; return }
    Assert-Equal '1' (Invoke-FreshlinesProbe -HeaderDate '2026-09-02') 'a CURRENT traceback still ships'
}

# ---------------------------------------------------------------------------
# 2026-09-02: the ownership gate.
#
# Operator verdict that produced it: "if I'm getting pinged 40+ times that's
# just noise, not a useful alert, therefore it's not fulfilling its function."
# Measured 45 pages / 24h, 2 of them real. The largest single family (14) was
# one managed app logging that it could not reach ANOTHER managed app - always
# a duplicate of a monitor that already owns that target, in both directions.
#
# Every excerpt below is VERBATIM from the 2026-09-02 collector fetch or the
# live logs, not invented.
# ---------------------------------------------------------------------------

function New-ConnFinding { param([string]$Excerpt, [string]$App = 'x')
    [pscustomobject]@{ app=$App; file='f'; severity='error'; time='2026-09-02T00:00:00Z'
                       summary='s'; signature='sig'; excerpt=$Excerpt }
}

Test-Case 'the port registry is read from secrets/*.port' {
    $map = Get-StackPortMap
    Assert-True ($map.Count -ge 10) 'registry populated from the real secrets dir'
    Assert-Equal 'plex'     $map['17025'] 'plex port mapped'
    Assert-Equal 'sonarr2'  $map['17003'] 'sonarr2 port mapped'
    Assert-Equal 'postgres' $map['42009'] 'postgres port mapped'
}

Test-Case 'Get-ConnectionTargetPort reads all three shapes this stack emits' {
    Assert-Equal '17003' (Get-ConnectionTargetPort "urllib3.exceptions.NewConnectionError: HTTPConnection(host='127.0.0.1', port=17003): Failed to establish a new connection: [Errno 111] Connection refused") 'urllib3 port= form'
    Assert-Equal '42009' (Get-ConnectionTargetPort 'manager.go:431: error fetching campaigns: dial tcp 127.0.0.1:42009: connect: connection refused') 'go dial tcp form'
    Assert-Equal '17025' (Get-ConnectionTargetPort '[error][Plex Scan]: Scan interrupted {"errorMessage":"connect ECONNREFUSED 172.17.0.1:17025"}') 'node ECONNREFUSED form'
    Assert-Equal ''      (Get-ConnectionTargetPort 'no port anywhere in this line') 'no port -> empty'
}

Test-Case 'the four real 2026-09-02 connectivity families are all held' {
    # bazarr2 -> sonarr2
    Assert-Equal 'monitor-owns-target:sonarr2' (Test-IsOwnedByAMonitor (New-ConnFinding "urllib3.exceptions.MaxRetryError: HTTPConnectionPool(host='127.0.0.1', port=17003): Max retries exceeded with url: /sonarr2/signalr/messages/negotiate" 'bazarr2')) 'bazarr2 -> sonarr2 held'
    # tautulli -> plex
    Assert-Equal 'monitor-owns-target:plex' (Test-IsOwnedByAMonitor (New-ConnFinding "Failed to access uri endpoint /status/sessions. Connection error: HTTPConnectionPool(host='172.17.0.1', port=17025): Max retries exceeded with url: /status/sessions (Caused by NewConnectionError(""HTTPConnection(host='172.17.0.1', port=17025): Failed to establish a new connection: [Errno 111] Connection refused""))" 'tautulli')) 'tautulli -> plex held'
    # stream-stats -> plex
    Assert-Equal 'monitor-owns-target:plex' (Test-IsOwnedByAMonitor (New-ConnFinding "requests.exceptions.ConnectionError: HTTPConnectionPool(host='127.0.0.1', port=17025): Max retries exceeded with url: / (Caused by NewConnectionError(""HTTPConnection(host='127.0.0.1', port=17025): Failed to establish a new connection: [Errno 111] Connection refused""))" 'stream-stats')) 'stream-stats -> plex held'
    # listmonk -> postgres
    Assert-Equal 'monitor-owns-target:postgres' (Test-IsOwnedByAMonitor (New-ConnFinding 'manager.go:431: error fetching campaigns: dial tcp 127.0.0.1:42009: connect: connection refused' 'listmonk')) 'listmonk -> postgres held'
    # seerr -> plex
    Assert-Equal 'monitor-owns-target:plex' (Test-IsOwnedByAMonitor (New-ConnFinding '[error][Plex Scan]: Scan interrupted {"errorMessage":"connect ECONNREFUSED 172.17.0.1:17025"}' 'seerr')) 'seerr -> plex held'
}

Test-Case 'the gate does NOT eat the real fault it sat next to' {
    # THE finding that mattered on 2026-09-02. No transport failure, no port -
    # it must survive, or the whole exercise made the alerting worse.
    Assert-Equal $null (Test-IsOwnedByAMonitor (New-ConnFinding 'INFO lib.notify: alert sent: [error] X plex could not be started after 3 attempts - operator needed' 'maint-pusher')) 'plex could-not-start still pages'
}

Test-Case 'an UNMANAGED target still pages - that is what REA is actually for' {
    Assert-Equal $null (Test-IsOwnedByAMonitor (New-ConnFinding "requests.exceptions.ConnectionError: HTTPConnectionPool(host='api.themoviedb.org', port=443): Max retries exceeded" 'radarr')) 'external API not in the port registry -> pages'
    Assert-Equal $null (Test-IsOwnedByAMonitor (New-ConnFinding "HTTPConnectionPool(host='127.0.0.1', port=39999): Failed to establish a new connection: [Errno 111] Connection refused" 'x')) 'unregistered local port -> pages'
}

Test-Case 'an app-level error that merely MENTIONS a managed port is not a transport failure' {
    # The gate keys on the transport giving up, never on a port appearing.
    Assert-Equal $null (Test-IsOwnedByAMonitor (New-ConnFinding 'Plex returned HTTP 500 from http://127.0.0.1:17025/library/sections while scanning' 'seerr')) 'a 500 from a LIVE plex is a real fault, not a connection failure'
    Assert-Equal $null (Test-IsOwnedByAMonitor (New-ConnFinding 'EDQUOT: disk quota exceeded writing to port 17025 log' 'plex')) 'quota failure survives'
}

Test-Case 'the gate is EXCERPT-scoped: model prose cannot mute a finding' {
    # Same law as the 2026-08-16 rules. A speculative summary must never be able
    # to suppress a finding whose real evidence is something else.
    $f = [pscustomobject]@{ app='x'; file='f'; severity='error'; time='t'
                            summary="probably just connection refused on port=17025 again"
                            signature='connection-refused'; excerpt='EDQUOT: disk quota exceeded' }
    Assert-Equal $null (Test-IsOwnedByAMonitor $f) 'prose-only match does not suppress an EDQUOT excerpt'
}

Test-Case 'the gate FAILS OPEN when it cannot judge' {
    Assert-Equal $null (Test-IsOwnedByAMonitor $null) 'null finding -> pages'
    $noExcerpt = [pscustomobject]@{ app='x'; file='f'; severity='error'; time='t'
                                    summary='s'; signature='s'; excerpt='' }
    Assert-Equal $null (Test-IsOwnedByAMonitor $noExcerpt) 'empty excerpt -> pages'
}

Test-Case 'the ownership gate runs as ENFORCEMENT in the filter loop, not as advice' {
    $src = Get-Content -Raw -LiteralPath (Join-Path (Get-RepoRoot) 'scripts/local-llm/qflix-rea.ps1')
    Assert-True ($src.Contains('$ownedBy = Test-IsOwnedByAMonitor $h')) 'called in the per-finding loop'
    Assert-True ($src -match '\$ownedBy\) \{ \$suppressed \+= \$ownedBy; continue \}') 'a held finding is counted in the suppressed list, never silent'
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

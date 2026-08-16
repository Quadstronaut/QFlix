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
    Assert-True ($h -match 'grep -vE "\^\[\[:space:\]\]\+at "') 'express stack continuations stripped'
    Assert-True ($h -match 'Tdarr_Server_Log\.txt')            'timestamped server log included'
    Assert-True ($h -match 'Tdarr_Node_Log\.txt')              'timestamped node log included'
    Assert-True ($h -match 'uniq -c')                          'repeated faults collapsed, not truncated away'
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
    # Bazarr: inheritance awk over a 3x pre-window, trimmed to 120 AFTER, so a
    # stale header just above the 120 cut still sheds its body. Tdarr: NO
    # inheritance - it runs post-grep where adjacent lines are weeks apart and
    # the only undated lines are interleave-corrupted REAL errors that must
    # pass (fail open), not inherit a stale verdict (fail closed).
    $bazarrSite = 'tail -n 360 "$f" | awk -v c="$FRESH_CUTOFF" "BEGIN{keep=1} { d=substr(\$0,1,10); if (d ~ /^[0-9]{4}-/) keep=(d >= c); if (keep) print }"'
    $tdarrSite  = 'awk -v c="$FRESH_CUTOFF" "{ d=substr(\$0,2,10); if (d ~ /^[0-9]{4}-/ && d < c) next; print }"'
    Assert-True ($h.Contains($bazarrSite)) 'bazarr filter verbatim (360 pre-window, col-1 offset, inheritance)'
    Assert-True ($h.Contains($tdarrSite))  'tdarr filter verbatim (col-2 offset, plain fail-open)'
    # arr Error/Fatal grep (2026-08-15: expired July indexer-backoff line paged
    # from a sparse-error file - whole-file grep needs the same date floor).
    $arrSite = 'grep -aE "\|(Error|Fatal)\|" "$f" | awk -v c="$FRESH_CUTOFF" "{ d=substr(\$0,1,10); if (d ~ /^[0-9]{4}-/ && d < c) next; print }" | tail -n 8'
    Assert-True ($h.Contains($arrSite)) 'arr Error/Fatal filter verbatim, before tail -8'
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
    $cfgSite = 'tail -n 180 "$f" | awk -v c="$FRESH_CUTOFF" "BEGIN{keep=1} { d=substr($0,1,10); if (d ~ /^[0-9]{4}-/) keep=(d >= c); if (keep) print }" | tail -n 60'
    Assert-True ($h.Contains($cfgSite.Replace('$0','\$0'))) 'config_sync filter verbatim (inheritance, 180 pre-window, trim to 60 after)'
    Assert-False ($h.Contains("collect config_sync bash -c 'tailfresh")) 'config_sync no longer ships a raw tailfresh window'
    Assert-True ($h -match 'BEGIN\{keep=1\}[\s\S]{0,120}\n\s*\| tail -n 120 \\\n') 'bazarr trims to 120 AFTER the filter'
    # Tdarr ordering: the filter must sit BETWEEN the [ERROR] grep and
    # tail -n 400, so stale lines cannot eat the 400-line window.
    Assert-True ($h -match 'grep -a "\\\[ERROR\\\]" "\$f"[\s\S]*?awk -v c="\$FRESH_CUTOFF"[\s\S]*?tail -n 400') 'tdarr filter before the 400-line window'
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

Test-Case 'system prompt carries the 2026-08-16 classes with their still-report carve-outs' {
    $sp = Get-SystemPrompt
    Assert-True ($sp.Contains('Binary test N: handbrakePath not working')) 'handbrake clause present'
    Assert-True ($sp.Contains('ffmpegPath not working')) 'ffmpeg carve-out present'
    Assert-True ($sp.Contains('UNIQUE constraint failed: media.tvdbId')) 'seerr clause present'
    Assert-True ($sp -match '(?i)other seerr sqlite_constraint') 'other-column carve-out present'
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

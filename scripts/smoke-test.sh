#!/usr/bin/env bash
# Phase 14: Manitoba smoke test. Idempotent. Exits non-zero on critical failures.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/ssh.sh"
source "$HERE/lib/log.sh"
source "$HERE/lib/secrets.sh"

PASS=0; FAIL=0; SKIP=0
record() {
  local name="$1" status="$2" detail="${3:-}"
  case "$status" in
    pass) printf '✓ %-38s %s\n' "$name" "$detail"; PASS=$((PASS+1)) ;;
    fail) printf '✗ %-38s %s\n' "$name" "$detail"; FAIL=$((FAIL+1)) ;;
    skip) printf '~ %-38s %s\n' "$name" "$detail"; SKIP=$((SKIP+1)) ;;
  esac
}

# 1. Prowlarr indexer count + at least 80% test pass-rate (already audited in Phase 3)
echo "1. Prowlarr indexers + reachability"
PROW_KEY="$(secret_read prowlarr.key)"
PROW_PORT="$(secret_read prowlarr.port)"
PROW_BASE="$(secret_read prowlarr.urlbase 2>/dev/null || echo prowlarr)"
COUNT=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $PROW_KEY' http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/indexer 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
if [ "${COUNT:-0}" -ge 5 ]; then
  record "indexer-count" pass "$COUNT indexers in Prowlarr"
else
  record "indexer-count" fail "only $COUNT indexers"
fi

# 2. Indexer search round-trip
echo "2. Indexer search"
# Retry once — Prowlarr search occasionally times out under load even though the next call is fast.
for attempt in 1 2; do
  RESULTS=$(sshm "curl -sf -m 120 -H 'X-Api-Key: $PROW_KEY' 'http://127.0.0.1:$PROW_PORT/$PROW_BASE/api/v1/search?query=ubuntu' 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
  [ "${RESULTS:-0}" -ge 1 ] && break
done
if [ "${RESULTS:-0}" -ge 1 ]; then
  record "indexer-search" pass "$RESULTS results for ubuntu"
else
  record "indexer-search" fail "0 results after retry"
fi

# 3. *arr → qBit reachability (testall endpoint)
echo "3. *arr -> qBit"
for app in sonarr radarr sonarr2 radarr2; do
  KEY=$(secret_read $app.key 2>/dev/null || echo "")
  PORT=$(secret_read $app.port 2>/dev/null || echo "")
  BASE=$(secret_read $app.urlbase 2>/dev/null || echo $app)
  if [ -z "$KEY" ] || [ -z "$PORT" ]; then
    record "$app-qbit" skip "no key/port"
    continue
  fi
  CODE=$(sshm "curl -s -m 15 -X POST -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/v3/downloadclient/testall -d '{}' -H 'Content-Type: application/json' -o /dev/null -w '%{http_code}'")
  if [ "$CODE" = "200" ]; then
    record "$app-qbit" pass
  else
    record "$app-qbit" fail "HTTP $CODE"
  fi
done

# 3b. Bazarr2 — bare-Python install, anime *arr pair
echo "3b. bazarr2"
B2_KEY=$(secret_read bazarr2.key 2>/dev/null || echo "")
B2_PORT=$(secret_read bazarr2.port 2>/dev/null || echo "")
if [ -n "$B2_KEY" ] && [ -n "$B2_PORT" ]; then
  CODE=$(sshm "curl -s -m 10 -o /dev/null -w '%{http_code}' -H 'X-API-KEY: $B2_KEY' http://127.0.0.1:$B2_PORT/bazarr2/api/system/status")
  if [ "$CODE" = "200" ]; then
    record "bazarr2-api" pass "127.0.0.1:$B2_PORT/bazarr2"
  else
    record "bazarr2-api" fail "HTTP $CODE"
  fi
  B2_ACTIVE=$(sshm "systemctl --user is-active bazarr2.service 2>/dev/null" || echo "unknown")
  if [ "$B2_ACTIVE" = "active" ]; then
    record "bazarr2-service" pass
  else
    record "bazarr2-service" fail "state=$B2_ACTIVE"
  fi
  # bazarr2-sync timer scheduled + last run not failed.
  B2S_TIMER=$(sshm "systemctl --user list-timers bazarr2-sync.timer --no-pager 2>/dev/null | grep -c bazarr2-sync.timer" 2>/dev/null)
  if [ "${B2S_TIMER:-0}" -ge 1 ]; then
    record "bazarr2-sync-timer" pass "scheduled hourly"
  else
    record "bazarr2-sync-timer" fail "timer not scheduled"
  fi
  B2S_RESULT=$(sshm "systemctl --user show bazarr2-sync.service -p Result --value 2>/dev/null" 2>/dev/null)
  case "$B2S_RESULT" in
    success|"") record "bazarr2-sync-last-run" pass "result=${B2S_RESULT:-pending}" ;;
    *) record "bazarr2-sync-last-run" fail "result=$B2S_RESULT" ;;
  esac
else
  record "bazarr2-api" skip "no bazarr2.{key,port} secrets"
  record "bazarr2-service" skip "no bazarr2.{key,port} secrets"
  record "bazarr2-sync-timer" skip "no bazarr2.{key,port} secrets"
  record "bazarr2-sync-last-run" skip "no bazarr2.{key,port} secrets"
fi

# 5. Hardlink sanity
echo "5. Hardlinks"
# Sample library files and report the ACTUAL linkcounts.
#
# The old version counted files whose linkcount starts with '2' and, on zero,
# recorded skip "no .mkv samples in ~/media/Movies". That message was simply
# false: measured 2026-07-30 there are 800 GB of Movies and 50+ .mkv files, all
# with linkcount 1. It was reporting "no samples" when what it meant was "no
# sample had two links" -- a real result disguised as an absent one.
#
# And linkcount 1 is the CORRECT steady state now. A library file has two links
# only while its torrent is still seeding; qflix-torrent-janitor purges
# completed *arr-untracked torrents daily and qBit currently tracks 0, so the
# library copy is legitimately the only remaining link. Content arriving over
# Usenet never has a torrent link at all.
#
# So this cannot assert linkcount>=2 from the library side. The real guard is
# the hardlink-integrity canary, which enumerates from the QBIT side (completed
# torrent -> check the library inode) and therefore only asserts the invariant
# where it actually has to hold. This gate now reports honestly and defers.
HL_SAMPLE=$(sshm "find ~/media/Movies -type f -name '*.mkv' 2>/dev/null | head -5 | xargs -I{} stat -c '%h' {} 2>/dev/null | tr '\n' ' '" 2>/dev/null)
HL_N=$(printf '%s' "$HL_SAMPLE" | wc -w | tr -d ' ')
HL_LINKED=$(printf '%s' "$HL_SAMPLE" | tr ' ' '\n' | grep -c '^[2-9]' 2>/dev/null || true)
HL_LINKED=${HL_LINKED:-0}   # grep -c exits 1 on no match; '|| echo 0' would CONCATENATE
if [ "${HL_N:-0}" -eq 0 ]; then
  record "hardlink-sanity" fail "no .mkv files found under ~/media/Movies at all — library missing or path wrong"
elif [ "${HL_LINKED:-0}" -ge 1 ]; then
  record "hardlink-sanity" pass "$HL_LINKED/$HL_N sampled files still share an inode with a seeding torrent"
else
  record "hardlink-sanity" pass "$HL_N sampled, all linkcount 1 — expected with 0 torrents tracked; hardlink-integrity canary owns the real assertion"
fi

# 6. Disk quota (best-effort — find the largest mount under home)
echo "6. Disk usage"
USAGE=$(sshm "df -h ~ | tail -1 | awk '{print \$3 \" / \" \$2 \" (\" \$5 \" used)\"}'")
record "disk-usage" pass "$USAGE"

# 7. Landing page - the public root serves the QFlix Dashboard directly, AND the
# shell it serves is internally consistent.
#
# 200 + the `data-qflix-dash` marker is NOT sufficient, and believing it was cost
# ~22h of customer-visible outage on 2026-07-29. That marker lives in the
# server-RENDERED html, so it survives a total hydration failure. A fresh build/
# was shipped over a live node process; adapter-node hands static files to sirv,
# which snapshots its file manifest ONCE at process start, so 6 of the 10
# /_app/immutable modules the shell referenced returned 404 while every file sat
# on disk at mode 644 - and the document itself advertised the Content-Length
# sirv computed at boot, so browsers aborted with ERR_CONTENT_LENGTH_MISMATCH and
# never reached domcontentloaded. This gate stayed green throughout.
#
# So it now asserts the same invariant as the dash-asset-integrity canary (every
# 15 min, "Canary Dash Asset Integrity"): every asset the shell references must
# resolve from the process that served the shell, and the advertised byte count
# must match the bytes delivered. Folded into the single `landing-page` record on
# purpose - one customer-visible surface, one gate, and the detail string carries
# the diagnosis; a second record would only drift the hand-maintained smoke
# badge. Homarr was decommissioned 2026-07-13; mobile-ux owns root reachability
# and mobile page weight on this same surface, this owns hydratability.
echo "7. Landing page"
PUBLIC_HOST=$(secret_read seedbox.host 2>/dev/null || echo "quadstronaut.seedbox.example.com")
LP_HDR=$(mktemp); LP_BODY=$(mktemp)
# NOTE `|| echo 000` would CONCATENATE here: on a mid-transfer failure curl
# prints the status it received AND the fallback appends, so a truncated 200
# becomes "200000" and the gate blames the wrong thing. Capture the exit status
# separately and normalise a non-numeric result to 000.
ROOT_CODE=$(curl -sk -L -m 20 -D "$LP_HDR" -o "$LP_BODY" -w "%{http_code}" "https://${PUBLIC_HOST}/" 2>/dev/null)
ROOT_RC=$?
case "$ROOT_CODE" in '' | *[!0-9]*) ROOT_CODE=000 ;; esac
HITS=$(grep -c "data-qflix-dash" "$LP_BODY" 2>/dev/null || true)
# Content-Length agreement (the ERR_CONTENT_LENGTH_MISMATCH leg). Independent of
# the 404 sweep: a file rewritten in place at a path sirv DID index at boot still
# returns 200, just under a stale length. An absent header (chunked / encoded, or
# root unreachable) is inconclusive, not a failure.
LP_ADV=$(tr -d '\r' < "$LP_HDR" 2>/dev/null | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)
LP_GOT=$(wc -c < "$LP_BODY" 2>/dev/null | tr -d ' ')
LP_CL_OK=1; LP_CL="skipped(no-header)"
if [ -n "${LP_ADV:-}" ]; then
  if [ "$LP_ADV" = "${LP_GOT:-0}" ]; then
    LP_CL="agrees"
  else
    LP_CL_OK=0; LP_CL="MISMATCH(adv=$LP_ADV got=${LP_GOT:-0})"
  fi
elif [ "${ROOT_CODE}" = "200" ] && [ "${ROOT_RC:-0}" != "0" ]; then
  # 200 with no Content-Length AND a broken transfer: the document did not
  # arrive intact and there is no header to arbitrate with. Not a free pass.
  LP_CL_OK=0; LP_CL="TRANSFER-FAILED(curl-rc=${ROOT_RC})"
fi
# Asset resolvability (the 404-while-present-on-disk leg). Zero references is
# itself a failure - it means what came back was not a SvelteKit shell.
LP_REFS=$(grep -oE '/_app/immutable/[A-Za-z0-9._/-]+' "$LP_BODY" 2>/dev/null | sort -u)
LP_N=$(printf '%s\n' "$LP_REFS" | grep -c . || true)
LP_BAD=0
for ref in $LP_REFS; do
  RC=$(curl -sk -m 12 -o /dev/null -w "%{http_code}" "https://${PUBLIC_HOST}${ref}" 2>/dev/null)
  ARC=$?
  case "$RC" in '' | *[!0-9]*) RC=000 ;; esac
  # A 200 whose transfer broke mid-body counts as bad: the browser cannot
  # execute a module it never fully received.
  { [ "$RC" = "200" ] && [ "$ARC" = "0" ]; } || LP_BAD=$((LP_BAD+1))
done
rm -f "$LP_HDR" "$LP_BODY"
if [ "${ROOT_CODE}" = "200" ] && [ "${HITS:-0}" -ge 1 ] && [ "${LP_N:-0}" -ge 1 ] \
   && [ "${LP_BAD:-0}" -eq 0 ] && [ "${LP_CL_OK}" -eq 1 ]; then
  record "landing-page" pass "/ -> QFlix Dashboard (200, $HITS marker hits, ${LP_N}/${LP_N} assets 200, content-length $LP_CL)"
else
  record "landing-page" fail "dead or inconsistent shell (code=$ROOT_CODE marker=${HITS:-0} assets_404=${LP_BAD}/${LP_N:-0} content-length=$LP_CL) - assets 404 while present under ~/.apps/qflix-dash/build/client means a stale in-process sirv manifest: systemctl --user restart qflix-dash"
fi

# 8. Unpackerr daemon
echo "8. Unpackerr"
# Match `/unpackerr` (upstream golift image entrypoint, post-2026-05 migration);
# also matches the legacy Ultra.cc `/app/unpackerr` path, which ends in it.
UNPACK=$(sshm "ps -ef | grep '/unpackerr' | grep -v grep | wc -l")
if [ "${UNPACK:-0}" -ge 1 ]; then
  record "unpackerr-running" pass
else
  record "unpackerr-running" fail "no daemon process"
fi

# 9 + 10 (prune-cron dry-run, library-rescan helper smoke): RETIRED 2026-08-16.
# Both existed only to exercise the books stack. The four reader apps were
# decommissioned that day and the scripts under test (prune-text-libraries.sh,
# library-rescan-*.sh) were deleted with them, so these checks could only ever
# have failed. Section numbers are left with a gap, as 11 already is.

# 12. qBittorrent health + seeding count
echo "12. qBittorrent"
# Assert the API is REACHABLE AND AUTHENTICATED, not that it is populated.
#
# This used to fail whenever the torrent count was 0, which conflates two very
# different states: "qBittorrent is broken" and "nothing happens to be seeding
# right now". Zero is a legitimate steady state here -- qflix-torrent-janitor
# purges completed *arr-untracked leftovers daily, and content now also arrives
# over Usenet via SABnzbd, so the torrent pool can drain to empty with the stack
# perfectly healthy. The hardlink-integrity canary was taught to tolerate an
# empty completed pool in 1f37f9c; this gate was not, so it stayed a standing
# false red. A check that cries wolf trains you to ignore it.
#
# Login response is the discriminator: 'Ok.' means the API answered and the
# credentials work. Anything else is a real fault and still fails.
QOUT=$(sshm "C=\$(mktemp)
L=\$(curl -sS -m 15 -c \$C --data-urlencode 'username=$(secret_read qbittorrent.user)' --data-urlencode 'password=$(secret_read qbittorrent.password)' http://127.0.0.1:17041/api/v2/auth/login 2>/dev/null)
N=\$(curl -sS -m 15 -b \$C 'http://127.0.0.1:17041/api/v2/sync/maindata' 2>/dev/null | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin); print(len(d.get(\"torrents\",{})))
except Exception:
    print(\"ERR\")' 2>/dev/null)
echo \"\$L|\$N\"" 2>/dev/null)
QLOGIN=${QOUT%%|*}
QC=${QOUT##*|}
if [ "$QLOGIN" != "Ok." ]; then
  record "qbit-torrents" fail "auth failed (login='${QLOGIN:-no response}') — API unreachable or credentials wrong"
elif [ "$QC" = "ERR" ] || [ -z "$QC" ]; then
  record "qbit-torrents" fail "authenticated but maindata unreadable"
elif [ "${QC:-0}" -ge 1 ]; then
  record "qbit-torrents" pass "$QC torrents tracked"
else
  record "qbit-torrents" pass "API up + authenticated; 0 torrents (empty pool is legitimate — janitor purges, Usenet path active)"
fi

# 13b. qflix-newsletter (replaces Conjurr+Newsletterr 2026-05-10) — timer scheduled + last run
echo "13b. qflix-newsletter"
QN_TIMER=$(sshm "systemctl --user list-timers qflix-newsletter.timer --no-pager 2>/dev/null | grep -c qflix-newsletter.timer" 2>/dev/null)
if [ "${QN_TIMER:-0}" -ge 1 ]; then
  record "qflix-newsletter-timer" pass "scheduled (Mon 08:00)"
else
  record "qflix-newsletter-timer" fail "timer not scheduled"
fi
QN_DRY=$(sshm 'cd ~/.apps/qflix-newsletter && .venv/bin/python -m qflix_newsletter --dry-run 2>&1 | tail -1' 2>/dev/null)
if echo "$QN_DRY" | grep -q "dry-run: subject="; then
  record "qflix-newsletter-renders" pass "$(echo "$QN_DRY" | sed 's/.*\(subject=.*\)/\1/' | head -c 80)"
else
  record "qflix-newsletter-renders" fail "$(echo "$QN_DRY" | tail -c 100)"
fi

# 13c. Buildarr (cron-class — timer scheduled, no UI)
echo "13c. Buildarr"
BA_TIMER=$(sshm "systemctl --user list-timers buildarr.timer --no-pager 2>/dev/null | grep -c buildarr.timer" 2>/dev/null)
if [ "${BA_TIMER:-0}" -ge 1 ]; then
  record "buildarr-timer" pass "scheduled (nightly 04:30)"
else
  record "buildarr-timer" fail "timer not scheduled"
fi
BA_VER=$(sshm "~/.apps/buildarr/.venv/bin/pip show buildarr 2>/dev/null | grep '^Version:' | awk '{print \$2}'" 2>/dev/null)
if [ -n "$BA_VER" ]; then
  record "buildarr-installed" pass "v$BA_VER"
else
  record "buildarr-installed" fail "venv missing or buildarr not installed"
fi
# Buildarr patch sentinel: each patch carries a `QFlix patch 2026-05-11`
# marker in the modified venv file. If a pip upgrade silently overwrote the
# patched files, markers disappear and the next 04:30 buildarr run fails.
# This check fires immediately on smoke instead of waiting for the next
# nightly to go red.
BA_PATCHES=$(sshm "grep -lr 'QFlix patch 2026-05-11' ~/.apps/buildarr/.venv/lib/ 2>/dev/null | wc -l" 2>/dev/null)
if [ "${BA_PATCHES:-0}" -ge 5 ]; then
  record "buildarr-patches-applied" pass "$BA_PATCHES files carry QFlix patch marker"
elif [ "${BA_PATCHES:-0}" -ge 1 ]; then
  record "buildarr-patches-applied" fail "only $BA_PATCHES patched files (expected 5+); re-run 60-buildarr-patches.sh"
else
  record "buildarr-patches-applied" fail "0 patched files — venv reset or never patched; run 60-buildarr-patches.sh"
fi

# 13g. python-plexapi venv healthy
echo "13g. python-plexapi venv"
PV=$(sshm "~/.apps/python-plexapi/venv/bin/python -c 'import plexapi; print(plexapi.VERSION)' 2>/dev/null" 2>/dev/null)
if [ -n "$PV" ]; then
  record "plexapi-venv" pass "$PV"
else
  record "plexapi-venv" fail "missing or broken"
fi

# 13h. stream-stats JSON is current and valid
echo "13h. stream-stats JSON"
SS_AGE=$(sshm "stat -c %Y ~/.apps/stream-stats/state.json 2>/dev/null || echo 0" 2>/dev/null)
NOW=$(date +%s)
if [ -n "$SS_AGE" ] && [ "$SS_AGE" -gt 0 ] && [ $((NOW - SS_AGE)) -lt 180 ]; then
  record "stream-stats-fresh" pass "$((NOW - SS_AGE))s ago"
else
  record "stream-stats-fresh" fail "stale or missing (${SS_AGE:-?})"
fi

# 13i. Upgradinatorr timer scheduled
echo "13i. Upgradinatorr timer"
UT=$(sshm "systemctl --user list-timers upgradinatorr.timer --no-pager 2>/dev/null | grep -c upgradinatorr.timer" 2>/dev/null)
if [ "${UT:-0}" -ge 1 ]; then
  record "upgradinatorr-timer" pass "scheduled"
else
  record "upgradinatorr-timer" fail "timer not scheduled"
fi

# 13m. VictoriaLogs ingest timer scheduled + last run success
# Closes the audit gap: a vlogs-ingest regression (e.g., logs.py import
# crashing silently) would only surface via the 15-min stall canary
# otherwise. This catches it on every smoke run.
echo "13m. VictoriaLogs ingest timer"
VI_TIMER=$(sshm "systemctl --user list-timers qflix-vlogs-ingest.timer --no-pager 2>/dev/null | grep -c qflix-vlogs-ingest" 2>/dev/null)
if [ "${VI_TIMER:-0}" -ge 1 ]; then
  record "vlogs-ingest-timer" pass "scheduled (every 5min)"
else
  record "vlogs-ingest-timer" fail "qflix-vlogs-ingest.timer not scheduled"
fi
VI_RESULT=$(sshm "systemctl --user show qflix-vlogs-ingest.service -p Result --value 2>/dev/null" 2>/dev/null)
case "$VI_RESULT" in
  success|"") record "vlogs-ingest-last-run" pass "result=${VI_RESULT:-pending}" ;;
  *) record "vlogs-ingest-last-run" fail "result=$VI_RESULT" ;;
esac

# 13l. Tdarr server reachable on loopback + node registered
# Tdarr is loopback-only by design (see scripts/configure/50-tdarr-install.sh);
# public nginx proxy is intentionally disabled, so probe 127.0.0.1 directly.
echo "13l. Tdarr"
TD_PORT=$(secret_read tdarr.server_port 2>/dev/null || echo "")
if [ -n "$TD_PORT" ]; then
  TD_HTTP=$(sshm "curl -s -m 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$TD_PORT/api/v2/status" 2>/dev/null)
  case "$TD_HTTP" in
    200) record "tdarr-up" pass "HTTP $TD_HTTP" ;;
    *)   record "tdarr-up" fail "HTTP $TD_HTTP on 127.0.0.1:$TD_PORT/api/v2/status" ;;
  esac
  TD_NODES=$(sshm "curl -sf -m 5 http://127.0.0.1:$TD_PORT/api/v2/get-nodes 2>/dev/null | python3 -c 'import sys,json; print(len(json.load(sys.stdin)))'" 2>/dev/null)
  if [ "${TD_NODES:-0}" -ge 1 ]; then
    record "tdarr-node-registered" pass "$TD_NODES node(s)"
  else
    record "tdarr-node-registered" fail "no nodes registered"
  fi
  # Flow engaged: exactly 3 libraries, all with flowId=qflix-direct-play-fix
  # and decisionMaker.settingsFlows=true (so the Flow engine — not the classic
  # plugin stack — drives transcode decisions on every new arrival).
  TD_FLOW=$(sshm "curl -sf -m 5 -X POST http://127.0.0.1:$TD_PORT/api/v2/cruddb -H 'Content-Type: application/json' -d '{\"data\":{\"collection\":\"LibrarySettingsJSONDB\",\"mode\":\"getAll\"}}' 2>/dev/null | python3 -c 'import sys,json; libs=json.load(sys.stdin); ok=[l for l in libs if l.get(\"flowId\")==\"qflix-direct-play-fix\" and (l.get(\"decisionMaker\") or {}).get(\"settingsFlows\") is True]; print(str(len(ok))+\"/\"+str(len(libs)))'" 2>/dev/null)
  case "$TD_FLOW" in
    3/3) record "tdarr-flow-engaged" pass "$TD_FLOW libs attached to qflix-direct-play-fix" ;;
    *)   record "tdarr-flow-engaged" fail "$TD_FLOW libs attached (expected 3/3)" ;;
  esac
  # Quiet-hours timers armed (18:00-23:00 UTC pause to spare streaming users).
  TD_QH=$(sshm "systemctl --user list-timers tdarr-node-pause.timer tdarr-node-resume.timer --no-pager 2>/dev/null | grep -cE 'tdarr-node-(pause|resume).timer'" 2>/dev/null)
  if [ "${TD_QH:-0}" -ge 2 ]; then
    record "tdarr-quiet-hours-armed" pass "pause+resume timers loaded"
  else
    record "tdarr-quiet-hours-armed" fail "expected 2 timers, found ${TD_QH:-0}"
  fi
  # worker1.js cleanup-handler null-guard patch — without this, Tdarr 2.17.01
  # crashes the entire node on every job completion (TypeError: worker2[T(...)]
  # is not a function inside the Exit handler).  Patch marker is injected by
  # 50-tdarr-install.sh; absence means a fresh unzip needs re-patching.
  TD_PATCH=$(sshm "grep -c QFLIX-WORKER2-EXIT-NULLGUARD ~/.apps/tdarr/Tdarr_Node/srcug/workers/worker1.js 2>/dev/null" 2>/dev/null)
  if [ "${TD_PATCH:-0}" -ge 1 ]; then
    record "tdarr-worker-patch" pass "worker1.js exit-handler null-guard applied"
  else
    record "tdarr-worker-patch" fail "patch missing — re-run 50-tdarr-install.sh"
  fi
else
  record "tdarr-up" skip "no server_port"
  record "tdarr-node-registered" skip "no server_port"
  record "tdarr-flow-engaged" skip "no server_port"
  record "tdarr-quiet-hours-armed" skip "no server_port"
  record "tdarr-worker-patch" skip "no server_port"
fi

# 13j. Kometa timer scheduled
echo "13j. Kometa timer"
KT=$(sshm "systemctl --user list-timers kometa.timer --no-pager 2>/dev/null | grep -c kometa.timer" 2>/dev/null)
if [ "${KT:-0}" -ge 1 ]; then
  record "kometa-timer" pass "scheduled"
else
  record "kometa-timer" fail "timer not scheduled"
fi

# 13k. Kometa last run result
echo "13k. Kometa last run"
KR=$(sshm "systemctl --user show kometa.service -p Result --value 2>/dev/null" 2>/dev/null)
case "$KR" in
  success|"") record "kometa-last-run" pass "result=${KR:-pending}" ;;
  *) record "kometa-last-run" fail "result=$KR" ;;
esac

# 13e. Recyclarr timer scheduled
echo "13e. Recyclarr timer"
RT=$(sshm "systemctl --user list-timers recyclarr.timer --no-pager 2>/dev/null | grep -c recyclarr.timer" 2>/dev/null)
if [ "${RT:-0}" -ge 1 ]; then
  record "recyclarr-timer" pass "scheduled"
else
  record "recyclarr-timer" fail "timer not scheduled"
fi

# 13f. No 4K policy — count any 2160p quality entries marked allowed across *arr instances
echo "13f. Recyclarr no-4k policy"
UHD_COUNT=0
for app in sonarr sonarr2 radarr radarr2; do
  KEY=$(secret_read $app.key 2>/dev/null || echo "")
  PORT=$(secret_read $app.port 2>/dev/null || echo "")
  BASE=$(secret_read $app.urlbase 2>/dev/null || echo $app)
  [ -z "$KEY" ] && continue
  V=v3
  N=$(sshm "curl -sf -m 10 -H 'X-Api-Key: $KEY' http://127.0.0.1:$PORT/$BASE/api/$V/qualityprofile 2>/dev/null | python3 -c 'import sys,json; q=json.load(sys.stdin); print(sum(1 for p in q for i in p.get(\"items\",[]) if i.get(\"allowed\") and \"2160\" in (i.get(\"quality\",{}).get(\"name\",\"\") if isinstance(i.get(\"quality\"),dict) else \"\")))'" 2>/dev/null)
  UHD_COUNT=$((UHD_COUNT + ${N:-0}))
done
if [ "$UHD_COUNT" = 0 ]; then
  record "recyclarr-no-4k" pass "no UHD profiles enabled (per policy)"
else
  record "recyclarr-no-4k" fail "$UHD_COUNT UHD entries found — policy violation"
fi

# 13n. Remux cap policy - live re-read gate for scripts/configure/58-remux-cap-enforce.py
#
# WHY: 58 is a one-shot policy WRITE with no verification surface of its own.
# A UI click, a Radarr upgrade that restores factory profile defaults, or a
# recyclarr sync can silently re-allow Remux-1080p, and the only symptom is a
# member's Plex client failing every movie again (2026-07-25 through
# 2026-08-19: 23 of 46 movies-with-a-file were Remux-1080p, 20-37 Mbps against
# a 1927 kbps transcode-only client). 13f is that same shape for the no-4k
# policy; this is its remux twin.
#
# THE WALK IS RECURSIVE AND THAT IS NOT DECORATION. 13f above walks items[]
# non-recursively; Radarr/Sonarr items[] is a NESTED tree where a group entry
# carries its own items[] list. Measured on the box 2026-08-19: sonarr2 scores
# flat=0 / recursive=1 remux entries - a copy of 13f would report that instance
# clean while it allows a remux tier. The lambda below recurses (an entry with
# a non-empty items[] is a group; anything else is a leaf quality).
#
# SCOPE: radarr main only, matching 58's own SCOPE block. sonarr main and the
# two anime instances knowingly allow remux (recyclarr owns sonarr2's profile),
# so gating them here would be a permanent, meaningless red.
echo "13n. Remux cap policy (radarr main)"
RX_KEY=$(secret_read radarr.key 2>/dev/null || echo "")
RX_PORT=$(secret_read radarr.port 2>/dev/null || echo "")
RX_BASE=$(secret_read radarr.urlbase 2>/dev/null || echo radarr)
if [ -n "$RX_KEY" ] && [ -n "$RX_PORT" ]; then
  RX_OUT=$(sshm "curl -sf -m 15 -H 'X-Api-Key: $RX_KEY' http://127.0.0.1:$RX_PORT/$RX_BASE/api/v3/qualityprofile 2>/dev/null | python3 -c 'import sys,json; w=lambda its:[x for i in (its or []) for x in (w(i.get(\"items\")) if i.get(\"items\") else [i])]; ps=json.load(sys.stdin); bad=sorted(str(p.get(\"id\")) for p in ps if any(i.get(\"allowed\") and \"remux\" in ((i.get(\"quality\") or {}).get(\"name\") or \"\").lower() for i in w(p.get(\"items\")))); print(str(len(bad))+\" \"+(\",\".join(bad) or \"-\"))'" 2>/dev/null)
  RX_N="${RX_OUT%% *}"
  RX_IDS="${RX_OUT#* }"
  if [ -z "$RX_OUT" ]; then
    record "remux-cap-radarr" fail "could not read radarr qualityprofile"
  elif [ "${RX_N:-1}" = 0 ]; then
    record "remux-cap-radarr" pass "no profile allows a remux tier (per policy)"
  else
    record "remux-cap-radarr" fail "$RX_N profile(s) allow remux: $RX_IDS - run scripts/configure/58-remux-cap-enforce.py"
  fi
else
  record "remux-cap-radarr" skip "no radarr key/port"
fi

# 13. Listmonk health + subscribers (mass-comms Phase 19+20)
echo "13. Listmonk"
LM_API_USER=$(secret_read listmonk.api_user 2>/dev/null || echo "")
LM_API_TOKEN=$(secret_read listmonk.api_token 2>/dev/null || echo "")
if [ -n "$LM_API_USER" ] && [ -n "$LM_API_TOKEN" ]; then
  LM_HEALTH=$(curl -sfk -m 5 -u "$LM_API_USER:$LM_API_TOKEN" "https://${PUBLIC_HOST}/listmonk/api/health" 2>/dev/null)
  if echo "$LM_HEALTH" | grep -q '"data":true'; then
    record "listmonk-health" pass
  else
    record "listmonk-health" fail "$LM_HEALTH"
  fi
  LM_SUB=$(curl -sfk -m 10 -u "$LM_API_USER:$LM_API_TOKEN" "https://${PUBLIC_HOST}/listmonk/api/dashboard/counts" 2>/dev/null \
    | python3 -c 'import sys,json; print(json.load(sys.stdin)["data"]["subscribers"]["total"])' 2>/dev/null)
  if [ "${LM_SUB:-0}" -ge 13 ]; then
    record "listmonk-subscribers" pass "$LM_SUB subscribers"
  else
    record "listmonk-subscribers" fail "expected >=13, got $LM_SUB"
  fi
else
  record "listmonk-health" skip "no api_user/api_token"
  record "listmonk-subscribers" skip "no api_user/api_token"
fi

# 14. Maintenance system (Phase 240)
echo "14. Maintenance system"
M_PORT=$(sshm "cat ~/.opt/maint/maintenance.port 2>/dev/null" </dev/null 2>/dev/null)
if [ -n "$M_PORT" ]; then
  M_BODY=$(sshm "curl -sf -m 5 http://127.0.0.1:${M_PORT}/health 2>/dev/null" </dev/null 2>/dev/null || echo "")
  if [ "$M_BODY" = "ok" ]; then
    record "maint-webhook-up" pass "127.0.0.1:$M_PORT body=ok"
  else
    record "maint-webhook-up" fail "body='$M_BODY'"
  fi
  M_TIMER=$(sshm "systemctl --user list-timers manitoba-maint-window.timer --no-pager 2>/dev/null | grep -c manitoba-maint-window.timer" </dev/null 2>/dev/null)
  if [ "${M_TIMER:-0}" -ge 1 ]; then
    record "maint-window-timer" pass "scheduled"
  else
    record "maint-window-timer" fail "timer not scheduled"
  fi
  M_VAL=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint manifest validate 2>&1; echo exit=\$?" </dev/null 2>/dev/null)
  if echo "$M_VAL" | grep -q "exit=0"; then
    record "maint-manifest-valid" pass
  else
    record "maint-manifest-valid" fail "$(echo "$M_VAL" | tail -3 | head -1)"
  fi
  # Pusher service alive
  M_PUSHER=$(sshm "systemctl --user is-active manitoba-maint-pusher.service 2>/dev/null" </dev/null 2>/dev/null || echo "unknown")
  if [ "$M_PUSHER" = "active" ]; then
    record "maint-pusher-up" pass
  else
    record "maint-pusher-up" fail "state=$M_PUSHER"
  fi
  # Kuma drift — manifest's kuma_monitor names should all match live monitors.
  M_AUDIT=$(sshm "MANITOBA_MANIFEST=~/.opt/maint/apps.yaml ~/bin/manitoba-maint kuma audit 2>&1; echo exit=\$?" </dev/null 2>/dev/null)
  if echo "$M_AUDIT" | grep -q "exit=0"; then
    M_MATCH=$(echo "$M_AUDIT" | grep -oP 'matched: \K[0-9]+')
    record "maint-kuma-no-drift" pass "matched=${M_MATCH:-?}"
  else
    record "maint-kuma-no-drift" fail "$(echo "$M_AUDIT" | tail -3 | tr '\n' ' ' | head -c 100)"
  fi
  # All Kuma monitors UP — counts monitor_status{...} lines with value 1.
  K_PORT=$(sshm "cat ~/secrets/uptimekuma.port 2>/dev/null" </dev/null 2>/dev/null)
  K_KEY=$(sshm "cat ~/secrets/uptimekuma.key 2>/dev/null" </dev/null 2>/dev/null)
  if [ -n "$K_PORT" ] && [ -n "$K_KEY" ]; then
    # Pull live monitor_status lines once; build EXTERNAL_GREP from the manifest's
    # kuma_external_monitors so they don't count toward total or up.
    K_METRICS=$(sshm "curl -s -m 5 -u ':$K_KEY' http://127.0.0.1:$K_PORT/metrics 2>/dev/null" </dev/null 2>/dev/null)
    # Build the exclude list: external monitors (from kuma_external_monitors)
    # PLUS monitors for apps marked parked: true (being-down is the intended
    # state for parked apps — Ombi is the canonical example).
    K_EXCLUDE=$(python3 -c '
import os, sys, yaml
candidates = ["manifest/apps.yaml", os.path.expanduser("~/.opt/maint/apps.yaml")]
path = next((p for p in candidates if os.path.isfile(p)), None)
if not path:
    sys.exit(0)
m = yaml.safe_load(open(path))
ext = list(m.get("kuma_external_monitors", []) or [])
parked = [a.get("kuma_monitor") for a in (m.get("apps", {}) or {}).values()
          if a.get("parked") and a.get("kuma_monitor")]
sys.stdout.write("\n".join(ext + parked))
' 2>/dev/null | tr -d "\r")
    if [ -n "$K_EXCLUDE" ]; then
      EXCLUDE_RE=$(printf '%s\n' "$K_EXCLUDE" | sed 's/[^A-Za-z0-9 ]/./g' | paste -sd'|' -)
      K_FILTERED=$(echo "$K_METRICS" | grep '^monitor_status' | grep -Ev "monitor_name=\"($EXCLUDE_RE)\"")
    else
      K_FILTERED=$(echo "$K_METRICS" | grep '^monitor_status')
    fi
    K_TOTAL=$(echo "$K_FILTERED" | grep -c '^monitor_status' 2>/dev/null || echo 0)
    K_UP=$(echo "$K_FILTERED" | grep -cE ' 1(\.0+)?$' 2>/dev/null || echo 0)

    # /metrics is NOT the authoritative monitor set. It omits infrequent
    # self-pushers between beats -- measured 2026-07-30: 61 monitors in /metrics
    # vs 62 active in kuma.db, the missing one being "Canary Newsletter Digest",
    # a weekly Monday canary. That is exactly why `kuma audit` was moved to the
    # DB; this sibling gate was left on /metrics and so had been silently
    # checking 61 of 62 while reporting "all up".
    #
    # The danger is not the quiet weekly pusher, it is that a monitor which
    # VANISHES (deleted, deactivated) is likewise absent from /metrics -- so it
    # is neither counted nor checked, and UP==TOTAL still holds. Cross-check the
    # DB's active set and fail on any DB-active monitor that is DOWN, including
    # ones /metrics never mentioned.
    K_DB_DOWN=$(sshm "python3 -c \"
import sqlite3
c=sqlite3.connect('file:/home/quadstronaut/.apps/uptimekuma/kuma.db?mode=ro',uri=True)
bad=[]
for mid,name in c.execute('select id,name from monitor where active=1'):
    r=c.execute('select status from heartbeat where monitor_id=? order by time desc limit 1',(mid,)).fetchone()
    if r is None or r[0]==0:
        bad.append(name)
print(len(bad));print('|'.join(sorted(bad)))
\" 2>/dev/null" </dev/null 2>/dev/null)
    K_DB_DOWN_N=$(printf '%s' "$K_DB_DOWN" | head -1 | tr -dc '0-9')
    K_DB_DOWN_NAMES=$(printf '%s' "$K_DB_DOWN" | sed -n '2p' | head -c 160)
    K_DB_ACTIVE=$(sshm "python3 -c \"
import sqlite3
c=sqlite3.connect('file:/home/quadstronaut/.apps/uptimekuma/kuma.db?mode=ro',uri=True)
print(c.execute('select count(*) from monitor where active=1').fetchone()[0])
\" 2>/dev/null" </dev/null 2>/dev/null | tr -dc '0-9')

    if [ -n "$K_DB_DOWN_N" ] && [ "$K_DB_DOWN_N" -gt 0 ]; then
      record "maint-kuma-all-up" fail "${K_DB_DOWN_N} DB-active monitor(s) DOWN or never-beat: ${K_DB_DOWN_NAMES}"
    elif [ -z "$K_DB_ACTIVE" ]; then
      record "maint-kuma-all-up" fail "could not read kuma.db to verify the monitor set"
    elif [ "${K_TOTAL:-0}" -ge 1 ] && [ "$K_UP" = "$K_TOTAL" ]; then
      record "maint-kuma-all-up" pass "${K_DB_ACTIVE}/${K_DB_ACTIVE} DB-active monitors UP (/metrics saw $K_UP/$K_TOTAL; quiet self-pushers omitted there)"
    else
      record "maint-kuma-all-up" fail "$K_UP/$K_TOTAL UP in /metrics — see Kuma UI for which are down"
    fi
  else
    record "maint-kuma-all-up" skip "no uptimekuma.{port,key}"
  fi
else
  record "maint-webhook-up" skip "no maintenance.port"
  record "maint-window-timer" skip "no maintenance.port"
  record "maint-manifest-valid" skip "no maintenance.port"
  record "maint-pusher-up" skip "no maintenance.port"
  record "maint-kuma-no-drift" skip "no maintenance.port"
  record "maint-kuma-all-up" skip "no maintenance.port"
fi

# 15. Phase-15 canaries (movie / anime / quota / mobile-ux)
# Spot-checks a representative subset of the 20 live canaries - not all of them.
# (deletion canary retired 2026-06-20 with Maintainerr; swapped to quota.)
echo "15. Phase-15 canaries"
HERE="$(cd "$(dirname "$0")" && pwd)"
for canary in movie anime quota mobile-ux; do
  if bash "$HERE/canaries/$canary.sh" >/dev/null 2>&1; then
    record "canary-$canary" pass
  else
    OUT=$(bash "$HERE/canaries/$canary.sh" 2>&1 | tail -3 | tr '\n' '|')
    record "canary-$canary" fail "$OUT"
  fi
done

# 15b. Canary Kuma push-monitor verification
# For each canary, verify its Kuma Push monitor exists in /metrics with status=1 (up).
# A missing or down monitor means the cron-driven canary path is broken.
echo "15b. Canary Kuma push monitors"
K_PORT=$(sshm "cat ~/secrets/uptimekuma.port 2>/dev/null" </dev/null 2>/dev/null)
K_KEY=$(sshm "cat ~/secrets/uptimekuma.key 2>/dev/null" </dev/null 2>/dev/null)
if [ -n "$K_PORT" ] && [ -n "$K_KEY" ]; then
  K_METRICS=$(sshm "curl -s -m 5 -u ':$K_KEY' http://127.0.0.1:$K_PORT/metrics 2>/dev/null" </dev/null 2>/dev/null)
  for canary_name in "Canary Movie" "Canary Anime" "Canary Quota" "Canary Mobile-UX"; do
    slug=$(echo "$canary_name" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
    # Filter to monitor_status lines specifically — there are multiple
    # Prometheus metrics per monitor (status, response_time, cert_days), and
    # we only want the binary up/down value.
    STATUS=$(echo "$K_METRICS" | grep "^monitor_status" | grep "monitor_name=\"${canary_name}\"" | grep -oE ' [01](\.[0-9]+)?$' | tr -d ' ' | head -1)
    if [ "${STATUS:-}" = "1" ]; then
      record "canary-kuma-${slug}" pass "monitor_status=1 (up)"
    elif [ -z "${STATUS:-}" ]; then
      record "canary-kuma-${slug}" fail "monitor '${canary_name}' not found in Kuma /metrics — needs Kuma bootstrap"
    else
      record "canary-kuma-${slug}" fail "monitor_status=${STATUS} (expected 1=up)"
    fi
  done
else
  for canary_name in movie anime quota mobile-ux; do
    record "canary-kuma-canary-${canary_name}" skip "no uptimekuma.{port,key}"
  done
fi

# 16. Release-tag freshness — reminder when Manitoba's master has drifted
# >30 days from the last cut, so the soak rhythm doesn't atrophy. Customer
# nodes pin to release-* tags (see docs/release-promotion.md); if no fresh
# tags exist, customers can't catch the latest stable.
echo "16. Release-tag freshness"
LAST_TAG=$(git -C "$HERE/.." tag --list 'release-*' --sort=-v:refname 2>/dev/null | head -n1 || true)
if [ -z "$LAST_TAG" ]; then
  record "release-tag-fresh" fail "no release-* tags exist; cut one with scripts/ops/cut-release.sh"
else
  LAST_TAG_TIME=$(git -C "$HERE/.." log -1 --format=%ct "$LAST_TAG" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  AGE_DAYS=$(( (NOW - LAST_TAG_TIME) / 86400 ))
  if [ "$AGE_DAYS" -le 30 ]; then
    record "release-tag-fresh" pass "$LAST_TAG is $AGE_DAYS day(s) old"
  else
    record "release-tag-fresh" fail "$LAST_TAG is $AGE_DAYS day(s) old (>30); cut a new tag"
  fi
fi

# Summary
echo
printf "Total: %d   Pass: %d   Fail: %d   Skip: %d\n" $((PASS+FAIL+SKIP)) "$PASS" "$FAIL" "$SKIP"
[ "$FAIL" = 0 ]

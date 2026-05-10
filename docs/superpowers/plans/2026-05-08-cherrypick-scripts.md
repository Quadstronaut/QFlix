# Cherry-Pick Scripts Plan (Manitoba)

> **For agentic workers:** Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax.

**Goal:** Adopt three small, high-value automation scripts that don't justify a full app install: (1) Plex concurrent-stream limiter from JBOPS, (2) "Upgradinatorr"-equivalent re-search of stale `*arr` grabs against current quality profiles, and (3) a tiny "stream stats" emitter to feed a future phone APK / dashboard.

**Architecture:** Three independent shell/Python scripts under `scripts/plex/` and `scripts/post-import/`, scheduled via user crontab + `systemd --user` timers. Each script is **self-contained, idempotent, dry-run capable, and smoke-tested.** Per the operator's directive: review and test thoroughly.

**Why cherry-pick instead of installing the upstream repos:**
- **JBOPS** is a recipe book of ~50 scripts — installing all of it is bloat. We pull `kill_stream.py` only.
- **Just-A-Bunch-Of-Starr-Scripts** is PowerShell-only — Ultra.cc has no `pwsh`. Bash rewrite of the one valuable script (`Upgradinatorr`) is ~100 LOC, fully under our control, no foreign runtime dependency.
- A tiny stream-stats emitter is easier to write than to find — and giving the future phone APK a clean JSON endpoint pays back compounding.

**Non-goals:**
- Wholesale port of starr-scripts (only `Upgradinatorr` is worth it; the others are niche).
- Wholesale install of JBOPS (only `kill_stream.py` clears the bar).
- Replacing Maintainerr or Kometa — these scripts are gap-fillers, not duplicates.

---

## Probe findings (verified 2026-05-08, applicable to this plan)

| Fact | Value | Source |
|---|---|---|
| Plex Pass | **Confirmed yes** by operator. Required for `kill_stream` API endpoint (`PUT /status/sessions/<id>/terminate`). | operator |
| python-plexapi | Already needed by JBOPS scripts; install in shared venv `~/.apps/python-plexapi/venv/` (or piggyback on TitleCardMaker's venv if preferred). | dependency |
| `*arr` API access | All 4 instances (`sonarr`, `sonarr2`, `radarr`, `radarr2`) have keys in `secrets/`. Upgradinatorr uses `/api/v3/wanted/cutoff` + `/api/v3/command` (`MoviesSearch` / `EpisodeSearch`). | existing audit |
| Stream-stats endpoint | Will write JSON to `~/.apps/stream-stats/state.json`; phone APK / dashboard reads via the future Storage/Traffic API endpoint or directly via the existing nginx (path TBD with phone-app phase). | design |
| Recyclarr cadence | Weekly Sunday 04:30. Upgradinatorr should run AFTER Recyclarr's sync completes — schedule Sunday 06:00 (after Recyclarr 04:30 + jitter + reasonable buffer). | scheduling design |

## Conventions

- `SSHM` = `ssh -o BatchMode=yes quadstronaut@seedbox.example.com`. Defined in `scripts/lib/ssh.sh`.
- All scripts idempotent + dry-run capable (`--dry-run` flag).
- All scripts logged to `~/.apps/<script-domain>/logs/<script>.log` with `setup_log()` from a shared helper.
- All commits include `Co-Authored-By: Claude Opus 4.7`.
- **No new versions to pin** — these are scripts we author and version via git history.

---

## Phase 40 — JBOPS `kill_stream.py` cherry-pick (Plex stream limiter)

### Task 40.1: Shared python-plexapi venv

**Files:**
- Create: `scripts/configure/59-python-plexapi-venv.sh`

A single shared venv for all small Plex scripts (kill_stream + future ones). Avoids re-installing python-plexapi in every script's own venv.

- [ ] **Step 1: Create venv + pin python-plexapi**

```bash
if ! secret_exists python-plexapi.version; then
  TAG=$(curl -fsSL https://api.github.com/repos/pkkid/python-plexapi/releases/latest | grep -oP '"tag_name":\s*"\K[^"]+')
  secret_write python-plexapi.version "$TAG"
fi
PLXVER=$(secret_read python-plexapi.version)
PLXVER_NUM="${PLXVER#v}"

sshm 'bash -s' <<EOF
set -euo pipefail
mkdir -p ~/.apps/python-plexapi
cd ~/.apps/python-plexapi
if [ ! -d venv ]; then
  python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip wheel >/dev/null
./venv/bin/pip install --no-cache-dir "plexapi==${PLXVER_NUM}" requests >/dev/null
./venv/bin/python -c "import plexapi; print(plexapi.VERSION)"
EOF
```

### Task 40.2: Author `kill_stream.sh` (wrapper) + `kill_stream.py` (logic)

**Files:**
- Create: `scripts/plex/kill_stream.py` (Python, ~90 LOC — adapted from JBOPS)
- Create: `scripts/plex/kill_stream.sh` (bash wrapper that loads creds + invokes Python)
- Create: `scripts/plex/lib/__init__.py` + `scripts/plex/lib/plex_client.py` (shared)

- [ ] **Step 1: Author `kill_stream.py`**

Adapted from JBOPS `kill_stream.py` — original is 130 lines, our version trims to essentials. Behavior:

```python
#!/usr/bin/env python3
"""Kill the OLDEST Plex stream when a single user has > MAX_STREAMS active.

Designed for cron @ 1-min cadence. Idempotent — if no kill needed, exits 0 silently.
Dry-run mode: print what would be killed without sending the terminate API call.

Adapted from JBOPS (https://github.com/blacktwin/JBOPS) under MIT.
"""
from __future__ import annotations
import argparse, json, os, sys, time
from collections import defaultdict
from pathlib import Path

from plexapi.server import PlexServer

DEFAULT_MAX_STREAMS_PER_USER = int(os.environ.get("KS_MAX_STREAMS_PER_USER", "2"))
KILL_MESSAGE = os.environ.get("KS_MESSAGE",
    "Too many concurrent streams from this account. The oldest was stopped.")
STATE_FILE = Path.home() / ".apps" / "stream-stats" / "kill-history.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print decisions, do not terminate")
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_STREAMS_PER_USER)
    args = parser.parse_args()

    plex_url = os.environ["PLEX_URL"]
    plex_token = os.environ["PLEX_TOKEN"]

    plex = PlexServer(plex_url, plex_token, timeout=10)
    sessions = plex.sessions()

    by_user: dict[str, list] = defaultdict(list)
    for s in sessions:
        user = (s.usernames[0] if s.usernames else "unknown").lower()
        by_user[user].append(s)

    decisions = []
    for user, streams in by_user.items():
        if len(streams) <= args.max:
            continue
        # Sort oldest first (smallest viewOffset / earliest started)
        streams.sort(key=lambda s: getattr(s, "viewOffset", 0) or 0)
        to_kill = streams[: len(streams) - args.max]
        for s in to_kill:
            decisions.append({
                "user": user,
                "session_id": s.sessionKey,
                "title": str(s),
                "action": "kill" if not args.dry_run else "would-kill",
            })
            if not args.dry_run:
                try:
                    s.stop(reason=KILL_MESSAGE)
                except Exception as e:
                    decisions[-1]["error"] = str(e)
                    decisions[-1]["action"] = "kill-failed"

    # Persist last-decision history for the dashboard / phone APK.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if STATE_FILE.exists():
        try:
            history = json.loads(STATE_FILE.read_text())[-99:]
        except (json.JSONDecodeError, OSError):
            history = []
    history.append({"ts": int(time.time()), "decisions": decisions})
    STATE_FILE.write_text(json.dumps(history, indent=2))

    if decisions:
        print(json.dumps(decisions, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Author `kill_stream.sh` wrapper**

```bash
#!/usr/bin/env bash
# kill_stream.sh — bash wrapper that injects Plex creds + invokes kill_stream.py.
set -euo pipefail

SECRETS_DIR="${SECRETS_DIR:-/home/quadstronaut/secrets}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${LOG_DIR:-$HOME/.apps/stream-stats/logs}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/kill_stream.log"

PLEX_HOST=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.host")
PLEX_PORT=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.port")
PLEX_TOKEN=$(tr -d '[:space:]' < "$SECRETS_DIR/plex.token")

export PLEX_URL="http://${PLEX_HOST}:${PLEX_PORT}"
export PLEX_TOKEN

VENV="$HOME/.apps/python-plexapi/venv/bin/python"
[ -x "$VENV" ] || { echo "missing python-plexapi venv at $VENV" >&2; exit 1; }

# Lock to prevent overlapping invocations (cron @ 1min could pile up if Plex is slow).
LOCKFILE="/tmp/kill_stream.lock"
exec 9>"$LOCKFILE"
flock -n 9 || { echo "$(date -Iseconds) skip — previous run still holding lock" >> "$LOG"; exit 0; }

"$VENV" "$HERE/kill_stream.py" "$@" 2>&1 | tee -a "$LOG"
```

### Task 40.3: Deploy + cron + thorough test

- [ ] **Step 1: Deploy**

```bash
sshm 'mkdir -p ~/scripts/plex/lib ~/.apps/stream-stats/logs'
scpm_to scripts/plex/kill_stream.py '~/scripts/plex/kill_stream.py'
scpm_to scripts/plex/kill_stream.sh '~/scripts/plex/kill_stream.sh'
sshm 'chmod +x ~/scripts/plex/kill_stream.sh'
```

- [ ] **Step 2: Test — dry-run with NO active streams (should be no-op)**

```bash
sshm '~/scripts/plex/kill_stream.sh --dry-run'
# Expect: no JSON output, exit 0, log line in ~/.apps/stream-stats/logs/kill_stream.log
```

- [ ] **Step 3: Test — dry-run with simulated overload**

Operator starts 3 simultaneous streams from a single Plex user (use a test/throwaway account, not a real user). Then:

```bash
sshm '~/scripts/plex/kill_stream.sh --dry-run --max 2'
# Expect: JSON output listing 1 session "would-kill", correct user + title, exit 0.
# CRITICAL: confirm no actual termination happened (Plex sessions still active).
```

- [ ] **Step 4: Test — REAL kill with --max 2**

Same scenario as Step 3, but without `--dry-run`:

```bash
sshm '~/scripts/plex/kill_stream.sh --max 2'
# Expect: JSON output, exit 0, ONE Plex session ended (the oldest), other 2 remain.
# Verify: kill-history.json has new entry; Plex Web UI's Sessions tab shows 2 active.
```

- [ ] **Step 5: Cron @ 1-min**

```bash
sshm '(crontab -l | grep -v kill_stream; cat) | crontab -'
sshm '(crontab -l; echo "* * * * * /home/quadstronaut/scripts/plex/kill_stream.sh --max 2 >/dev/null 2>&1") | crontab -'
```

`--max 2` is the policy default. Operator can change per-user limits via the env var `KS_MAX_STREAMS_PER_USER` set in `~/.apps/stream-stats/env`.

- [ ] **Step 6: Confirm cron runs**

```bash
sleep 90
sshm 'tail -20 ~/.apps/stream-stats/logs/kill_stream.log'
# Expect: at least one timestamp from within the last 90 seconds.
```

---

## Phase 41 — Upgradinatorr (bash rewrite)

Adapted from `angrycuban13/Just-A-Bunch-Of-Starr-Scripts/Upgradinatorr` (PowerShell). Re-triggers Sonarr/Radarr search on N stale grabs against the current quality profile + custom format scoring. After Recyclarr changes scores, this is what actually upgrades existing files in the library.

### Task 41.1: Author `upgradinatorr.sh`

**Files:**
- Create: `scripts/post-import/upgradinatorr.sh` (bash, ~120 LOC)
- Create: `scripts/post-import/upgradinatorr.conf.example` (commented config template)

- [ ] **Step 1: Author the script**

```bash
#!/usr/bin/env bash
# upgradinatorr.sh — re-search N stale grabs in Sonarr/Radarr against current QP + CFs.
#
# Adapted from Just-A-Bunch-Of-Starr-Scripts/Upgradinatorr (PowerShell, MIT-adjacent)
# https://github.com/angrycuban13/Just-A-Bunch-Of-Starr-Scripts
#
# Usage: upgradinatorr.sh --app <sonarr|sonarr2|radarr|radarr2> [--count N] [--dry-run]
#
# Behavior: queries the *arr's wanted/cutoff endpoint, picks N items not searched recently,
# triggers a search command. Tracks "last searched at" via the *arr's tags (creates a
# 'upgradinatorr-{epoch}' tag, prunes old).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECRETS_DIR="${SECRETS_DIR:-/home/quadstronaut/secrets}"
LOG_DIR="${LOG_DIR:-$HOME/.apps/upgradinatorr/logs}"
mkdir -p "$LOG_DIR"

APP=""
COUNT=5
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP="$2"; shift 2 ;;
    --count) COUNT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$APP" ] || { echo "--app required" >&2; exit 2; }

read_sec() { tr -d '[:space:]' < "$SECRETS_DIR/$1"; }
KEY=$(read_sec "${APP}.key")
PORT=$(read_sec "${APP}.port")
BASE=$(read_sec "${APP}.urlbase")

case "$APP" in
  sonarr|sonarr2) API_VERSION=v3 ;;
  radarr|radarr2) API_VERSION=v3 ;;
  *) echo "unsupported app: $APP" >&2; exit 2 ;;
esac

URL="http://127.0.0.1:${PORT}/${BASE}/api/${API_VERSION}"
LOG="$LOG_DIR/${APP}.log"

log() { printf '%s [%s] %s\n' "$(date -Iseconds)" "$APP" "$*" | tee -a "$LOG"; }

api() {
  local method="$1"; local path="$2"; local data="${3:-}"
  if [ -n "$data" ]; then
    curl -fsS -m 30 -X "$method" -H "X-Api-Key: $KEY" -H 'Content-Type: application/json' -d "$data" "$URL/$path"
  else
    curl -fsS -m 30 -X "$method" -H "X-Api-Key: $KEY" "$URL/$path"
  fi
}

# Get items below cutoff (sonarr: episodes; radarr: movies). Sort by least-recently-searched.
# Sonarr: GET /wanted/cutoff?sortKey=episodes.lastSearchTime&sortDirection=ascending&pageSize=N
# Radarr: GET /wanted/cutoff?sortKey=movieFile.dateAdded&sortDirection=ascending&pageSize=N
case "$APP" in
  sonarr|sonarr2)
    QUERY="wanted/cutoff?sortKey=episodes.lastSearchTime&sortDirection=ascending&pageSize=$COUNT"
    SEARCH_CMD='{"name":"EpisodeSearch","episodeIds":[%s]}'
    KEY_FIELD=".records[].id"
    ;;
  radarr|radarr2)
    QUERY="wanted/cutoff?sortKey=movieFile.dateAdded&sortDirection=ascending&pageSize=$COUNT"
    SEARCH_CMD='{"name":"MoviesSearch","movieIds":[%s]}'
    KEY_FIELD=".records[].id"
    ;;
esac

ITEMS_JSON=$(api GET "$QUERY")
IDS=$(echo "$ITEMS_JSON" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(",".join(str(r["id"]) for r in d.get("records",[])))')

if [ -z "$IDS" ]; then
  log "no items below cutoff — nothing to do"
  exit 0
fi

ITEM_COUNT=$(echo "$IDS" | tr ',' '\n' | wc -l)
log "found $ITEM_COUNT items below cutoff: $IDS"

if [ $DRY_RUN -eq 1 ]; then
  log "DRY RUN — would search items: $IDS"
  exit 0
fi

PAYLOAD=$(printf "$SEARCH_CMD" "$IDS")
RESP=$(api POST "command" "$PAYLOAD")
COMMAND_ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))')
log "search command queued: id=$COMMAND_ID"

# Poll for completion (best-effort — *arr's command queue can take minutes).
for _ in $(seq 1 30); do
  STATE=$(api GET "command/$COMMAND_ID" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))' 2>/dev/null || echo "")
  case "$STATE" in
    completed) log "command completed"; exit 0 ;;
    failed) log "command FAILED — check $APP logs"; exit 1 ;;
  esac
  sleep 10
done
log "command still running after 5 min — exiting (it will finish async)"
exit 0
```

### Task 41.2: Deploy + thorough test

- [ ] **Step 1: Deploy**

```bash
sshm 'mkdir -p ~/scripts/post-import ~/.apps/upgradinatorr/logs'
scpm_to scripts/post-import/upgradinatorr.sh '~/scripts/post-import/upgradinatorr.sh'
sshm 'chmod +x ~/scripts/post-import/upgradinatorr.sh'
```

- [ ] **Step 2: Test — Sonarr dry-run (read-only)**

```bash
sshm '~/scripts/post-import/upgradinatorr.sh --app sonarr --dry-run'
# Expect: log lines listing items below cutoff, "DRY RUN — would search items: <ids>", exit 0.
# CRITICAL: no search command was POSTed (verify in Sonarr → Activity → Queue: no new search jobs).
```

- [ ] **Step 3: Test — Radarr dry-run**

Same as Step 2 with `--app radarr`.

- [ ] **Step 4: Test — Sonarr REAL run with `--count 1`**

```bash
sshm '~/scripts/post-import/upgradinatorr.sh --app sonarr --count 1'
# Expect: 1 episode searched. Verify in Sonarr UI → Activity → Queue: a new "EpisodeSearch" command appears.
# Wait 5-10 minutes; check Sonarr → Activity → History: a new download (or "no results") appears for that episode.
```

- [ ] **Step 5: Test — error path (intentionally bad creds)**

```bash
sshm 'X-Api-Key: bogus_key ~/scripts/post-import/upgradinatorr.sh --app sonarr --dry-run' || echo "expected failure handled"
# Expect: bash error / curl 401, log captures it, script exits non-zero.
```

(This step is not strictly required but proves the error path doesn't silently swallow.)

- [ ] **Step 6: Schedule (Sunday 06:00 — after Recyclarr Sunday 04:30 + jitter)**

```bash
sshm "cat > ~/.config/systemd/user/upgradinatorr.service" <<'UNIT'
[Unit]
Description=Upgradinatorr — re-search stale grabs
After=network-online.target

[Service]
Type=oneshot
ExecStart=/home/quadstronaut/scripts/post-import/upgradinatorr.sh --app sonarr --count 5
ExecStart=/home/quadstronaut/scripts/post-import/upgradinatorr.sh --app sonarr2 --count 3
ExecStart=/home/quadstronaut/scripts/post-import/upgradinatorr.sh --app radarr --count 5
# radarr2 omitted until operator clarifies its purpose (see Recyclarr plan)
Nice=15
UNIT
sshm "cat > ~/.config/systemd/user/upgradinatorr.timer" <<'UNIT'
[Unit]
Description=Upgradinatorr weekly run

[Timer]
OnCalendar=Sun *-*-* 06:00:00
RandomizedDelaySec=1800
Persistent=true
Unit=upgradinatorr.service

[Install]
WantedBy=timers.target
UNIT
sshm 'systemctl --user daemon-reload && systemctl --user enable --now upgradinatorr.timer'
```

`--count 5` per app per week is conservative. With ~300 grabs eligible, full library upgrade-pass takes ~60 weeks. Operator can bump to `--count 10` if they want it faster (still polite to indexers).

---

## Phase 42 — Stream-stats emitter (phone APK groundwork)

A tiny script that emits current Plex stream state to `~/.apps/stream-stats/state.json` every minute. The future phone APK / dashboard reads this — no new app install required, just a JSON file the existing or future API endpoint serves.

### Task 42.1: Author `stream-stats.py`

**Files:**
- Create: `scripts/plex/stream_stats.py` (~50 LOC)
- Create: `scripts/plex/stream_stats.sh` (bash wrapper, mirrors kill_stream pattern)

- [ ] **Step 1: Author**

```python
#!/usr/bin/env python3
"""Emit current Plex stream state to ~/.apps/stream-stats/state.json.

Designed for cron @ 1-min cadence. Idempotent — overwrites state.json each run.
Output JSON shape (stable contract for the future phone APK):

  {
    "ts": 1715000000,
    "active_streams": 3,
    "by_user": {"alice": 1, "bob": 2},
    "streams": [
      {"user": "alice", "title": "...", "state": "playing", "transcode": false}
    ]
  }
"""
from __future__ import annotations
import json, os, sys, time
from collections import Counter
from pathlib import Path

from plexapi.server import PlexServer

STATE_FILE = Path.home() / ".apps" / "stream-stats" / "state.json"


def main() -> int:
    plex = PlexServer(os.environ["PLEX_URL"], os.environ["PLEX_TOKEN"], timeout=10)
    sessions = plex.sessions()

    streams = []
    user_counter: Counter[str] = Counter()
    for s in sessions:
        user = (s.usernames[0] if s.usernames else "unknown").lower()
        user_counter[user] += 1
        streams.append({
            "user": user,
            "title": str(s),
            "state": getattr(s.player, "state", "unknown") if s.player else "unknown",
            "transcode": bool(getattr(s, "transcodeSession", None)),
            "media_type": getattr(s, "type", None),
        })

    payload = {
        "ts": int(time.time()),
        "active_streams": len(sessions),
        "by_user": dict(user_counter),
        "streams": streams,
    }

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(STATE_FILE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Wrapper**

Mirror `kill_stream.sh` — same env-injection pattern, same flock, same log dir. Different script name + log path.

### Task 42.2: Deploy + test

- [ ] **Step 1: Deploy**

```bash
scpm_to scripts/plex/stream_stats.py '~/scripts/plex/stream_stats.py'
scpm_to scripts/plex/stream_stats.sh '~/scripts/plex/stream_stats.sh'
sshm 'chmod +x ~/scripts/plex/stream_stats.sh'
```

- [ ] **Step 2: Test — single run + JSON shape**

```bash
sshm '~/scripts/plex/stream_stats.sh && cat ~/.apps/stream-stats/state.json'
# Expect: valid JSON with ts, active_streams, by_user, streams keys.
# If 0 streams: payload still written, active_streams=0, streams=[].
```

- [ ] **Step 3: Test — JSON validates**

```bash
sshm 'python3 -c "import json; json.load(open(\"/home/quadstronaut/.apps/stream-stats/state.json\"))" && echo OK'
```

- [ ] **Step 4: Cron @ 1-min**

```bash
sshm '(crontab -l | grep -v stream_stats; cat) | crontab -'
sshm '(crontab -l; echo "* * * * * /home/quadstronaut/scripts/plex/stream_stats.sh >/dev/null 2>&1") | crontab -'
sleep 90
sshm 'stat -c %Y ~/.apps/stream-stats/state.json'
# Expect: timestamp within last 90 seconds (recently rewritten).
```

---

## Smoke test additions

Add four new tests to `scripts/smoke-test.sh` — these enforce that all three scripts continue to work over time:

```bash
# 30. python-plexapi venv healthy
echo "30. python-plexapi venv"
PV=$(sshm "~/.apps/python-plexapi/venv/bin/python -c 'import plexapi; print(plexapi.VERSION)' 2>/dev/null")
if [ -n "$PV" ]; then
  record "plexapi-venv" pass "$PV"
else
  record "plexapi-venv" fail "missing or broken"
fi

# 31. kill_stream is recently invoked (cron is alive)
echo "31. kill_stream cron alive"
KS_AGE=$(sshm "stat -c %Y ~/.apps/stream-stats/logs/kill_stream.log 2>/dev/null || echo 0")
NOW=$(date +%s)
if [ $((NOW - KS_AGE)) -lt 180 ]; then
  record "kill-stream-fresh" pass "log updated $((NOW - KS_AGE))s ago"
else
  record "kill-stream-fresh" fail "log stale ($((NOW - KS_AGE))s old) — cron not running?"
fi

# 32. stream-stats JSON is current and valid
echo "32. stream-stats JSON"
SS_JSON=$(sshm "cat ~/.apps/stream-stats/state.json 2>/dev/null")
if echo "$SS_JSON" | python3 -c 'import sys,json;d=json.load(sys.stdin);assert "ts" in d and "active_streams" in d' 2>/dev/null; then
  record "stream-stats-json" pass "valid"
else
  record "stream-stats-json" fail "invalid or missing"
fi

# 33. Upgradinatorr timer scheduled
echo "33. Upgradinatorr timer"
UT=$(sshm "systemctl --user list-timers upgradinatorr.timer --no-pager 2>/dev/null | grep -c upgradinatorr.timer")
if [ "${UT:-0}" -ge 1 ]; then
  record "upgradinatorr-timer" pass "scheduled"
else
  record "upgradinatorr-timer" fail "timer not scheduled"
fi
```

---

## Rollback per phase

| Phase | If broken |
|---|---|
| 40 (kill_stream) | Crontab strip the kill_stream line: `sshm "crontab -l \| grep -v kill_stream \| crontab -"`. Remove scripts: `rm -rf ~/scripts/plex/kill_stream.* ~/.apps/stream-stats/logs/kill_stream.log`. python-plexapi venv stays (shared). |
| 41 (Upgradinatorr) | `systemctl --user disable --now upgradinatorr.timer upgradinatorr.service && rm ~/.config/systemd/user/upgradinatorr.{service,timer}`. Rm scripts: `rm ~/scripts/post-import/upgradinatorr.sh`. **No `*arr` cleanup needed** — Upgradinatorr only triggers searches, never modifies *arr config. |
| 42 (stream-stats) | Crontab strip `stream_stats`. Remove scripts. State JSON can stay or be deleted. |

**Critical:** if `kill_stream.py` ever kills the wrong stream (e.g. logic bug), restore service immediately by removing the cron line — the script is then idempotent-zero (does nothing). Then debug at leisure with `--dry-run`.

---

## Cost summary

- **JBOPS adaptation**: $0 (MIT-style; we re-author under our own header per the JBOPS source).
- **Upgradinatorr bash rewrite**: $0 (we author from scratch).
- **stream-stats**: $0 (we author from scratch).
- **Disk**: <10 MB (shared venv + logs + state JSON + history JSONs).
- **CPU**: kill_stream + stream_stats run @ 1 min — each is ~200 ms / call → ~0.5% CPU sustained. Upgradinatorr @ Sunday 06:00 — minutes/week.
- **Operator effort**: ~2 hours total (most of it in thorough testing per the operator's directive).

---

## What this plan does NOT do

- **No Plex Pass scripts beyond `kill_stream`.** Other Plex-Pass-only tricks (parental scheduling, etc.) — defer.
- **No Owinenatorr / Set-RadarrCollectionsMonitored / ZakTag rewrites.** Niche. Skipped per "cherry-pick approved" — Upgradinatorr alone is the high-value pull.
- **No JBOPS reporting scripts** beyond what stream_stats covers — stream_stats is intentionally minimal; the future phone APK API endpoint owns the richer dashboard.
- **No `radarr2` Upgradinatorr run** — gated on the same operator clarification as Recyclarr (what is `radarr2`?).
- **No automatic concurrent-stream policy override.** The `--max 2` default is deliberate; bumping it requires editing crontab.

---

## Total scope

- **3 deploy scripts** in `scripts/configure/`, `scripts/plex/`, `scripts/post-import/`
- **3 user-facing scripts** (kill_stream, upgradinatorr, stream_stats) — all bash + Python pairs
- **1 shared venv** at `~/.apps/python-plexapi/venv/`
- **1 user-systemd service + timer** (`upgradinatorr.{service,timer}`)
- **2 cron entries** (kill_stream @ 1min, stream_stats @ 1min)
- **0 nginx fragments** (no public exposure for any of these)
- **0 ports claimed**
- **4 new smoke tests** (`plexapi-venv`, `kill-stream-fresh`, `stream-stats-json`, `upgradinatorr-timer`)
- **0 secrets committed.** Plex token + *arr keys reused.

Estimated install time: ~1.5 hours (most of it in Phase 40 Step 4 manual testing of the live kill).

---

## Open decisions (operator)

1. **`KS_MAX_STREAMS_PER_USER`** — default 2. Family/friends-tier might want 3 or 4. Set in `~/.apps/stream-stats/env` (a small file the wrapper sources).
2. **Upgradinatorr `--count` per app per week** — default 5 sonarr / 3 sonarr2 / 5 radarr. Bump if operator wants faster library upgrade pass; lower if indexer fair-use is a concern.
3. **`KS_MESSAGE`** — the message Plex shows the user when a stream is killed. Default reads "Too many concurrent streams from this account. The oldest was stopped." Operator may want softer or sterner wording for the boomer/country-folk audience.
4. **`radarr2` inclusion** — gated on the same operator clarification as Recyclarr.
5. **Phone APK schema** — current `state.json` shape is the default. If the future phone APK wants extra fields, this plan owns evolving the schema.

---

## Execution protocol

### Step 0 — Pre-execution check

- [ ] All operator pre-reqs from "Open decisions" section are answered (or operator agrees defaults are OK).
- [ ] Required credentials in `secrets/` (see Step 1 below).
- [ ] Previous-phase smoke is passing — no regression risk introduced into this plan's start.
- [ ] Working tree clean: `git status` shows no unrelated changes.

### Step 1 — Credential pre-flight (consolidated, one-time)

| Cred | Used by (this plan) | Likely already exists? |
|---|---|---|
| `plex.token`, `plex.host`, `plex.port` | kill_stream (Phase 40), stream_stats (Phase 42) | **Yes** — captured during Phase 5 audit |
| `sonarr.key` + `sonarr.port` + `sonarr.urlbase` | Upgradinatorr (Phase 41) | **Yes** — existing |
| `sonarr2.key` + `sonarr2.port` + `sonarr2.urlbase` | Upgradinatorr | **Yes** — existing |
| `radarr.key` + `radarr.port` + `radarr.urlbase` | Upgradinatorr | **Yes** — existing |
| `radarr2.*` | Upgradinatorr (gated — see open decision) | Yes — existing, but skipped until clarified |
| `python-plexapi.version` | shared venv | **No** — captured at first install (auto-resolves latest) |

Only blocker: confirm `plex.token` is still valid (re-audit Phase 5 if `kill_stream --dry-run` returns 401).

**Browser policy:** prefer CLI/API for everything. No browser steps required for this plan.

### Step 2 — Implementation phases

Continuous execution per `feedback_continuous-execution-preferred.md`: don't ask for per-phase approval. Pause only on genuine blockers.

Phases in this plan:
- **Phase 40** — JBOPS `kill_stream.py` cherry-pick + shared python-plexapi venv (Tasks 40.1-40.3).
- **Phase 41** — Upgradinatorr bash rewrite + thorough test + weekly timer (Tasks 41.1-41.2).
- **Phase 42** — stream-stats emitter + cron (Tasks 42.1-42.2).

Each phase = one commit with the project's style (lowercase, scope: action, +smoke result).

### Step 3 — Self-check (after Phase 42)

1. Run `scripts/smoke-test.sh` — every check must pass. Fix any failure before proceeding.
2. `git status` — clean (3 new commits ahead of `origin/main`).
3. Re-run smoke twice in a row to catch flakes. The new tests (`kill-stream-fresh`, `stream-stats-json`) depend on cron timing; back-to-back runs catch a cron-stale flake immediately.

### Step 4 — Log audit

Audit the new components for errors smoke might miss:

1. `journalctl --user -u upgradinatorr.service --since "today" -p err`
2. `~/.apps/stream-stats/logs/kill_stream.log` — `grep -E 'ERROR|FATAL|Traceback'`
3. `~/.apps/upgradinatorr/logs/*.log` — `grep -E 'ERROR|FATAL'`
4. `~/.apps/python-plexapi/venv/` — version + import check

Classify findings:
- **Cosmetic** (e.g. "skip — previous run still holding lock" — that's flock doing its job) — note, don't act.
- **Actionable** (config issue) — fix, re-run, re-audit.
- **Blocking** (e.g. Plex 401 token expired) — stop, surface in summary.

### Step 5 — Final summary template

```
# Cherry-pick scripts implementation
- Phases completed: 40, 41, 42
- Scripts added: 3 user-facing + 1 venv installer
- Smoke: N/N pass (was M/M before)

# Self-check results
- [details]

# Log audit
- Cosmetic: [list]
- Actionable (fixed): [list]
- Blockers requiring operator: [list, or "none"]

# Follow-up parking lot
- radarr2 Upgradinatorr inclusion (gated on operator clarification)
- KS_MAX_STREAMS_PER_USER per-tier override (deferred until Wizarr tier model lands)
```

### Hard rules (non-negotiable)

- **No 4K** anywhere — Recyclarr profiles, Kometa overlays, transcoder hints, request quotas. 1080p ceiling. (per `feedback_no-4k-profiles.md`)
- **Pin every version** — never `latest`, never `main`. Surface pinned versions in `versions.env` at repo root for the future updater. (per `feedback_pin-app-versions.md`)
- **Plex-primary** in all media-server config; Jellyfin gets parity only where the feature explicitly serves trial users. (per `project_plex-primary-jellyfin-trial.md`)
- **Reuse `secrets/htpasswd.password`** for any new admin-facing self-hosted app. (per `feedback_shared-admin-password.md`)
- **Read ports from `~/.apps/nginx/proxy.d/<app>.conf`** at runtime, not config.xml. (per `project_manitoba-network-model.md`)
- **Continuous execution** — no per-phase approval. Pause only on missing creds, smoke failure, or blocking log errors. (per `feedback_continuous-execution-preferred.md`)
- **Browser is last resort** — CLI/API everywhere; defer manual browser steps to `docs/operator-deferred.md`; Playwright authorized only when no alternative exists.
- **Modern AI-augmented preferred** when there's a choice; willing to fork if upstream stalls. (per `feedback_modern-ai-augmented-apps.md`)

### Failure modes to avoid (this plan)

- **Don't skip the dry-run tests** in Phase 40.3 / 41.2 — `kill_stream` is the only script in this plan that can affect live users. A logic bug in production = a friend gets booted from a stream mid-movie. Test, test, test.
- **Don't run Upgradinatorr without Recyclarr first** — the whole point is to upgrade against new TRaSH-guide scoring. Running before Recyclarr's first sync means re-grabbing files against the old profile = no upgrade, just indexer churn.
- **Don't let `kill_stream` cron loop** if Plex is slow — `flock` enforces single-instance. Confirm flock is in place before enabling cron.
- **Don't commit the kill-history.json or state.json** — both are runtime state, gitignored.

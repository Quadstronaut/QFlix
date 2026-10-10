#!/usr/bin/env bash
# vlogs-time-integrity canary (QFLX-44): is every log event in VictoriaLogs
# stored at the instant it was written, exactly once?
#
# Two independent predicates, both over the whole index (every app):
#
#   FUTURE      any event whose _time is later than now + FUTURE_MIN minutes.
#               A log line cannot be written in the future, so this is the
#               fingerprint of a zone-less LOCAL stamp read as UTC (CEST is
#               +02:00: such lines land two hours ahead). This is the exact
#               2026-10-09 listmonk bug, caught for every app, not one.
#   DUPLICATE   any (app, _time, _msg) group with more than one row in the
#               last 24h. The ingester keeps a byte-offset / journal cursor so
#               a line ships once; a duplicate means the cursor reset, a
#               format change re-read a file, or two ingesters ran.
#
#   SKEW        an actively written file whose newest stored _time is more than
#               SKEW_MIN minutes BEHIND the file's mtime. This is the other
#               direction of the same bug: a UTC zone-less stamp read as local
#               lands 2h EARLY and no future check can see it (tautulli, plex
#               and seerr sat 2h behind on 2026-10-10). Needs the file on this
#               box, so journald-routed sources are skipped (their stamps
#               carry an explicit zone).
#
# Red on any of them, and the message NAMES the offenders: app, count, and for
# FUTURE how far ahead. It never prints _msg: log text can carry member data
# and this output reaches Kuma and Discord.
#
# Known one-off after the QFLX-44 deploy: rows ingested BEFORE the fix with a
# local stamp sit up to 2h in the future and stay red until the wall clock
# passes them (<=2h). They cannot be edited in place.
#
# Exit 0 clean / 1 red (a STAGE=... line on stderr says why). A query that
# never returns 200 is red too (`vlti-query-fail`): an unanswerable integrity
# check must not read as clean.
#
# Env (all optional):
#   QFLIX_CANARY_VLTI_URL        base URL; default http://127.0.0.1:<vlogs.port>
#   QFLIX_CANARY_VLTI_FUTURE_MIN minutes of tolerated clock skew     (default 2)
#   QFLIX_CANARY_VLTI_DUP_MAX    duplicate groups tolerated          (default 0)
#   QFLIX_CANARY_VLTI_SKEW_MIN   stored-vs-mtime lag tolerated, min  (default 30)
#   QFLIX_CANARY_VLTI_LOG        append-only reason log
#   MANITOBA_SECRETS             secrets dir (default ~/secrets)
#
# Lives at ~/scripts/canaries/vlogs-time-integrity.sh (240-maintenance-install.sh).
# Fired hourly by manitoba-maint-canary-vlogs-time-integrity, which pushes to
# Kuma monitor "Canary VLogs Time Integrity".
set -uo pipefail

SECRETS="${MANITOBA_SECRETS:-$HOME/secrets}"
LOG="${QFLIX_CANARY_VLTI_LOG:-$HOME/.opt/maint/canary-vlogs-time-integrity.log}"
export QFLIX_CANARY_VLTI_FUTURE_MIN="${QFLIX_CANARY_VLTI_FUTURE_MIN:-2}"
export QFLIX_CANARY_VLTI_DUP_MAX="${QFLIX_CANARY_VLTI_DUP_MAX:-0}"
export QFLIX_CANARY_VLTI_SKEW_MIN="${QFLIX_CANARY_VLTI_SKEW_MIN:-30}"

if [ -n "${QFLIX_CANARY_VLTI_URL:-}" ]; then
  VL="$QFLIX_CANARY_VLTI_URL"
else
  PORT="$(tr -d '[:space:]' < "$SECRETS/vlogs.port" 2>/dev/null)"
  if [ -z "$PORT" ]; then
    printf 'STAGE=vlti-config-missing msg=secrets/vlogs.port-empty\n' >&2
    exit 1
  fi
  VL="http://127.0.0.1:$PORT"
fi
export VL LOG

exec python3 - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VL = os.environ["VL"]
LOG = os.environ["LOG"]
FUTURE_MIN = int(os.environ["QFLIX_CANARY_VLTI_FUTURE_MIN"])
DUP_MAX = int(os.environ["QFLIX_CANARY_VLTI_DUP_MAX"])
SKEW_MIN = int(os.environ["QFLIX_CANARY_VLTI_SKEW_MIN"])


def logfail(reason):
    """Append the reason locally (Kuma's heartbeat table is unreadable to the
    SSH user); keep the file bounded."""
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {reason}\n")
        with open(LOG, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > 300:
            with open(LOG, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-200:])
    except OSError:
        pass


def die(stage, msg):
    print(f"STAGE={stage} msg={msg}", file=sys.stderr)
    logfail(f"{stage} {msg}")
    sys.exit(1)


def query(q, **params):
    """LogsQL query -> list of JSON rows. Retries transient failures 3x (this
    is a shared box; neighbour I/O storms stall a normally fast query).
    Returns None when no try answered 200."""
    qs = urllib.parse.urlencode({"query": q, **params})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(f"{VL}/select/logsql/query?{qs}", timeout=60) as r:
                if r.status == 200:
                    rows = []
                    for line in r.read().decode("utf-8", "replace").splitlines():
                        line = line.strip()
                        if line:
                            try:
                                rows.append(json.loads(line))
                            except ValueError:
                                pass
                    return rows
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        if attempt < 2:
            time.sleep(2)
    return None


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    s = (s or "").rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + frac[:6]
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=timezone.utc)
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


try:
    with urllib.request.urlopen(f"{VL}/health", timeout=10) as r:
        if r.status != 200:
            raise OSError(r.status)
except (urllib.error.URLError, TimeoutError, OSError) as exc:
    die("vlti-down", f"health-failed-{type(exc).__name__}")

now = datetime.now(timezone.utc)
findings = []

# FUTURE: everything stamped later than now + tolerance, up to a week ahead.
fut = query("* | stats by (app) count() as n",
            start=iso(now + timedelta(minutes=FUTURE_MIN)),
            end=iso(now + timedelta(days=7)))
if fut is None:
    die("vlti-query-fail", "future-query-no-200-after-retries")
future_apps = {}
for row in fut:
    n = int(row.get("n", 0) or 0)
    if n > 0:
        future_apps[row.get("app", "?")] = n
if future_apps:
    # How far ahead is the worst one? One more query; failure here only drops
    # the detail, never the verdict.
    top = query("* | sort by (_time desc) | limit 1 | fields _time",
                start=iso(now + timedelta(minutes=FUTURE_MIN)),
                end=iso(now + timedelta(days=7)))
    ahead = ""
    if top:
        try:
            mins = int((parse_time(top[0]["_time"]) - now).total_seconds() // 60)
            ahead = f" max-ahead={mins}m"
        except (KeyError, ValueError):
            pass
    names = ",".join(f"{a}:{n}" for a, n in sorted(future_apps.items()))
    findings.append(("vlti-future-time",
                     f"events-after-now+{FUTURE_MIN}m apps={names}{ahead}"))

# DUPLICATE: same app + instant + text more than once, last 24h.
dup = query("* | stats by (app, _time, _msg) count() as n | filter n:>1"
            " | stats by (app) count() as groups, sum(n) as rows",
            start=iso(now - timedelta(hours=24)), end=iso(now + timedelta(minutes=FUTURE_MIN)))
if dup is None:
    die("vlti-query-fail", "duplicate-query-no-200-after-retries")
dup_apps = {}
total_groups = 0
for row in dup:
    g = int(row.get("groups", 0) or 0)
    if g > 0:
        dup_apps[row.get("app", "?")] = (g, int(row.get("rows", 0) or 0))
        total_groups += g
if total_groups > DUP_MAX:
    names = ",".join(f"{a}:{g}groups/{r}rows" for a, (g, r) in sorted(dup_apps.items()))
    findings.append(("vlti-duplicates", f"dup-groups-24h={total_groups} apps={names}"))

# SKEW: newest stored _time per file vs the file's mtime. Only files written
# in the last hour but not in the last 10 minutes are judged: the ingester runs
# every 5 minutes, so anything older than 10 has been shipped and anything
# older than an hour may simply be idle.
latest = query("* | stats by (app, source_file) max(_time) as latest",
               start=iso(now - timedelta(hours=24)), end=iso(now + timedelta(days=7)))
if latest is None:
    die("vlti-query-fail", "skew-query-no-200-after-retries")
skewed = {}
for row in latest:
    path = row.get("source_file") or ""
    if not path or path.startswith("journalctl:") or not os.path.isabs(path):
        continue
    try:
        age = now.timestamp() - os.stat(path).st_mtime
        mtime = datetime.fromtimestamp(os.stat(path).st_mtime, timezone.utc)
        stored = parse_time(row["latest"])
    except (OSError, KeyError, ValueError):
        continue
    if not (600 <= age <= 3600):
        continue
    lag = (mtime - stored).total_seconds() / 60
    if lag > SKEW_MIN:
        skewed[row.get("app", "?")] = max(skewed.get(row.get("app", "?"), 0), int(lag))
if skewed:
    names = ",".join(f"{a}:{m}m-behind" for a, m in sorted(skewed.items()))
    findings.append(("vlti-time-skew", f"stored-time-behind-file-mtime-over-{SKEW_MIN}m apps={names}"))

if findings:
    for stage, msg in findings:
        print(f"STAGE={stage} msg={msg.replace(' ', '-')}", file=sys.stderr)
        logfail(f"{stage} {msg}")
    print("vlogs-time-integrity RED " + "; ".join(f"{s}: {m}" for s, m in findings))
    sys.exit(1)

print(f"vlogs-time-integrity ok future=0 dup_groups={total_groups} skewed=0")
sys.exit(0)
PY

#!/usr/bin/env python3
"""One-off (2026-08-20): triage the bazarr half of the BR-DISK ISO cascade.

WHY THIS EXISTS
---------------
On 2026-08-20 the REA log-reader paged with eleven findings, two of which
blamed bazarr2:

    RuntimeError: can't start new thread
    urllib3.exceptions.MaxRetryError: .../signalr/messages/negotiate ...

both read out of ~/.apps/bazarr2/logs/bazarr2.err. The same alert also carried
the 47.6 GB "In the Mouth of Madness (1995) BR-DISK.iso" that Radarr re-graded
to BR-DISK on import, so the obvious reading was "the ISO is starving the box
of threads and bazarr2 is the first casualty". Measured live, that reading is
WRONG in both directions, and this script exists to keep the measurement
reproducible rather than a one-time claim in a chat log:

  1. The bazarr2 errors are STALE. bazarr2.err is append-only since
     2026-05-11 and has NEVER been rotated (8.4 MB, 36,239 lines). Its last
     error line is dated 2026-08-18 21:18:42 box-local; the running bazarr2
     process started 2026-08-18 21:19:27 - forty-five seconds LATER. Every
     error line in the file therefore belongs to a process that no longer
     exists, and the live process has logged zero errors in 37+ hours. The two
     "can't start new thread" hits are 2026-08-18 06:39:05, the boot storm
     after the host reboot, where bazarr2 came up before radarr2/sonarr2 were
     listening (hence the paired "Connection refused" on the SignalR
     negotiate) and its reconnect thread lost the pthread_create race against
     everything else starting at once. The other two in the file's whole
     lifetime are 2026-06-22. None of them can be ISO-related: the ISO landed
     2026-08-20 07:14 UTC, nearly two days after the newest of them.

  2. bazarr is not the thread producer. Sampled three times over four minutes,
     bazarr2 held a flat 78 tasks against 37.6 h of uptime and bazarr-1 a flat
     176 against 49.4 h - neither is leaking. The live pressure was two
     concurrent Tdarr ffmpeg jobs at 273 and 129 tasks (402 of the 1,414 total
     against ulimit -u 2000). One of those two ffmpegs is transcoding the ISO
     itself, so the ISO does consume the ceiling - just through Tdarr, not
     through bazarr.

  3. bazarr IS however holding the ISO, and that part is real and current.
     bazarr-1 (the movies instance, the CONTAINER one) has table_movies
     radarrId 441 pointing at the .iso path, and logged a fresh error at
     2026-08-20 09:14:11 box-local:
         BAZARR Error ('.iso' is not a valid video extension) trying to get
         video information for this file: .../BR-DISK.iso
     bazarr2 (series) has no movie row for it at all. That error is a GOOD
     alarm - it is the only thing in the stack that noticed a non-video file
     had entered the movie library - so it is deliberately NOT suppressed and
     NOT worked around here. It clears when the ISO leaves the library, which
     is the Radarr-side remediation's job, not this script's.

WHAT THIS SCRIPT DOES
---------------------
Three read-only audits, always, on every run:

  A. bazarr2.err staleness   - newest error line vs. the running process's
                               start time. Proves stale-or-live rather than
                               asserting it.
  B. non-video library rows  - both bazarr DBs, read-only, for disc-image and
                               disc-folder extensions in table_movies /
                               table_episodes. Names the ISO row (and any
                               sibling) with its radarrId/sonarrEpisodeId.
  C. thread census           - tasks vs. ulimit -u, the top task producers,
                               and each producer's tasks-per-hour so a leak is
                               visible as a ratio rather than a raw count.

NOTHING HERE MAY REPORT CLEAN WHEN IT COULD NOT MEASURE
------------------------------------------------------
Every audit returns an explicit error channel, and any error makes the whole
run exit 2 (unknown) instead of 0 (clean). This is not theoretical
defensiveness - the first cut of this script failed OPEN, and it was proved by
forcing run_remote to rc=255: main() returned 0 while printing "bazarr2.err
errors live? no (stale)" and "non-video rows: 0". A total SSH failure
manufactured a clean bill of health for a box the script never reached. The
staleness verdict already failed toward LIVE on an unreadable clock; that is
now the discipline everywhere:

  - run_remote catches TimeoutExpired and OSError and reports them as a
    non-zero rc rather than raising through the audits;
  - every audit treats rc != 0, an unparseable payload, or a sqlite stderr
    line as a hard error;
  - main() prints UNKNOWN (never "no") for any audit that errored and returns
    2;
  - --execute is refused outright if any audit errored, because a mutation
    driven by measurements that failed is a mutation taken blind.

One remedy, only under --execute:

  D. archive-and-truncate bazarr2.err.

     SCOPE OF THE PROBLEM, STATED HONESTLY. REA already bounds how far the
     dead-process tracebacks can page: it reads this file through `tail -n
     360` AND applies a line-date filter with FreshDays=3
     (scripts/local-llm/qflix-rea.ps1), so any given error line can only be
     reported for at most three days after it was written. The claim in the
     first draft - that the 360-line window "cannot roll past the 2026-08-18
     tracebacks for MONTHS" - was wrong about the consequence: the window does
     retain them for months, but the date filter stops them being findings
     after 3 days. The real cost is therefore bounded and modest: up to three
     days of paging per *arr restart storm, plus an 8.4 MB file that grows
     forever.

     THE DURABLE FIX IS A LOGROTATE ENTRY, NOT THIS TRUNCATE. Root cause found
     2026-08-20: the box already runs user logrotate weekly
     (~/.config/systemd/user/logrotate.timer ->
     /usr/sbin/logrotate -s ~/.logrotate.status ~/.config/logrotate.conf) and
     that config already sets `copytruncate` as a global default. bazarr2 is
     simply not matched by it. The generated conf carries

         "/home/quadstronaut/.apps/bazarr2/log/*.log" { size 50M }

     but bazarr2's log directory is `logs` (PLURAL) and the file is `.err`,
     not `.log`. The pattern matches nothing, `missingok` swallows it silently,
     and ~/.logrotate.status still holds the raw unexpanded glob - proof it
     has never matched a file. bazarr-1 uses the singular `log/` dir, so ITS
     block works, which is why only bazarr2's log grew unbounded.

     The fix belongs in scripts/configure/250-logrotate-install.sh (the
     generator; NOT this file's lane), adding blocks for
     "~/.apps/bazarr2/logs/*.log" and "~/.apps/bazarr2/logs/*.err" and
     dropping the dead singular-`log` bazarr2 block. They inherit the global
     rotate 7 / maxage 7 / copytruncate, which is exactly the operation this
     script performs by hand - so once that lands, --execute here becomes a
     one-time catch-up for the existing 8.4 MB backlog and nothing more.

     Safe without a restart because both holders (the supervisor pid and the
     bazarr2 main pid) hold fd 2 with O_APPEND set - verified live as flags
     0102001 = O_WRONLY|O_APPEND|O_LARGEFILE. Under O_APPEND the kernel seeks
     to end-of-file on every write, so a truncated file resumes at offset 0
     with no sparse NUL hole. That is exactly logrotate's copytruncate. The
     script RE-VERIFIES O_APPEND on every holder at execute time and refuses
     to truncate if any holder lacks it, because getting this wrong leaves an
     8.4 MB NUL-padded file that is worse than what we started with.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
  - No service restart. bazarr2 is a live subtitle service; the lead decides.
  - No write to either bazarr DB. Both are open by running services and both
    resync from their *arr, so a hand-deleted row returns on the next SignalR
    event - a write that cannot stick is a write not worth the risk.
  - No provider changes. tvsubtitles / greeksubtitles / hosszupuska stay
    disabled.
  - No touching of the ISO, Radarr, or Tdarr. Different lane.
  - No suppression of the '.iso is not a valid video extension' error.
  - No edit to logrotate.conf or its generator. Named above, handed off.

USAGE
-----
    python scripts/ops/remediate-2026-08-20-bazarr.py            # audit only
    python scripts/ops/remediate-2026-08-20-bazarr.py --execute  # + truncate

Exit codes:
    0  audits all completed AND all clean, or --execute completed and verified
    1  audits all completed and found something an operator should look at
       (non-video row in a bazarr library, live bazarr2 errors, or thread use
       at/over the canary's fail trip)
    2  UNKNOWN - an audit could not be completed (ssh failure, timeout,
       unparseable payload, sqlite error), a guard tripped, or the truncate
       did not verify. Never treat 2 as clean.

Idempotent. A second --execute on an already-empty bazarr2.err reports
"already empty (ok)" and skips the archive.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Mirrors scripts/lib/ssh.sh: the real FQDN lives in gitignored secrets, and
# the public repo falls back to a sanitized placeholder so this file stays
# readable without leaking the operator's host.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30"]
SSH_USER = "quadstronaut"

# Disc images and disc folders. Bazarr's video-extension whitelist rejects all
# of these, so any row carrying one is a file bazarr can never read.
NONVIDEO_PATTERNS = ["%.iso", "%.img", "%.bin", "%.nrg", "%VIDEO_TS%", "%BDMV%"]

# Two DBs x two tables (table_movies, table_episodes). The scan must report
# this many `scan_ok=` markers or the result is inconclusive - see
# audit_nonvideo().
EXPECTED_SCANS = 4

# Must track scripts/canaries/thread-ceiling.sh. Kept here as literals rather
# than parsed out of the shell script: this is a one-off audit, and a silent
# drift between the two is less harmful than a fragile parse that breaks the
# audit entirely. Re-check by hand if the canary moves.
#
# The canary compares INTEGER task counts against a trip point derived once
# from the percentage (limit * pct // 100); this file must do the identical
# integer comparison. The earlier version compared a raw float percentage
# here while the canary compared an awk-rounded one-decimal string, so at
# threads=1559/2000 the two disagreed about whether the canary would fail.
CANARY_WARN_PCT = 65
CANARY_FAIL_PCT = 85
# Consecutive over-trip samples the canary needs before it actually pages.
# A single audit run is one sample, so it can only ever say "would arm".
CANARY_STREAK = 2

# Sentinel rc for "the ssh call never produced an exit status of its own"
# (timeout, or ssh could not be launched at all). Distinct from 255, which is
# ssh's own transport-failure code, so the two are distinguishable in output.
RC_NO_EXIT = -1


def ssh_host():
    """Resolve the seedbox SSH target the same way scripts/lib/ssh.sh does."""
    secrets = REPO / "secrets"
    for name in ("seedbox.ssh-host", "seedbox.host"):
        f = secrets / name
        if f.is_file():
            fqdn = f.read_text(encoding="utf-8").strip()
            if fqdn:
                return SSH_USER + "@" + fqdn
    return SSH_USER + "@seedbox.example.com"


def run_remote(payload, timeout=120):
    """Ship a bash program over stdin and return (rc, stdout, stderr).

    stdin rather than `ssh host bash -c '...'` on purpose: the -c form makes
    every apostrophe in the payload a quoting hazard, and this repo has been
    burned by exactly that. `bash -s` reads the program as data, so quoting in
    the payload is bash's problem only, never ssh's or PowerShell's.
    Encoded BOM-less UTF-8 because a BOM lands in argv[0] of the first line
    and bash reports a bogus command-not-found.

    Never raises. A timeout or a missing/unlaunchable ssh binary comes back as
    rc=RC_NO_EXIT with the reason in stderr, so callers see the same "rc != 0"
    shape they already handle instead of an exception unwinding past the
    summary - or, worse, a caller that swallows it and reports clean.
    """
    cmd = ["ssh"] + SSH_OPTS + [ssh_host(), "bash", "-s"]
    try:
        proc = subprocess.run(
            cmd, input=payload.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (RC_NO_EXIT, "", "ssh timed out after %ds" % timeout)
    except OSError as exc:
        return (RC_NO_EXIT, "", "could not launch ssh: %r" % (exc,))
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            proc.stderr.decode("utf-8", "replace"))


def _ssh_error(rc, err):
    """One-line label for a failed remote call, for the hard-error channel."""
    return "ssh rc=%d %s" % (rc, " ".join(err.split())[:160] or "(no stderr)")


def kv(out):
    """Parse `key=value` lines. Unknown/blank lines are ignored, not fatal."""
    d = {}
    for line in out.splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            d[k.strip()] = v.strip()
    return d


def in_maintenance_window(now=None):
    """Mon 11:00-15:00 UTC, per manitoba-maint-window{,-watchdog}.timer.

    Operator directive: no box operations during the window. Duplicated here
    as a two-line predicate rather than importing lib/window.py, which pulls
    in deep_check/health/kuma/lifecycle/listmonk and would make a read-only
    audit depend on the whole maintenance stack.
    """
    now = now or datetime.now(timezone.utc)
    return now.weekday() == 0 and 11 <= now.hour < 15


# ---------------------------------------------------------------------------
# A. bazarr2.err staleness
# ---------------------------------------------------------------------------

AUDIT_STALENESS = r"""
set -uo pipefail
R=$(cd ~ && pwd -P)
F=$R/.apps/bazarr2/logs/bazarr2.err
echo "err_path=$F"
if [ ! -f "$F" ]; then echo "err_present=no"; exit 0; fi
echo "err_present=yes"
echo "err_bytes=$(stat -c %s "$F")"
echo "err_lines=$(wc -l < "$F")"
# The bazarr2.err line format is "YYYY-MM-DD HH:MM:SS,mmm - ...", so the first
# 19 characters are a sortable timestamp. Traceback bodies are undated and are
# skipped by the date-shape test rather than fail-open-kept: here we want the
# newest DATED error marker, and an undated continuation carries no time.
LAST_ERR=$(grep -aE "ERROR|^[A-Za-z_.]+(Error|Exception):" "$F" \
           | grep -aoE "^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}" \
           | tail -n 1)
echo "err_last_error_ts=${LAST_ERR:-none}"
echo "err_thread_errors_total=$(grep -ac "can.t start new thread" "$F")"
echo "err_thread_errors_lastline=$(grep -an "can.t start new thread" "$F" | tail -n 1 | cut -d: -f1)"
# Newest process whose argv names bazarr2's main.py. etimes (seconds of
# uptime) is used for the machine comparison because it needs no locale or
# timezone parsing at all.
PID=$(pgrep -u "$(id -u)" -f "bazarr2/bin/bazarr/main.py" | tail -n 1)
echo "proc_pid=${PID:-none}"
if [ -n "${PID:-}" ]; then
  UP=$(ps -o etimes= -p "$PID" | tr -d " ")
  echo "proc_uptime_s=$UP"
  echo "proc_started_utc=$(date -u -d "@$(( $(date +%s) - UP ))" +%FT%TZ)"
  echo "proc_threads=$(ls /proc/$PID/task 2>/dev/null | wc -l)"
fi
echo "now_utc=$(date -u +%FT%TZ)"
echo "now_local=$(date +%FT%T)"
"""


def audit_staleness():
    """Returns (has_live_errors, facts, error).

    `error` is None on a completed audit, or a short label when the audit
    could not be completed at all. A non-None error means the boolean is
    meaningless and the caller must not report it as a verdict.
    """
    rc, out, err = run_remote(AUDIT_STALENESS)
    print("--- A. bazarr2.err staleness -------------------------------------")
    if rc != 0:
        label = _ssh_error(rc, err)
        print("  HARD ERROR: " + label
              + " - staleness UNKNOWN, not assessed", file=sys.stderr)
        return (False, {}, label)
    f = kv(out)
    if not f:
        label = "empty payload (no key=value lines returned)"
        print("  HARD ERROR: " + label, file=sys.stderr)
        return (False, {}, label)
    if f.get("err_present") != "yes":
        print("  bazarr2.err absent - nothing to assess")
        return (False, f, None)
    print("  file            : " + f.get("err_path", "?"))
    print("  size            : " + f.get("err_bytes", "?") + " bytes, "
          + f.get("err_lines", "?") + " lines")
    print("  newest ERROR    : " + f.get("err_last_error_ts", "?") + " (box-local)")
    print("  thread errors   : " + f.get("err_thread_errors_total", "?")
          + " in the file's whole lifetime, newest at line "
          + f.get("err_thread_errors_lastline", "-"))
    print("  running pid     : " + f.get("proc_pid", "?")
          + "  started " + f.get("proc_started_utc", "?")
          + "  (" + f.get("proc_uptime_s", "?") + "s, "
          + f.get("proc_threads", "?") + " tasks)")

    # The verdict. An error line older than the process that would have
    # written it cannot describe the running service - it is a corpse. This is
    # the whole point of the audit, so it is computed, not assumed.
    ts = f.get("err_last_error_ts", "none")
    started = f.get("proc_started_utc", "")
    if ts == "none" or not started:
        print("  VERDICT         : UNKNOWN (no dated error, or no running "
              "process) - treating as not-live")
        return (False, f, None)
    try:
        # bazarr2.err timestamps are box-local; proc_started_utc is UTC. Derive
        # the offset from the two clocks the payload sampled in the same run,
        # so a DST change or a relocated box needs no constant here.
        local_now = datetime.strptime(f.get("now_local", ""), "%Y-%m-%dT%H:%M:%S")
        utc_now = datetime.strptime(f.get("now_utc", ""), "%Y-%m-%dT%H:%M:%SZ")
        offset_s = round((local_now - utc_now).total_seconds() / 3600.0) * 3600
        err_local = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        err_utc = err_local.replace(tzinfo=timezone.utc).timestamp() - offset_s
        proc_utc = (datetime.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp())
    except ValueError as exc:
        # Fail toward "live": a clock we cannot read must not be able to
        # silence a real error.
        print("  VERDICT         : UNKNOWN (clock parse: " + repr(exc)
              + ") - treating as LIVE, the safe direction")
        return (True, f, None)

    delta = int(proc_utc - err_utc)
    if delta <= 0:
        print("  VERDICT         : LIVE - newest error postdates the process "
              "start by " + str(-delta) + "s")
        return (True, f, None)
    print("  VERDICT         : STALE - newest error PREDATES the running "
          "process by " + str(delta) + "s; every error line in this file "
          "belongs to a dead process")
    return (False, f, None)


# ---------------------------------------------------------------------------
# B. non-video rows in the bazarr libraries
# ---------------------------------------------------------------------------

NONVIDEO_TEMPLATE = r"""
set -uo pipefail
R=$(cd ~ && pwd -P)
scan() {
  DB="$1"; LABEL="$2"
  if [ ! -f "$DB" ]; then echo "db_error=$LABEL:db-file-missing:$DB"; return 0; fi
  # Two tables, same shape. Iterated rather than written twice so the stderr
  # capture below exists in exactly one place.
  for SPEC in "movie:table_movies:radarrId" "episode:table_episodes:sonarrEpisodeId"; do
    KIND=${SPEC%%:*}; REST=${SPEC#*:}; TAB=${REST%%:*}; IDCOL=${REST##*:}
    OUT=$(mktemp) || { echo "db_error=$LABEL/$TAB:mktemp-failed"; continue; }
    # sqlite3's stderr is CAPTURED, never discarded. It used to be sent to
    # /dev/null, which meant a renamed table or column (bazarr migrates its
    # schema on upgrade) turned this audit into a permanent
    # "none - both bazarr libraries are all-video" - a clean verdict produced
    # by a query that never ran. `2>&1 >"$OUT"` points stderr at the command
    # substitution FIRST, then stdout at the temp file, so the two separate.
    ERRTXT=$(sqlite3 -readonly -separator "|" "file:$DB" \
      "SELECT '$LABEL','$KIND',$IDCOL,title,path FROM $TAB WHERE __WHERE__;" 2>&1 >"$OUT")
    RC=$?
    cat "$OUT"
    rm -f "$OUT"
    if [ "$RC" -ne 0 ] || [ -n "$ERRTXT" ]; then
      MSG=$(printf "%s" "$ERRTXT" | head -n 1 | tr "|" " ")
      echo "db_error=$LABEL/$TAB:rc$RC:${MSG:-no-stderr}"
    else
      # POSITIVE EVIDENCE that this query actually ran. Absence of db_error is
      # not proof of success: an ssh that exits 0 with no output at all (a
      # truncated session, a MaxSessions refusal that still returns 0) would
      # otherwise be read as "zero non-video rows, everything is fine". The
      # caller counts these and refuses to report clean unless all four are
      # present.
      echo "scan_ok=$LABEL/$TAB"
    fi
  done
}
scan "$R/.apps/bazarr/db/bazarr.db" bazarr-1
scan "$R/.apps/bazarr2/data/db/bazarr.db" bazarr2
"""


def audit_nonvideo():
    """Read-only sqlite scan of both bazarr DBs. Returns (count, rows, error).

    A missing DB file is a hard error, not an informational note: this script
    is dated for a box that demonstrably has both instances, and "I could not
    open the library" must never render as "the library is clean".
    """
    # -readonly still attaches the WAL, so rows committed by the running
    # service but not yet checkpointed ARE visible (established in the
    # bazarr-two-instances work). Without that, a freshly-imported movie would
    # look absent - which is precisely the row we are hunting.
    where = " OR ".join(["path LIKE '" + p + "'" for p in NONVIDEO_PATTERNS])
    payload = NONVIDEO_TEMPLATE.replace("__WHERE__", where)
    rc, out, err = run_remote(payload)
    print("--- B. non-video rows in the bazarr libraries ---------------------")
    if rc != 0:
        label = _ssh_error(rc, err)
        print("  HARD ERROR: " + label
              + " - library contents UNKNOWN, not scanned", file=sys.stderr)
        return (0, [], label)
    rows = []
    db_errors = []
    scans_ok = 0
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("scan_ok="):
            scans_ok += 1
            continue
        if line.startswith("db_error="):
            db_errors.append(line.partition("=")[2])
            print("  DB ERROR: " + line.partition("=")[2], file=sys.stderr)
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        rows.append(parts)
        print("  " + parts[0] + " " + parts[1] + " id=" + parts[2] + " " + parts[3])
        print("      " + parts[4])
    if db_errors:
        # Any query that did not run makes the whole scan inconclusive. Report
        # whatever rows the surviving queries did return - they are still real
        # findings - but refuse to call the result clean.
        return (len(rows), rows,
                "sqlite: " + "; ".join(db_errors[:3]))
    if scans_ok != EXPECTED_SCANS:
        label = ("only %d of %d scans reported success - the payload did not "
                 "run to completion" % (scans_ok, EXPECTED_SCANS))
        print("  HARD ERROR: " + label, file=sys.stderr)
        return (len(rows), rows, label)
    if not rows:
        print("  none - both bazarr libraries are all-video "
              "(all four queries ran clean)")
    else:
        print("  NOTE: bazarr logs \"'.iso' is not a valid video extension\" "
              "once per scan for each of these. That error is a CORRECT alarm "
              "for a non-video file in a media library and is deliberately "
              "not suppressed. It clears when the file leaves the library, "
              "which is the Radarr-side fix, not a bazarr fix.")
    return (len(rows), rows, None)


# ---------------------------------------------------------------------------
# C. thread census
# ---------------------------------------------------------------------------

AUDIT_THREADS = r"""
set -uo pipefail
U=$(id -u)
LIMIT=$(ulimit -u)
# RLIMIT_NPROC is enforced against every TASK the user owns, thread or
# process, so -L (one row per thread) is the number that matters. `ps -u`
# alone undercounts by an order of magnitude on this box.
THREADS=$(ps -u "$U" -L --no-headers 2>/dev/null | wc -l)
PROCS=$(ps -u "$U" --no-headers 2>/dev/null | wc -l)
echo "limit=$LIMIT"
echo "threads=$THREADS"
echo "procs=$PROCS"
for p in $(ps -u "$U" -o pid=); do
  n=$(ls /proc/$p/task 2>/dev/null | wc -l)
  [ "$n" -ge 40 ] || continue
  et=$(ps -o etimes= -p "$p" 2>/dev/null | tr -d " ")
  cm=$(ps -o comm= -p "$p" 2>/dev/null)
  echo "top|$n|$p|${et:-0}|$cm"
done | sort -t"|" -k2 -rn | head -n 10
"""


def audit_threads():
    """Returns (at_or_over_fail_trip, facts, error)."""
    rc, out, err = run_remote(AUDIT_THREADS)
    print("--- C. thread census vs ulimit -u --------------------------------")
    if rc != 0:
        label = _ssh_error(rc, err)
        print("  HARD ERROR: " + label
              + " - thread headroom UNKNOWN, not sampled", file=sys.stderr)
        return (False, {}, label)
    f = kv(out)
    try:
        limit = int(f.get("limit", "0"))
        threads = int(f.get("threads", "0"))
    except ValueError:
        label = "unparseable census (limit=%r threads=%r)" % (
            f.get("limit"), f.get("threads"))
        print("  HARD ERROR: " + label, file=sys.stderr)
        return (False, f, label)
    if limit <= 0 or threads <= 0:
        label = "ulimit -u or task count unreadable (limit=%d threads=%d)" % (
            limit, threads)
        print("  HARD ERROR: " + label, file=sys.stderr)
        return (False, f, label)
    # Integer trip point, identical arithmetic to the canary's
    # TRIP_FAIL=$(( LIMIT * FAIL_PCT / 100 )). No float percentage is compared
    # against a threshold anywhere in either program.
    trip_fail = limit * CANARY_FAIL_PCT // 100
    trip_warn = limit * CANARY_WARN_PCT // 100
    pct = 100.0 * threads / limit
    print("  tasks           : " + str(threads) + "/" + str(limit)
          + " (" + ("%.1f" % pct) + "%), " + f.get("procs", "?") + " processes")
    print("  headroom        : " + str(limit - threads) + " tasks")
    biggest = 0
    for line in out.splitlines():
        if not line.startswith("top|"):
            continue
        fields = line.split("|")
        if len(fields) < 5:
            continue
        n, pid, et, comm = fields[1], fields[2], fields[3], fields[4]
        try:
            n_i = int(n)
        except ValueError:
            continue
        biggest = max(biggest, n_i)
        # tasks-per-hour makes a leak visible as a ratio: a steady-state
        # service sits flat regardless of uptime, a leaker climbs with it.
        hours = max(int(et or "0"), 1) / 3600.0
        print("    " + n.rjust(4) + " tasks  pid " + pid.ljust(8)
              + " up " + ("%7.1f" % hours) + "h  "
              + ("%7.1f" % (n_i / hours)) + " tasks/h  " + comm)
    print("  largest single producer: " + str(biggest) + " tasks")
    # Read tasks/h only for LONG-LIVED processes. A Tdarr ffmpeg allocates its
    # whole thread pool in the first seconds, so at 0.2h of uptime it scores
    # ~1200 tasks/h and looks like a runaway leaker when it is simply young.
    # The leak signal is a service whose count climbs across HOURS of uptime.
    print("  (tasks/h is only meaningful above ~1h uptime; a young ffmpeg "
          "allocates its pool at once and inflates the ratio)")
    over = threads >= trip_fail
    if over:
        verdict = ("OVER the fail trip - one sample only, so the canary would "
                   "ARM its streak; it pages on the %dth consecutive sample"
                   % CANARY_STREAK)
    elif threads >= trip_warn:
        verdict = "PASS-WARN (over the warn trip, under the fail trip)"
    else:
        verdict = "PASS"
    print("  canary would report: " + verdict)
    print("    warn trip " + str(trip_warn) + " tasks (" + str(CANARY_WARN_PCT)
          + "%), fail trip " + str(trip_fail) + " tasks ("
          + str(CANARY_FAIL_PCT) + "%), streak " + str(CANARY_STREAK))
    if biggest:
        print("  fail trip leaves " + str(limit - trip_fail)
              + " tasks of headroom = "
              + ("%.2f" % ((limit - trip_fail) / float(biggest)))
              + "x the largest observed single producer (" + str(biggest)
              + " tasks). Above 1.0 means one more spawn from the trip point "
              "cannot reach EAGAIN; the "
              + str(CANARY_STREAK) + "-sample streak is what covers the rest, "
              "since a healthy box under the Tdarr schedule's 4-worker grant "
              "can legitimately sit above the trip for a single interval.")
    return (over, f, None)


# ---------------------------------------------------------------------------
# D. remedy - archive and truncate bazarr2.err
# ---------------------------------------------------------------------------

REMEDY_TRUNCATE = r"""
set -uo pipefail
R=$(cd ~ && pwd -P)
F=$R/.apps/bazarr2/logs/bazarr2.err
ARCHDIR=$R/.opt/maint/bazarr2
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
ARCH=$ARCHDIR/bazarr2.err.$STAMP

[ -f "$F" ] || { echo "result=absent"; exit 0; }
BEFORE=$(stat -c %s "$F")
echo "before_bytes=$BEFORE"
if [ "$BEFORE" -eq 0 ]; then echo "result=already-empty"; exit 0; fi

# GUARD 1 - every holder must have O_APPEND. Under O_APPEND the kernel seeks
# to EOF before each write, so a truncated file resumes at offset 0. Without
# it the holder keeps its old offset and the next write lands at ~8.4 MB,
# leaving a NUL-padded sparse file strictly worse than the pile we set out to
# remove. Octal flags in fdinfo: O_APPEND is 02000.
HOLDERS=0; BAD=0
for p in $(ps -u "$(id -u)" -o pid=); do
  for fd in /proc/$p/fd/*; do
    t=$(readlink "$fd" 2>/dev/null) || continue
    [ "$t" = "$F" ] || continue
    HOLDERS=$((HOLDERS + 1))
    N=$(basename "$fd")
    FL=$(sed -n "s/^flags:[[:space:]]*//p" "/proc/$p/fdinfo/$N" 2>/dev/null)
    if [ -z "$FL" ] || [ $(( 8#$FL & 8#2000 )) -eq 0 ]; then
      echo "holder_no_append=pid$p:fd$N:flags${FL:-unknown}"
      BAD=$((BAD + 1))
    else
      echo "holder_ok=pid$p:fd$N:flags$FL"
    fi
  done
done 2>/dev/null
echo "holders=$HOLDERS"
if [ "$BAD" -gt 0 ]; then echo "result=abort-no-append"; exit 3; fi
# Zero holders means nothing is writing this file - either bazarr2 is down or
# the path moved. Refuse rather than silently rotating a file whose writer we
# could not identify.
if [ "$HOLDERS" -eq 0 ]; then echo "result=abort-no-holders"; exit 3; fi

mkdir -p "$ARCHDIR" || { echo "result=abort-mkdir"; exit 4; }
cp "$F" "$ARCH" || { echo "result=abort-copy"; exit 5; }

# GUARD 2 - the archive must match the original size before the original is
# destroyed. A short copy (full disk, quota) that went unchecked would trade a
# noisy log for a lost one. The file may have grown a line between stat and
# cp, so the archive is allowed to be >= BEFORE, never smaller.
ASZ=$(stat -c %s "$ARCH")
echo "archive=$ARCH"
echo "archive_bytes=$ASZ"
if [ "$ASZ" -lt "$BEFORE" ]; then echo "result=abort-short-copy"; exit 6; fi

truncate -s 0 "$F" || { echo "result=abort-truncate"; exit 7; }

# VERIFY BY RE-READING. House rule: never trust the write, re-read it. A
# non-trivial size here right after a truncate would mean a holder wrote at a
# stale offset and GUARD 1 was wrong; the small allowance absorbs the log
# lines bazarr2 legitimately appends in the intervening milliseconds.
AFTER=$(stat -c %s "$F")
echo "after_bytes=$AFTER"
if [ "$AFTER" -gt 65536 ]; then echo "result=verify-failed"; exit 8; fi
echo "result=truncated"
"""


def remedy_truncate(dry_run):
    print("--- D. remedy: archive + truncate bazarr2.err --------------------")
    if dry_run:
        print("  DRY RUN - would copy ~/.apps/bazarr2/logs/bazarr2.err to")
        print("            ~/.opt/maint/bazarr2/bazarr2.err.<UTC>, verify the")
        print("            copy, then truncate the original to 0 bytes.")
        print("  Rerun with --execute to do it. No restart is involved: both")
        print("  holders keep fd 2 open with O_APPEND, which is re-verified at")
        print("  execute time and aborts the run if it does not hold.")
        print("  SCOPE: this is a one-time catch-up, not the durable fix. REA")
        print("         bounds the blast radius to <=3 days per restart storm")
        print("         (tail -n 360 + FreshDays=3 line-date filter). The")
        print("         durable fix is a logrotate entry - see the module")
        print("         docstring: ~/.config/logrotate.conf globs")
        print("         .apps/bazarr2/log/*.log, but the real path is")
        print("         .apps/bazarr2/logs/bazarr2.err, so it has never")
        print("         matched. Fix belongs in")
        print("         scripts/configure/250-logrotate-install.sh.")
        return 0
    rc, out, err = run_remote(REMEDY_TRUNCATE, timeout=300)
    f = kv(out)
    for line in out.splitlines():
        if line.startswith("holder_"):
            print("  " + line)
    result = f.get("result", "unknown")
    if rc == RC_NO_EXIT:
        print("  FAILED: " + _ssh_error(rc, err)
              + " - remedy state UNKNOWN, re-run the audit before retrying",
              file=sys.stderr)
        return 2
    if result == "absent":
        print("  bazarr2.err absent - nothing to do")
        return 0
    if result == "already-empty":
        print("  already empty (ok) - idempotent no-op")
        return 0
    if result == "truncated":
        print("  archived : " + f.get("archive", "?")
              + " (" + f.get("archive_bytes", "?") + " bytes)")
        print("  truncated: " + f.get("before_bytes", "?") + " -> "
              + f.get("after_bytes", "?") + " bytes, verified by re-stat")
        print("  holders  : " + f.get("holders", "?") + ", all O_APPEND")
        print("  REMINDER : without the logrotate entry named in the module")
        print("             docstring, this file grows unbounded again.")
        return 0
    print("  FAILED: result=" + result + " rc=" + str(rc) + " " + err.strip(),
          file=sys.stderr)
    return 2


def main():
    ap = argparse.ArgumentParser(
        description="Audit the bazarr side of the 2026-08-20 BR-DISK ISO "
                    "cascade; optionally rotate the never-rotated bazarr2.err.")
    ap.add_argument("--execute", action="store_true",
                    help="perform the archive+truncate remedy "
                         "(default: audit only)")
    ap.add_argument("--force-window", action="store_true",
                    help="allow --execute inside the Monday maintenance "
                         "window (operator override; audits never need it)")
    args = ap.parse_args()

    print("remediate-2026-08-20-bazarr.py  mode="
          + ("EXECUTE" if args.execute else "DRY-RUN")
          + "  " + datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print()

    if args.execute and in_maintenance_window() and not args.force_window:
        print("REFUSING: inside the Monday maintenance window "
              "(Mon 11:00-15:00 UTC). Operator directive is no box operations "
              "during the window. Run before or after it, or pass "
              "--force-window.", file=sys.stderr)
        return 2

    hard = []
    live_errors, _, e_a = audit_staleness()
    if e_a:
        hard.append("A(staleness): " + e_a)
    print()
    nonvideo_count, _, e_b = audit_nonvideo()
    if e_b:
        hard.append("B(non-video): " + e_b)
    print()
    over_fail, _, e_c = audit_threads()
    if e_c:
        hard.append("C(threads): " + e_c)
    print()

    # A mutation driven by measurements that failed is a mutation taken blind.
    # The remedy is skipped entirely - not merely reported - when any audit
    # could not complete.
    if hard and args.execute:
        print("--- D. remedy: archive + truncate bazarr2.err --------------------")
        print("  SKIPPED: an audit could not be completed; refusing to mutate "
              "the box on unknown state.", file=sys.stderr)
        rc = 0
    else:
        rc = remedy_truncate(dry_run=not args.execute)
    print()

    print("--- summary ------------------------------------------------------")
    print("  bazarr2.err errors live?     "
          + ("UNKNOWN" if e_a else ("YES" if live_errors else "no (stale)")))
    print("  non-video rows in bazarr:    "
          + (str(nonvideo_count) + " (INCOMPLETE SCAN)" if e_b
             else str(nonvideo_count)))
    print("  thread use at/over canary fail trip: "
          + ("UNKNOWN" if e_c else ("YES" if over_fail else "no")))
    if hard:
        print()
        print("  RESULT: UNKNOWN (exit 2). One or more audits did not run:")
        for h in hard:
            print("    - " + h)
        print("  Do NOT read this run as a clean bill of health.")
        return 2
    if rc:
        return rc
    return 1 if (live_errors or nonvideo_count or over_fail) else 0


if __name__ == "__main__":
    sys.exit(main())

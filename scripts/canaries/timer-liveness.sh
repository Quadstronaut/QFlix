#!/usr/bin/env bash
# Timer-liveness canary: the generic dead-man for EVERY scheduled job.
#
# WHY: manifest/jobs.yaml is the timer <-> dead-man ledger, and four entries were
# adjudicated `open_gap: true` — a scheduled job that can stop with nothing
# noticing. Their own reasons say it plainly:
#
#   manitoba-maint-window          "if BOTH the window and the watchdog fail to
#                                   start, nothing pages"
#   manitoba-maint-window-watchdog "it is itself a watcher with no watcher —
#                                   THE TURTLE PROBLEM"
#   manitoba-maint-arr-audit       "a permanently-failing unit is
#                                   indistinguishable from a clean week"
#   manitoba-maint-flaresolverr-canary
#                                  "the ONLY signal is the absence of Discord
#                                   messages nobody was expecting anyway"
#
# The obvious fix — one Kuma monitor per gap — is the wrong shape. It adds four
# more monitors, four more push tokens, and four more things that can be born
# mute or born tokenless (both happened on 2026-07-30). It also does nothing for
# the NEXT timer somebody adds.
#
# So this watches timer liveness GENERICALLY, driven off manifest/jobs.yaml, and
# terminates the turtle chain in the one place it can legitimately end: Kuma
# itself. This canary pushes a heartbeat, so if IT stops, Kuma flips it DOWN with
# no heartbeat in the window — the dead-man dead-mans itself. That is the only
# non-circular termination available, and it is externally visible on the public
# status page.
#
# PREDICATES — deliberately cadence-INDEPENDENT. Encoding each timer's interval
# would duplicate its OnCalendar into a second policy surface, and this repo's
# recurring defect is exactly two surfaces drifting apart. systemd already knows
# the schedule; ask it about health instead:
#
#   1. LOADED    — every timer named in jobs.yaml must exist on the box. A unit
#                  that was never installed cannot fire (sab-stall, tdarr-scanner
#                  and tdarr-healthcheck all shipped uninstalled for weeks).
#   2. ACTIVE    — an inactive/disabled/masked timer will never fire again,
#                  whatever its schedule says.
#   3. NEXT      — an active timer with no next-elapse is stuck: systemd has
#                  nothing scheduled, so it is dead in place.
#   4. RESULT    — the timer's .service last exited success. A unit failing every
#                  run is the arr-audit shape: it runs, it fails, nobody hears.
#
# Predicate 4 is a WARN not a FAIL: a canary service legitimately exits non-zero
# to report ITS subject being down, and that is already someone else's monitor.
# Escalating it here would double-page one fault.
#
# Stage labels (stderr -> Kuma msg=):
#   STAGE=timer-missing    — declared in jobs.yaml, not loaded on the box
#   STAGE=timer-inactive   — loaded but not active; will never fire again
#   STAGE=timer-stuck      — active but no next elapse scheduled
#   STAGE=timer-read-fail  — could not read the ledger or systemd
#
# Exit:
#   0 — every declared timer is loaded, active and scheduled (failed .service
#       results are reported as PASS-WARN)
#   1 — at least one timer is missing, inactive or stuck
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

# Locate the ledger. This canary runs BOTH from a repo checkout (workstation,
# ad-hoc) and from the deployed box, where there is no checkout: the manifests
# are staged flat into ~/.opt/maint/ and $ROOT resolves to $HOME. Assuming a repo
# layout made the deployed canary red on its very first scheduled run with
# "no-jobs-manifest-at-/home28/quadstronaut/manifest/jobs.yaml". Same candidate
# list smoke-test.sh already uses for apps.yaml.
LEDGER=""
for _cand in "$ROOT/manifest/jobs.yaml" "$HOME/.opt/maint/jobs.yaml" "$HOME/manifest/jobs.yaml"; do
  [ -f "$_cand" ] && { LEDGER="$_cand"; break; }
done
[ -n "$LEDGER" ] || { echo "STAGE=timer-read-fail msg=no-jobs-manifest-in-repo-or-.opt/maint" >&2; exit 1; }

# Derive the timer UNIT names from the ledger locally (the box has no yaml
# module guaranteed, and the manifest is the authority the audit uses too).
UNITS=$(python3 - "$LEDGER" <<'PY'
import sys, yaml, os
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
out = []
for name, v in (d.get("jobs") or {}).items():
    if not isinstance(v, dict):
        continue
    # `may_be_absent` opts a job out of the LOADED predicate. Only for units
    # whose intended terminal state is "gone" (a self-destructing watcher),
    # never as a way to silence a timer that merely failed to install.
    if v.get("may_be_absent"):
        continue
    t = v.get("timer")
    if t:
        out.append(os.path.basename(t))
print("\n".join(sorted(set(out))))
PY
) || { echo "STAGE=timer-read-fail msg=could-not-parse-jobs-manifest" >&2; exit 1; }
# STRIP CR. This canary runs from a WINDOWS workstation, where python's print()
# emits CRLF, so every unit name arrived with a trailing \r. systemd then saw an
# invalid unit and helpfully appended .service:
#     Id=bazarr2-sync.timer\x0d.service   LoadState=not-found
# which made all 40 timers look uninstalled on the first live run. The canary
# would have paged that the entire schedule had vanished.
UNITS=$(printf '%s' "$UNITS" | tr -d '\r')

COUNT=$(printf '%s\n' "$UNITS" | grep -c . || true)
# Flatten to one space-separated line HERE, not inside the ssh string. $'\n' is
# ANSI-C quoting and does NOT apply inside double quotes, so substituting in-line
# produced a single mangled token and every timer came back "missing" — a canary
# whose first live run cried wolf about all 39 timers at once.
# shellcheck disable=SC2086
UNITS_LINE=$(printf '%s ' $UNITS)
# The .service each timer activates, for the last-run Result check.
# shellcheck disable=SC2086
SERVICES_LINE=$(printf '%s ' $UNITS | sed 's/\.timer/\.service/g')
[ "${COUNT:-0}" -ge 1 ] || { echo "STAGE=timer-read-fail msg=ledger-declared-zero-timers" >&2; exit 1; }

# TWO batched `systemctl show` calls, not one per unit per property. The naive
# loop issued 4 calls x 40 units = 160 round trips inside a single ssh session on
# a shared box; batching also makes the whole check one atomic view of systemd
# rather than 160 separately-timed samples that can disagree with each other.
# `show` accepts many units and emits blank-line-separated blocks keyed by Id=.
RES=$(sshm "
set -uo pipefail
systemctl --user show ${UNITS_LINE} -p Id -p LoadState -p ActiveState -p NextElapseUSecRealtime -p NextElapseUSecMonotonic 2>/dev/null
# Blank lines around the marker are LOAD-BEARING: systemctl does not end its
# output with one, so without them the last timer block, the marker and the
# first service block merge into a single paragraph record. The last timer
# then inherits a Result= and is classified as a service, silently losing its
# verdict (measured: 38/39, with qflix-vlogs-ingest.timer unchecked).
echo; echo '---SERVICES---'; echo
systemctl --user show ${SERVICES_LINE} -p Id -p Result 2>/dev/null
") || { echo "STAGE=timer-read-fail msg=ssh-or-systemd-unreadable" >&2; exit 1; }

# Parse in PARAGRAPH mode: one blank-line-separated block per unit.
#
# Do NOT treat `Id=` as the block start. systemd does not emit properties in the
# order you request them -- measured on the box, a timer block comes back as:
#
#     NextElapseUSecRealtime=
#     NextElapseUSecMonotonic=3w 17h 22min 56.114690s
#     Id=qflix-vlogs-ingest.timer
#     LoadState=loaded
#     ActiveState=active
#
# with Id in the MIDDLE. Starting a record at Id= attributed each timer's
# schedule to the PREVIOUS unit, which reported a perfectly healthy
# qflix-vlogs-ingest (next run in 4 minutes) as "stuck". Silent mis-attribution
# in a canary is the exact failure class this canary exists to catch.
VERDICTS=$(printf '%s\n' "$RES" | tr -d '\r' | awk '
  BEGIN { RS = ""; FS = "\n" }
  {
    if ($0 ~ /^---SERVICES---/) next
    id = ""; load = ""; active = ""; nr = ""; nm = ""; res = ""; seen_res = 0
    for (i = 1; i <= NF; i++) {
      line = $i
      p = index(line, "=")
      if (p == 0) continue
      k = substr(line, 1, p - 1); v = substr(line, p + 1)
      if      (k == "Id")                      id = v
      else if (k == "LoadState")               load = v
      else if (k == "ActiveState")             active = v
      else if (k == "NextElapseUSecRealtime")  nr = v
      else if (k == "NextElapseUSecMonotonic") nm = v
      else if (k == "Result")                { res = v; seen_res = 1 }
    }
    if (id == "") next
    # Classify on the unit SUFFIX, not on whether a Result= happened to appear in
    # the record. Presence-based classification made a merged record look like a
    # service and dropped a real timer from the count.
    if (id ~ /\.service$/) {
      if (seen_res && res != "" && res != "success") print "FAILED " id "=" res
      next
    }
    if (load != "loaded")        { print "MISSING " id;  next }
    if (active != "active")      { print "INACTIVE " id; next }
    # A realtime timer (OnCalendar) reports a date in NextElapseUSecRealtime and
    # 0 for monotonic; a monotonic timer (OnUnitActiveSec) reports the reverse.
    # Stuck = nothing scheduled on EITHER clock while claiming to be active.
    sched = 0
    if (nr != "" && nr != "0" && nr != "n/a") sched = 1
    if (nm != "" && nm != "0" && nm != "n/a") sched = 1
    if (sched) print "OK " id; else print "STUCK " id
  }
')

pick() { printf '%s\n' "$VERDICTS" | awk -v k="$1" '$1==k {print $2}' | paste -sd, - ; }
MISSING=$(pick MISSING); INACTIVE=$(pick INACTIVE)
STUCK=$(pick STUCK);     FAILED=$(pick FAILED)
OKN=$(printf '%s\n' "$VERDICTS" | grep -c '^OK ' || true)

trim() { printf '%s' "$1" | sed 's/^,*//; s/,*$//'; }

if [ -n "$(trim "$MISSING")" ]; then
  echo "STAGE=timer-missing msg=declared-in-jobs.yaml-but-not-installed:$(trim "$MISSING")" >&2
  exit 1
fi
if [ -n "$(trim "$INACTIVE")" ]; then
  echo "STAGE=timer-inactive msg=will-never-fire-again:$(trim "$INACTIVE")" >&2
  exit 1
fi
if [ -n "$(trim "$STUCK")" ]; then
  echo "STAGE=timer-stuck msg=active-but-nothing-scheduled:$(trim "$STUCK")" >&2
  exit 1
fi
if [ -n "$(trim "$FAILED")" ]; then
  # WARN, not FAIL — see the header. Still UP, but the message names them.
  echo "PASS-WARN: ${OKN}/${COUNT} timers live; last-run-not-success:$(trim "$FAILED")"
  exit 0
fi
echo "PASS: timer-liveness ${OKN}/${COUNT} declared timers loaded+active+scheduled"
exit 0

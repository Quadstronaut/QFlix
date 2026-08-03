#!/usr/bin/env bash
# prowlarr-app-sync canary: what Prowlarr BELIEVES it syncs to each *arr must
# actually be present in that *arr, and the indexers whose download path has
# already been remediated must stay remediated.
#
# ===========================================================================
# WHY THIS EXISTS (fault cluster diagnosed 2026-08-02/03, verified live)
# ===========================================================================
# FAULT (a) — LimeTorrents has never reached Radarr, and nothing noticed.
#   Prowlarr's ApplicationIndexerSync task runs every 6 h (10:13/16:13/22:14/
#   04:14). Every cycle it POSTs LimeTorrents [Prowlarr indexer 2] to Radarr
#   [app 2]; Radarr runs its add-time live validation (`t=movie&cat=2000` with
#   an EMPTY search term, i.e. the RSS/latest feed), the bundled `limetorrents`
#   Cardigann definition maps every /latest100 row to 8000 Other + 5000 TV and
#   ZERO rows to 2000 Movies, so Radarr returns
#     400 "Query successful, but no results in the configured categories were
#          returned from your indexer."
#   Prowlarr logs `Warn|RadarrV3Proxy|No Results in configured categories`,
#   retries once with ?forceSave=true, gets 400 again, and gives up. Net effect:
#   Radarr has 8 indexers and LimeTorrents is not one of them, while Sonarr has
#   it and it works there. Measured live: keyword search on the same indexer
#   returns 33/40 results in cat 2000, so the SITE is fine — the RSS row->
#   category mapping is the defect.
#
#   The same warning is present in the logs on 2026-05-22. It has therefore been
#   recurring roughly every six hours for at least ten weeks with:
#     - Prowlarr /api/v1/health  == []   (Prowlarr only raises an indexer health
#                                         item after >6 h DISABLED-for-failures;
#                                         LimeTorrents never fails, it returns
#                                         200 with nothing in cat 2000)
#     - Radarr   /api/v3/health  == []
#     - Canary Prowlarr Indexer Health   GREEN throughout
#   Nothing in this repo reads /api/v1/applications, and nothing compares
#   "indexers Prowlarr intends to sync" against "indexers the *arr actually
#   has". That comparison is this canary's entire job.
#
# FAULT (b)+(c) — the Knaben grab chain, and why this canary locks the fix.
#   Knaben [21] has torrentBaseSettings.preferMagnetUrl = False, so Radarr
#   prefers the Prowlarr proxy downloadUrl over the magnet. That URL wraps
#   `knaben.org/live/dl/rutracker/?...` which answers 403 with a 26 KB HTML
#   login wall. Chain, verified to the second:
#     403 (Prowlarr->upstream) -> Prowlarr `Downloading for release failed`
#     -> `ReleaseDownloadException` -> Radarr gets 500 -> repeated 500s trip
#     PROWLARR's own ~15-minute failure backoff -> Radarr's retry gets 429
#     -> Radarr logs "API Grab Limit reached" -> "Couldn't add release".
#   "API Grab Limit reached" is a Radarr label for ANY 429 on a download URL.
#   It is not a quota: baseSettings.grabLimit is None on all 14 enabled
#   indexers and /api/v1/indexerstatus is []. 100% of Knaben results carry a
#   magnet (83/83, 37/37, 74/74 measured), so ticking Prefer Magnet URL removes
#   the broken path entirely. Tokyo Toshokan [38] is the same mechanism on the
#   anime side (154 of the 1,005 24 h TooManyRequests lines).
#
#   That fix is A CHECKBOX IN THE PROWLARR UI. It has no representation in this
#   repo, no installer step, and no test — exactly the shape of config change
#   that silently reverts on a restore, a re-add, or an operator clicking
#   through the indexer edit dialog. Predicate P2 below is the regression lock:
#   the remediation, expressed as an assertion.
#   Runbook: docs/prowlarr-indexer-remediation-2026-08-03.md
#
# ===========================================================================
# COMPARTMENTALIZE BOUNDARY (operator design law)
# ===========================================================================
# scripts/canaries/prowlarr-indexer-health.sh owns two LOG-DERIVED signals:
# the *arr->Prowlarr 429 cascade (vlogs) and Prowlarr's own /api/v1/health.
# This canary owns a CONFIG-DERIVED signal: Prowlarr's declared intent vs the
# *arr's actual state. Different data source (config APIs, not logs), different
# latency (a sync failure is permanent until fixed, a cascade is a burst),
# different remediation (a Prowlarr settings change vs an upstream outage that
# clears itself), different cadence. Folding P1/P2 into that script would put a
# permanent, deterministic fault behind a burst-threshold that is green 99% of
# the time — which is precisely how (a) hid for ten weeks. Own module, own
# timer, own Kuma check, independently swappable.
#
# P1 and P2 DO share this module deliberately: both are "the Prowlarr -> *arr
# handoff is misconfigured", both are answered by the same two API surfaces in
# the same round trip, both are remediated by the same operator in the same
# Prowlarr settings screen, and both are read-only config assertions. Splitting
# them would double the born-mute / born-tokenless surface that
# scripts/canaries/timer-liveness.sh's header argues against, for zero new
# signal.
#
# ===========================================================================
# PREDICATES
# ===========================================================================
# P1  SYNC INTEGRITY. For every Prowlarr application whose syncLevel is not
#     "disabled", compute the set of indexers Prowlarr INTENDS to sync:
#         enabled  AND  (app has no tags OR tags intersect)  AND
#         indexer categories (flattened through subCategories) intersect the
#         app's syncCategories + animeSyncCategories
#     and require every one of them to be present in that *arr's own
#     /api/v{3,1}/indexer list. Any intended-but-absent indexer is a FAIL.
#
#     Presence is matched by the PROWLARR INDEXER ID embedded in the *arr
#     indexer's `fields.baseUrl` (".../prowlarr/21/"), not by name, because a
#     renamed indexer would otherwise read as simultaneously missing and
#     orphaned. If NOT ONE id can be extracted while the *arr does hold
#     "<name> (Prowlarr)" entries, the URL shape has changed underneath us; the
#     canary falls back to name matching for that app and SAYS SO in the output
#     (match=name) rather than reporting every indexer missing.
#
# P2  GRAB-PATH REGRESSION LOCK. Every indexer named in MAGNET_REQUIRED must
#     have torrentBaseSettings.preferMagnetUrl = true. Deliberately a NAMED
#     LIST, not a blanket rule: all 12 enabled torrent indexers currently have
#     it false and only two of them are known to have a broken proxy download
#     path, so a blanket assertion would demand eleven unjustified config
#     changes and be born red on arrival. A name in the list means "we
#     diagnosed this one and the runbook fixes it"; the list is the acceptance
#     test for the runbook.
#
#     THIS CANARY IS RED UNTIL STEP 1 OF THE RUNBOOK IS APPLIED. That is
#     intended, not a defect: the fault is live right now and the fix is one
#     checkbox each. If the operator decides NOT to remediate an entry, remove
#     it from MAGNET_REQUIRED — an explicit, reviewable decision rather than a
#     canary quietly tuned into silence.
#
# ===========================================================================
# RULE 4 — every skip is COUNTED and NAMED, never silent
# ===========================================================================
# Six indexers are legitimately not synced to some app because they publish no
# category that app wants (MagnetDownload/TorrentsCSV publish only 8000 Other;
# YTS only 2000 so Sonarr skips it; Nipponsei/Shana Project/Tokyo Toshokan for
# the mirrored reason on the anime side). Prowlarr logs those at Debug and they
# are BY DESIGN. They are counted in skip_nocat and enumerated in --json, so a
# genuine regression (an indexer that used to publish 2000 and stopped) shows
# up as a MOVE from intended into skip_nocat instead of vanishing.
# Likewise skip_notag (tag mismatch), skip_disabled (indexer disabled in
# Prowlarr) and orphan (present in the *arr, not intended by Prowlarr — today
# 8 disabled-legacy Torznab leftovers). Orphans are counted and reported but do
# NOT fail: a stale entry an *arr keeps after Prowlarr disables an indexer is
# untidy, not broken, and paging on it would park a permanent red.
#
# ===========================================================================
# RULE 5 — empty-because-clean vs empty-because-broken, by EXIT CODE
# ===========================================================================
#   0  every application was queried successfully; zero missing, zero P2
#      violations.                          stdout: "PASS: prowlarr-app-sync ..."
#      ALSO 0: a transport-class BROKEN state suppressed by the maintenance
#      window.                       stdout: "SUPPRESSED: prowlarr-app-sync ..."
#      The two are distinguishable by the leading token, on purpose — an exit 0
#      that means "we could not look" must never wear the word PASS.
#   1  a real finding — intended-but-absent indexer(s), and/or a MAGNET_REQUIRED
#      indexer whose preferMagnetUrl is still false.
#   2  BROKEN, could not establish anything: secrets unreadable, Prowlarr
#      unreachable / non-JSON, ZERO applications configured, ZERO enabled
#      indexers, an *arr unreachable or its key missing, or an application
#      implementation whose API version we do not know.
#   "No applications configured" is exit 2, not a clean pass: a wiped
#   Applications page produces an empty intended set for which "nothing is
#   missing" is trivially true and completely wrong.
#
#   THE CONDITION IS GUARDED, NOT JUST ITS TWO KNOWN CAUSES. Zero apps and zero
#   enabled indexers are the two ways an empty intended set was ANTICIPATED, but
#   the thing that makes the verdict vacuous is the empty set itself. Category
#   data going missing on either side produces it too: an indexer whose
#   `capabilities.categories` comes back [] after a Prowlarr upgrade, or an app
#   whose syncCategories is wiped, drops EVERY indexer into skip_nocat, leaves
#   `intended` empty, makes `missing` trivially empty, and reclassifies every
#   present entry as an orphan — which by design never fails. Reproduced against
#   the live 2026-08-03 topology: perturbing either field alone yielded
#   `intended=0, orphan=16, skip_nocat=10` and exit 0. Guarding the causes and
#   not the condition is how a guard passes while blind.
#   cli.py collapses every non-zero to a Kuma DOWN, so the 1-vs-2 split is for
#   the operator on the command line and for the tests — the STAGE label
#   carries it into Kuma.
#
# ===========================================================================
# EXECUTION MODEL — runs LOCALLY, no sshm hop
# ===========================================================================
# Same as dash-asset-integrity.sh / kometa-deploy-drift.sh. Everything it needs
# (loopback Prowlarr, loopback *arrs, ~/secrets) is co-resident wherever the
# systemd ExecStart runs, and a local shape is the only one the unit suite can
# drive hermetically against fake HTTP servers.
#
# NO durable logfile, on purpose. dash-asset-integrity keeps one because it
# MUTATES and needs an audit trail of restarts. This canary is read-only and
# stateless: its entire verdict fits in the Kuma msg, and --json reproduces the
# full detail on demand. A new logfile would be a new maintenance concern
# (rotation + a stale-log-watchdog registration) bought for nothing — the same
# argument the 2026-08-03 quality-fallback audit made against adding one there.
#
# ===========================================================================
# MAINTENANCE WINDOW — what is suppressed, and what is emphatically not
# ===========================================================================
# The Monday 11:00-15:00 UTC window (scripts/maint/lib/window.py) stops,
# upgrades and restarts apps ON PURPOSE. "Prowlarr unreachable" then is the
# expected state, and paging on it is a guaranteed weekly false red.
#
# THE LINE IS DRAWN ON THE FAILURE REASON, NOT ON THE STAGE. Only the two
# http_json() call sites opt in (fail(..., reason=...)), and only when
# is_transport_reason() says the reason is transport-class:
#     transport-*  (urlopen raised: refused/timeout/reset/DNS)
#     http-5xx     (a half-started service behind its own proxy)
#     non-json-body (the "Application is starting up" HTML page)
# Everything else pages exactly as it would at 03:00 on a Thursday:
#     http-4xx     an ANSWER — a wrong key or a wrong urlbase. A restart does
#                  not change a key, so this is the same config fault in-window
#                  as out. NOT suppressed.
#     not-a-list   an API contract change. NOT suppressed.
#     config-missing / no-apps / no-indexers / no-intent / unknown-app / the two
#     arr-down legs that fire BEFORE any request (no baseUrl, no local key) /
#     missing / magnet-pref-off / probe-error — none of these ever pass a
#     `reason`, so no window state can silence them. A MISSING INDEXER IS STILL
#     A MISSING INDEXER AT 12:00 UTC.
#
# Suppression is COUNTED and LOGGED, never silent: stdout carries
# `window_suppressed=1 suppressed_stage=... window_leg=...` (cli.py forwards
# stdout verbatim as the Kuma msg= on exit 0, so the count lands in the
# monitor's own history) and the identical line goes to stderr for journald.
# Under --json the finding object gains a `window_suppressed` block.
#
# Fail direction is OPEN — an undeterminable window does NOT suppress. This
# gate silences an alert, so a false silence hides a real outage while a false
# page costs one glance. dash-asset-integrity.sh fails CLOSED because its gate
# guards an unattended RESTART, where those costs invert.
#
# ===========================================================================
# STAGE labels (one line on stderr -> Kuma msg=)
# ===========================================================================
#   prowlarr-appsync-config-missing   ~/secrets/prowlarr.{port,key} unreadable
#   prowlarr-appsync-prowlarr-down    Prowlarr API unreachable / non-JSON
#   prowlarr-appsync-no-apps          zero applications configured
#   prowlarr-appsync-no-indexers      zero ENABLED indexers
#   prowlarr-appsync-no-intent        the intended set came out EMPTY across
#                                     every application (see below)
#   prowlarr-appsync-arr-down         an application's *arr could not be read
#   prowlarr-appsync-unknown-app      application implementation we cannot map
#                                     to an API version (never guess a path)
#   prowlarr-appsync-missing          FAULT (a): intended-but-absent indexer(s)
#   prowlarr-appsync-magnet-pref-off  FAULT (b)/(c): a MAGNET_REQUIRED indexer
#                                     still has preferMagnetUrl=false
#   prowlarr-appsync-probe-error      last-resort boundary; a traceback in
#                                     Kuma msg= is unreadable AND looks like a
#                                     verdict, so it becomes a named BROKEN
#
# Tunables (env; cli.py invokes with NO arguments, so every knob needs a default)
#   MANITOBA_SECRETS                        secrets dir, default ~/secrets
#   QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED    comma-separated indexer NAMES that
#                                           must have preferMagnetUrl=true.
#                                           Default "Knaben,Tokyo Toshokan".
#                                           Empty string disables P2 entirely.
#   QFLIX_CANARY_APPSYNC_TIMEOUT_S          per-request timeout, default 15
#   QFLIX_CANARY_APPSYNC_RETRIES            transport retries per request, 1
#   QFLIX_CANARY_APPSYNC_FORCE_WINDOW       "1" force in-window, "0" force out,
#                                           "" (default) measure. Tests only.
#   QFLIX_CANARY_APPSYNC_MAINT_LIB          dir holding lib/suppression.py for
#                                           window leg (b); default $ROOT/scripts/
#                                           maint. EXPLICITLY EMPTY disables it.
#   MANITOBA_STATE_DIR                      window lockfile dir, default
#                                           ~/.opt/maint (legs b and c)
#
# Argv:  --json   print the full finding object on stdout (same exit code).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# Where lib.suppression lives, for the canonical maintenance-window predicate.
# On the box ROOT resolves to $HOME and this is ~/scripts/maint. Same knob, same
# reasoning, as scripts/canaries/dash-asset-integrity.sh:351-360 -- including
# `-` and NOT `:-`: an EXPLICITLY EMPTY value must disable the lib.suppression
# leg so a test can isolate the wall-clock and lockfile legs. With `:-` the
# empty string reads as unset and is silently replaced by the default, which is
# how the equivalent test went green against the wrong leg on the dash canary.
export QFLIX_CANARY_APPSYNC_MAINT_LIB="${QFLIX_CANARY_APPSYNC_MAINT_LIB-$ROOT/scripts/maint}"

exec python3 - "$@" <<'PY'
import datetime as dt
import importlib.util
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

EXIT_OK, EXIT_FINDING, EXIT_BROKEN = 0, 1, 2

ARGV = sys.argv[1:]
WANT_JSON = "--json" in ARGV

SECRETS = os.environ.get("MANITOBA_SECRETS") or os.path.join(
    os.path.expanduser("~"), "secrets")
TIMEOUT = float(os.environ.get("QFLIX_CANARY_APPSYNC_TIMEOUT_S") or 15)
RETRIES = int(os.environ.get("QFLIX_CANARY_APPSYNC_RETRIES") or 1)

_mag = os.environ.get("QFLIX_CANARY_APPSYNC_MAGNET_REQUIRED")
if _mag is None:
    _mag = "Knaben,Tokyo Toshokan"
MAGNET_REQUIRED = [s.strip() for s in _mag.split(",") if s.strip()]

# --- maintenance window ----------------------------------------------------
# "1" forces in-window, "0" forces out-of-window, "" (default) means measure.
FORCE_WINDOW = (os.environ.get("QFLIX_CANARY_APPSYNC_FORCE_WINDOW") or "").strip()
MAINT_LIB = os.environ.get("QFLIX_CANARY_APPSYNC_MAINT_LIB") or ""
WINDOW_START_HOUR_UTC = 11
WINDOW_END_HOUR_UTC = 15

# Prowlarr application implementation -> the *arr's API version. NEVER default:
# guessing /api/v3 at a Readarr would 404 and read as "every indexer missing",
# turning an unknown app into a fake outage. Unknown -> exit 2, named.
APP_API_VERSION = {
    "radarr": "v3",
    "sonarr": "v3",
    "whisparr": "v3",
    "lidarr": "v1",
    "readarr": "v1",
}


def _slug(text):
    """Kuma msg= is parsed as whitespace-separated tokens, so nothing that
    reaches it may contain a space."""
    return re.sub(r"\s+", "_", str(text).strip())


def _join_capped(parts, limit, sep=";"):
    """Join WHOLE elements up to `limit` chars, then say how many were dropped.

    Same helper, same reasoning, as scripts/canaries/plex-unmatched.sh: a plain
    slice cuts mid-token, which reads as a different (wrong) value rather than
    as an obvious truncation — and it drops silently, so the operator cannot
    tell a two-item finding from a fifty-item one."""
    parts = list(parts)
    out = []
    used = 0
    for part in parts:
        cost = len(part) + (len(sep) if out else 0)
        if used + cost > limit:
            break
        out.append(part)
        used += cost
    else:
        return sep.join(out)

    # Something was dropped, so the marker has to fit INSIDE the budget too --
    # otherwise the "+N more" itself pushes the line past cli.py's 200-char cut
    # and gets sliced, which is the mid-token truncation this helper exists to
    # prevent. Give back whole elements until it fits.
    while True:
        dropped = len(parts) - len(out)
        marker = ("%s+%d more" % (sep, dropped)) if out else "+%d more" % dropped
        if not out or used + len(marker) <= limit:
            return sep.join(out) + marker
        last = out.pop()
        used -= len(last) + (len(sep) if out else 0)


# ===========================================================================
# MAINTENANCE WINDOW
# ===========================================================================
# During the Monday 11:00-15:00 UTC window (scripts/maint/lib/window.py) apps
# are stopped, upgraded and restarted ON PURPOSE, so Prowlarr or an *arr being
# unreachable is the EXPECTED state, not a fault. Suppress that -- and ONLY
# that. See is_transport_reason() for exactly where the line is drawn.
#
# FAIL DIRECTION: OPEN. This gate silences an ALERT, so if the window state
# cannot be determined the canary does NOT suppress and pages as normal. That
# is the OPPOSITE of dash-asset-integrity.sh, which fails CLOSED -- but that
# canary's gate guards a MUTATION (an unattended restart), where the costs
# invert: a false restart breaks an absolute operator directive, while a false
# page here costs one operator glance and a false silence hides a real outage.
#
# In production `manitoba-maint canary push` already short-circuits every
# canary during the window (scripts/maint/lib/cli.py, in_maintenance_window()
# -> push UP "[maint-window: upgrades in progress]" and skip the run). This
# in-script gate is the second, independent leg -- it covers a direct operator
# invocation, the installer's verify step, a systemd ExecStart that calls the
# script rather than the CLI, and the case where cli.py's own check is bypassed.
# Unlike cli.py it still RUNS the checks and still reports the CONFIG verdict;
# it suppresses only the transport-class BROKEN states.


def _window_lock_path():
    return Path(os.environ.get(
        "MANITOBA_STATE_DIR",
        str(Path.home() / ".opt" / "maint"))) / "lock"


def _window_lock_is_leaked():
    """True iff the window lock exists but its owning PID is gone.

    ONE liveness rule shared by both lockfile-consulting legs. Leg 2 asks the
    canonical lib.suppression predicate, which is a BARE existence check by
    design; leg 3 liveness-checks the PID. Without a shared rule a leaked lock
    makes them disagree and leg 2 wins -- suppressing for as long as the stale
    file sits there. Same defect, same fix, as dash-asset-integrity.sh.

    Anything unreadable/unparseable/non-POSIX returns False (NOT leaked), which
    keeps the lock's suppressing power. os.kill(pid, 0) TERMINATES on Windows,
    so it is never called there.
    """
    try:
        lock = _window_lock_path()
        if not lock.exists():
            return False
        lines = lock.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0].strip()) if lines and lines[0].strip() else 0
        if pid <= 0 or os.name != "posix":
            return False
        try:
            os.kill(pid, 0)
            return False          # owner alive -> a real, held lock
        except ProcessLookupError:
            return True           # owner gone -> leaked
        except PermissionError:
            return False          # exists, owned by someone else -> real
    except Exception:
        return False


def window_active():
    """(active, leg). Three OR-ed legs, mirroring dash-asset-integrity.sh:816
    and scripts/maint/qflix-torrent-janitor.py:

      (a) wall clock Mon 11:00-15:00 UTC -- authoritative because the calendar
          lives only in the units (manitoba-maint-window.timer OnCalendar=Mon
          11:00 UTC, ...-watchdog.timer 15:00 UTC), and this leg still protects
          if the orchestrator crashed before ever opening the lock;
      (b) the canonical lib.suppression.in_maintenance_window() predicate;
      (c) a direct live-PID lockfile read, so the gate survives lib being
          unimportable mid-deploy.

    Raises nothing: the caller treats an exception as NOT-in-window (fail open).
    """
    if FORCE_WINDOW == "1":
        return True, "forced-on"
    if FORCE_WINDOW == "0":
        return False, "forced-off"

    now = dt.datetime.now(dt.timezone.utc)
    if (now.weekday() == 0
            and WINDOW_START_HOUR_UTC <= now.hour < WINDOW_END_HOUR_UTC):
        return True, "wallclock-mon-%02d00-%02d00-utc" % (
            WINDOW_START_HOUR_UTC, WINDOW_END_HOUR_UTC)

    if MAINT_LIB:
        try:
            # Loaded BY PATH, not `from lib import suppression`. A bare package
            # import resolves against the whole sys.path, so an ambient
            # PYTHONPATH could answer this leg from a DIFFERENT copy of the
            # maintenance library than the one we were pointed at -- silently.
            _sup_path = Path(MAINT_LIB) / "lib" / "suppression.py"
            _spec = importlib.util.spec_from_file_location(
                "_appsync_canary_suppression", str(_sup_path))
            if _spec is None or _spec.loader is None:
                raise ImportError("no loader for %s" % _sup_path)
            _sup = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_sup)
            if _sup.in_maintenance_window() and not _window_lock_is_leaked():
                return True, "suppression-in_maintenance_window"
        except Exception as exc:                      # noqa: BLE001 - boundary
            sys.stderr.write("window leg suppression-unavailable=%s\n"
                             % type(exc).__name__)

    try:
        lock = _window_lock_path()
        if lock.exists():
            lines = lock.read_text(encoding="utf-8").splitlines()
            pid = int(lines[0].strip()) if lines and lines[0].strip() else 0
            if pid > 0:
                if os.name != "posix":
                    return True, "lock-present"
                try:
                    os.kill(pid, 0)
                    return True, "lock-live-pid"
                except ProcessLookupError:
                    pass
                except PermissionError:
                    return True, "lock-live-pid"
    except Exception as exc:                          # noqa: BLE001 - boundary
        sys.stderr.write("window leg lock-unreadable=%s\n" % type(exc).__name__)

    return False, "clear"


def is_transport_reason(reason):
    """WHERE THE LINE IS DRAWN, and it is drawn on the REASON, not the stage.

    Suppressible during the window (the app is being cycled on purpose):
      transport-*    urlopen raised -- ConnectionRefused / timeout / DNS / reset.
                     The literal signature of a unit that is stopped right now.
      http-5xx       the service answered, but as a server error. A half-started
                     *arr behind its own reverse proxy returns 502/503 for the
                     seconds-to-minutes it takes to come up.
      non-json-body  a 200 whose body is not JSON -- the "Application is
                     starting up" HTML page. Included deliberately: it is
                     unreachability that happens to arrive with a status code.

    NOT suppressible -- these still page at 12:00 UTC on a Monday:
      http-4xx       an ANSWER. 401/403 is a wrong/missing API key, 404 is a
                     wrong urlbase or API version. A restart does not change a
                     key or a path, so a 4xx in-window is the same config fault
                     it is out-of-window.
      not-a-list     valid JSON of the wrong shape -- an API contract change.
      everything else, by construction: this predicate is only ever consulted
      at the two http_json() failure sites. Every other exit -- config-missing,
      no-apps, no-indexers, no-intent, unknown-app, the two arr-down legs that
      fire BEFORE any request is made (no baseUrl, no local key), missing,
      magnet-pref-off and probe-error -- never reaches it and therefore cannot
      be suppressed by any window state.

    A missing indexer is still a missing indexer at 12:00 UTC. THAT is the rule
    this function exists to enforce, and it enforces it by only ever being asked
    about transport.
    """
    r = str(reason or "")
    if r.startswith("transport-"):
        return True
    if r == "non-json-body":
        return True
    m = re.match(r"^http-(\d{3})$", r)
    return bool(m and 500 <= int(m.group(1)) <= 599)


def suppress(stage, msg, leg, detail=None):
    """Emit the window suppression -- COUNTED and LOGGED, never silent.

    COUNTED: the stdout line carries `window_suppressed=1` and names the stage
    and reason that were suppressed, so it is a machine-readable tally rather
    than a shrug. cli.py takes stdout verbatim as the Kuma msg= whenever the
    script exits zero, so the count lands in the monitor's own history -- which
    is why this canary still needs no durable logfile of its own (see the
    EXECUTION MODEL header). The wording here deliberately avoids the literal
    "exit"+"0" pair: tests/unit/audit/test_c09_silent_exit.py cross-checks the
    audit's enumeration of clean-exit SITES against a regex over non-comment
    lines, and that regex cannot tell a Python docstring inside a heredoc from
    code, so prose containing it inflates the reference count by one and fails
    a test about something else entirely.
    LOGGED: the same fact is also written to stderr, which the systemd unit
    sends to journald, so it survives independently of Kuma retention.
    """
    line = ("SUPPRESSED: prowlarr-app-sync window_suppressed=1 "
            "suppressed_stage=%s class=transport window_leg=%s msg=%s"
            % (stage, leg, msg))
    if WANT_JSON and detail is not None:
        detail = dict(detail)
        detail["window_suppressed"] = {
            "count": 1, "stage": stage, "reason": msg, "leg": leg,
            "class": "transport",
        }
        print(json.dumps(detail, indent=2, sort_keys=True))
    sys.stderr.write(line + "\n")
    print(line)
    sys.exit(EXIT_OK)


def fail(stage, msg, code=EXIT_FINDING, detail=None, reason=None):
    """`reason` is the raw http_json() failure string. Supplying it OPTS THIS
    EXIT IN to window suppression -- and only if the reason is transport-class.
    Every call site that omits it pages regardless of the window, which is the
    safe default and the reason this is opt-in rather than opt-out."""
    if reason is not None and is_transport_reason(reason):
        try:
            active, leg = window_active()
        except Exception as exc:                      # noqa: BLE001 - boundary
            # Fail OPEN: an undeterminable window must never silence a real
            # outage. Named on stderr so the indeterminacy is not itself silent.
            sys.stderr.write("window determination FAILED (%s), NOT suppressing\n"
                             % type(exc).__name__)
            active, leg = False, "indeterminate"
        if active:
            suppress(stage, msg, leg, detail=detail)
    if WANT_JSON and detail is not None:
        print(json.dumps(detail, indent=2, sort_keys=True))
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(code)


def read_secret(name):
    try:
        with open(os.path.join(SECRETS, name), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def http_json(url, api_key):
    """(ok, payload_or_reason). Retries transport errors only — an HTTP status
    is an answer, not a blip, and retrying a 401 just doubles the latency."""
    last = "unknown"
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(
            url, headers={"X-Api-Key": api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            try:
                return True, json.loads(raw or "null")
            except ValueError:
                return False, "non-json-body"
        except urllib.error.HTTPError as exc:
            return False, "http-%s" % exc.code
        except Exception as exc:                      # noqa: BLE001 - boundary
            last = "transport-%s" % type(exc).__name__
            if attempt >= RETRIES:
                return False, last
    return False, last


def flatten_categories(cats):
    """Newznab categories nest one level (5000 TV -> 5070 TV/Anime) and Prowlarr
    matches an app's syncCategories against BOTH levels. Sonarr's
    animeSyncCategories is literally [5070], so a flatten that stopped at the
    parent would mark every anime indexer as skip_nocat."""
    out = set()
    stack = list(cats or [])
    while stack:
        c = stack.pop()
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if isinstance(cid, int):
            out.add(cid)
        stack.extend(c.get("subCategories") or [])
    return out


def field_map(resource):
    return {f.get("name"): f.get("value")
            for f in (resource.get("fields") or []) if isinstance(f, dict)}


PROWLARR_ID_RE = re.compile(r"/(\d+)/?$")


def prowlarr_id_of(arr_indexer):
    """Prowlarr writes the *arr indexer's baseUrl as <prowlarrUrl>/<id>/ .
    That id is the only stable link between the two records — the display name
    is operator-editable on both sides."""
    base = field_map(arr_indexer).get("baseUrl") or ""
    if not isinstance(base, str) or not base:
        return None
    m = PROWLARR_ID_RE.search(urllib.parse.urlparse(base).path)
    return int(m.group(1)) if m else None


def main():
    port = read_secret("prowlarr.port")
    key = read_secret("prowlarr.key")
    urlbase = read_secret("prowlarr.urlbase") or "prowlarr"
    if not port or not key:
        fail("prowlarr-appsync-config-missing",
             "prowlarr.port=%s-prowlarr.key=%s"
             % ("SET" if port else "EMPTY", "SET" if key else "EMPTY"),
             EXIT_BROKEN)
    base = "http://127.0.0.1:%s/%s" % (port, urlbase)

    ok, indexers = http_json(base + "/api/v1/indexer", key)
    if not ok or not isinstance(indexers, list):
        reason = indexers if not ok else "not-a-list"
        fail("prowlarr-appsync-prowlarr-down",
             "indexer-api-%s" % _slug(reason),
             EXIT_BROKEN, reason=reason)
    ok, apps = http_json(base + "/api/v1/applications", key)
    if not ok or not isinstance(apps, list):
        reason = apps if not ok else "not-a-list"
        fail("prowlarr-appsync-prowlarr-down",
             "applications-api-%s" % _slug(reason),
             EXIT_BROKEN, reason=reason)

    enabled = [i for i in indexers if i.get("enable")]
    skip_disabled = len(indexers) - len(enabled)
    if not enabled:
        # Zero enabled indexers makes every intended set empty, so "nothing is
        # missing" would be trivially true. Broken, not clean.
        fail("prowlarr-appsync-no-indexers",
             "prowlarr-has-%d-indexers-0-enabled" % len(indexers), EXIT_BROKEN)
    if not apps:
        fail("prowlarr-appsync-no-apps",
             "prowlarr-applications-page-is-empty", EXIT_BROKEN)

    idx_cats = {i.get("id"): flatten_categories(
        (i.get("capabilities") or {}).get("categories")) for i in enabled}
    idx_name = {i.get("id"): (i.get("name") or "?") for i in indexers}

    report = {
        "indexers_total": len(indexers),
        "indexers_enabled": len(enabled),
        "skip_disabled": skip_disabled,
        "apps": [],
        "missing": [],
        "magnet": {"ok": [], "violations": [], "absent": [], "not_torrent": []},
        "totals": {"intended": 0, "present": 0, "orphan": 0,
                   "skip_notag": 0, "skip_nocat": 0, "apps_checked": 0,
                   "apps_sync_disabled": 0},
    }

    # --- P1 ---------------------------------------------------------------
    for app in apps:
        name = app.get("name") or "?"
        if (app.get("syncLevel") or "").lower() == "disabled":
            report["totals"]["apps_sync_disabled"] += 1
            report["apps"].append({"app": name, "state": "sync-disabled"})
            continue

        impl = (app.get("implementation") or "").lower()
        api_v = APP_API_VERSION.get(impl)
        if api_v is None:
            fail("prowlarr-appsync-unknown-app",
                 "app-%s-implementation-%s-has-no-known-api-version"
                 % (_slug(name), _slug(impl or "EMPTY")), EXIT_BROKEN,
                 detail=report)

        fm = field_map(app)
        arr_url = (fm.get("baseUrl") or "").rstrip("/")
        if not arr_url:
            fail("prowlarr-appsync-arr-down",
                 "app-%s-has-no-baseUrl" % _slug(name), EXIT_BROKEN,
                 detail=report)
        # Prowlarr MASKS secret fields on read (the apiKey it returns is an
        # 8-char placeholder, measured live), so the app record cannot
        # authenticate us. The *arr's real key is ~/secrets/<slug>.key, where
        # <slug> is the urlbase Prowlarr was configured with — the same
        # ~/secrets/<slug>.{key,port,urlbase} convention scripts/mcp/lib/
        # arr_client.py uses.
        slug = urllib.parse.urlparse(arr_url).path.strip("/").split("/")[-1]
        arr_key = read_secret(slug + ".key") if slug else ""
        if not arr_key:
            fail("prowlarr-appsync-arr-down",
                 "app-%s-no-local-key-for-slug-%s"
                 % (_slug(name), _slug(slug or "EMPTY")), EXIT_BROKEN,
                 detail=report)

        ok, arr_indexers = http_json(
            "%s/api/%s/indexer" % (arr_url, api_v), arr_key)
        if not ok or not isinstance(arr_indexers, list):
            reason = arr_indexers if not ok else "not-a-list"
            fail("prowlarr-appsync-arr-down",
                 "app-%s-%s" % (_slug(name), _slug(reason)),
                 EXIT_BROKEN, detail=report, reason=reason)

        app_tags = set(app.get("tags") or [])
        app_cats = set(fm.get("syncCategories") or []) | set(
            fm.get("animeSyncCategories") or [])

        intended, skip_notag, skip_nocat = [], [], []
        for i in enabled:
            iid = i.get("id")
            if app_tags and not (set(i.get("tags") or []) & app_tags):
                skip_notag.append(idx_name.get(iid, "?"))
            elif not (idx_cats.get(iid) or set()) & app_cats:
                skip_nocat.append(idx_name.get(iid, "?"))
            else:
                intended.append(iid)

        present_ids = {pid for pid in (prowlarr_id_of(x) for x in arr_indexers)
                       if pid is not None}
        match_mode = "id"
        if not present_ids:
            # The baseUrl shape changed (or every entry is a native, non-Prowlarr
            # indexer). Reporting all-missing here would be a fabricated outage,
            # so degrade to name matching and DISCLOSE it.
            suffixed = {re.sub(r"\s*\(Prowlarr\)\s*$", "", x.get("name") or "")
                        for x in arr_indexers
                        if (x.get("name") or "").endswith("(Prowlarr)")}
            if suffixed:
                match_mode = "name"
                # idx_name (ALL indexers), not idx_cats (enabled only) — a
                # disabled indexer still sitting in the *arr is an orphan and
                # must be counted as one, not silently dropped.
                present_ids = {iid for iid, nm in idx_name.items()
                               if nm in suffixed}

        missing = [iid for iid in intended if iid not in present_ids]
        orphan = sorted(present_ids - set(intended))

        report["apps"].append({
            "app": name, "impl": impl, "api": api_v, "match": match_mode,
            "intended": [idx_name.get(i, "?") for i in intended],
            "present": len(present_ids),
            "missing": [idx_name.get(i, "?") for i in missing],
            "orphan": [idx_name.get(i, "id-%d" % i) for i in orphan],
            "skip_notag": sorted(skip_notag),
            "skip_nocat": sorted(skip_nocat),
        })
        for iid in missing:
            report["missing"].append(
                {"app": name, "indexer": idx_name.get(iid, "?"), "id": iid})
        t = report["totals"]
        t["apps_checked"] += 1
        t["intended"] += len(intended)
        t["present"] += len(present_ids)
        t["orphan"] += len(orphan)
        t["skip_notag"] += len(skip_notag)
        t["skip_nocat"] += len(skip_nocat)

    if report["totals"]["apps_checked"] == 0:
        fail("prowlarr-appsync-no-apps",
             "all-%d-applications-have-syncLevel-disabled" % len(apps),
             EXIT_BROKEN, detail=report)

    # The CONDITION, not just its two anticipated causes. If nothing at all is
    # intended, "nothing is missing" is vacuously true and this canary has
    # asserted exactly zero. See the RULE 5 block in the header.
    if report["totals"]["intended"] == 0:
        fail("prowlarr-appsync-no-intent",
             "%d-enabled-indexers-x-%d-apps-yield-ZERO-intended-syncs-"
             "skip_notag=%d-skip_nocat=%d-orphan=%d"
             % (len(enabled), report["totals"]["apps_checked"],
                report["totals"]["skip_notag"], report["totals"]["skip_nocat"],
                report["totals"]["orphan"]),
             EXIT_BROKEN, detail=report)

    # --- P2 ---------------------------------------------------------------
    by_name = {}
    for i in enabled:
        by_name.setdefault((i.get("name") or "").strip().lower(), i)
    for want in MAGNET_REQUIRED:
        i = by_name.get(want.lower())
        if i is None:
            # Not a failure: the operator may have removed the indexer, and a
            # policy entry for something that no longer exists must not park a
            # permanent red. Counted and named so it cannot rot unnoticed.
            report["magnet"]["absent"].append(want)
            continue
        if (i.get("protocol") or "").lower() != "torrent":
            report["magnet"]["not_torrent"].append(i.get("name"))
            continue
        if field_map(i).get("torrentBaseSettings.preferMagnetUrl"):
            report["magnet"]["ok"].append(i.get("name"))
        else:
            report["magnet"]["violations"].append(i.get("name"))

    # --- verdict ----------------------------------------------------------
    t = report["totals"]
    tail = ("intended=%d,present=%d,orphan=%d,skip_notag=%d,skip_nocat=%d,"
            "skip_disabled=%d" % (t["intended"], t["present"], t["orphan"],
                                  t["skip_notag"], t["skip_nocat"],
                                  skip_disabled))
    magnet_tail = ""
    if report["magnet"]["violations"]:
        magnet_tail = ";magnet_off=%s" % _join_capped(
            [_slug(n) for n in report["magnet"]["violations"]], 40, ",")

    if report["missing"]:
        # THE BUDGET IS SPENT DELIBERATELY, WORST CASE FIRST. cli.py takes
        # stderr verbatim as the Kuma msg= and truncates at 200 chars
        # (scripts/maint/lib/cli.py). An unbounded join put the rule-4 counts
        # LAST, so a total sync outage — the widest, most urgent finding —
        # dropped the counts entirely and cut the final name mid-token, with
        # nothing saying anything had been dropped. Measured on a synthetic
        # 4-app x 14-indexer outage: 1327 bytes of stderr, of which Kuma saw 9
        # of 56 entries, the last one severed, and no tally at all. The wider
        # the outage the less the operator was told. So: reserve the tally and
        # the magnet clause, then fill what is left with WHOLE entries and say
        # how many were dropped.
        prefix = "STAGE=prowlarr-appsync-missing msg="
        budget = 200 - len(prefix) - len(magnet_tail) - len(tail) - 1
        where = _join_capped(
            ["%s:%s" % (_slug(m["app"]), _slug(m["indexer"]))
             for m in report["missing"]], max(budget, 0), ",")
        fail("prowlarr-appsync-missing", "%s%s;%s" % (where, magnet_tail, tail),
             EXIT_FINDING, detail=report)

    if report["magnet"]["violations"]:
        prefix = "STAGE=prowlarr-appsync-magnet-pref-off msg=preferMagnetUrl-false-on-"
        budget = 200 - len(prefix) - len(tail) - 1
        fail("prowlarr-appsync-magnet-pref-off",
             "preferMagnetUrl-false-on-%s;%s"
             % (_join_capped([_slug(n) for n in report["magnet"]["violations"]],
                             max(budget, 0), ","), tail),
             EXIT_FINDING, detail=report)

    if WANT_JSON:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("PASS: prowlarr-app-sync apps=%d idx=%d/%d %s magnet_ok=%d "
              "magnet_absent=%d" % (t["apps_checked"], len(enabled),
                                    len(indexers), tail,
                                    len(report["magnet"]["ok"]),
                                    len(report["magnet"]["absent"])))
    return EXIT_OK


try:
    sys.exit(main())
except SystemExit:
    raise
except Exception as exc:                              # noqa: BLE001 - boundary
    # A traceback in Kuma msg= is unreadable and, worse, indistinguishable from
    # a real verdict. Boundary it into a named BROKEN state.
    sys.stderr.write("STAGE=prowlarr-appsync-probe-error msg=%s-%s\n"
                     % (type(exc).__name__, re.sub(r"\s+", "-", str(exc))[:120]))
    sys.exit(EXIT_BROKEN)
PY

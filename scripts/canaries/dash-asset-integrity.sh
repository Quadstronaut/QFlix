#!/usr/bin/env bash
# dash-asset-integrity canary: the QFlix Dashboard's SERVED app shell may only
# reference assets the RUNNING server can actually deliver.
#
# ===========================================================================
# WHY THIS EXISTS (incident 2026-07-29, verified live)
# ===========================================================================
# The dashboard at the public root served a DEAD app shell for ~22 hours with
# every monitor green.
#
#   - build/ assets on the box were rewritten 2026-07-29 01:33 and 03:31-03:32.
#   - The node process (PID 15178) had been running since 2026-07-08 21:14 and
#     was NEVER restarted.
#   - SvelteKit adapter-node serves static assets through sirv, which snapshots
#     its file manifest ONCE at process start. Files created after boot are
#     invisible to it.
#   - Result: 6 of the 10 /_app/immutable/* modules referenced by the served
#     HTML returned 404 even though the files existed on disk at mode 644
#     (app.CGGeqWLt.js, 2830 bytes, present -> HTTP 404).
#   - sirv ALSO precomputes Content-Length/ETag/Last-Modified at boot, so for a
#     file rewritten IN PLACE it advertised a stale byte count while streaming
#     the fresh bytes. Browsers got net::ERR_CONTENT_LENGTH_MISMATCH on the
#     HTML document itself and navigation never reached domcontentloaded.
#   - Net effect: zero hydration. No Support modal, no live refresh, no client
#     nav. Warm-cache browsers kept working off the stale-but-consistent
#     June 28 shell, which is why it looked fine to some clients, dead to
#     others.
#
# WHY NOTHING CAUGHT IT (the gap this canary closes):
#   - scripts/smoke-test.sh and scripts/canaries/mobile-ux.sh:29 only grep the
#     HTML body for the string "data-qflix-dash". That marker lives in the
#     server-rendered shell, so a completely non-hydrating dashboard stays
#     GREEN. This canary deliberately does NOT re-grep that marker.
#   - manifest/apps.yaml gives qflix-dash an http_root health probe on
#     /healthz expecting 200. The stale in-memory server answers /healthz 200
#     forever, so the pusher's probe never failed, recovery.trigger_async never
#     fired, and the "QFlix Dashboard" app monitor stayed green. No existing
#     surface in this repo can see this fault class.
#   - scripts/configure/90-qflix-dash-install.sh used `enable --now`, which
#     STARTS a stopped unit but does NOT restart a running one - so the
#     installer shipped a fresh build/ over a live process and left the stale
#     manifest serving, then "verified" through /healthz and /api/status, both
#     answered by the old in-memory server.
#
# COMPARTMENTALIZE BOUNDARY (operator design law): mobile-ux owns "root
# reachable + page small enough for mobile". This canary owns "the deployed
# build is internally consistent and the app can actually hydrate". Different
# concern, separate module/timer/Kuma check, independently tunable.
#
# ===========================================================================
# PREDICATES
# ===========================================================================
# P1  ASSET RESOLVABILITY. Fetch the public root, extract EVERY
#     /_app/immutable/* reference out of the ACTUALLY SERVED HTML (never a
#     local build - the whole point is to compare what the server ADVERTISES
#     against what the server SERVES), and require every one of them to
#     resolve 200. Any 404 is a hard FAIL on the first cycle. There is no
#     multi-cycle "pending" tier for a 404 the way sab-stall/qbit-stall need
#     one, because sirv's manifest is immutable for the process lifetime: a
#     referenced module that 404s once will 404 forever until the process is
#     replaced. Nothing about it is transient, so waiting only lengthens the
#     outage.
#
# P2  DECLARED-VS-DELIVERED LENGTH. For every 200, the Content-Length the
#     server advertises must equal the number of body bytes it actually
#     delivers (an http.client.IncompleteRead, or a header/body disagreement,
#     is a fail).
#
#     Unlike P1, a P2 hit is CORROBORATED with an immediate re-probe of the
#     same URL/encoding before it is believed. P1's "no pending tier" argument
#     is about sirv's manifest being immutable for the process lifetime; that
#     argument does NOT extend to body delivery, which one cycling nginx worker
#     or TCP reset can break transiently. A genuine stale sirv Content-Length
#     is deterministic (the stat tuple was computed at process start), so the
#     re-probe reproduces it every time - which makes it a clean discriminator
#     at the cost of one request, keeping single-cycle detection intact. A
#     mismatch that does NOT reproduce is filed as transport noise
#     (INCONCLUSIVE), never as a repairable fault. Without this, ONE dropped
#     response restarted a healthy dashboard and burned the 24h breaker.
#
# P4  BODY ACTUALLY DELIVERED. A 200 whose body cannot be read at all (socket
#     timeout mid-stream, reset, a worker that sends headers then stalls) is a
#     FAIL - stage dash-asset-unread - after one retry. It gets its own bucket
#     because `received` is None there, so it satisfies neither P1 nor P2 and
#     an earlier draft filed it in NO bucket and reported
#     "PASS ... all-200-and-length-consistent" for an asset the browser can
#     never execute. Alert only, never a restart: headers-then-stall is not the
#     stale-manifest signature.
#
#     THIS IS A SEPARATE PREDICATE, NOT IMPLIED BY P1, and that is the whole
#     reason the second symptom existed. The two symptoms come out of two
#     DIFFERENT sirv caches:
#       - the path -> file map (missed a file created after boot -> 404), and
#       - the per-file stat tuple (size/mtime captured at boot -> stale
#         Content-Length/ETag on a file rewritten IN PLACE).
#     Either can occur without the other. Content-hashed asset names
#     (app.CGGeqWLt.js) change on every build, so a rebuild trips P1 - but the
#     prerendered root document is named index.html and NEVER changes name, so
#     it stays 200 and only ever trips P2. That is exactly the leg that threw
#     ERR_CONTENT_LENGTH_MISMATCH on the HTML document. A canary with only P1
#     would have missed it.
#
# P3  NON-EMPTY REFERENCE SET. A root that returns 200 with ZERO
#     /_app/immutable/* references is a FAIL, not a pass. Without this, a
#     future SvelteKit change to the asset path prefix (or a regression in the
#     extractor below) would silently turn this canary into another blind
#     check - which is the failure mode being fixed here, not repeated.
#
# ===========================================================================
# CONTENT-ENCODING: WHY THREE PROFILES, NOT ONE
# ===========================================================================
# During the incident a bare curl and a real browser got DIFFERENT BYTES, and
# that divergence is exactly why the smoke test could pass while every browser
# was broken. adapter-node's sirv is built with precompressed support and the
# build ships a .br and a .gz sibling for every asset, indexed as SEPARATE
# manifest entries with SEPARATE precomputed stat tuples. Measured live on the
# box, one URL, three encodings:
#
#   Accept-Encoding: identity  ->  200  Content-Length 2830   (app.CGGeqWLt.js)
#   Accept-Encoding: br        ->  200  Content-Length 1129   (.js.br)
#   Accept-Encoding: gzip      ->  200  Content-Length 1293   (.js.gz)
#
# and for the root document: identity 6472 / br 1621 / gzip 1957, each matching
# the on-disk size of build/prerendered/index.html{,.br,.gz}.
#
# Consequences:
#   - A bare curl sends no Accept-Encoding and therefore only ever exercises
#     the IDENTITY entry. Every browser on earth sends "gzip, deflate, br" and
#     is served the .br entry. If the .br entry is stale or unindexed and the
#     identity entry is fine, an identity-only check is GREEN while 100% of
#     real traffic is broken. That is the smoke test's blind spot, restated.
#   - The reverse is equally possible (identity stale, .br fine), and gzip is
#     its own third entry serving br-less clients.
# So all three are probed, per URL. Cost is ~34 small requests per 15-minute
# tick against a loopback-proxied nginx: negligible, and it is the only way the
# canary cannot be fooled the way the smoke test was.
#
# Requests are issued with urllib and an explicit Accept-Encoding header and
# WITHOUT --compressed-style negotiation, so nothing is transparently
# decompressed and len(body) is directly comparable to Content-Length
# (verified live: identity 6472==6472, br 1621==1621, gzip 1957==1957).
# Cache-Control/Pragma no-cache are sent so an intermediary cannot serve a
# stale-but-consistent copy over a broken origin.
#
# ===========================================================================
# SELF-HEAL
# ===========================================================================
# REPAIRABLE SIGNATURE (stale in-process sirv state -> a restart replaces the
# process and rebuilds the manifest):
#   - one or more referenced URLs 404 AND every 404-ing URL's file EXISTS on
#     disk under the build dir, and/or
#   - one or more URLs return 200 with a Content-Length that disagrees with
#     the delivered body (the file necessarily exists, we just read it).
#
# NOT REPAIRABLE - alert, do NOT restart (a restart is pure churn and would
# mask the real fault; this refuse-on-wrong-signature discipline is
# scripts/maint/flaresolverr-canary.py:12-18 applied here):
#   - a 404 whose file is genuinely ABSENT on disk -> broken/partial deploy.
#   - the build directory itself missing/unreadable -> cannot arbitrate.
#   - a non-200/non-404 status (403/5xx) -> a different fault.
#   - zero asset references -> a build/template regression.
#   - a 200 whose body never arrives (P4) -> a stalled worker, not stale state.
#
# The repair is gated, in this order, and EVERY gate still alerts (a refused
# heal is never a silent pass):
#   1. armed?          QFLIX_CANARY_DASH_SELF_HEAL=0 or --dry-run disarms.
#   2. pause window?   NO box operations during the Monday maintenance window.
#   3. breaker?        at most ONE self-heal restart per 24h, durable latch.
#   4. cold start?     do not restart a unit that entered active less than
#                      MIN_UPTIME_S ago (default 2 timer ticks; it MUST exceed
#                      one tick or the guard can never fire).
#   5. RESERVE the breaker latch - write it atomically and read it back BEFORE
#      the restart is issued. If it will not persist, the 1-per-24h guarantee
#      does not exist, so the restart is REFUSED with stage
#      dash-heal-latch-unwritable rather than issued unbounded. The earlier
#      order (issue, then stamp best-effort, ignoring the result) deleted the
#      breaker entirely whenever the state dir was unwritable: measured 3 ticks
#      -> 3 restarts, i.e. 96 unattended restarts/day with no durable record.
#      Every path that turns out NOT to have issued the mutation RELEASES the
#      reservation, so council defect D1 stays fixed.
#   6. issue, then RE-VERIFY by re-fetching and re-checking. Three honest
#      outcomes: recovered / issued-but-not-verified / command failed. Success
#      is never claimed unverified.
#
# PAUSE WINDOW - a deliberate, documented deviation from the letter of the
# brief. `suppression.in_pause_window(app)`
# (scripts/maint/lib/suppression.py:88-111) is the PER-APP DAILY quiet-hours
# predicate: it reads `app.pause_window`, manifest.Canary
# (scripts/maint/lib/manifest.py:52-57) has no such field, and qflix-dash
# declares none - so calling it here would hit the `pw is None -> return False`
# branch and be an unconditional no-op. That would satisfy the letter of the
# rule while providing ZERO protection, i.e. exactly the committed-but-inert
# failure this work exists to prevent. The Monday 11:00-15:00 UTC window is
# `suppression.in_maintenance_window()` (suppression.py:45-64), a lockfile
# test. Three OR-ed legs are consulted, mirroring
# scripts/maint/qflix-torrent-janitor.py:214-234:
#   (a) wall clock Mon 11:00-15:00 UTC - authoritative because the calendar
#       lives only in the units (manitoba-maint-window.timer OnCalendar=Mon
#       11:00:00 UTC, manitoba-maint-window-watchdog.timer 15:00:00 UTC) and
#       this leg still protects if the orchestrator never opened the lock;
#   (b) the canonical lib.suppression.in_maintenance_window() predicate;
#   (c) a direct live-PID lockfile read, so the guard survives lib being
#       unimportable mid-deploy.
# FAIL DIRECTION SPLITS: detection/alerting fails OPEN (never silence a real
# outage on a parse glitch - suppression.py:55-57), the RESTART fails CLOSED
# (if window state cannot be determined the restart is skipped). A false
# suppression costs one 15-minute cycle of delayed healing; a false restart
# violates an absolute operator directive.
#
# Note that in production `manitoba-maint canary push` already short-circuits
# the whole canary during the window (scripts/maint/lib/cli.py:562-576). This
# in-script gate is the second, independent leg: it covers a direct operator
# invocation, the installer's verify step, and the case where cli.py's own
# check is bypassed - and unlike cli.py it still RUNS detection and reports,
# suppressing only the mutation.
#
# ===========================================================================
# EXECUTION MODEL - runs LOCALLY, no sshm hop
# ===========================================================================
# Same local-execution pattern as scripts/canaries/kometa-deploy-drift.sh and
# scripts/canaries/newsletter-digest-stale.sh, chosen for three reasons:
#   1. Everything it needs is co-resident wherever it runs: outbound HTTPS to
#      the public root, read access to the build dir, and `systemctl --user`.
#      The systemd ExecStart already runs ON the seedbox.
#   2. It is the only shape that is hermetically testable. The unit suite is
#      explicitly no-SSH; wrapping this in sshm would make the breaker, the
#      window gate and the exists-vs-absent branch untestable.
#   3. It makes the mutating leg physically unable to reach the box from a
#      workstation. An sshm-wrapped restart would fire against production the
#      moment anyone ran the script during development; here
#      `systemctl --user restart` is a LOCAL command that simply fails off-box
#      and reports dash-heal-not-issued without burning the breaker.
#
# ===========================================================================
# STAGE labels (stderr on failure -> Kuma msg=)
# ===========================================================================
#   dash-host-secret-missing     ~/secrets/seedbox.host unreadable/empty
#   dash-config-missing          root URL could not be resolved at all
#   dash-no-asset-refs           root 200 but ZERO /_app/immutable refs (P3)
#   dash-assets-404              referenced asset(s) 404 while present on disk
#                                (the incident signature - heal attempted)
#   dash-length-mismatch         Content-Length disagrees with the delivered
#                                body (heal attempted)
#   dash-assets-missing-on-disk  asset(s) 404 and genuinely ABSENT on disk ->
#                                broken/partial deploy, NO restart
#   dash-build-dir-missing       404s present but the build dir is unusable ->
#                                cannot arbitrate, NO restart
#   dash-asset-badstatus         asset returned a non-200/non-404 status
#   dash-healed                  repairable signature, restart issued,
#                                RE-VERIFIED healthy (still DOWN once, on
#                                purpose - see below)
#   dash-heal-unverified         restart issued but assets still failing at
#                                the re-verify deadline
#   dash-heal-failed             restart command ran and returned non-zero
#   dash-heal-not-issued         restart could not be attempted at all
#                                (command absent) - latch NOT burned
#   dash-heal-breaker-open       repairable, but a heal already fired inside
#                                the 24h cooldown -> operator needed
#   dash-heal-suppressed-window  repairable, but the Monday window is open
#   dash-heal-disarmed           repairable, but self-heal is switched off
#   dash-heal-cold-start         repairable, but the unit entered active less
#                                than MIN_UPTIME_S ago
#   dash-probe-error             last-resort boundary (never a traceback)
#
# stdout PASS:          "PASS: dash-asset-integrity refs=N probes=N ..."  (0)
# stdout INCONCLUSIVE:  "dash-check-inconclusive ..."                     (0)
#
# dash-healed exits NON-ZERO on purpose. scripts/canaries/quota.sh:67-80 sets
# the precedent: an autonomous mutation pushes DOWN so the operator sees that
# something happened. The next 15-minute cycle goes green, so it is one edge
# page, not a loop - and after an incident whose entire character was
# "everything green while broken", an unattended restart of a customer-facing
# app must not be silent.
#
# INCONCLUSIVE (exit 0, deferring) covers root unreachable / non-200 and
# transport-only errors. The "QFlix Dashboard" app monitor and the mobile-ux
# canary already own "the dashboard is down"; two reds for one cause is the
# correlated noise this repo keeps removing. This cannot mask the incident:
# its signature was root 200 with a readable body (which is exactly why the
# smoke test passed), so the inconclusive branch is unreachable for it.
#
# ===========================================================================
# Tunables (env; cli.py invokes the script with NO arguments, so every knob
# must be an env var with an in-script default)
# ===========================================================================
#   QFLIX_CANARY_DASH_URL                root URL. Default https://<seedbox.host>/
#   QFLIX_CANARY_DASH_BUILD_DIR          default ~/.apps/qflix-dash/build
#   QFLIX_CANARY_DASH_UNIT               default qflix-dash.service
#   QFLIX_CANARY_DASH_STATE_DIR          default ~/.opt/maint/dash-asset-integrity
#   QFLIX_CANARY_DASH_ENCODINGS          default identity,br,gzip
#   QFLIX_CANARY_DASH_TIMEOUT_S          per-request timeout, default 10
#   QFLIX_CANARY_DASH_BUDGET_S           whole-sweep budget, default 120
#   QFLIX_CANARY_DASH_MAX_ASSETS         cap on refs probed, default 40
#   QFLIX_CANARY_DASH_RETRY_SLEEP_S      transport retry backoff, default 1
#   QFLIX_CANARY_DASH_SELF_HEAL          1 = armed (default), 0 = detect only
#   QFLIX_CANARY_DASH_HEAL_COOLDOWN_H    breaker window, default 24
#   QFLIX_CANARY_DASH_MIN_UPTIME_S       cold-start guard, default 120
#   QFLIX_CANARY_DASH_RESTART_CMD        default "systemctl --user restart <unit>"
#   QFLIX_CANARY_DASH_RESTART_TIMEOUT_S  default 90
#   QFLIX_CANARY_DASH_VERIFY_DELAY_S     settle before re-verify, default 5
#   QFLIX_CANARY_DASH_VERIFY_DEADLINE_S  re-verify poll deadline, default 60
#   QFLIX_CANARY_DASH_VERIFY_POLL_S      re-verify poll interval, default 5
#   QFLIX_CANARY_DASH_NOW                ISO8601 UTC override for "now"
#   QFLIX_CANARY_DASH_FORCE_WINDOW       "1" force in-window, "0" force out
#   QFLIX_CANARY_DASH_UPTIME_S           override measured unit uptime (tests)
#   MANITOBA_STATE_DIR                   where the window lockfile lives
#
# Argv: `--dry-run` disarms the heal (probe + decide + log, never mutate).
# Self-test hooks, mirroring newsletter-digest-stale.sh's __is_fresh__ so a
# python test can exercise THIS deployed artifact rather than a
# re-derivation of it:
#   dash-asset-integrity.sh __refs__ <html-file>  -> one ref per line
#   dash-asset-integrity.sh __disk__ <url-path>   -> "present" | "absent"
#
# Durable audit trail (journald on this shared seedbox is
# permission-restricted and rotation-prone, so the logfile is the record that
# is actually trusted - see scripts/maint/qflix-reaper.py:140-192):
#   ~/.opt/maint/dash-asset-integrity/dash-asset-integrity-YYYYMMDD.log
#   ~/.opt/maint/dash-asset-integrity/events/YYYY-MM-DD.jsonl  (heal attempts)
#   ~/.opt/maint/dash-asset-integrity/heal-latch.epoch          (24h breaker)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# --- Self-test hooks (must run before any secret resolution) ----------------
if [ "${1:-}" = "__refs__" ] || [ "${1:-}" = "__disk__" ]; then
  export QFLIX_CANARY_DASH_SELFTEST="${1}"
  export QFLIX_CANARY_DASH_SELFTEST_ARG="${2:-}"
  export QFLIX_CANARY_DASH_URL="${QFLIX_CANARY_DASH_URL:-http://127.0.0.1:0/}"
fi

# --- Root URL: explicit override, else the public-host secret ---------------
# A missing/empty secret must FAIL LOUDLY, never read as green - the silent
# skip is a documented failure class in this repo (scripts/maint/lib/cli.py
# :628-630 skips the Kuma push silently when a token key is absent, which is
# how a canary can "pass" while pushing nothing). Same shape as
# scripts/canaries/mobile-ux.sh:13-14.
if [ -z "${QFLIX_CANARY_DASH_URL:-}" ]; then
  PUBLIC_HOST=$(cat "$HOME/secrets/seedbox.host" 2>/dev/null) || {
    printf 'STAGE=dash-host-secret-missing msg=seedbox.host-unreadable\n' >&2
    exit 1
  }
  PUBLIC_HOST=$(printf '%s' "$PUBLIC_HOST" | tr -d '[:space:]')
  [ -n "$PUBLIC_HOST" ] || {
    printf 'STAGE=dash-host-secret-missing msg=seedbox.host-empty\n' >&2
    exit 1
  }
  export QFLIX_CANARY_DASH_URL="https://${PUBLIC_HOST}/"
fi

# Where lib.suppression lives, for the canonical window predicate. On the box
# ROOT resolves to $HOME and this is ~/scripts/maint. Overridable so a test or
# an out-of-tree invocation can point it somewhere real (or nowhere, in which
# case the window check falls back to its wall-clock + lockfile legs).
export QFLIX_CANARY_DASH_MAINT_LIB="${QFLIX_CANARY_DASH_MAINT_LIB:-$ROOT/scripts/maint}"

python3 - "$@" <<"PYEOF"
import datetime as dt
import importlib.util
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead
from pathlib import Path

# --- config ---------------------------------------------------------------


def _env(name, default):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def _env_int(name, default):
    try:
        return int(str(_env(name, default)).strip())
    except Exception:
        return int(default)


ARGV = sys.argv[1:]
DRY_RUN = "--dry-run" in ARGV

SELFTEST = _env("QFLIX_CANARY_DASH_SELFTEST", "")
SELFTEST_ARG = _env("QFLIX_CANARY_DASH_SELFTEST_ARG", "")

ROOT_URL = _env("QFLIX_CANARY_DASH_URL", "")
BUILD_DIR = Path(_env("QFLIX_CANARY_DASH_BUILD_DIR",
                      str(Path.home() / ".apps" / "qflix-dash" / "build")))
UNIT = _env("QFLIX_CANARY_DASH_UNIT", "qflix-dash.service")
STATE_DIR = Path(_env("QFLIX_CANARY_DASH_STATE_DIR",
                      str(Path.home() / ".opt" / "maint" / "dash-asset-integrity")))
ENCODINGS = [e.strip() for e in
             _env("QFLIX_CANARY_DASH_ENCODINGS", "identity,br,gzip").split(",")
             if e.strip()]
TIMEOUT_S = _env_int("QFLIX_CANARY_DASH_TIMEOUT_S", 10)
BUDGET_S = _env_int("QFLIX_CANARY_DASH_BUDGET_S", 120)
MAX_ASSETS = _env_int("QFLIX_CANARY_DASH_MAX_ASSETS", 40)
RETRY_SLEEP_S = _env_int("QFLIX_CANARY_DASH_RETRY_SLEEP_S", 1)

SELF_HEAL = (not DRY_RUN) and _env("QFLIX_CANARY_DASH_SELF_HEAL", "1") == "1"
COOLDOWN_H = _env_int("QFLIX_CANARY_DASH_HEAL_COOLDOWN_H", 24)
# The timer's tick, seconds. manitoba-maint-canary-dash-asset-integrity.timer
# says `OnCalendar=*:0/15`, i.e. 900s; tests/unit/test_dash_asset_integrity.py
# pins this literal against that unit so the two cannot drift.
TICK_S = _env_int("QFLIX_CANARY_DASH_TICK_S", 900)
# Cold-start guard: "the unit was JUST (re)started and the assets are STILL
# broken, so another restart is churn". It MUST exceed one timer tick or it can
# never fire - the first draft used 120s against a 900s tick, so measured uptime
# at the next tick was always ~900s and the guard the header advertised as the
# breaker's backstop was dead code. 2 ticks gives real margin.
MIN_UPTIME_S = _env_int("QFLIX_CANARY_DASH_MIN_UPTIME_S", TICK_S * 2)
RESTART_CMD = _env("QFLIX_CANARY_DASH_RESTART_CMD",
                   "systemctl --user restart " + UNIT)
RESTART_TIMEOUT_S = _env_int("QFLIX_CANARY_DASH_RESTART_TIMEOUT_S", 90)
VERIFY_DELAY_S = _env_int("QFLIX_CANARY_DASH_VERIFY_DELAY_S", 5)
VERIFY_DEADLINE_S = _env_int("QFLIX_CANARY_DASH_VERIFY_DEADLINE_S", 60)
VERIFY_POLL_S = _env_int("QFLIX_CANARY_DASH_VERIFY_POLL_S", 5)

NOW_OVERRIDE = _env("QFLIX_CANARY_DASH_NOW", "")
FORCE_WINDOW = _env("QFLIX_CANARY_DASH_FORCE_WINDOW", "")
UPTIME_OVERRIDE = _env("QFLIX_CANARY_DASH_UPTIME_S", "")
MAINT_LIB = _env("QFLIX_CANARY_DASH_MAINT_LIB", "")

# The Monday maintenance window, UTC. The calendar is authoritative in the
# units, not in python: scripts/maint/systemd/manitoba-maint-window.timer says
# `OnCalendar=Mon *-*-* 11:00:00 UTC` and manitoba-maint-window-watchdog.timer
# says `OnCalendar=Mon *-*-* 15:00:00 UTC`. These two literals are pinned
# against those files by tests/unit/test_dash_asset_integrity.py so they cannot
# drift apart silently.
WINDOW_START_HOUR_UTC = 11
WINDOW_END_HOUR_UTC = 15

# A real browser identity. sirv keys nothing off User-Agent, but proxy layers
# do vary compression on it, and the entire premise of this canary is to see
# what a browser sees rather than what a bare curl sees.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 qflix-canary/1")

REF_RE = re.compile(r"/_app/immutable/[A-Za-z0-9._~/-]+")

_LOG_FH = None
_LOG_RETENTION_DAYS = 30


# --- clock ----------------------------------------------------------------


def utcnow():
    """Current UTC time, honoring the NOW override. Falls back to the real
    clock on an unparseable override rather than failing the run."""
    if NOW_OVERRIDE:
        try:
            n = dt.datetime.fromisoformat(NOW_OVERRIDE.replace("Z", "+00:00"))
            if n.tzinfo is None:
                n = n.replace(tzinfo=dt.timezone.utc)
            return n.astimezone(dt.timezone.utc)
        except Exception:
            pass
    return dt.datetime.now(dt.timezone.utc)


# --- durable logging ------------------------------------------------------
# Every write is best-effort and NEVER raises; a logging failure degrades to
# "no durable trail", never to a failed canary run. Narrative goes ONLY to the
# logfile: stdout/stderr are the Kuma msg= contract (exactly one line each
# way, truncated to 200 chars by cli.py:609), so they cannot carry detail.


def _setup_log():
    global _LOG_FH
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        day = utcnow().strftime("%Y%m%d")
        _LOG_FH = open(STATE_DIR / ("dash-asset-integrity-" + day + ".log"),
                       "a", encoding="utf-8")
        cutoff = time.time() - _LOG_RETENTION_DAYS * 86400
        for old in STATE_DIR.glob("dash-asset-integrity-*.log"):
            try:
                if old.stat().st_mtime < cutoff:
                    old.unlink()
            except OSError:
                pass
    except Exception:
        _LOG_FH = None


def log(msg):
    if _LOG_FH is None:
        return
    try:
        _LOG_FH.write(utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                      + " [dash-asset-integrity] " + str(msg) + "\n")
        _LOG_FH.flush()
    except Exception:
        pass


def log_event(action, trigger, outcome, extra=None):
    """One JSON object per line, keys ts/action/trigger/outcome (+extras) -
    the structured action trail, matching qflix-collect.py:838-844. Used for
    heal attempts specifically, so 'did this canary ever restart the app, and
    what happened' is machine-answerable."""
    try:
        d = STATE_DIR / "events"
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "action": action,
            "trigger": trigger,
            "outcome": outcome,
        }
        if extra:
            rec.update(extra)
        with open(d / (utcnow().strftime("%Y-%m-%d") + ".jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception:
        pass


# --- output contract ------------------------------------------------------


def slug(s, maxlen=150):
    """Collapse to a single dash-separated token with no whitespace and no
    control characters. `msg=` becomes the Kuma message field, so a value that
    could contain newlines would corrupt the single-line contract. 150 keeps
    "STAGE=<longest-label> msg=<slug>" inside the 200-char truncation cli.py
    applies at scripts/maint/lib/cli.py:609, so the tail is never cut off; the
    full detail lives in the durable log."""
    t = re.sub(r"[\x00-\x1f\x7f]", "?", str(s))
    t = re.sub(r"\s+", "-", t.strip())
    return t[:maxlen]


def emit_fail(stage, msg):
    line = "STAGE=" + stage + " msg=" + slug(msg)
    print(line, file=sys.stderr)
    log("FAIL " + line)
    return 1


def emit_pass(msg):
    print("PASS: dash-asset-integrity " + slug(msg))
    log("PASS " + slug(msg))
    return 0


def emit_inconclusive(msg):
    print("dash-check-inconclusive " + slug(msg))
    log("INCONCLUSIVE " + slug(msg))
    return 0


# --- HTTP probing ---------------------------------------------------------

_SSL_CTX = None


def ssl_ctx():
    """Unverified TLS context. Certificate validity is a DIFFERENT concern
    with a different owner (per the compartmentalize law); this canary must
    keep reporting asset integrity even mid cert-renewal. Same posture as
    every sibling canary's `curl -sk` (scripts/canaries/mobile-ux.sh:15)."""
    global _SSL_CTX
    if _SSL_CTX is None:
        try:
            _SSL_CTX = ssl._create_unverified_context()
        except Exception:
            _SSL_CTX = False
    return _SSL_CTX or None


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def probe(url, enc, want_body=False):
    """One raw HTTP GET with an explicit Accept-Encoding. Never raises.

    Nothing is transparently decompressed, so `received` is the wire byte
    count and is directly comparable to `declared` (the Content-Length
    header). That comparison IS predicate P2.
    """
    out = {"url": url, "enc": enc, "status": 0, "declared": None,
           "received": None, "encoding": "", "err": "", "body": None,
           "unread": False}
    req = urllib.request.Request(url, headers={
        "Accept-Encoding": enc,
        "Accept": "*/*",
        "User-Agent": UA,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    kw = {"timeout": TIMEOUT_S}
    ctx = ssl_ctx()
    if ctx is not None and url.lower().startswith("https"):
        kw["context"] = ctx
    resp = None
    try:
        resp = urllib.request.urlopen(req, **kw)
    except urllib.error.HTTPError as exc:
        out["status"] = _int_or_none(getattr(exc, "code", 0)) or 0
        hdrs = getattr(exc, "headers", None)
        if hdrs is not None:
            out["declared"] = _int_or_none(hdrs.get("Content-Length"))
        try:
            exc.read()
        except Exception:
            pass
        try:
            exc.close()
        except Exception:
            pass
        return out
    except Exception as exc:
        out["err"] = type(exc).__name__
        return out

    try:
        out["status"] = (_int_or_none(getattr(resp, "status", None))
                         or _int_or_none(getattr(resp, "code", None)) or 0)
        out["declared"] = _int_or_none(resp.headers.get("Content-Length"))
        out["encoding"] = resp.headers.get("Content-Encoding") or ""
        try:
            body = resp.read()
            out["received"] = len(body)
            if want_body:
                out["body"] = body
        except IncompleteRead as exc:
            # The server promised Content-Length bytes and delivered fewer.
            # This is precisely what a browser reports as
            # net::ERR_CONTENT_LENGTH_MISMATCH.
            partial = getattr(exc, "partial", b"") or b""
            out["received"] = len(partial)
            out["err"] = "IncompleteRead"
            if want_body:
                out["body"] = partial
        except Exception as exc:
            # A 200 whose body could NOT be read (socket timeout mid-body, reset
            # mid-stream, a wedged worker that sends headers and then stalls).
            # `received` stays None, so this must be flagged EXPLICITLY: an
            # earlier draft left it unflagged and record() then filed it in no
            # bucket at all, which reported
            #   PASS ... all-200-and-length-consistent
            # for an asset the browser can never execute - the same false-green
            # class this whole canary exists to eliminate. Proven 2026-07-29
            # against a fixture that answered 200/Content-Length 5000, wrote 10
            # bytes and stalled: the shipped script exited 0 with PASS.
            out["err"] = type(exc).__name__
            out["unread"] = True
    finally:
        try:
            resp.close()
        except Exception:
            pass
    return out


def probe_with_retry(url, enc, want_body=False):
    """Retry ONCE on a transport error (status 0) or on a 200 whose body could
    not be read - both are transport-level and genuinely transient.

    A definitive HTTP status with a complete body is never retried: a 404 from
    sirv is immutable for the process lifetime, so retrying it only delays the
    page. That immutability argument does NOT extend to body delivery, which is
    why `unread` is retried here."""
    p = probe(url, enc, want_body=want_body)
    if p["status"] != 0 and not p["unread"]:
        return p
    if RETRY_SLEEP_S > 0:
        time.sleep(RETRY_SLEEP_S)
    return probe(url, enc, want_body=want_body)


def base_url(root_url):
    """scheme://host[:port] from the root URL, for joining absolute paths."""
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.-]*://[^/]+)", root_url)
    return m.group(1) if m else root_url.rstrip("/")


# --- reference extraction (predicate P1/P3 input) -------------------------


def extract_refs(html):
    """Every /_app/immutable/* reference in the SERVED html, deduped, in
    first-seen order. Deliberately reads the served bytes, never a local
    build: the invariant is 'the shell may only reference what the RUNNING
    server can serve', which a local build cannot express."""
    refs = []
    seen = set()
    for m in REF_RE.finditer(html):
        r = m.group(0).rstrip(".")
        if not r or r in seen:
            continue
        seen.add(r)
        refs.append(r)
    return refs


# --- on-disk arbitration (the load-bearing branch) ------------------------


def disk_candidates(url_path):
    """The on-disk files adapter-node would serve `url_path` from.

    Verified live on the box: served /_app/immutable/X lives at
    <build>/client/_app/immutable/X, and the root document is the prerendered
    <build>/prerendered/index.html. Both roots are checked either way so a
    future non-prerendered build does not silently misclassify.
    """
    p = url_path.split("?", 1)[0].split("#", 1)[0]
    parts = [c for c in p.split("/") if c not in ("", ".")]
    if any(c == ".." for c in parts):
        # Refuse to resolve traversal. Treated as absent, which routes to
        # "alert, do not restart" - the safe direction.
        return []
    rel = "/".join(parts)
    if not rel:
        return [BUILD_DIR / "prerendered" / "index.html",
                BUILD_DIR / "client" / "index.html"]
    out = []
    for sub in ("client", "prerendered"):
        out.append(BUILD_DIR / sub / rel)
        out.append(BUILD_DIR / sub / rel / "index.html")
    return out


def exists_on_disk(url_path):
    """True iff the BASE file exists. The base file is the load-bearing one:
    sirv falls back to the identity entry when a .br/.gz sibling is not
    indexed, so a missing sibling cannot itself produce a 404."""
    for c in disk_candidates(url_path):
        try:
            if c.is_file():
                return True
        except OSError:
            pass
    return False


def build_dir_usable():
    """The build tree must be readable before an exists-vs-absent verdict
    means anything. If it is not, the correct action is to alert - NEVER to
    restart on the strength of an unanswerable question."""
    try:
        return BUILD_DIR.is_dir() and (BUILD_DIR / "client").is_dir()
    except OSError:
        return False


# --- pause window ---------------------------------------------------------


def window_active():
    """(active, leg). Three OR-ed legs; see the header for why
    suppression.in_pause_window is the wrong predicate here."""
    if FORCE_WINDOW == "1":
        return True, "forced-on"
    if FORCE_WINDOW == "0":
        return False, "forced-off"

    now = utcnow()
    if (now.weekday() == 0
            and WINDOW_START_HOUR_UTC <= now.hour < WINDOW_END_HOUR_UTC):
        return True, "wallclock-mon-%02d00-%02d00-utc" % (
            WINDOW_START_HOUR_UTC, WINDOW_END_HOUR_UTC)

    if MAINT_LIB:
        try:
            # Loaded BY PATH from MAINT_LIB, not via `from lib import ...`.
            # A bare package import resolves against the whole sys.path, so an
            # ambient PYTHONPATH could satisfy this leg from a DIFFERENT copy of
            # the maintenance library than the one this canary was pointed at -
            # silently, and with no way to tell which one answered. Loading the
            # exact file makes the leg deterministic (and makes the test that
            # proves leg 3 works when the lib is unavailable independent of
            # whatever PYTHONPATH the runner happens to export).
            _sup_path = Path(MAINT_LIB) / "lib" / "suppression.py"
            _spec = importlib.util.spec_from_file_location(
                "_dash_canary_suppression", str(_sup_path))
            if _spec is None or _spec.loader is None:
                raise ImportError("no loader for " + str(_sup_path))
            _sup = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_sup)
            if _sup.in_maintenance_window():
                return True, "suppression-in_maintenance_window"
        except Exception as exc:
            log("window leg suppression-unavailable=" + type(exc).__name__)

    try:
        lock = Path(os.environ.get(
            "MANITOBA_STATE_DIR",
            str(Path.home() / ".opt" / "maint"))) / "lock"
        if lock.exists():
            lines = lock.read_text(encoding="utf-8").splitlines()
            pid = _int_or_none(lines[0]) if lines else None
            if pid and pid > 0:
                if os.name == "posix":
                    # os.kill(pid, 0) is a liveness probe on POSIX only. On
                    # Windows it TERMINATES the process, so it is never called
                    # there - a present lock is simply honored.
                    try:
                        os.kill(pid, 0)
                        return True, "lock-live-pid"
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        return True, "lock-live-pid"
                else:
                    return True, "lock-present"
    except Exception as exc:
        log("window leg lock-unreadable=" + type(exc).__name__)

    return False, "clear"


def restart_suppressed_by_window():
    """Fail CLOSED: if the window state cannot be determined the restart is
    skipped. A false suppression costs one 15-minute cycle; a false restart
    breaks an absolute operator directive."""
    try:
        return window_active()
    except Exception as exc:
        log("window determination FAILED, suppressing restart: "
            + type(exc).__name__)
        return True, "indeterminate-" + type(exc).__name__


# --- 24h breaker ----------------------------------------------------------
# Ported from the only 1-per-24h restart breaker in this repo,
# qflix-collect.py:675-696: a single integer epoch in a plain file. The READ
# fails OPEN on a missing/corrupt latch ("the worst case there is one extra
# fire, not a permanently-stuck queue").
#
# The WRITE fails CLOSED, and that asymmetry is load-bearing. An earlier draft
# let stamp_latch() swallow its write error and return False, and the caller
# only LOGGED the result. That silently deleted the breaker: latch absent ->
# heal_cooldown_active() fails open -> restart again next tick, forever. Proven
# by a two-arm experiment on 2026-07-29 (latch file unwritable: 3 ticks -> 3
# restarts; writable: 3 ticks -> 1 restart, only variable changed), i.e. 96
# unattended restarts/day of a customer-facing app with no durable record,
# because the narrative log and events/*.jsonl fail on the same ENOSPC/EPERM.
#
# The stated backstop did not exist either: the cold-start guard is MIN_UPTIME_S
# (120s) while the timer is OnCalendar=*:0/15, so measured uptime at the next
# tick is ~900s and the guard is always already past. Both are fixed here -
# the latch is now RESERVED (written and read back) BEFORE the mutation is
# issued, and there is a second, stateless cap that needs no filesystem at all.
#
# ENOSPC/EDQUOT is not hypothetical on this slot: scripts/canaries/quota.sh
# exists because it reaches 80/90/98% of the disk limit, and quota.sh's 90%
# branch autonomously fires the reaper.


def latch_path():
    return STATE_DIR / "heal-latch.epoch"


def heal_cooldown_active():
    p = latch_path()
    try:
        if not p.exists():
            return False, 0
        last = float(p.read_text(encoding="utf-8").strip())
    except Exception:
        return False, 0
    age = utcnow().timestamp() - last
    return age < (COOLDOWN_H * 3600), int(age)


def stamp_latch():
    """Persist the breaker latch ATOMICALLY and verify it landed.

    tmp-file + os.replace so a kill mid-write cannot leave a truncated latch
    that heal_cooldown_active() would then fail open on. The read-back is not
    paranoia: on a full filesystem write() can succeed and flush can fail, and
    the whole point of this function is that its return value is now a hard
    gate on issuing an unattended restart.
    """
    tmp = None
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        stamp = str(int(utcnow().timestamp()))
        tmp = STATE_DIR / ("heal-latch.epoch.tmp." + str(os.getpid()))
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(stamp)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(latch_path()))
        # Read back. A latch that cannot be re-read is not a breaker.
        if latch_path().read_text(encoding="utf-8").strip() != stamp:
            log("WARNING latch-read-back-MISMATCH - breaker not durable")
            return False
        return True
    except Exception as exc:
        # Loud AND load-bearing: attempt_heal() refuses to issue the restart
        # when this returns False (stage dash-heal-latch-unwritable).
        log("WARNING latch-write-FAILED=" + type(exc).__name__
            + " - refusing to restart without a durable breaker")
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def release_latch():
    """Undo a reservation when the mutation turned out NOT to be issued.

    Council defect D1 (.claude/council-ledger.jsonl:122): the SAB breaker spent
    its whole 24h budget on an `error:no-secrets` no-op. Reserving before acting
    is what makes the breaker durable; releasing on the not-issued paths is what
    keeps D1 fixed. Best-effort - a failed release only costs one cooldown
    period of delayed healing, which is the safe direction."""
    try:
        latch_path().unlink()
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        log("WARNING latch-release-FAILED=" + type(exc).__name__
            + " - breaker will hold for %dh though nothing was issued"
            % COOLDOWN_H)
        return False


# --- unit uptime (cold-start guard) --------------------------------------


def unit_uptime_s():
    """Seconds since the unit last entered active, or None if it cannot be
    determined. Fails OPEN (None -> heal allowed): failing closed here would
    permanently disable the heal on any box where `systemctl show` is
    unreadable, and the durable latch is the real safety mechanism."""
    if UPTIME_OVERRIDE:
        try:
            return float(UPTIME_OVERRIDE)
        except Exception:
            return None
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", UNIT,
             "-p", "ActiveEnterTimestampMonotonic", "--value"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or b"").decode("utf-8", "replace").strip()
    aet_us = _int_or_none(raw)
    if not aet_us or aet_us <= 0:
        return None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            up_s = float(fh.read().split()[0])
    except Exception:
        return None
    return max(0.0, up_s - (aet_us / 1000000.0))


# --- the sweep ------------------------------------------------------------


def sweep():
    """Full outside-in integrity sweep. Never raises."""
    v = {"healthy": False, "inconclusive": "", "no_refs": False,
         "refs": [], "probes": 0, "html_bytes": 0, "truncated": False,
         "budget_exhausted": False,
         "notfound": [], "badstatus": [], "mismatch": [], "transport": [],
         "unread": []}
    deadline = time.time() + BUDGET_S
    base = base_url(ROOT_URL)

    def record(path, url, p):
        v["probes"] += 1
        if p["status"] == 0:
            v["transport"].append((path, p["enc"], p["err"] or "unknown"))
            return
        if p["status"] in (404, 410):
            v["notfound"].append((path, p["enc"]))
            return
        if p["status"] != 200:
            v["badstatus"].append((path, p["enc"], p["status"]))
            return
        if p["unread"] or p["received"] is None:
            # 200 with a body we could not read, and probe_with_retry already
            # tried twice. Its own bucket: NOT healthy (that was the false-green
            # bug) and NOT a heal trigger (a stalled body is not the stale-sirv
            # signature; the heal precondition stays narrow on purpose).
            v["unread"].append((path, p["enc"], p["err"] or "no-body"))
            return
        declared, received = p["declared"], p["received"]
        if p["err"] == "IncompleteRead" or (
                declared is not None and received is not None
                and declared != received):
            # CORROBORATE before believing it. A mismatch is transport-level and
            # genuinely transient (one mid-body drop from a cycling nginx worker
            # or a TCP reset), unlike a 404, whose immutability-for-the-process-
            # lifetime argument is what licenses single-cycle escalation. An
            # earlier draft escalated straight to the repairable bucket, so ONE
            # dropped response restarted a HEALTHY dashboard, reported
            # "restarted-and-RE-VERIFIED-healthy", and burned the 24h latch so a
            # REAL incident in the next 24h would need a human. Proven live
            # 2026-07-29 with a fixture that lied exactly once.
            #
            # A genuine stale sirv Content-Length is DETERMINISTIC - the stat
            # tuple was computed at process start and never changes - so a
            # re-probe reproduces it every time. That makes this a clean
            # discriminator and it costs one request, keeping single-cycle
            # (<=15 min) detection intact.
            re_p = probe(url, p["enc"])
            v["probes"] += 1
            re_dec, re_rec = re_p["declared"], re_p["received"]
            confirmed = (
                re_p["status"] == 200
                and (re_p["err"] == "IncompleteRead"
                     or (re_dec is not None and re_rec is not None
                         and re_dec != re_rec)))
            if confirmed:
                v["mismatch"].append((path, p["enc"], re_dec if re_dec
                                      is not None else declared,
                                      re_rec if re_rec is not None else received))
            else:
                # Did not reproduce -> wire noise, not a deploy fault. Filed as
                # transport, which resolves to INCONCLUSIVE (exit 0) if it is the
                # only thing that failed. Never silently discarded.
                v["transport"].append(
                    (path, p["enc"],
                     "length-mismatch-not-reproduced-declared-%s-got-%s"
                     % (declared, received)))

    # --- root, every encoding. The identity fetch doubles as the source of
    # the reference set, so no extra request is spent on extraction.
    root_identity = None
    for enc in ENCODINGS:
        p = probe_with_retry(ROOT_URL, enc, want_body=(enc == "identity"))
        if enc == "identity":
            root_identity = p
        record("/", ROOT_URL, p)
    if root_identity is None:
        root_identity = probe_with_retry(ROOT_URL, "identity", want_body=True)
        v["probes"] += 1

    if root_identity["status"] != 200 or root_identity["body"] is None:
        if root_identity["status"] == 0:
            v["inconclusive"] = ("root-transport-"
                                 + (root_identity["err"] or "unknown"))
        else:
            v["inconclusive"] = "root-http-" + str(root_identity["status"])
        # NOTE a 200 whose DOCUMENT body could not be read reaches here with
        # `inconclusive` set to "root-http-200", but record() has already filed
        # it under `unread` and main() checks `unread` FIRST - deliberately, so
        # the document-level form of the incident pages instead of being
        # reported as "deferring to app monitor". That ordering in main() is
        # what makes this correct; do not reorder it.
        return v

    html = root_identity["body"].decode("utf-8", "replace")
    v["html_bytes"] = len(root_identity["body"])
    refs = extract_refs(html)
    if not refs:
        v["no_refs"] = True
        return v
    if len(refs) > MAX_ASSETS:
        v["truncated"] = True
        refs = refs[:MAX_ASSETS]
    v["refs"] = refs

    for ref in refs:
        for enc in ENCODINGS:
            if time.time() >= deadline:
                v["budget_exhausted"] = True
                break
            record(ref, base + ref, probe_with_retry(base + ref, enc))
        if v["budget_exhausted"]:
            break

    if v["notfound"] or v["badstatus"] or v["mismatch"] or v["unread"]:
        return v
    if v["budget_exhausted"]:
        # Some referenced assets were never probed, so "everything resolves"
        # was not actually established. Reporting PASS here would be the same
        # class of lie as the marker check: a green light that proves nothing.
        v["inconclusive"] = "budget-exhausted-after-%ds-%d-of-%d-probes-done" % (
            BUDGET_S, v["probes"], len(refs) * len(ENCODINGS) + len(ENCODINGS))
        return v
    if v["transport"]:
        # Definitive responses are actionable; transport noise is not. The first
        # reason is carried through so a non-reproduced length mismatch is
        # distinguishable from a plain connection error without reading the log.
        path, enc, why = v["transport"][0]
        v["inconclusive"] = "transport-errors-%d-of-%d-probes-first=%s/%s-%s" % (
            len(v["transport"]), v["probes"], path.rsplit("/", 1)[-1], enc, why)
        return v
    v["healthy"] = True
    return v


def brief(v):
    """A compact, single-token failure summary for the Kuma msg field."""
    bits = []
    if v["notfound"]:
        bits.append("404=%d" % len(v["notfound"]))
        path, enc = v["notfound"][0]
        bits.append("first=%s/%s" % (path.rsplit("/", 1)[-1], enc))
    if v["mismatch"]:
        bits.append("len-mismatch=%d" % len(v["mismatch"]))
        path, enc, dec, rec = v["mismatch"][0]
        bits.append("first=%s/%s-declared-%s-got-%s" % (
            path.rsplit("/", 1)[-1], enc, dec, rec))
    if v["badstatus"]:
        bits.append("badstatus=%d" % len(v["badstatus"]))
        path, enc, st = v["badstatus"][0]
        bits.append("first=%s/%s-http-%s" % (path.rsplit("/", 1)[-1], enc, st))
    if v["unread"]:
        bits.append("body-unread=%d" % len(v["unread"]))
        path, enc, err = v["unread"][0]
        bits.append("first=%s/%s-%s" % (path.rsplit("/", 1)[-1], enc, err))
    if v["transport"]:
        bits.append("transport=%d" % len(v["transport"]))
    bits.append("refs=%d" % len(v["refs"]))
    return "-".join(bits)


# --- self-heal ------------------------------------------------------------


def attempt_heal(v):
    """Gate, issue, re-verify. Returns (stage, msg). Never raises, and every
    return path is a FAILURE - a refused or successful heal both alert."""
    trigger = brief(v)
    log("REPAIRABLE signature detected: " + trigger)

    if not SELF_HEAL:
        log_event("restart", trigger, "skipped:disarmed")
        return ("dash-heal-disarmed",
                trigger + "-self-heal-off-restart-qflix-dash-manually")

    in_window, leg = restart_suppressed_by_window()
    if in_window:
        log("heal SUPPRESSED by maintenance window (leg=" + leg + ")")
        log_event("restart", trigger, "skipped:pause-window",
                  {"window_leg": leg})
        return ("dash-heal-suppressed-window",
                trigger + "-restart-suppressed-window-" + leg)

    cooling, age = heal_cooldown_active()
    if cooling:
        log("heal REFUSED by 24h breaker (last fire %ds ago)" % age)
        log_event("restart", trigger, "skipped:breaker",
                  {"latch_age_s": age, "cooldown_h": COOLDOWN_H})
        return ("dash-heal-breaker-open",
                trigger + "-heal-REFUSED-last-fire-%dh-ago-cooldown-%dh"
                "-OPERATOR-INTERVENTION-NEEDED" % (age // 3600, COOLDOWN_H))

    up = unit_uptime_s()
    if up is not None and up < MIN_UPTIME_S:
        log("heal REFUSED, cold start (unit active for %.0fs)" % up)
        log_event("restart", trigger, "skipped:cold-start",
                  {"uptime_s": int(up)})
        return ("dash-heal-cold-start",
                trigger + "-restart-refused-unit-only-%ds-old-still-broken"
                "-OPERATOR-INTERVENTION-NEEDED" % int(up))

    argv = []
    try:
        argv = shlex.split(RESTART_CMD, posix=True)
    except Exception as exc:
        log("restart command unparseable: " + type(exc).__name__)
    if not argv:
        # NOT issued: the latch must NOT be burned. Stamping here would spend
        # the whole 24h budget on a no-op and mis-signal a fire - the exact
        # defect the council recorded against the SAB breaker (D1, 2026-07-20).
        log_event("restart", trigger, "not-issued:unparseable-command")
        return ("dash-heal-not-issued",
                trigger + "-restart-command-unparseable-latch-not-burned")

    # RESERVE THE BREAKER BEFORE MUTATING. This ordering is the whole fix for
    # the fail-open breaker: if the latch cannot be persisted and read back, the
    # 1-per-24h guarantee does not exist, and an unattended restart of a
    # customer-facing app without that guarantee is a restart loop waiting for a
    # full disk. Refuse, and page loudly with a distinct stage so the operator
    # sees WHY the heal was withheld. The not-issued paths below release the
    # reservation, so council defect D1 stays fixed.
    if not stamp_latch():
        log_event("restart", trigger, "refused:latch-unwritable",
                  {"state_dir": str(STATE_DIR)})
        return ("dash-heal-latch-unwritable",
                trigger + "-restart-REFUSED-no-durable-breaker"
                "-OPERATOR-INTERVENTION-NEEDED-check-writability-of-"
                + slug(str(STATE_DIR), 40))

    log("heal ISSUING restart: " + " ".join(argv))
    issued = True
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,
                              timeout=RESTART_TIMEOUT_S)
        rc = proc.returncode
        detail = ((proc.stdout or b"") + (proc.stderr or b"")).decode(
            "utf-8", "replace").strip()
    except subprocess.TimeoutExpired:
        # systemd may still be mid-cycle: the mutation WAS issued, so the
        # latch is burned and no success is claimed.
        rc, detail = 124, "restart-command-timed-out-after-%ds" % RESTART_TIMEOUT_S
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        issued = False
        rc, detail = 127, "restart-command-absent-" + type(exc).__name__
    except Exception as exc:
        issued = False
        rc, detail = -1, "restart-spawn-failed-" + type(exc).__name__

    if not issued:
        # The mutation never happened, so RELEASE the reservation (council D1).
        released = release_latch()
        log("heal NOT ISSUED (rc=%s %s) - latch reservation released=%s"
            % (rc, detail, released))
        log_event("restart", trigger, "not-issued",
                  {"rc": rc, "detail": detail, "latch_released": released})
        return ("dash-heal-not-issued",
                trigger + "-" + slug(detail, 60) + "-latch-not-burned")

    log("heal issued rc=%s (breaker latch already reserved) detail=%s"
        % (rc, detail))

    if rc != 0:
        log_event("restart", trigger, "failed", {"rc": rc, "detail": detail})
        return ("dash-heal-failed",
                trigger + "-restart-rc=%s-%s" % (rc, slug(detail, 60)))

    ok, attempts, last = reverify()
    if ok:
        log("heal VERIFIED healthy after %d re-probe attempt(s)" % attempts)
        log_event("restart", trigger, "recovered", {"attempts": attempts})
        return ("dash-healed",
                trigger + "-restarted-and-RE-VERIFIED-healthy-after-%d-probe(s)"
                % attempts)

    log("heal issued but NOT verified: " + last)
    log_event("restart", trigger, "issued-not-verified",
              {"attempts": attempts, "last": last})
    return ("dash-heal-unverified",
            trigger + "-restarted-but-still-failing-after-%ds-%s"
            % (VERIFY_DEADLINE_S, slug(last, 60)))


def reverify():
    """Re-fetch and re-check after the repair. Polls rather than single-shot,
    because a slow-but-successful recovery must not be reported as a failure
    (scripts/maint/flaresolverr-canary.py:329-332 records that exact
    regression). Returns (ok, attempts, last_summary) and NEVER raises -
    verification is observability, it must not turn a real heal into a crash.
    """
    if VERIFY_DELAY_S > 0:
        time.sleep(VERIFY_DELAY_S)
    started = time.time()
    attempts = 0
    last = "no-attempt"
    while True:
        attempts += 1
        try:
            v = sweep()
            if v["healthy"]:
                return True, attempts, "healthy"
            last = v["inconclusive"] or brief(v)
        except Exception as exc:
            last = "reverify-error-" + type(exc).__name__
        if (time.time() - started) >= VERIFY_DEADLINE_S:
            return False, attempts, last
        time.sleep(max(1, VERIFY_POLL_S))


# --- main -----------------------------------------------------------------


def run_selftest():
    if SELFTEST == "__refs__":
        try:
            with open(SELFTEST_ARG, "r", encoding="utf-8",
                      errors="replace") as fh:
                html = fh.read()
        except Exception as exc:
            print("ERROR " + type(exc).__name__, file=sys.stderr)
            return 1
        for r in extract_refs(html):
            print(r)
        return 0
    if SELFTEST == "__disk__":
        print("present" if exists_on_disk(SELFTEST_ARG) else "absent")
        return 0
    print("ERROR unknown-selftest", file=sys.stderr)
    return 1


def main():
    if SELFTEST:
        return run_selftest()

    _setup_log()

    if not ROOT_URL:
        return emit_fail("dash-config-missing", "root-url-unresolved")

    v = sweep()

    # BEFORE the inconclusive check on purpose: a 200 whose body never arrives
    # is a definitive, browser-visible dead asset, not wire noise. It is checked
    # first so the document-level form of it cannot be filed as "deferring".
    if v["unread"]:
        log("UNHEALTHY " + brief(v))
        # Alert only, never a restart. probe_with_retry already tried twice, so
        # this is persistent - but "headers arrive, body stalls" is NOT the
        # stale-sirv-manifest signature, and the heal precondition stays narrow
        # by design (restarting on the wrong signature is churn that masks the
        # real fault).
        return emit_fail(
            "dash-asset-unread",
            brief(v) + "-200-but-body-never-delivered-NOT-restartable")

    if v["inconclusive"]:
        # Deferring on purpose: "the dashboard is down" is already owned by the
        # QFlix Dashboard app monitor and the mobile-ux canary. This branch is
        # unreachable for the 2026-07-29 signature (root was 200).
        return emit_inconclusive(
            v["inconclusive"] + " probes=%d (deferring to app monitor)"
            % v["probes"])

    if v["no_refs"]:
        return emit_fail(
            "dash-no-asset-refs",
            "root-200-but-ZERO-_app-immutable-refs-html-bytes=%d"
            % v["html_bytes"])

    if v["healthy"]:
        return emit_pass(
            "refs=%d probes=%d enc=%s all-200-and-length-consistent%s"
            % (len(v["refs"]), v["probes"], ",".join(ENCODINGS),
               "-CAPPED-at-%d-refs" % MAX_ASSETS if v["truncated"] else ""))

    log("UNHEALTHY " + brief(v))

    # A non-200/non-404 status is not the stale-manifest signature. Alert only:
    # restarting on the wrong signature is churn that masks the real fault.
    if v["badstatus"]:
        return emit_fail("dash-asset-badstatus", brief(v))

    # --- exists-vs-absent: the load-bearing branch ------------------------
    if v["notfound"]:
        if not build_dir_usable():
            return emit_fail(
                "dash-build-dir-missing",
                brief(v) + "-build-dir-unusable-" + str(BUILD_DIR))
        absent = [(p, e) for (p, e) in v["notfound"] if not exists_on_disk(p)]
        if absent:
            # A restart cannot conjure files that are not there. This is a
            # broken/partial deploy - a DIFFERENT fault with a different fix.
            names = ",".join(sorted(set(p.rsplit("/", 1)[-1]
                                        for (p, e) in absent))[:3])
            return emit_fail(
                "dash-assets-missing-on-disk",
                brief(v) + "-absent-on-disk=%d-%s-BROKEN-DEPLOY-not-restartable"
                % (len(absent), names))

    stage, msg = attempt_heal(v)
    return emit_fail(stage, msg)


try:
    sys.exit(main())
except Exception as exc:  # boundary of last resort - never a raw traceback
    print("STAGE=dash-probe-error msg=unhandled-" + type(exc).__name__,
          file=sys.stderr)
    sys.exit(1)
PYEOF
exit $?

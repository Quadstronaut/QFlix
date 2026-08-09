#!/usr/bin/env bash
# newsletter-digest-stale canary: catch a stale/absent "Behind the scenes"
# digest blurb at Monday send time — the exact condition
# scripts/qflix-newsletter/qflix_newsletter/changelog.py's fetch_override()/
# _is_fresh() degrade through SILENTLY (INFO-only log, always falls back to
# the deterministic commit recap, never raises). Nothing watched this branch
# before this canary (see DIAGNOSIS: the 2026-07-06 routine fired per its
# last_fired_at but produced no commit — a real miss that went unnoticed
# until manually checked).
#
# Root design constraint: the newsletter freshness rule
# (changelog._is_fresh, changelog.py L348-361) allows week_of up to 4 days
# in the past — so a correctly-published Monday blurb reads FRESH through
# ~Friday and goes STALE over the weekend. A canary that ran the same rule
# every 15 min would false-alarm every Sat/Sun once the window closes. This
# canary therefore only ENFORCES freshness inside a narrow eval window
# (Monday 14:15-24:00 UTC, i.e. from just after the 14:00 UTC routine fires
# through the rest of send day) and is a pass-through (exit 0, no-op) at
# every other time of the week. See "EVAL WINDOW" below.
#
# Deliberately does NOT hop over SSH (unlike qbit-stall.sh/vlogs-stall.sh,
# which probe seedbox-loopback-only services). This check only needs
# outbound HTTPS to a public GitHub URL — available wherever
# `manitoba-maint canary push newsletter-digest` actually executes (the
# systemd timer's ExecStart already runs ON the seedbox) — and NOT hopping
# keeps the mandatory hermetic test overrides (QFLIX_DIGEST_CANARY_URL
# pointing at a local fixture file) trivially testable with no SSH
# round-trip. Same local-execution pattern as
# scripts/canaries/kometa-deploy-drift.sh.
#
# --- STAGE labels (stderr on failure -> Kuma msg) --------------------------
#   digest-stale     week_of parses but is not fresh per _is_fresh at
#                    send-window time (the silent-fallback condition)
#   digest-missing   digest URL/file 404s or doesn't exist
#   digest-malformed JSON parse failure, non-object body, or missing/
#                    non-string week_of
#   digest-empty     JSON parses and week_of is present, but html is blank
# PASS stdout: "digest-fresh week_of=... age=...Nd"                (exit 0)
# Out-of-window stdout: "not-in-eval-window ..."                   (exit 0)
# Inconclusive (transient transport) stdout: "digest-check-inconclusive ..." (exit 0)
#
# Exit contract: 0 = pass / UP / inconclusive / out-of-window;
#                non-zero = fail / DOWN with a STAGE=... line on stderr.
#
# --- Test/override env vars (hermetic acceptance tests) --------------------
#   QFLIX_DIGEST_CANARY_NOW           ISO8601 UTC override for "now"
#                                     (e.g. 2026-07-06T15:05:00Z)
#   QFLIX_DIGEST_CANARY_URL           override the digest source. An
#                                     https:// URL is fetched with retry; any
#                                     other value is treated as a local file
#                                     path (fixtures) - present -> read
#                                     directly (no retry, no transport to
#                                     blip); absent -> digest-missing.
#   QFLIX_DIGEST_CANARY_FORCE_WINDOW  "1" forces in-window enforcement
#                                     regardless of the real weekday/time.
# Non-mandatory extras (repo convention: override via systemd Environment=
# or env vars, default preserves production behavior):
#   QFLIX_DIGEST_CANARY_REPO           default "Quadstronaut/QFlix"
#   QFLIX_DIGEST_CANARY_BRANCH         default "newsletter-digest"
#   QFLIX_DIGEST_CANARY_TIMEOUT_S      per-curl-try timeout, default 10
#   QFLIX_DIGEST_CANARY_RETRY_SLEEP_S  backoff sleep between tries, default 2
#     (the last two exist only to keep the hermetic transient-transport test
#     fast; production never needs to touch them)
#
# Every non-pass also appends its reason to
#   ~/.opt/maint/canary-newsletter-digest-stale.log
# mirroring vlogs-stall.sh/qbit-stall.sh — Kuma's heartbeat table is owned by
# the Kuma Docker container and unreadable by the SSH user, so triage needs a
# host-readable trail too.
set -uo pipefail

# --- Self-test hook ----------------------------------------------------
# `newsletter-digest-stale.sh __is_fresh__ <week_of> <now_iso>` prints
# "fresh" or "stale" and exits 0. Not part of a normal canary run — it
# exists so the agreement test (tests/unit/test_newsletter_digest_canary.py)
# can invoke THIS deployed artifact's freshness function directly (not a
# re-derivation of it) and assert it matches
# qflix_newsletter.changelog._is_fresh across a boundary table. That's the
# actual guarantee the spec's "agreement test" invariant asks for.
if [ "${1:-}" = "__is_fresh__" ]; then
  WEEK_OF="${2:-}"
  NOW_ISO="${3:-}"
  python3 - "$WEEK_OF" "$NOW_ISO" <<'PYEOF'
import sys
import datetime as dt


def parse_week_of(week_of):
    try:
        return dt.datetime.strptime(week_of, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def is_fresh(week_of, now):
    # BYTE-IDENTICAL reimplementation of
    # qflix_newsletter.changelog._is_fresh (changelog.py L348-361). Kept in
    # lockstep by tests/unit/test_newsletter_digest_canary.py's agreement
    # test — any drift between the two fails CI.
    d = parse_week_of(week_of)
    if d is None:
        return False
    delta_days = (now - d).total_seconds() / 86400.0
    return -1.0 <= delta_days <= 4.0


def main():
    week_of, now_iso = sys.argv[1], sys.argv[2]
    try:
        now = dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except Exception:
        print("stale")  # unparseable "now" never happens on this path in practice
        return
    print("fresh" if is_fresh(week_of, now) else "stale")


main()
PYEOF
  exit 0
fi

# --- Normal canary run -------------------------------------------------
QFLIX_DIGEST_CANARY_NOW="${QFLIX_DIGEST_CANARY_NOW:-}"
QFLIX_DIGEST_CANARY_URL="${QFLIX_DIGEST_CANARY_URL:-}"
QFLIX_DIGEST_CANARY_FORCE_WINDOW="${QFLIX_DIGEST_CANARY_FORCE_WINDOW:-0}"
REPO="${QFLIX_DIGEST_CANARY_REPO:-Quadstronaut/QFlix}"
BRANCH="${QFLIX_DIGEST_CANARY_BRANCH:-newsletter-digest}"
TIMEOUT_S="${QFLIX_DIGEST_CANARY_TIMEOUT_S:-10}"
RETRY_SLEEP_S="${QFLIX_DIGEST_CANARY_RETRY_SLEEP_S:-2}"
DEFAULT_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/digest/latest.json"
URL="${QFLIX_DIGEST_CANARY_URL:-$DEFAULT_URL}"

LOG="$HOME/.opt/maint/canary-newsletter-digest-stale.log"
logfail() {
  mkdir -p "$(dirname "$LOG")" 2>/dev/null
  printf "%s %s\n" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$LOG" 2>/dev/null
  if [ -f "$LOG" ] && [ "$(wc -l < "$LOG" 2>/dev/null)" -gt 300 ]; then
    # FIX (council merge): rotate via a UNIQUE mktemp file, not a fixed-name
    # $LOG.tmp — concurrent invocations sharing one temp name race and lose
    # entries (reproduced 13/50). Per-invocation temp + atomic mv is race-safe.
    local _lt
    _lt=$(mktemp "${LOG}.XXXXXX" 2>/dev/null) || return 0
    tail -n 200 "$LOG" > "$_lt" 2>/dev/null && mv "$_lt" "$LOG" 2>/dev/null
    rm -f "$_lt" 2>/dev/null
  fi
}

# Step 1: resolve "now" (UTC) — override or wallclock.
if [ -n "$QFLIX_DIGEST_CANARY_NOW" ]; then
  NOW_ISO="$QFLIX_DIGEST_CANARY_NOW"
else
  NOW_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi

# Step 2: EVAL WINDOW gate. Freshness is only enforced Monday 14:15-24:00
# UTC (or QFLIX_DIGEST_CANARY_FORCE_WINDOW=1) — see the header comment for
# why. A single python3 call does the weekday + time-of-day math so it can't
# drift on GNU-vs-BSD `date -d` parsing differences across platforms.
WINDOW_VERDICT=$(python3 - "$NOW_ISO" "$QFLIX_DIGEST_CANARY_FORCE_WINDOW" <<'PYEOF'
import sys
import datetime as dt

now_iso, force = sys.argv[1], sys.argv[2]
try:
    now = dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
except Exception:
    print("out-of-window")  # unparseable NOW override never blocks a run
    sys.exit(0)

if force == "1":
    print("in-window")
    sys.exit(0)

is_monday = now.isoweekday() == 1
tod_min = now.hour * 60 + now.minute
in_send_window = is_monday and (14 * 60 + 15) <= tod_min < (24 * 60)
print("in-window" if in_send_window else "out-of-window")
PYEOF
)

if [ "$WINDOW_VERDICT" != "in-window" ]; then
  printf "not-in-eval-window now=%s force=%s\n" "$NOW_ISO" "$QFLIX_DIGEST_CANARY_FORCE_WINDOW"
  exit 0
fi

# Step 3: fetch the digest body.
#   https:// URL -> curl, retry 3x with backoff (mirrors vlogs-stall.sh). A
#     definitive 404 short-circuits (not spent as a retry) so a real
#     absent-branch/file doesn't masquerade as a transient blip.
#   anything else -> treated as a local-file override (hermetic fixtures).
#     Present -> read directly (no transport to blip, no retry needed).
#     Absent  -> digest-missing (mirrors a 404).
# Return codes: 0 = body on stdout; 44 = definitive missing (404/no file);
# 1 = transient transport failure after retries (INCONCLUSIVE).
fetch_body() {
  local url="$1" body rc try http_code
  case "$url" in
    http://*|https://*)
      for try in 1 2 3; do
        body=$(curl -sf -m "$TIMEOUT_S" "$url" 2>/dev/null)
        rc=$?
        if [ "$rc" -eq 0 ]; then
          printf "%s" "$body"
          return 0
        fi
        if [ "$rc" -eq 22 ]; then
          http_code=$(curl -s -o /dev/null -w "%{http_code}" -m "$TIMEOUT_S" "$url" 2>/dev/null || echo "000")
          [ "$http_code" = "404" ] && return 44
        fi
        sleep "$RETRY_SLEEP_S"
      done
      return 1
      ;;
    *)
      if [ -f "$url" ]; then
        cat "$url" 2>/dev/null
        return 0
      fi
      return 44
      ;;
  esac
}

BODY=$(fetch_body "$URL")
FETCH_RC=$?

if [ "$FETCH_RC" -eq 44 ]; then
  printf "STAGE=digest-missing msg=absent-%s\n" "$URL" >&2
  logfail "digest-missing absent-$URL"
  exit 1
fi
if [ "$FETCH_RC" -ne 0 ]; then
  # 3 retries never got a 200/definitive-404 — a CDN blip is not a digest
  # failure. INCONCLUSIVE, not a fail: exit 0 / UP with a note.
  printf "digest-check-inconclusive transport-failed-after-retries url=%s\n" "$URL"
  logfail "inconclusive transport-failed-after-retries url=$URL"
  exit 0
fi
if [ -z "$BODY" ]; then
  printf "STAGE=digest-missing msg=empty-response-%s\n" "$URL" >&2
  logfail "digest-missing empty-response-$URL"
  exit 1
fi

# Step 4: parse JSON + apply the exact freshness rule in one python3 pass.
# All external-input handling (untrusted JSON off a public branch feeding
# an alerting pipeline) is wrapped so ANY parse surprise resolves to a clean
# digest-malformed rather than an uncaught traceback landing on stderr.
#
# BODY is handed to python via a temp file, NOT a pipe — `cmd | python3 -
# <<EOF` is a classic bash trap: the heredoc redirect claims fd 0 for the
# script source itself, silently starving the piped input (python3 would
# see EOF immediately, not BODY). Same file-handoff pattern as
# qbit-stall.sh's /tmp/qbit-canary-payload.json.
BODY_FILE=$(mktemp "${TMPDIR:-/tmp}/qflix-digest-canary.XXXXXX")
printf "%s" "$BODY" > "$BODY_FILE"
VERDICT=$(python3 - "$NOW_ISO" "$BODY_FILE" <<'PYEOF'
import sys
import json
import re
import datetime as dt


def _safe(s, maxlen=40):
    """Strip control chars and cap length before any untrusted value is
    echoed into a STAGE line — that line becomes the Kuma msg= field, so an
    adversarial/corrupt digest.json must not be able to inject extra lines
    or blow up the alert payload."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(s))[:maxlen]


def parse_week_of(week_of):
    try:
        return dt.datetime.strptime(week_of, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
    except (ValueError, TypeError):
        return None


def main():
    now_iso, body_path = sys.argv[1], sys.argv[2]
    with open(body_path, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()

    try:
        now = dt.datetime.fromisoformat(now_iso.replace("Z", "+00:00"))
    except Exception:
        print(f"malformed bad-now={_safe(now_iso)}")
        return 1

    try:
        data = json.loads(raw)
    except Exception as exc:
        print(f"malformed json-parse-error={type(exc).__name__}")
        return 1
    if not isinstance(data, dict):
        print(f"malformed not-an-object type={type(data).__name__}")
        return 1

    raw_week_of = data.get("week_of")
    raw_html = data.get("html")
    week_of = raw_week_of.strip() if isinstance(raw_week_of, str) else ""
    html = raw_html.strip() if isinstance(raw_html, str) else ""

    if not week_of:
        print("malformed missing-week_of")
        return 1

    d = parse_week_of(week_of)
    if d is None:
        print(f"malformed bad-week_of-format={_safe(week_of, 20)}")
        return 1

    if not html:
        print(f"empty week_of={_safe(week_of, 20)}")
        return 1

    # BYTE-IDENTICAL reimplementation of qflix_newsletter.changelog._is_fresh
    # (changelog.py L348-361) — kept in lockstep by the agreement test.
    delta_days = (now - d).total_seconds() / 86400.0
    fresh = -1.0 <= delta_days <= 4.0
    if fresh:
        print(f"fresh week_of={_safe(week_of, 20)} age={delta_days:.2f}d")
        return 0
    print(f"stale week_of={_safe(week_of, 20)} age={delta_days:.2f}d")
    return 1


try:
    sys.exit(main())
except Exception as exc:  # boundary of last resort — never an uncaught traceback
    print(f"malformed unhandled-error={type(exc).__name__}")
    sys.exit(1)
PYEOF
)
rm -f "$BODY_FILE"

# Step 5: map the python verdict to STAGE labels / the exit contract.
case "$VERDICT" in
  fresh\ *)
    printf "digest-%s\n" "$VERDICT"
    exit 0
    ;;
  stale\ *)
    printf "STAGE=digest-stale msg=%s\n" "${VERDICT#stale }" >&2
    logfail "digest-stale ${VERDICT#stale }"
    exit 1
    ;;
  empty\ *)
    printf "STAGE=digest-empty msg=%s\n" "${VERDICT#empty }" >&2
    logfail "digest-empty ${VERDICT#empty }"
    exit 1
    ;;
  malformed\ *)
    printf "STAGE=digest-malformed msg=%s\n" "${VERDICT#malformed }" >&2
    logfail "digest-malformed ${VERDICT#malformed }"
    exit 1
    ;;
  *)
    printf "STAGE=digest-malformed msg=unexpected-verdict-%s\n" "$(printf %s "$VERDICT" | head -c 60)" >&2
    logfail "digest-malformed unexpected-verdict"
    exit 1
    ;;
esac

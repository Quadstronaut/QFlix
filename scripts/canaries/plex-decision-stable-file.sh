#!/usr/bin/env bash
# plex-decision-stable-file canary: the EARNED half of the REA noise class
# `plex-vanished-file-decision-failure`.
#
# WHY THIS EXISTS
# ---------------
# On 2026-09-10 that noise class was added, silencing Plex's
#
#   ERROR - Failed to get a decision for: <path>
#   ERROR - MDE: video has neither a video stream nor an audio stream
#   ERROR - MDE: no compatible media decisions are available
#
# on the evidence that all 79 occurrences across every retained PMS rotation
# were a file that had been deleted or was being rewritten underneath an
# analysis pass already in flight: 54 gone today, 25 present with an mtime
# LATER than the error, and ZERO for a file that existed before the error and
# was still there unchanged.
#
# An adversarial review made the right objection: that is a HISTORICAL CENSUS,
# not a mechanism. The rule matches on message text, so nothing stops the
# ninety-first occurrence from being a genuinely broken file. Worse, the class
# named scripts/canaries/plex-playback.sh as the monitor that owns "Plex can
# still produce a decision" -- and that canary is hardcoded to the
# `QFlix - Movies` section, while 100% of the suppressed corpus is TV and
# Anime. The cited backstop had ZERO overlap with the population the rule
# silences. That is the same shape as a remedy nothing schedules.
#
# So this canary turns the census into an INVARIANT that is re-checked every
# hour, forever. It re-derives the exact bucket the census found empty:
#
#   file is GONE                      -> benign (retention delete / *arr removal)
#   file EXISTS, mtime  > error time  -> benign (it was rewritten afterwards)
#   file EXISTS, mtime <= error time  -> FINDING. The file was already there,
#                                        unchanged, when MDE could not grade it.
#                                        That is not a race; that is a file a
#                                        member cannot play.
#
# The day that third bucket stops being empty, REA is deliberately silent about
# it and this is the only thing that will say so.
#
# THE SUPPRESSION MUST BE EARNED (the 2026-08-23 tdarr-ghost lesson, applied).
# "The file is absent" and "I could not look" are the same `[ -e ]` answer and
# want OPPOSITE verdicts. Lose +x on a parent, unmount the media tree, or
# migrate the slot, and an absence-only rule would reclassify every path as a
# benign delete and hold this canary GREEN on a library that had vanished --
# strictly worse than the false red it replaced. A path only counts as `gone`
# when the nearest ancestor that still exists is a readable, traversable
# directory INSIDE the media root (see _provably_absent); anything else is
# counted and NAMED as `unreachable`, and enough of them is a cannot-assert.
#
# Validated against the live log 2026-09-12 at WINDOW_H=500: seen=10, gone=10,
# replaced=0, unreachable=0, STABLE=0 -- the census reproduced by the running
# artifact rather than asserted in a comment.
#
# TIMEZONE: PMS logs in UTC, the box runs CEST, and Prowlarr logs box-local --
# three different answers in one fleet, which is exactly how the 2026-08-20
# audit double-counted runs. Verified 2026-09-12: the newest PMS line read
# `Sep 12, 2026 00:24:00` while `date -u` on the box read 00:25:47 and `date`
# read 02:25:47 CEST. Every PMS stamp is therefore parsed with `TZ=UTC date -d`
# -- plain `date -d` would read them as CEST and make every age two hours wrong.
#
# NO ROTATION HANDLING IS NEEDED, and that is measured, not assumed: the live
# `Plex Media Server.log` spanned 2026-09-02 18:33 -> 2026-09-12 00:24 on
# inspection, nearly ten days against a 26h window. If Plex ever starts rotating
# inside the window the freshness probe below still holds, because it reads the
# same file.
#
# WINDOW is 26h against an hourly cadence -- the same 1.5x-of-a-daily-cycle
# grace the rest of the fleet uses. A decision failure is not urgent (the member
# has already hit it); what matters is that it is never silently dropped.
#
# EXECUTION MODEL -- runs LOCALLY on the box, no sshm hop. It reads one file and
# stats paths on the same filesystem. Same pattern as rea-liveness.sh and
# prowlarr-proxy-link-fatal.sh, and it is what lets the hermetic tests drive the
# real artifact against fixture directories.
#
# --- STAGE labels (stderr on failure -> Kuma msg) --------------------------
#   plex-decision-stable-file   >=1 decision failure names a file that EXISTS
#                               and was NOT rewritten afterwards (exit 1)
#   plex-log-missing            the PMS log is not there (exit 2)
#   plex-log-unreadable         it exists but cannot be read (exit 2)
#   plex-log-stale              no PMS line inside the freshness budget, so a
#                               zero count carries no information (exit 2)
#   plex-decision-unreachable   too many paths could not be adjudicated because
#                               their parent directory was unreadable -- the
#                               media tree may be gone (exit 2)
#   plex-canary-bad-config      a numeric override is not a positive integer
#                               (exit 2)
# PASS stdout names every bucket, always, including the zero case:
#   "PASS: plex-decision-stable-file - 0 stable (window=26h seen=N gone=N
#    replaced=N unreachable=N)"
#
# Exit contract: 0 = pass / UP; 1 = fail / DOWN; 2 = cannot assert / DOWN with a
# cannot-assert STAGE. A zero count is never printed as health when the input
# cannot support it.
#
# --- Test/override env vars (hermetic acceptance tests) --------------------
#   PLEX_DECISION_LOG            path to the PMS log
#   PLEX_DECISION_WINDOW_H       lookback hours, default 26
#   PLEX_DECISION_STALE_BUDGET_MIN  freshness budget for the log, default 120
#   PLEX_DECISION_UNREACHABLE_MAX   unreachable paths tolerated, default 2
#   PLEX_DECISION_NOW            "YYYY-MM-DD HH:MM:SS" (UTC) override for now
#   PLEX_DECISION_TRAIL          durable log path
#   PLEX_DECISION_MEDIA_ROOT     the tree whose disappearance means "cannot
#                                look" rather than "all deleted", default ~/media
# All numeric knobs are validated as positive integers before use; a bad value
# is exit 2, never a silently-disabled probe.
set -uo pipefail

LOG="${PLEX_DECISION_LOG:-$HOME/.config/plex/Library/Application Support/Plex Media Server/Logs/Plex Media Server.log}"
WINDOW_H="${PLEX_DECISION_WINDOW_H:-26}"
STALE_MIN="${PLEX_DECISION_STALE_BUDGET_MIN:-120}"
UNREACHABLE_MAX="${PLEX_DECISION_UNREACHABLE_MAX:-2}"
TRAIL="${PLEX_DECISION_TRAIL:-$HOME/.opt/maint/canary-plex-decision-stable-file.log}"
MEDIA_ROOT="${PLEX_DECISION_MEDIA_ROOT:-$HOME/media}"

# "Sep 09, 2026 05:04:52"
TS_RX='^[A-Z][a-z]{2} [0-9]{2}, [0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}'

note() {
  mkdir -p "$(dirname "$TRAIL")" 2>/dev/null
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >> "$TRAIL" 2>/dev/null
}

# Is this path PROVABLY absent, as opposed to merely un-lookable?
#
# The first draft asked only whether the file's immediate parent directory was
# readable, and it was wrong in the single most common benign case: when the
# reaper deletes a whole SERIES, the season directory and the show directory go
# with it, so the parent does not exist either and every genuine retention
# delete scored `unreachable`. Replayed over the live log it called all ten
# occurrences unadjudicable -- a canary that cannot assert is not a canary.
#
# So: climb to the nearest ANCESTOR that still exists. The path is provably
# absent when that ancestor is a readable, traversable directory AND it still
# lies inside the media root -- the root being the thing whose disappearance
# means "I cannot look", not "everything was deleted". An unmounted or
# unreadable media tree therefore reports `unreachable` and the canary says it
# cannot assert, instead of cheerfully reporting an empty library as clean.
_provably_absent() {
  local p="$1" anc
  case "$p" in
    "$MEDIA_ROOT"/*) : ;;
    *) return 1 ;;   # outside the root we own: refuse to guess
  esac
  anc=$(dirname "$p")
  while [ ! -e "$anc" ] && [ "$anc" != "/" ] && [ "$anc" != "." ]; do
    anc=$(dirname "$anc")
  done
  case "$anc" in
    "$MEDIA_ROOT"|"$MEDIA_ROOT"/*) : ;;
    *) return 1 ;;   # climbed out past the root => the root itself is gone
  esac
  [ -d "$anc" ] && [ -r "$anc" ] && [ -x "$anc" ]
}

for _knob in WINDOW_H:PLEX_DECISION_WINDOW_H STALE_MIN:PLEX_DECISION_STALE_BUDGET_MIN; do
  _name="${_knob%%:*}"; _env="${_knob##*:}"; _val="${!_name}"
  case "x$_val" in
    x|x*[!0-9]*)
      printf 'STAGE=plex-canary-bad-config msg=%s=%s-must-be-a-positive-integer-cannot-assert\n' \
        "$_env" "${_val:-EMPTY}" >&2
      note "bad-config $_env=${_val:-EMPTY}"; exit 2 ;;
  esac
  if [ "$_val" -le 0 ]; then
    printf 'STAGE=plex-canary-bad-config msg=%s=%s-must-be-greater-than-zero-cannot-assert\n' \
      "$_env" "$_val" >&2
    note "bad-config $_env=$_val"; exit 2
  fi
done
# UNREACHABLE_MAX may legitimately be 0 (strictest), so it is checked separately.
case "x$UNREACHABLE_MAX" in
  x|x*[!0-9]*)
    printf 'STAGE=plex-canary-bad-config msg=PLEX_DECISION_UNREACHABLE_MAX=%s-must-be-a-non-negative-integer-cannot-assert\n' \
      "${UNREACHABLE_MAX:-EMPTY}" >&2
    note "bad-config PLEX_DECISION_UNREACHABLE_MAX=${UNREACHABLE_MAX:-EMPTY}"; exit 2 ;;
esac

if [ ! -f "$LOG" ]; then
  printf 'STAGE=plex-log-missing msg=no-file-at-%s-cannot-assert\n' "$LOG" >&2
  note "plex-log-missing $LOG"; exit 2
fi
if [ ! -r "$LOG" ]; then
  printf 'STAGE=plex-log-unreadable msg=cannot-read-%s-cannot-assert\n' "$LOG" >&2
  note "plex-log-unreadable $LOG"; exit 2
fi

NOW_OVERRIDE="${PLEX_DECISION_NOW:-}"
if [ -n "$NOW_OVERRIDE" ]; then
  NOW_EPOCH=$(TZ=UTC date -d "$NOW_OVERRIDE" +%s 2>/dev/null || echo 0)
else
  NOW_EPOCH=$(date +%s)
fi
# x0 is load-bearing: the `|| echo 0` fallback above produces a string that is
# all digits, so a digits-only test ACCEPTS a failed parse, and an epoch of 0
# makes every age hugely negative -- which then clamps to 0 and reads as
# perfectly fresh. A bad clock override printed a clean PASS before this existed.
case "x${NOW_EPOCH:-}" in
  x|x*[!0-9]*|x0)
    printf 'STAGE=plex-canary-bad-config msg=bad-now-override=%s\n' "${NOW_OVERRIDE:-EMPTY}" >&2
    note "bad-now-override ${NOW_OVERRIDE:-EMPTY}"; exit 2 ;;
esac
CUTOFF_EPOCH=$(( NOW_EPOCH - WINDOW_H * 3600 ))

# --- Probe 0: is Plex writing at all? --------------------------------------
NEWEST=$(grep -oE "$TS_RX" "$LOG" | tail -1)
if [ -z "$NEWEST" ]; then
  printf 'STAGE=plex-log-unreadable msg=no-timestamped-line-in-%s-cannot-assert\n' "$LOG" >&2
  note "plex-log-unparseable $LOG"; exit 2
fi
NEWEST_EPOCH=$(TZ=UTC date -d "$NEWEST" +%s 2>/dev/null || echo 0)
case "x${NEWEST_EPOCH:-}" in
  x|x*[!0-9]*|x0)
    printf 'STAGE=plex-log-unreadable msg=newest-line-timestamp-unparseable=%s\n' "$NEWEST" >&2
    note "newest-line-unparseable $NEWEST"; exit 2 ;;
esac
AGE_MIN=$(( ( NOW_EPOCH - NEWEST_EPOCH ) / 60 ))
[ "$AGE_MIN" -lt 0 ] && AGE_MIN=0
if [ "$AGE_MIN" -gt "$STALE_MIN" ]; then
  printf 'STAGE=plex-log-stale msg=newest-plex-line-%smin-old-budget=%smin-a-zero-count-would-be-meaningless\n' \
    "$AGE_MIN" "$STALE_MIN" >&2
  note "plex-log-stale age=${AGE_MIN}m budget=${STALE_MIN}m"; exit 2
fi

# --- Probe 1: adjudicate every in-window decision failure -------------------
SEEN=0; GONE=0; REPLACED=0; UNREACHABLE=0; STABLE=0
STABLE_NAMES=""
while IFS= read -r ln; do
  [ -n "$ln" ] || continue
  # "Mon DD, YYYY HH:MM:SS" is exactly 21 characters. The trailing * in the
  # pattern is load-bearing: a bash `case` matches the WHOLE string, so a
  # prefix-shaped pattern with no * silently matches nothing and every line is
  # skipped -- which renders as seen=0 and a clean PASS. Caught in smoke.
  ts="${ln:0:21}"
  case "x$ts" in
    x[A-Z][a-z][a-z]\ [0-9][0-9],\ [0-9][0-9][0-9][0-9]\ [0-9][0-9]:[0-9][0-9]:[0-9][0-9]) : ;;
    *) continue ;;
  esac
  ep=$(TZ=UTC date -d "$ts" +%s 2>/dev/null || echo "")
  case "x$ep" in x|x*[!0-9]*) continue ;; esac
  [ "$ep" -gt "$CUTOFF_EPOCH" ] || continue
  path="${ln#*decision for: }"
  [ -n "$path" ] && [ "$path" != "$ln" ] || continue
  SEEN=$(( SEEN + 1 ))
  if [ -e "$path" ]; then
    mt=$(stat -c %Y "$path" 2>/dev/null || echo "")
    case "x$mt" in
      x|x*[!0-9]*) UNREACHABLE=$(( UNREACHABLE + 1 )); continue ;;
    esac
    if [ "$mt" -gt "$ep" ]; then
      REPLACED=$(( REPLACED + 1 ))
    else
      STABLE=$(( STABLE + 1 ))
      STABLE_NAMES="$STABLE_NAMES $(basename "$path" | tr -cd 'A-Za-z0-9._-' | cut -c1-40)"
    fi
  elif _provably_absent "$path"; then
    GONE=$(( GONE + 1 ))
  else
    # Could not look. NEVER count this as a benign delete.
    UNREACHABLE=$(( UNREACHABLE + 1 ))
  fi
done < <( grep -F 'Failed to get a decision for: ' "$LOG" )

SUMMARY="window=${WINDOW_H}h seen=$SEEN gone=$GONE replaced=$REPLACED unreachable=$UNREACHABLE"

if [ "$UNREACHABLE" -gt "$UNREACHABLE_MAX" ]; then
  printf 'STAGE=plex-decision-unreachable msg=%d-paths-unadjudicable-max=%d-%s-media-tree-may-be-unmounted\n' \
    "$UNREACHABLE" "$UNREACHABLE_MAX" "$SUMMARY" >&2
  note "plex-decision-unreachable $SUMMARY"
  exit 2
fi

if [ "$STABLE" -gt 0 ]; then
  WHO=$(printf '%s' "$STABLE_NAMES" | sed -E 's/^ +//; s/ +$//; s/ +/,/g' | cut -c1-90)
  printf 'STAGE=plex-decision-stable-file msg=%d-decision-failures-on-files-that-EXIST-and-were-not-replaced-%s-files=%s\n' \
    "$STABLE" "$SUMMARY" "$WHO" >&2
  note "plex-decision-stable-file stable=$STABLE $SUMMARY files=$WHO"
  exit 1
fi

printf 'PASS: plex-decision-stable-file - 0 stable (%s age=%smin)\n' "$SUMMARY" "$AGE_MIN"
exit 0

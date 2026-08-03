#!/usr/bin/env bash
# REA liveness canary — the box-side watchdog for a WORKSTATION process.
#
# ============================================================================
# THE ASYMMETRY, AND WHY IT DICTATES THE DESIGN
# ============================================================================
# scripts/local-llm/qflix-rea.ps1 (the Random Error Audit) is the only piece of
# QFlix that does NOT run on the seedbox. It runs on the operator's Windows
# workstation, SSHes IN to collect logs, feeds them to local Ollama models, and
# posts to Discord. It is gitignored (.gitignore:55), so it is not even covered
# by the deploy-drift canary. Nothing has ever watched it.
#
# A watchdog that dies with the thing it watches is worthless. So the ONLY
# question that matters here is: whose liveness does the ALARM depend on?
#
#   subject : qflix-rea.ps1, on the workstation. May be off, asleep, on
#             holiday, crash-looping, or uninstalled.
#   reporter: THIS script, on the box, from its own systemd timer, pushing its
#             own Kuma monitor. The box is up by definition — Kuma runs on it.
#             This timer is itself dead-manned by the timer-liveness canary
#             (manifest/jobs.yaml -> Canary Timer Liveness, every 15 min), so
#             the turtle chain terminates in the one place it legitimately can.
#
# REA's contribution is a FACT ("here is my last terminal audit-log line"),
# never a VERDICT ("I am healthy"). All judgement lives here, on the box, in
# versioned code with tests. That separation is the whole point.
#
# ============================================================================
# THE THREE OPTIONS, AND WHY THE OTHER TWO LOSE
# ============================================================================
# (a) CHOSEN — REA writes a heartbeat record the box can read; the box judges it.
#
# (b) REJECTED — box-side check of "the last REA artifact". There is no such
#     artifact. Verified read-only on the box 2026-08-03:
#       - REA's remote collector does `TMP=$(mktemp -d -t qflix-rea.XXXXXX)`
#         with `trap 'rm -rf "$TMP"' EXIT`, so every trace it creates is
#         deleted before the SSH session closes.
#       - `ls ~/.opt/maint/rea*` matches only `reaper`. Nothing else on the box
#         mentions REA.
#       - The box cannot even observe REA's SSH session: `last` returns
#         "cannot open /var/log/wtmp: Permission denied", `lastlog` returns
#         "Permission denied", and `who` is empty — this is a shared Ultra.cc
#         slot, not a box we own.
#     So (b) is not a different option from (a); it is (a) with the writer left
#     unbuilt, i.e. a monitor whose numerator is structurally zero. That is the
#     exact shape of the prowlarr canary's Probe 2 (`/api/v1/health` is `[]`, so
#     no threshold tuning can ever make it fire). Rejected.
#
# (c) REJECTED — a Kuma push monitor REA pings, with a grace period.
#     Superficially it satisfies the asymmetry rule: Kuma runs on the box, so
#     silence flips the monitor DOWN without REA's cooperation. Two reasons it
#     still loses, and the second is disqualifying.
#
#     c1. It re-imports noise the REA design already rejected on purpose. The
#         push would traverse the workstation's network, so a workstation
#         network fault reds it — and REA's own robustness matrix classifies
#         that as "Workstation network issue; not seedbox concern" (hence the
#         silent `ssh_fail` reason rather than a page).
#
#     c2. IT MAKES REA THE JUDGE OF ITS OWN HEALTH. A push monitor is binary:
#         push = up, silence = down. For it to distinguish "REA ran and did its
#         job" from "REA ran, exited 0, and audited nothing", REA must decide to
#         WITHHOLD the push — i.e. REA must be healthy enough to correctly
#         diagnose itself as unhealthy. That is precisely the failure this
#         canary exists to rule out.
#
#         This is not hypothetical. Measured over REA's entire life, 72 runs
#         2026-05-11 -> 2026-08-03 (%APPDATA%\qflix-rea\audit.log):
#             25  outcome=heartbeat        clean audit
#             18  reason=all_models_noop   RAN, EXITED 0, AUDITED NOTHING
#              9  outcome=dryrun_heartbeat operator dev run, posts nothing
#              8  reason=ollama_down       (this one does page Discord)
#              5  outcome=error_post       findings posted
#              3  outcome=silent           clean, already heartbeated today
#              1  reason=no_secrets
#              1  reason=ssh_fail
#         20 of 72 runs (28%) failed with ZERO notification anywhere. A naive
#         push would have been green through all 20. `all_models_noop` alone is
#         a quarter of REA's history and is documented as deliberately silent.
#         Option (c) cannot see the dominant failure mode. Rejected.
#
#     Third, minor: rule 6. A new push monitor is born BOTH mute and tokenless,
#     and its token would have to live on the WORKSTATION, outside the box-side
#     secret-mode audit. Option (a) also needs a monitor, but it is an ordinary
#     canary monitor pushed by `manitoba-maint canary push` from the box — the
#     already-hardened path, with the token in ~/secrets on the box.
#
# ============================================================================
# THE CONTRACT — one file, two fields, no new vocabulary
# ============================================================================
#   ~/.opt/maint/rea/heartbeat
#
#   mtime   = when REA last REACHED THE BOX. Written by the box's own clock
#             during the SSH fetch REA already performs, so no workstation
#             clock skew and no extra connection.
#   content = ONE line: verbatim the PREVIOUS run's terminal Write-AuditLog
#             line. e.g.
#               2026-08-02T20:07:42-07:00 ok findings=3 models=1/3 duration=422s outcome=error_post
#               2026-07-25T01:17:15-07:00 fail reason=all_models_noop models=3
#
# WHY THE PREVIOUS RUN'S LINE, NOT THIS RUN'S: REA's single SSH hop happens
# BEFORE the model phase, so at fetch time this run has no verdict yet. Writing
# run N-1's line inside the existing heredoc costs zero extra SSH connections
# and covers EVERY outcome — including `ssh_fail` and `tunnel_timeout`, which an
# end-of-run write could never report because by definition the SSH is dead.
# The cost is a one-run lag, which is irrelevant when runs are days apart and
# the age thresholds are in weeks. The two fields are checked independently
# precisely because of that lag: mtime answers "did REA reach me", content
# answers "did REA finish".
#
# This deliberately reuses REA's EXISTING deadman path rather than building
# beside it. The line is what `Write-AuditLog` already composes; the failure
# vocabulary is `$Script:DeadmanReasons`, already mirrored into git at
# manifest/rea-noise-classes.yaml:`deadman_reasons` for detector C-09. No new
# state, no new format, no second policy surface to drift.
#
# ============================================================================
# PREDICATES
# ============================================================================
#   P1 REACHED    mtime age <= MAX_SILENCE_H. Catches: task deleted, script
#                 moved, workstation permanently gone, SSH creds revoked.
#   P2 FINISHED   the recorded line's own timestamp age <= MAX_SILENCE_H.
#                 Catches the wedge P1 cannot see: REA fetches every day (mtime
#                 fresh) but never reaches a terminal verdict, so the content
#                 stamp freezes. A push monitor is green through this.
#   P3 VERDICT    the recorded outcome. `fail reason=<x>` -> RED, same day,
#                 age-independent. This is the predicate that catches the 18
#                 all_models_noop runs, and no cadence assumption is involved.
#   P4 PRESENT    no heartbeat file at all -> exit 2, NEVER green. "Nothing
#                 watches REA" with a green light is strictly worse than
#                 nothing — that is the tdarr-healthcheck class (ran 100% dead
#                 for 68 days while its monitor stayed green).
#
# THRESHOLD, JUSTIFIED FROM MEASURED DATA — NOT A WISH
# ----------------------------------------------------
# REA is triggered ONLOGON (schtasks /SC ONLOGON, StartWhenAvailable=true) on a
# personal workstation. There is no cadence to encode: a machine left running
# for a week fires it once, and a sleep/resume fires it not at all. So the age
# threshold has to clear the operator's real behaviour, which was measured
# rather than guessed. 71 inter-run gaps over 84 days:
#     max      275.3h  (11.5 d)   2026-06-25 -> 2026-07-07
#     2nd      145.3h  ( 6.1 d)
#     3rd      142.8h  ( 6.0 d)
#     p95      101.9h  ( 4.2 d)
# False fires that each candidate would have produced over that history:
#      72h -> 6      168h -> 1      336h -> 0
# DEFAULT 336h (14 d) clears the observed maximum by 60.7h. It is a slow signal
# and is labelled as such: P3 is the fast one, and P3 needs no cadence at all.
#
# ============================================================================
# STAGE LABELS (stderr -> Kuma msg=)
# ============================================================================
#   rea-not-auditing        P3: last recorded run failed (`fail reason=<x>`)
#   rea-unreached           P1: no REA contact within MAX_SILENCE_H
#   rea-verdict-stale       P2: REA still reaches the box but its verdict froze
#   rea-heartbeat-absent    P4: contract file does not exist (writer not wired)
#   rea-heartbeat-unreadable    exists but cannot be stat'd/read
#   rea-heartbeat-empty         exists but contains no non-blank line
#   rea-heartbeat-malformed     content does not parse, or carries an outcome
#                               token this canary does not recognise
#
# EXIT CODES (rule 5 — empty-because-clean must differ from empty-because-broken)
#   0  REA is auditing. Clean, or PASS-WARN for degraded-but-audited states.
#   1  REA is NOT auditing, and the canary is certain of it.
#   2  The canary CANNOT TELL — no contract file, unreadable, or an outcome
#      vocabulary it does not know. Deliberately distinct from 1: "REA is
#      broken" and "my instrument is broken" demand different operator actions.
#      Fails CLOSED — an unrecognised verdict is never treated as healthy.
#   (`manitoba-maint canary push` collapses 1 and 2 to Kuma DOWN; the STAGE
#   label in msg= is what tells them apart on the wire. The exit code is for
#   the operator running it by hand and for the tests.)
#
# ENV OVERRIDES (tests + operator)
#   QFLIX_CANARY_REA_HEARTBEAT      contract file. default ~/.opt/maint/rea/heartbeat
#   QFLIX_CANARY_REA_MAX_SILENCE_H  P1/P2 threshold in hours. default 336
#   QFLIX_CANARY_REA_NOW            epoch seconds override for "now"
#   QFLIX_CANARY_REA_REASON_TABLE   path to rea-noise-classes.yaml (label only)
#
# EXECUTION MODEL — runs LOCALLY on the box, no sshm hop. The subject is a file
# on the box's own filesystem; `sshm` there is just `bash -c` (scripts/lib/ssh.sh)
# and wrapping it would make the canary untestable from a checkout without a
# live SSH session. Same call dash-asset-integrity.sh makes, same reason.
#
# NO DURABLE LOGFILE, on purpose. Kuma keeps every heartbeat message, so the
# PASS/STAGE line below IS the durable record. A per-run logfile here would need
# its own rotation and its own stale-log-watchdog registration — a whole new
# maintenance concern (rule 3) bought for an observability need already met.
#
# HONEST LIMITS
#   1. The WRITER IS NOT SHIPPED BY THIS TRACK. qflix-rea.ps1 is gitignored and
#      workstation-only; this canary is the reader half of a two-part change.
#      Until the operator adds the one-line write to REA's heredoc, this canary
#      exits 2 `rea-heartbeat-absent` every run. That is the truthful state —
#      nothing watches REA — so the timer must be enabled AFTER the writer
#      lands, not before.
#   2. One-run lag on P2/P3, by construction (see the contract above).
#   3. This proves REA RAN AND FINISHED. It does not prove REA's findings were
#      correct, nor that the models reasoned well. `outcome=heartbeat` means
#      "audited, found nothing" — whether that nothing was right is out of scope.
#   4. P1 is slow by necessity (14 d). A REA that dies on a Monday is caught by
#      P3 on its next run and by P1 no later than two weeks out. There is no
#      faster honest bound while the trigger is ONLOGON on a personal machine.
set -uo pipefail

HB="${QFLIX_CANARY_REA_HEARTBEAT:-$HOME/.opt/maint/rea/heartbeat}"
MAX_SILENCE_H="${QFLIX_CANARY_REA_MAX_SILENCE_H:-336}"
NOW="${QFLIX_CANARY_REA_NOW:-$(date -u +%s)}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"

# Skips are ACCUMULATED and always printed, even when the count is zero
# (rule 4: a suppression or skip must be COUNTED and LOGGED, never silent).
SKIPS=()
skip() { SKIPS+=("$1"); }
skip_str() {
  if [ ${#SKIPS[@]} -eq 0 ]; then printf 'skips=0'
  else printf 'skips=%d(%s)' "${#SKIPS[@]}" "$(IFS=,; echo "${SKIPS[*]}")"
  fi
}
# Every exit path routes through these two, so no path can forget the skip
# tally — the tally is the audit trail for rule 4.
die()  { printf 'STAGE=%s msg=%s %s\n' "$1" "$2" "$(skip_str)" >&2; exit "$3"; }
pass() { printf 'PASS%s: rea-liveness — %s %s\n' "$1" "$2" "$(skip_str)"; exit 0; }

# ~-fold $HOME out of any path we print. Guarded on HOME being non-empty
# because `${var//<empty pattern>/~}` in bash inserts the replacement between
# EVERY character, which would turn a Kuma message into confetti.
tilde() { if [ -n "${HOME:-}" ]; then printf '%s' "${1//$HOME/\~}"; else printf '%s' "$1"; fi; }

# --- P4 PRESENT -----------------------------------------------------------
[ -e "$HB" ] || die rea-heartbeat-absent \
  "no-rea-heartbeat-at-$(tilde "$HB"):writer-not-wired-nothing-is-watching-rea" 2
[ -f "$HB" ] || die rea-heartbeat-unreadable "not-a-regular-file:$(tilde "$HB")" 2
[ -r "$HB" ] || die rea-heartbeat-unreadable "not-readable:$(tilde "$HB")" 2

MTIME=$(stat -c %Y "$HB" 2>/dev/null) || MTIME=""
case "$MTIME" in
  ''|*[!0-9]*) die rea-heartbeat-unreadable "stat-returned-no-mtime:$(tilde "$HB")" 2 ;;
esac

# LAST non-blank line, CR stripped. The writer emits one line, but taking the
# last means an accidental append still reads the most recent record rather
# than a fossil. The CR strip is not defensive padding: the producer is
# PowerShell, whose Out-File/Add-Content emit CRLF by default, and the same
# oversight made timer-liveness.sh report all 40 timers as uninstalled on its
# first live run.
LINE=$(tr -d '\r' < "$HB" | grep -v '^[[:space:]]*$' | tail -n 1)
[ -n "$LINE" ] || die rea-heartbeat-empty "file-exists-but-has-no-content-line" 2

# --- parse: "<iso8601> <verdict...>" --------------------------------------
STAMP=${LINE%% *}
REST=${LINE#* }
[ "$STAMP" != "$LINE" ] || die rea-heartbeat-malformed "single-token-line-no-verdict" 2

# `date -d` parses REA's `-Format o`-ish stamps (2026-08-02T20:07:42-07:00).
STAMP_EPOCH=$(date -u -d "$STAMP" +%s 2>/dev/null) || STAMP_EPOCH=""
case "$STAMP_EPOCH" in
  ''|*[!0-9]*) die rea-heartbeat-malformed "unparseable-timestamp:$STAMP" 2 ;;
esac

MAX_S=$(( MAX_SILENCE_H * 3600 ))
REACH_AGE=$(( NOW - MTIME ))
VERDICT_AGE=$(( NOW - STAMP_EPOCH ))
# A future mtime/stamp is clock skew between the box and the workstation, not
# evidence of freshness OR staleness. Clamp to 0 so it reads as "just now"
# rather than wrapping negative and silently defeating the age predicates.
[ "$REACH_AGE"   -lt 0 ] && REACH_AGE=0
[ "$VERDICT_AGE" -lt 0 ] && VERDICT_AGE=0
REACH_H=$(( REACH_AGE / 3600 ))
VERDICT_H=$(( VERDICT_AGE / 3600 ))

# --- P1 REACHED -----------------------------------------------------------
if [ "$REACH_AGE" -gt "$MAX_S" ]; then
  die rea-unreached \
    "rea-has-not-reached-the-box-in-${REACH_H}h>cap=${MAX_SILENCE_H}h:last-verdict=$REST" 1
fi

# --- P3 VERDICT (evaluated before P2: a named failure beats a stale clock) --
# Classification is a closed table on purpose. An outcome token this canary
# does not know is exit 2 (cannot tell), never exit 0 — a watchdog that greens
# on vocabulary it has never seen is not a watchdog.
WARN=""
case "$REST" in
  fail\ reason=*)
    R=${REST#fail reason=}; R=${R%% *}
    # LABEL ONLY — never changes the verdict. The reason table is mirrored from
    # $Script:DeadmanReasons into git so C-09 can enumerate REA's early-return
    # paths without the ps1; reading it here catches vocabulary drift between
    # REA and the manifest. It is NOT deployed to the box today, so its absence
    # is a counted skip, not a failure, and `fail` reds either way.
    TABLE="${QFLIX_CANARY_REA_REASON_TABLE:-}"
    if [ -z "$TABLE" ]; then
      for c in "$ROOT/manifest/rea-noise-classes.yaml" \
               "$HOME/.opt/maint/rea-noise-classes.yaml"; do
        [ -f "$c" ] && { TABLE="$c"; break; }
      done
    fi
    KNOWN="unknown-to-table"
    if [ -n "$TABLE" ] && [ -r "$TABLE" ]; then
      KNOWNLIST=$(python3 - "$TABLE" <<'PY' 2>/dev/null
import sys, yaml
d = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
print(" ".join(str(x) for x in (d.get("deadman_reasons") or [])))
PY
) || KNOWNLIST=""
      KNOWNLIST=$(printf '%s' "$KNOWNLIST" | tr -d '\r')
      if [ -z "$KNOWNLIST" ]; then
        skip "reason-table-unparseable"
      else
        # ollama_down and no_secrets are `fail reason=` values that are
        # deliberately NOT in $Script:DeadmanReasons: ollama_down owns its own
        # Discord-paging dead-man path, and no_secrets predates the list.
        # Naming them here keeps "known vocabulary" honest instead of
        # mislabelling two real reasons as drift.
        for k in $KNOWNLIST ollama_down no_secrets; do
          [ "$k" = "$R" ] && { KNOWN="known"; break; }
        done
      fi
    else
      skip "reason-table-unavailable"
    fi
    die rea-not-auditing \
      "last-rea-run-failed:reason=$R($KNOWN):verdict-age=${VERDICT_H}h:reached=${REACH_H}h-ago" 1
    ;;

  ok\ findings=*)
    O=${REST##*outcome=}; O=${O%% *}
    case "$O" in
      heartbeat|silent|error_post)
        : ;;                                   # a real, completed audit
      discord_post_failed|deadman_post_failed)
        skip "notify-failed:$O"
        WARN="-WARN" ;;                        # audited fine, could not notify
      dryrun_heartbeat|dryrun_error|dryrun_deadman)
        skip "dry-run-not-a-production-audit:$O"
        WARN="-WARN" ;;
      *)
        die rea-heartbeat-malformed "unrecognised-outcome-token:outcome=$O" 2 ;;
    esac
    ;;

  SKIPPED*)
    # Concurrent-run lock skip. Not a verdict at all, so it must not be counted
    # as evidence REA audited anything.
    skip "lock-skip-no-verdict"
    WARN="-WARN" ;;

  suppressed\ n=*)
    # REA's noise-suppressor line, which immediately PRECEDES the terminal line
    # in audit.log. Getting it here means the writer picked the wrong line.
    skip "writer-wrote-suppression-line-not-terminal-line"
    WARN="-WARN" ;;

  *)
    die rea-heartbeat-malformed "unrecognised-verdict-shape:${REST:0:60}" 2 ;;
esac

# --- P2 FINISHED ----------------------------------------------------------
# Reached only when the recorded verdict is not itself a failure. mtime fresh +
# verdict frozen = REA fetches but never finishes.
if [ "$VERDICT_AGE" -gt "$MAX_S" ]; then
  die rea-verdict-stale \
    "rea-reached-${REACH_H}h-ago-but-its-last-verdict-is-${VERDICT_H}h-old>cap=${MAX_SILENCE_H}h" 1
fi

pass "$WARN" "reached=${REACH_H}h-ago verdict=${VERDICT_H}h-old cap=${MAX_SILENCE_H}h [$REST]"

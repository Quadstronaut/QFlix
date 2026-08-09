#!/usr/bin/env bash
# deploy-drift canary: assert that what is RUNNING on the box is what is in git.
#
# WHY THIS EXISTS
# The source audit reads ~/.opt/qflix-src (a git checkout). The box RUNS
# ~/scripts. Nothing compared them, so every "0 findings" was a statement about
# source code that may or may not resemble the code actually executing. Council
# entries 12 and 15 named this; the measurement on 2026-07-31 found it real:
#
#   8 deployed files stale, the oldest by ~3 months, all with git ahead.
#
# The one that mattered: post-import/prune-text-libraries.sh runs DAILY at 04:00
# from crontab, and the deployed copy predated the fix that logs Notifiarr
# delivery failures. On the box it still had `|| true`, so a Notifiarr outage on
# prune day silently dropped the digest with no operator signal. The fix had been
# in git for weeks. That is the exact failure class this canary is here to make
# impossible to sit on: not a crash, not a red monitor, just a repo that
# describes something other than reality.
#
# WHAT IT ASSERTS
#   every file under ~/scripts matching *.py / *.sh
#     -> byte-identical to the same path under scripts/ at origin/master
#
# Direction matters. Files in git but NOT deployed are fine (not everything is
# deployed). Files DEPLOYED but not in git are the interesting direction: either
# a decommission that never finished, or code running with no source of truth.
#
# GENERATED FILES
# Some deployed scripts are legitimately produced ON the box by an installer
# heredoc rather than copied from git (library-rescan-comics.sh is written by
# configure/24-wire-rescan-callbacks.sh and wired into an *arr's extra_scripts).
# Those are declared below. The list is deliberately explicit and short: an
# exemption is how a real drift hides, so each entry names its generator.
#
# Stage labels (stderr -> Kuma msg=):
#   src-missing        the source checkout is absent or not a git repo
#   fetch-failed       could not reach origin; comparison would be against a
#                      possibly-stale ref, so it is reported, never silently passed
#   deploy-drift       >=1 deployed file differs from origin/master
#   deploy-orphan      >=1 deployed file has no counterpart in git
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
SRC=${QFLIX_SRC_DIR:-$HOME/.opt/qflix-src}
DEPLOYED=${QFLIX_DEPLOYED_DIR:-$HOME/scripts}
REF=${QFLIX_DRIFT_REF:-origin/master}

# Deployed files that are GENERATED on the box, each with the generator that
# writes it. Not a convenience list -- anything added here stops being checked.
is_generated() {
  case "$1" in
    post-import/library-rescan-comics.sh) return 0 ;;  # configure/24-wire-rescan-callbacks.sh heredoc
    *) return 1 ;;
  esac
}

[ -d "$SRC/.git" ] || { printf "STAGE=src-missing msg=no-git-checkout-at-%s\n" "$SRC" >&2; exit 1; }

# Refresh the ref we compare against. A stale ref makes every comparison look
# clean, which is the failure this canary exists to prevent -- so a fetch failure
# is REPORTED, not swallowed.
if ! git -C "$SRC" fetch -q origin 2>/dev/null; then
  printf "STAGE=fetch-failed msg=cannot-reach-origin-comparison-would-be-stale\n" >&2
  exit 1
fi

drift=0; orphan=0; match=0; skipped=0; modedrift=0
drift_list=""; orphan_list=""; modedrift_list=""

while IFS= read -r f; do
  rel="${f#$DEPLOYED/}"
  case "$rel" in
    *.venv/*|*site-packages/*|*node_modules/*|*__pycache__/*) continue ;;
  esac
  if is_generated "$rel"; then skipped=$((skipped+1)); continue; fi

  if git -C "$SRC" cat-file -e "$REF:scripts/$rel" 2>/dev/null; then
    a=$(md5sum < "$f" | cut -d" " -f1)
    # NB: piped, never $(git show ...) -- command substitution strips the
    # trailing newline and made all 100 files look like drift on first run.
    b=$(git -C "$SRC" show "$REF:scripts/$rel" | md5sum | cut -d" " -f1)
    if [ "$a" = "$b" ]; then
      match=$((match+1))
    else
      drift=$((drift+1)); drift_list="$drift_list $rel"
    fi
    # THE EXEC BIT IS PART OF THE DEPLOYMENT (added 2026-08-09).
    # A deploy rsynced -a from a checkout whose index carried 100644 and
    # silently stripped +x from 230 deployed scripts. Content still matched,
    # so this canary vouched for a tree where systemd got 203/EXEC on every
    # ExecStart and the per-minute stream-cap cron ran nothing for ~9 hours.
    # Content equality is not deployment equality; git is authoritative for
    # the bit, and a 100755 file that is not executable on disk is drift.
    gmode=$(git -C "$SRC" ls-tree "$REF" -- "scripts/$rel" 2>/dev/null | cut -d" " -f1)
    if [ "$gmode" = "100755" ] && [ ! -x "$f" ]; then
      modedrift=$((modedrift+1)); modedrift_list="$modedrift_list $rel"
    fi
  else
    orphan=$((orphan+1)); orphan_list="$orphan_list $rel"
  fi
done < <(find "$DEPLOYED" -type f \( -name "*.py" -o -name "*.sh" \))

REFSHA=$(git -C "$SRC" rev-parse --short "$REF" 2>/dev/null || echo "?")

if [ "$drift" -gt 0 ]; then
  printf "STAGE=deploy-drift msg=%d-of-%d-deployed-files-differ-from-%s(%s) files=%s\n" \
    "$drift" "$((drift+match))" "$REF" "$REFSHA" "$(echo $drift_list | cut -c1-160)" >&2
  exit 1
fi
if [ "$modedrift" -gt 0 ]; then
  printf "STAGE=deploy-mode-drift msg=%d-deployed-files-lost-the-exec-bit-git-says-755 files=%s\n" \
    "$modedrift" "$(echo $modedrift_list | cut -c1-160)" >&2
  exit 1
fi
if [ "$orphan" -gt 0 ]; then
  printf "STAGE=deploy-orphan msg=%d-deployed-files-absent-from-git files=%s\n" \
    "$orphan" "$(echo $orphan_list | cut -c1-160)" >&2
  exit 1
fi

printf "PASS: deploy-drift - %d deployed files match %s (%s); %d generated skipped\n" \
  "$match" "$REF" "$REFSHA" "$skipped"
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

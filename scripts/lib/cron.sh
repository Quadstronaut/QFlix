#!/usr/bin/env bash
# cron.sh — idempotent, marker-tagged crontab line writer (QFLX-63).
#
# WHY: the box crontab is the third scheduling plane (manifest/jobs.yaml
# `cron:`), and two of its lines (listmonk-sync, arr-housekeeping --unstick)
# had been added BY HAND with no installer writing them, so a slot rebuild or
# the Gold migration would silently drop them. Every installer that owns a cron
# line goes through cron_ensure so the semantics are the same everywhere:
#
#   exact line already present  -> NO-OP. The crontab is not even rewritten, so
#                                  re-running an installer on the live box
#                                  changes nothing (byte-for-byte).
#   line held by a swap         -> NO-OP. 312-native-postgres-install.sh --swap
#                                  comments listmonk writers out under
#                                  "#QFLX-37-HOLD# "; re-adding an active copy
#                                  would release the hold mid-swap.
#   drifted / absent            -> strip every ACTIVE line matching MATCH_ERE
#                                  (plus our own marker), append marker + LINE.
#                                  Comments are never stripped, so a hold or an
#                                  operator note survives.
#
# Guard the resulting STATE, not the exit status: after writing, the exact line
# must be in `crontab -l`, else return 1.
#
# Usage (on the box):        cron_ensure TAG LINE MATCH_ERE
# Usage (from workstation):  cron_ensure_remote TAG LINE MATCH_ERE   (needs ssh.sh)
# MATCH_ERE is passed to awk -v, which expands backslash escapes: write [.]
# rather than \. so the pattern survives unchanged.
# CRONTAB overrides the crontab binary (tests point it at a file-backed stub).

cron_ensure() {
  local tag="$1" line="$2" match="$3" ct="${CRONTAB:-crontab}" cur new
  local marker="# [qflix-cron:${tag}] managed by a scripts/configure installer - do not hand-edit"
  [ -n "$tag" ] && [ -n "$line" ] && [ -n "$match" ] || { echo "cron_ensure: TAG LINE MATCH required" >&2; return 2; }
  # `crontab -l` exits 1 on an empty crontab ("no crontab for user"): that is
  # an empty starting state, not an error.
  cur="$("$ct" -l 2>/dev/null)" || cur=""
  if printf '%s\n' "$cur" | grep -qxF -- "$line"; then
    echo "[=] cron ${tag}: present, unchanged"
    return 0
  fi
  if printf '%s\n' "$cur" | grep -F -- "$line" | grep -qE '^#[A-Za-z0-9_-]*HOLD# '; then
    echo "[=] cron ${tag}: held by a swap, left as-is"
    return 0
  fi
  new="$(
    if [ -n "$cur" ]; then
      printf '%s\n' "$cur" | awk -v re="$match" -v mk="$marker" \
        '$0 == mk { next } $0 !~ /^[[:space:]]*#/ && $0 ~ re { next } { print }'
    fi
    printf '%s\n' "$marker" "$line"
  )"
  printf '%s\n' "$new" | "$ct" - || { echo "cron ${tag}: crontab install failed" >&2; return 1; }
  if ! "$ct" -l 2>/dev/null | grep -qxF -- "$line"; then
    echo "cron ${tag}: line absent after install" >&2
    return 1
  fi
  echo "[+] cron ${tag}: installed"
}

# Ship cron_ensure's own definition to the box and run it there, so the box
# needs no copy of this file and the logic cannot drift between the two sides.
cron_ensure_remote() {
  { declare -f cron_ensure; printf 'cron_ensure %q %q %q\n' "$1" "$2" "$3"; } | sshm 'bash -s'
}

#!/usr/bin/env bash
# qflix-listen-set.sh - record an app's TCP listen set before a UCC->native swap
# (QFLX-20, spec 5.8 / 5.9 step 3). RECORD ONLY: it never starts, stops or
# changes any app.
#
#   qflix-listen-set.sh capture <slug>   record the `ss -tlnH` rows on the app's
#                                        port + its `appctl version` into
#                                        ~/.opt/maint/swap/<slug>/
#   qflix-listen-set.sh capture-all      capture every active (non-dormant) ucc app
#   qflix-listen-set.sh show <slug>      print recorded state + listen set
#   qflix-listen-set.sh set <slug> k=v.. swap_date= soak_until= rollback_window=open|closed
#
# `ss` output is PIPED into the python helper (never passed as argv: on the
# shared slot it lists every tenant's listener and overflows ARG_MAX).
# Overrides (tests): QFLIX_LIB, QFLIX_PYTHON, QFLIX_APPCTL, QFLIX_SS.
set -uo pipefail

LIB="${QFLIX_LIB:-$HOME/scripts/maint/lib}"
PY="${QFLIX_PYTHON:-python3}"
APPCTL="${QFLIX_APPCTL:-$HOME/bin/appctl}"
SS="${QFLIX_SS:-ss}"
SWAPSTATE="$LIB/swapstate.py"

usage() {
  echo "usage: qflix-listen-set.sh capture <slug> | capture-all | show <slug> | set <slug> k=v.." >&2
  exit 64
}

# `appctl version` of a ucc app prints {"data": {"version": "4.0.20"}, "result": true}
ucc_version() {
  local out ver
  out="$("$APPCTL" version "$1" 2>/dev/null)" || return 0
  ver="$(printf '%s' "$out" | sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' | tail -n 1)"
  [ -n "$ver" ] || ver="$(printf '%s' "$out" | tail -n 1 | tr -d '\r')"
  printf '%s' "$ver"
}

capture_one() {
  local slug="$1" ver rows
  ver="$(ucc_version "$slug")"
  # Read ss FIRST and check it: with a bare pipe the python side would still
  # run on empty stdin and record an empty listen set before pipefail reports.
  rows="$("$SS" -tlnH)" || { echo "qflix-listen-set: ss failed; nothing recorded for $slug" >&2; return 1; }
  printf '%s\n' "$rows" | "$PY" "$SWAPSTATE" capture "$slug" --ss-file - ${ver:+--ucc-version "$ver"}
}

[ $# -ge 1 ] || usage
case "$1" in
  capture)
    [ $# -eq 2 ] || usage
    capture_one "$2" ;;
  capture-all)
    slugs="$("$PY" "$SWAPSTATE" ucc-slugs)" || { echo "qflix-listen-set: cannot read manifest" >&2; exit 2; }
    rc=0
    while IFS= read -r s; do
      s="${s%$'\r'}"
      [ -n "$s" ] || continue
      capture_one "$s" || rc=1
    done <<<"$slugs"
    exit $rc ;;
  show)
    [ $# -eq 2 ] || usage
    exec "$PY" "$SWAPSTATE" show "$2" ;;
  set)
    [ $# -ge 3 ] || usage
    shift
    exec "$PY" "$SWAPSTATE" set "$@" ;;
  *) usage ;;
esac

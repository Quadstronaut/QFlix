#!/usr/bin/env bash
# Authenticated GitHub API helper. Source this from configure scripts.
#
# WHY THIS EXISTS
# ---------------
# GitHub's unauthenticated API allows 60 requests/hour PER IP. This is an
# Ultra.cc SHARED seedbox, so that quota is spent by every other tenant on the
# same address — QFlix's own usage is nowhere near it and is rate-limited
# anyway. Measured 2026-08-07: anonymous limit 60, authenticated limit 5000.
# Authenticating does not merely raise the ceiling, it moves us out of the
# shared pool entirely, which is the actual fix.
#
# The token is a fine-grained PAT with **ZERO permissions** and public-repo
# read only. That is deliberate and it is the whole point: authentication alone
# lifts the limit, so no permission needs to be granted to get the benefit. A
# leaked copy grants nothing that anonymous access did not already have, which
# is what makes it safe to keep on a shared box.
#
# FAILS OPEN, ALWAYS. If secrets/github.pat is absent or unreadable the call
# still goes out, unauthenticated, exactly as it did before this file existed.
# An install must never break because an optional rate-limit credential is
# missing — that would trade a slow path for a broken one.
#
# NOT WIRED INTO BAZARR, and it cannot be: Bazarr's updater builds its URL
# inline and calls requests.get() with no headers argument, has no token config
# field or environment variable anywhere in its tree, and bazarr2-sync.timer
# re-matches bazarr2 to bazarr-1's version hourly — so a patched call site is
# reverted by design within the hour. Bazarr's rate-limit line is classified as
# noise in manifest/rea-noise-classes.yaml instead.

# gh_curl <url> [extra curl args...]
#   Adds an Authorization header when a token is available. Otherwise behaves
#   exactly like the bare curl it replaces.
gh_curl() {
  local url="$1"; shift
  local tok=""
  # Prefer the repo-local secret (workstation), fall back to the deployed copy
  # (box). Same two-location convention the rest of scripts/lib uses.
  for _c in "${REPO_ROOT:-.}/secrets/github.pat" "$HOME/secrets/github.pat"; do
    if [ -r "$_c" ]; then
      tok=$(tr -d "[:space:]" < "$_c")
      [ -n "$tok" ] && break
    fi
  done
  if [ -n "$tok" ]; then
    curl -fsSL -H "Authorization: Bearer $tok" \
         -H "X-GitHub-Api-Version: 2022-11-28" "$url" "$@"
  else
    curl -fsSL "$url" "$@"
  fi
}

# gh_latest_tag <owner/repo>
#   The `tag_name` of the newest release, or empty on any failure. Callers keep
#   their own `[ -n "$TAG" ] || die ...` check — this helper deliberately does
#   not decide what a missing tag means for the caller.
gh_latest_tag() {
  gh_curl "https://api.github.com/repos/$1/releases/latest" 2>/dev/null \
    | grep -oP '"tag_name":\s*"\K[^"]+' || true
}

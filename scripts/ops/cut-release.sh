#!/usr/bin/env bash
# Cut a new release tag: master -> release-X.Y.Z.
#
# Pre-flight checks:
#   - working tree clean
#   - branch == master
#   - ≥7 days since last release-* tag (the soak window)
#   - HEAD is reachable from origin/master (i.e. pushed)
#
# Override the soak with `QFLIX_RELEASE_FORCE=1`. Skip the push step with
# `QFLIX_RELEASE_NO_PUSH=1` (for testing or air-gapped cuts).
#
# See docs/release-promotion.md for the model. Single-operator workflow;
# no PR gate.
set -euo pipefail

# Resolve repo root from script location so this works regardless of cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
cd "$REPO_ROOT"

SOAK_DAYS="${QFLIX_RELEASE_SOAK_DAYS:-7}"
BUMP="patch"
for arg in "$@"; do
  case "$arg" in
    --major) BUMP="major" ;;
    --minor) BUMP="minor" ;;
    --patch) BUMP="patch" ;;
    -h|--help)
      cat <<EOF
Usage: $0 [--major|--minor|--patch]

  --patch  (default) X.Y.Z -> X.Y.(Z+1)
  --minor              X.Y.Z -> X.(Y+1).0
  --major              X.Y.Z -> (X+1).0.0

Env:
  QFLIX_RELEASE_FORCE=1       skip the 7-day soak guard
  QFLIX_RELEASE_NO_PUSH=1     don't push the tag to origin
  QFLIX_RELEASE_SOAK_DAYS=N   override soak window (default 7)
EOF
      exit 0 ;;
  esac
done

log() { printf '[cut-release] %s\n' "$*"; }
die() { printf '[cut-release] FATAL: %s\n' "$*" >&2; exit 1; }

# ─── Pre-flight ────────────────────────────────────────────────────────────
CUR_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CUR_BRANCH" != "master" ]; then
  die "must be on master, currently on '$CUR_BRANCH'"
fi

if ! git diff --quiet || ! git diff --cached --quiet; then
  die "working tree is dirty; commit or stash first"
fi

# HEAD must be on origin/master so customer nodes can fetch the tag.
git fetch origin master --quiet
LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse origin/master)
if [ "$LOCAL_HEAD" != "$REMOTE_HEAD" ]; then
  die "HEAD ($LOCAL_HEAD) ≠ origin/master ($REMOTE_HEAD); push first"
fi

LAST_TAG=$(git tag --list 'release-*' --sort=-v:refname | head -n1 || true)
if [ -z "$LAST_TAG" ]; then
  log "no prior release tag; defaulting next to release-0.0.1"
  NEXT="release-0.0.1"
  LAST_TAG_TIME=""
else
  log "last release: $LAST_TAG"
  # Soak guard.
  LAST_TAG_TIME=$(git log -1 --format=%ct "$LAST_TAG")
  NOW=$(date +%s)
  AGE_DAYS=$(( (NOW - LAST_TAG_TIME) / 86400 ))
  log "soak window: $AGE_DAYS day(s) since $LAST_TAG (need >= $SOAK_DAYS)"
  if [ "$AGE_DAYS" -lt "$SOAK_DAYS" ] && [ -z "${QFLIX_RELEASE_FORCE:-}" ]; then
    die "soak window not met ($AGE_DAYS < $SOAK_DAYS days). Wait, or set QFLIX_RELEASE_FORCE=1."
  fi
  # Semver bump.
  STRIPPED="${LAST_TAG#release-}"
  IFS='.' read -r MAJ MIN PAT <<<"$STRIPPED"
  case "$BUMP" in
    major) MAJ=$((MAJ+1)); MIN=0; PAT=0 ;;
    minor) MIN=$((MIN+1)); PAT=0 ;;
    patch) PAT=$((PAT+1)) ;;
  esac
  NEXT="release-${MAJ}.${MIN}.${PAT}"
fi

if git rev-parse "$NEXT" >/dev/null 2>&1; then
  die "tag '$NEXT' already exists; aborting"
fi

# ─── Build tag message ─────────────────────────────────────────────────────
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

{
  echo "$NEXT"
  echo
  if [ -n "$LAST_TAG" ]; then
    COMMIT_COUNT=$(git rev-list --count "${LAST_TAG}..HEAD")
    echo "$COMMIT_COUNT commit(s) since $LAST_TAG."
    echo
    echo "Highlights:"
    # Pull merge-commit titles since the last tag (these are squashed PR
    # subjects on a squash-merge workflow — exactly what we want).
    git log "${LAST_TAG}..HEAD" --merges --format='  - %s' 2>/dev/null || true
    # If no merge commits (linear history), fall back to recent commit
    # subjects.
    if [ -z "$(git log "${LAST_TAG}..HEAD" --merges --format='%H' 2>/dev/null)" ]; then
      git log "${LAST_TAG}..HEAD" --format='  - %s' | head -20
    fi
  else
    echo "Initial release. Commit history:"
    git log --format='  - %s' | head -20
  fi
  echo
  echo "Soak: ${AGE_DAYS:-unknown} day(s) on Manitoba (target ${SOAK_DAYS}+)."
} >"$TMP"

# Show + confirm.
log "── proposed tag message ──"
cat "$TMP"
log "──────────────────────────"
log "next tag will be: $NEXT @ $LOCAL_HEAD"
printf "Proceed? [y/N] "
read -r CONFIRM
case "$CONFIRM" in
  y|Y|yes|YES) ;;
  *) die "aborted by operator" ;;
esac

# ─── Create + push ─────────────────────────────────────────────────────────
git tag -a "$NEXT" -F "$TMP"
log "tagged $NEXT locally"

if [ -n "${QFLIX_RELEASE_NO_PUSH:-}" ]; then
  log "QFLIX_RELEASE_NO_PUSH=1 — skipping push"
else
  git push origin "$NEXT"
  log "pushed $NEXT to origin"
fi

cat <<EOF

[cut-release] done. Next steps:
  1. Add a CHANGELOG.md entry referencing $NEXT (date, highlights, PRs).
  2. Commit + push CHANGELOG.md to master (post-tag updates are fine —
     the tag preserves the snapshot, the CHANGELOG just documents it).
  3. When a customer node arrives, point its install to QFLIX_RELEASE_TAG=$NEXT.

See docs/release-promotion.md for the full pipeline.
EOF

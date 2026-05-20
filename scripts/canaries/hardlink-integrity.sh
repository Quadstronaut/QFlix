#!/usr/bin/env bash
# Hardlink-integrity canary: assert *arr import workflow keeps hardlinks
# alive on the Plex library side.
#
# Why this exists: the *arr → Plex import chain relies on hardlinks so
# qBittorrent can keep seeding while Plex sees the same bytes. A silent
# break (copy-mode flag flipped, filesystem boundary changed, *arr's
# "Use Hard Links" setting drifted) doubles storage on every new grab.
# Nothing surfaces in *arr/Plex logs — only the disk-usage trend would
# show it days later.
#
# The smoke test (step 5) samples 5 Movies files. This canary widens
# the net to the 20 most-recently-modified library files across Movies
# + TV Shows + Anime — the ones most likely to reflect CURRENT import
# behavior. A regression caught here is fresh, not weeks old.
#
# Direction note: we check library-side linkcount (>=2 = hardlinked to a
# qBit seed somewhere), not qBit-side. qBit completed files may show
# linkcount=1 because the seed was removed after import — that is not a
# regression. The regression signal is when the LIBRARY file has
# linkcount=1, meaning *arr did NOT hardlink it on import.
#
# Stage labels (failure messages on stderr → Kuma msg=):
#   STAGE=library-empty       Library has no sampleable files
#   STAGE=hardlink-regression ≥50% of recent imports have linkcount=1
#
# Exits:
#   0 — pass (≥50% of sampled library files have linkcount >= 2)
#   1 — fail (STAGE label on stderr)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
MEDIA_DIR="$HOME/media"

# 20 most-recently-modified video files across the active libraries.
# -mmin sort proxy: we just take a random shuffle of the recent set
# (find lacks -newest, so sort by mtime via stat).
SAMPLES=$(find "$MEDIA_DIR/Movies" "$MEDIA_DIR/TV Shows" "$MEDIA_DIR/Anime" \
  -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.m4v" -o -name "*.avi" \) \
  -size +50M 2>/dev/null \
  -printf "%T@ %p\n" | sort -rn | head -20 | cut -d" " -f2-)

N=0
LINKED=0
ORPHAN=0
while IFS= read -r f; do
  [ -z "$f" ] && continue
  N=$((N+1))
  LINKS=$(stat -c "%h" "$f" 2>/dev/null) || continue
  if [ "${LINKS:-1}" -ge 2 ]; then
    LINKED=$((LINKED+1))
  else
    ORPHAN=$((ORPHAN+1))
  fi
done <<<"$SAMPLES"

if [ "$N" -lt 1 ]; then
  echo "STAGE=library-empty msg=no_samples_in_movies_tv_anime" >&2
  exit 1
fi

echo "N=$N LINKED=$LINKED ORPHAN=$ORPHAN"

# Fail threshold: ≥50% orphans suggests *arr "Use Hard Links" got
# flipped off or a mount changed. <50% is normal churn (some titles
# may have been imported via copy historically).
THRESHOLD=$((N/2))
if [ "$ORPHAN" -gt "$THRESHOLD" ]; then
  echo "STAGE=hardlink-regression msg=orphan=$ORPHAN-linked=$LINKED-of=$N" >&2
  exit 1
fi
') || RC=$?
RC=${RC:-0}
echo "$RES"

STAGE_LINE=$(printf "%s\n" "$RES" | grep "^STAGE=" || true)
if [ -n "$STAGE_LINE" ] || [ "$RC" != "0" ]; then
  [ -n "$STAGE_LINE" ] && echo "$STAGE_LINE" >&2
  exit 1
fi

LINKED=$(printf "%s\n" "$RES" | grep -oE 'LINKED=[0-9]+' | tail -1 | cut -d= -f2)
ORPHAN=$(printf "%s\n" "$RES" | grep -oE 'ORPHAN=[0-9]+' | tail -1 | cut -d= -f2)
N=$(printf "%s\n" "$RES" | grep -oE '^N=[0-9]+' | tail -1 | cut -d= -f2)
echo "PASS: hardlink-integrity — $LINKED/$N recent imports hardlinked, $ORPHAN orphan (sub-threshold)"

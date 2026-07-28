#!/usr/bin/env bash
# Hardlink-integrity canary: detect an *arr import regression by cross-
# referencing qBit-completed torrents against library hardlinks.
#
# History — the OLD design (pre-2026-05-22, kept as
# scripts/canaries/hardlink-integrity.sh.old on the seedbox during the
# transition) sampled the 20 most-recently-modified library files and
# failed if >50% had linkcount=1. Failure mode that retired it: qBit's
# share-ratio cleanup removes seeds faster than the sample window
# refreshes, so the "most recent library files" are almost always the
# ones whose qBit seed is already gone — linkcount=1 not because *arr
# skipped the hardlink, but because qBit deleted the source afterward.
# Fired 20/20 false-positive while *arr was at 100% hardlink coverage
# (verified 2026-05-22 by inode cross-check: 60/60 qBit-completed
# torrents had a library twin).
#
# NEW design (this script): enumerate qBit completed-state torrents,
# stat each content_path's (dev, inode), and check the media library
# has a sibling path with the same (dev, inode) that is NOT under
# ~/downloads. Hardlinked = at least one library path shares the inode.
#
# Classifying the NO-inode-twin case (2026-07-10 fix — see below):
#   COPY-mode regression ("detached") — the qBit file has NO inode twin
#     but the library DOES hold a byte-identical file (exact st_size) at a
#     DIFFERENT inode. That is storage actually doubled: the same bytes
#     stored twice because *arr copied instead of hardlinked. THIS is the
#     signal the canary exists to catch.
#   Orphan seed (benign, EXCLUDED) — the qBit file has no inode twin AND
#     no same-size library file. It is a superseded / different-release /
#     never-imported grab qBit still holds for ratio. It doubles nothing.
#     The library's own copy is a different release at a different byte
#     size, so no size match. Counting these as "detached" was the
#     2026-07-10 false-positive: with qBit down to 3-4 torrents after
#     ratio cleanup, 2 benign orphans out of 3 completed tripped both
#     thresholds — the same tiny-denominator flaw that retired the OLD
#     design, in a new form.
#
# Thresholds (tunable via env on the seedbox systemd unit's
# Environment= lines) — all evaluated over the orphan-EXCLUDED sample:
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED      default 2  (absolute floor — allows a lone copy-import in flight)
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED_PCT  default 5  (percentage floor — covers proportional regressions)
#   QFLIX_CANARY_HARDLINK_MIN_SAMPLE        default 5  (min imported torrents before asserting a regression)
# MAX_DETACHED and MAX_DETACHED_PCT must BOTH be exceeded to fail, and the
# imported-sample count must reach MIN_SAMPLE first — below that the run is
# inconclusive (passes) rather than crying wolf on a handful of torrents.
#
# Stage labels (printed to stderr on failure → Kuma msg=):
#   qbit-up-fail            qBit WebAPI unreachable
#   qbit-auth-fail          qBit auth rejected (password drift?)
#   qbit-no-completed       qBit reports zero completed torrents on disk (suspicious)
#   library-empty           media/ contains zero scanable video files
#   hardlink-regression     detached count and pct both exceed thresholds
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

RES=$(sshm '
set -uo pipefail
QB=http://127.0.0.1:17041
QBIT_USER=$(cat ~/secrets/qbittorrent.user 2>/dev/null || echo quadstronaut)
PWFILE=~/secrets/qbittorrent.password
[ -s "$PWFILE" ] || PWFILE=~/secrets/htpasswd.password

# Tunables — read from env so the systemd unit can tighten/loosen without
# editing the script. Defaulted inline so this also works when invoked
# standalone (no env propagation).
MAX_DETACHED=${QFLIX_CANARY_HARDLINK_MAX_DETACHED:-2}
MAX_DETACHED_PCT=${QFLIX_CANARY_HARDLINK_MAX_DETACHED_PCT:-5}
MIN_SAMPLE=${QFLIX_CANARY_HARDLINK_MIN_SAMPLE:-5}

# Auth — qBit WebUI form-login, same pattern as qbit-stall.sh. Referer
# header is mandatory on Ultra.cc-flavored qBit or it returns 403.
RC=$(curl -s -o /tmp/qfh-login.txt -w "%{http_code}" -m 8 \
  -c /tmp/qfh.cookie \
  -X POST "$QB/api/v2/auth/login" \
  -H "Referer: $QB" \
  --data-urlencode "username=$QBIT_USER" \
  --data-urlencode "password=$(cat "$PWFILE")")
[ "$RC" = "200" ] || { printf "STAGE=qbit-up-fail msg=login-http-%s\n" "$RC" >&2; exit 1; }
grep -q "^Ok\.$" /tmp/qfh-login.txt || { printf "STAGE=qbit-auth-fail msg=login-body-%s\n" "$(head -c 40 /tmp/qfh-login.txt)" >&2; exit 1; }

# Fetch every completed-state torrent. The "completed" filter covers
# uploading / stalledUP / queuedUP / pausedUP / forcedUP / checkingUP —
# anything that finished downloading and still has data on disk.
TFILE=/tmp/qfh-completed.json
curl -sf -m 12 -b /tmp/qfh.cookie "$QB/api/v2/torrents/info?filter=completed" > "$TFILE"
[ -s "$TFILE" ] || { printf "STAGE=qbit-up-fail msg=torrents-info-empty\n" >&2; exit 1; }

export MAX_DETACHED MAX_DETACHED_PCT MIN_SAMPLE
python3 <<"PYEND"
import json, os, sys

with open("/tmp/qfh-completed.json") as f:
    torrents = json.load(f)

if not torrents:
    # Zero completed torrents is NOT inherently suspicious anymore: the torrent
    # janitor (qflix-torrent-janitor, 2026-07-27) reaps completed *arr-untracked
    # seeds once they meet ratio/age, so an empty completed-pool is a legitimate
    # transient steady state (e.g. right after a purge, before new grabs finish),
    # not a nuked qBit data dir. A genuine qBit data-loss / mount-evaporation
    # surfaces via the qBit app monitor + qbit-stall canary. Pass as INCONCLUSIVE
    # rather than crying wolf — same philosophy as the min-sample branch below.
    print("PASS: hardlink-integrity — inconclusive (0 completed torrents; "
          "the torrent janitor may have reaped the seed pool)")
    sys.exit(0)

# Walk the four library roots and build two indexes:
#   library  : (dev, inode) → [paths]   — hardlink twin lookup
#   by_size  : st_size      → [(dev, inode, path)]  — copy-mode lookup, so a
#              qBit file with no inode twin can be told apart from a benign
#              orphan (superseded/different-release seed): a byte-identical
#              library file at a DIFFERENT inode == storage genuinely doubled.
DOWNLOADS = "/home/quadstronaut/downloads"
LIB_ROOTS = ["/home/quadstronaut/media/Movies",
             "/home/quadstronaut/media/TV Shows",
             "/home/quadstronaut/media/Anime",
             "/home/quadstronaut/media/Anime Movies"]
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov")
library = {}
by_size = {}
lib_count = 0
for root in LIB_ROOTS:
    if not os.path.isdir(root):
        continue
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.lower().endswith(VIDEO_EXTS):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except (FileNotFoundError, PermissionError):
                continue
            library.setdefault((st.st_dev, st.st_ino), []).append(p)
            by_size.setdefault(st.st_size, []).append((st.st_dev, st.st_ino, p))
            lib_count += 1

if lib_count == 0:
    sys.stderr.write("STAGE=library-empty msg=no_videos_under_media_root\n")
    sys.exit(1)

# For each qBit completed torrent: locate its largest video file (multi-
# file torrents → content_path is a directory), stat it, and classify:
#   hardlinked — a library path shares its (dev, inode)   [import used hardlink]
#   detached   — no inode twin, but a byte-identical library file exists at a
#                different inode                            [import COPIED = doubled]
#   orphan     — no inode twin AND no same-size library file [benign superseded seed]
# A library twin must be outside ~/downloads — the qBit-side path obviously
# shares the inode with itself.
resolved = 0        # torrents whose content resolved to an on-disk video file
total = 0           # of those: ones with a library presence (hardlinked + detached)
hardlinked = 0
detached = []       # imported-by-COPY = real storage-doubling regression
orphans = 0         # superseded / different-release / unimported seeds (excluded)
for t in torrents:
    cp = t.get("content_path", "")
    if not cp or not os.path.exists(cp):
        continue
    if os.path.isdir(cp):
        target = None
        biggest = 0
        for dirpath, _, files in os.walk(cp):
            for f in files:
                if not f.lower().endswith(VIDEO_EXTS):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    sz = os.path.getsize(p)
                except FileNotFoundError:
                    continue
                if sz > biggest:
                    biggest, target = sz, p
        if not target:
            continue
    else:
        target = cp
    try:
        st = os.stat(target)
    except (FileNotFoundError, PermissionError):
        continue
    resolved += 1
    key = (st.st_dev, st.st_ino)
    twins = [p for p in library.get(key, []) if not p.startswith(DOWNLOADS)]
    if twins:
        hardlinked += 1
        total += 1
        continue
    # No inode twin. A byte-identical library file at a DIFFERENT inode is a
    # copy-mode import (storage doubled). No size match → benign orphan seed
    # (its library counterpart, if any, is a different release at a different
    # byte size), so it is NOT evidence of an import regression — exclude it.
    copies = [p for (d, i, p) in by_size.get(st.st_size, [])
              if (d, i) != key and not p.startswith(DOWNLOADS)]
    if copies:
        detached.append((t.get("category", "?"), t.get("name", "?")[:60]))
        total += 1
    else:
        orphans += 1

if resolved == 0:
    # qBit reported completed torrents but none of them resolve to on-disk
    # files — qBit data dir was nuked, a remote mount evaporated, or
    # someone moved the downloads tree out from under qBit. All suspicious.
    sys.stderr.write("STAGE=qbit-no-completed msg=zero-content-paths-on-disk\n")
    sys.exit(1)

min_sample = int(os.environ.get("MIN_SAMPLE", "5"))
if total < min_sample:
    # Too few torrents currently coexist with their qBit seed to assert a
    # systemic copy-mode regression (a real one shows up as a HIGH copy
    # fraction across MANY imports, not 1-2 in a near-empty pool). Pass as
    # inconclusive rather than firing on small-sample noise — the failure
    # mode that produced the 2026-07-10 false positive.
    print(f"PASS: hardlink-integrity — inconclusive (imported={total} < "
          f"min={min_sample}; hardlinked={hardlinked} detached={len(detached)} "
          f"orphans={orphans} resolved={resolved})")
    sys.exit(0)

detached_n = len(detached)
detached_pct = 100.0 * detached_n / total
max_n = int(os.environ.get("MAX_DETACHED", "2"))
max_pct = float(os.environ.get("MAX_DETACHED_PCT", "5"))

if detached_n >= max_n and detached_pct >= max_pct:
    samples = ";".join(f"{c}:{n}" for c, n in detached[:3])[:80]
    sys.stderr.write(
        f"STAGE=hardlink-regression msg=detached={detached_n}/{total} "
        f"pct={detached_pct:.1f}% orphans={orphans} samples={samples}\n"
    )
    sys.exit(1)

print(f"PASS: hardlink-integrity — imported={total} hardlinked={hardlinked} "
      f"detached={detached_n} ({detached_pct:.1f}%, threshold={max_n}n@{max_pct}%) "
      f"orphans={orphans}")
sys.exit(0)
PYEND
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

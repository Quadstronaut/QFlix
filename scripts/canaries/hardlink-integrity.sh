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
# ~/downloads. Detached = qBit still has the file but no library twin
# — that's the real regression signal, observable only during the
# window when both copies coexist. Hardlinked = at least one library
# path shares the inode.
#
# Threshold (tunable via env on the seedbox systemd unit's
# Environment= lines):
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED      default 2  (absolute floor — allows brief in-flight imports)
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED_PCT  default 5  (percentage floor — covers proportional regressions)
# Both must be exceeded to fail, so a single in-flight import among 60
# completed torrents (~1.7%) is tolerated but 4/60 (6.7%) trips.
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

export MAX_DETACHED MAX_DETACHED_PCT
python3 <<"PYEND"
import json, os, sys

with open("/tmp/qfh-completed.json") as f:
    torrents = json.load(f)

if not torrents:
    sys.stderr.write("STAGE=qbit-no-completed msg=zero-completed-torrents-suspicious\n")
    sys.exit(1)

# Walk the four library roots and build a (dev, inode) → [paths] index.
# Anything outside this index is treated as "no library twin" later on.
LIB_ROOTS = ["/home/quadstronaut/media/Movies",
             "/home/quadstronaut/media/TV Shows",
             "/home/quadstronaut/media/Anime",
             "/home/quadstronaut/media/Anime Movies"]
VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov")
library = {}
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
            lib_count += 1

if lib_count == 0:
    sys.stderr.write("STAGE=library-empty msg=no_videos_under_media_root\n")
    sys.exit(1)

# For each qBit completed torrent: locate its largest video file (multi-
# file torrents → content_path is a directory), stat the inode, look up
# the library index. A "library twin" must be outside ~/downloads — the
# qBit-side path obviously shares the inode with itself.
total = 0
hardlinked = 0
detached = []
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
    total += 1
    key = (st.st_dev, st.st_ino)
    twins = [p for p in library.get(key, [])
             if not p.startswith("/home/quadstronaut/downloads")]
    if twins:
        hardlinked += 1
    else:
        detached.append((t.get("category", "?"), t.get("name", "?")[:60]))

if total == 0:
    # qBit reported completed torrents but none of them resolve to on-disk
    # files — qBit data dir was nuked, a remote mount evaporated, or
    # someone moved the downloads tree out from under qBit. All suspicious.
    sys.stderr.write("STAGE=qbit-no-completed msg=zero-content-paths-on-disk\n")
    sys.exit(1)

detached_n = len(detached)
detached_pct = 100.0 * detached_n / total
max_n = int(os.environ.get("MAX_DETACHED", "2"))
max_pct = float(os.environ.get("MAX_DETACHED_PCT", "5"))

if detached_n >= max_n and detached_pct >= max_pct:
    samples = ";".join(f"{c}:{n}" for c, n in detached[:3])[:80]
    sys.stderr.write(
        f"STAGE=hardlink-regression msg=detached={detached_n}/{total} "
        f"pct={detached_pct:.1f}% samples={samples}\n"
    )
    sys.exit(1)

print(f"PASS: hardlink-integrity — qbit_completed={total} hardlinked={hardlinked} "
      f"detached={detached_n} ({detached_pct:.1f}%, threshold={max_n}n@{max_pct}%)")
sys.exit(0)
PYEND
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

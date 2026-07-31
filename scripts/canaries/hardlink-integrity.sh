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
#   QFLIX_CANARY_HARDLINK_MAX_VACUOUS_DAYS  default 7  (max consecutive days of INCONCLUSIVE runs before failing)
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
#   hardlink-blind          canary has passed without asserting anything for
#                           MAX_VACUOUS_DAYS — the guard stopped guarding
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
# Council finding 8: how long this canary may pass WITHOUT asserting anything
# before the blindness itself becomes the alert. See the vacuity clock below.
MAX_VACUOUS_DAYS=${QFLIX_CANARY_HARDLINK_MAX_VACUOUS_DAYS:-7}

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

export MAX_DETACHED MAX_DETACHED_PCT MIN_SAMPLE MAX_VACUOUS_DAYS
python3 <<"PYEND"
import json, os, sys, time

# --- vacuity clock (council finding 8) --------------------------------------
# This canary has TWO exits that pass without asserting anything: an empty
# completed-pool, and a sample below MIN_SAMPLE. Both are individually correct
# — the torrent janitor legitimately empties the pool, and a 1-2 torrent sample
# cannot evidence a systemic copy-mode regression (that small-denominator trap
# is what retired the previous two designs).
#
# What was missing is that "inconclusive" and "verified good" both exited 0 and
# both rendered as a green monitor. So "this guard has asserted nothing for a
# week" was indistinguishable from "this guard checked and everything is fine".
# With the janitor now reaping the pool toward zero that is a reachable steady
# state, not a hypothetical — the guard can retire itself and stay green.
#
# A vacuous run is therefore timed. Below the threshold it still passes (and now
# SAYS how long it has been blind); past it, the blindness itself is the alert.
# The clock resets on any run that actually reaches the threshold comparison,
# pass or fail — what matters is that the assertion executed.
STATE_DIR = os.path.expanduser("~/.opt/maint/hardlink-integrity")
STATE_PATH = os.path.join(STATE_DIR, "vacuity.json")
MAX_VACUOUS_DAYS = float(os.environ.get("MAX_VACUOUS_DAYS", "7"))


def _read_vacuity():
    """Epoch of the first vacuous run in the current streak, or None."""
    try:
        with open(STATE_PATH) as fh:
            since = int(json.load(fh)["since"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, KeyError) as exc:
        # Unreadable state re-arms the clock rather than crashing. A canary that
        # dies on its own bookkeeping is a false page, which is worse than
        # forgetting one streak.
        sys.stderr.write("note: vacuity state unreadable (%s: %s), re-arming\n"
                         % (type(exc).__name__, exc))
        return None
    now = int(time.time())
    # Clock skew / bad write: a future timestamp would compute a negative age and
    # silently never trip. Treat it as "starts now".
    return now if since > now else since


def _clear_vacuity():
    try:
        os.remove(STATE_PATH)
    except FileNotFoundError:
        pass
    except OSError as exc:
        sys.stderr.write("note: vacuity state clear failed (%s)\n" % exc)


def _vacuous_exit(reason, detail):
    """Pass-but-blind, unless it has been blind too long."""
    since = _read_vacuity()
    now = int(time.time())
    if since is None:
        since = now
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(STATE_PATH, "w") as fh:
                json.dump({"since": since, "reason": reason}, fh)
        except OSError as exc:
            # Cannot persist -> cannot ever trip. Say so out loud instead of
            # degrading silently into the exact blindness this code exists for.
            sys.stderr.write("note: vacuity state write failed (%s) — the "
                             "blind-timer cannot arm\n" % exc)
    days = (now - since) / 86400.0
    if days >= MAX_VACUOUS_DAYS:
        sys.stderr.write(
            "STAGE=hardlink-blind msg=no-assertion-for-%.1fd-max-%.0fd "
            "reason=%s %s\n" % (days, MAX_VACUOUS_DAYS, reason, detail))
        sys.exit(1)
    print("PASS: hardlink-integrity — inconclusive (%s; %s) [blind %.1fd of "
          "%.0fd allowed]" % (reason, detail, days, MAX_VACUOUS_DAYS))
    sys.exit(0)


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
    _vacuous_exit("empty-pool", "0 completed torrents; the torrent janitor may "
                                "have reaped the seed pool")

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
    #
    # Timed on the same clock as the empty-pool exit: a pool that never grows
    # back above min_sample blinds this guard just as completely as an empty one,
    # and is in fact the MORE likely shape now that the janitor reaps to ratio.
    _vacuous_exit(
        "below-min-sample",
        f"imported={total} < min={min_sample}; hardlinked={hardlinked} "
        f"detached={len(detached)} orphans={orphans} resolved={resolved}")

detached_n = len(detached)
detached_pct = 100.0 * detached_n / total
max_n = int(os.environ.get("MAX_DETACHED", "2"))
max_pct = float(os.environ.get("MAX_DETACHED_PCT", "5"))

# Past this point the real assertion RAN, so the blind-streak is over — reset it
# on the failure path too. The clock measures "did this guard evaluate", not
# "did it like what it saw"; a firing canary is the opposite of a blind one.
_clear_vacuity()

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

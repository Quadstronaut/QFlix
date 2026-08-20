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
# 2026-08-06 fix — the STRUCTURAL blindness: this box deliberately runs
# qBit's completed pool near-empty (torrent-janitor purges *arr-untracked
# leftovers daily; qBit's own ratio cleanup removes seeds once they hit
# target ratio). A single run rarely sees MIN_SAMPLE torrents coexisting —
# measured 2026-08 at 2-3 total — so "5 CONCURRENT imported torrents" was not
# an occasional shortfall, it was permanently unreachable from one snapshot.
# The vacuity clock (below) was about to start firing weekly forever, which
# trains the operator to ignore it exactly like the two retired designs did.
#
# Lowering MIN_SAMPLE is not the fix — a tiny denominator is precisely the
# failure mode that retired BOTH prior designs (see above). Instead: stop
# requiring torrents to coexist. Each run records a per-torrent verdict
# (hardlinked/detached, orphans still excluded) keyed by qBit's stable
# info-hash into a rolling ledger at
# ~/.opt/maint/hardlink-integrity/observations.json, and MIN_SAMPLE is
# evaluated against the ACCUMULATED distinct-torrent count across many runs,
# not the concurrent snapshot. The pool holds a handful at once but many
# torrents pass through it across a week, so the sample fills even though the
# snapshot never does. Entries age out via a TTL (see below) so this stays a
# rolling window, not an unbounded ledger a decade-old fixed regression could
# haunt. Full rationale is in the embedded python next to the ledger code.
#
# 2026-08-19 fix — the EXTENSION-LIST false positive, and what a zero-resolved
# run actually means. Kuma monitor #90 went red every 30 min from 20:01Z with
# STAGE=qbit-no-completed while nothing at all was wrong. The completed pool
# held exactly one torrent: a BDMV disc rip (category radarr,
# ~/downloads/qbittorrent/radarr/Bull.Durham.1988 rus/BDMV/STREAM/00000.m2ts,
# 19,307,427,840 bytes — the ONLY video-bearing file in that tree; the rest is
# .bdmv/.mpls/.clpi index metadata). VIDEO_EXTS did not list .m2ts, so the
# multi-file walk found no target, the torrent was skipped, resolved stayed 0,
# and resolved==0 was wired straight to a red.
#
# TWO separate defects, fixed separately:
#
#   1. VIDEO_EXTS was incomplete. .m2ts (Blu-ray/BDMV STREAM payload) and .ts
#      (MPEG transport stream — DVB/HDTV captures arrive this way) are now
#      listed. DO NOT TRIM THIS LIST. It is the ONE constant feeding BOTH the
#      library index — which needs .m2ts to ever find an inode twin for a
#      disc-shaped import — AND torrent target resolution. Dropping an
#      extension here does not skip a check, it silently removes torrents from
#      the sample, which is how a healthy box looked like a storage emergency
#      for six hours.
#
#   2. resolved==0 was the WRONG predicate for the failure it names. That stage
#      is documented as "qBit data dir nuked / mount evaporated / downloads
#      tree moved" — all STORAGE facts. But `resolved` only increments after a
#      content path passes an existence check AND the extension filter, so a
#      perfectly intact pool holding only unrecognised shapes (disc rips,
#      music, ISOs, software grabs) scored identically to a vanished
#      filesystem. The predicate is now split:
#        present  — content_path exists on disk. present==0 against a non-empty
#                   completed list IS the storage signal, and still reds as
#                   STAGE=qbit-no-completed.
#        resolved — present AND a VIDEO_EXTS file was found inside. present>0
#                   with resolved==0 is NOT an incident. The pool is intact;
#                   this run simply contributes no new evidence, exactly like
#                   an empty pool. It falls through to the accumulated-ledger
#                   assertion, so the 2026-08-19 run would have evaluated its 7
#                   stored observations and passed on real evidence rather than
#                   reding on none.
#      Reding an unclassifiable torrent shape is not conservatism; it is the
#      third repeat of this canary's founding mistake — a tiny denominator
#      converted into an alarm. Blindness is already covered, and covered
#      better, by the vacuity clock: it pages only when the guard asserts
#      NOTHING for MAX_VACUOUS_DAYS. Unrecognised-shape runs are counted there
#      (reason=no-classifiable-torrents), not paged here.
#
# Thresholds (tunable via env on the seedbox systemd unit's
# Environment= lines) — all evaluated over the orphan-EXCLUDED, ACCUMULATED
# sample:
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED         default 2   (absolute floor — allows a lone copy-import in flight)
#   QFLIX_CANARY_HARDLINK_MAX_DETACHED_PCT     default 5   (percentage floor — covers proportional regressions)
#   QFLIX_CANARY_HARDLINK_MIN_SAMPLE           default 5   (min DISTINCT torrents observed, accumulated across runs, before asserting a regression)
#   QFLIX_CANARY_HARDLINK_MAX_VACUOUS_DAYS     default 7   (max consecutive days the ACCUMULATOR stays below MIN_SAMPLE before that blindness itself fails)
#   QFLIX_CANARY_HARDLINK_OBSERVATION_TTL_DAYS default 14  (rolling window — ledger entries not refreshed within this many days are pruned)
# MAX_DETACHED and MAX_DETACHED_PCT must BOTH be exceeded to fail, and the
# accumulated distinct-torrent count must reach MIN_SAMPLE first — below that
# the run is inconclusive (passes) rather than crying wolf on a handful of
# torrents.
#
# Stage labels (printed to stderr on failure → Kuma msg=):
#   qbit-up-fail            qBit WebAPI unreachable
#   qbit-auth-fail          qBit auth rejected (password drift?)
#   qbit-no-completed       qBit reports completed torrents but NOT ONE content
#                           path exists on disk (data dir nuked / mount gone).
#                           NOT fired when the paths are present but hold no
#                           recognised video — see the 2026-08-19 note above.
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
# 2026-08-06: rolling window for the per-torrent observation ledger. Entries
# not refreshed within this many days are pruned — see the ledger code below.
OBSERVATION_TTL_DAYS=${QFLIX_CANARY_HARDLINK_OBSERVATION_TTL_DAYS:-14}

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

export MAX_DETACHED MAX_DETACHED_PCT MIN_SAMPLE MAX_VACUOUS_DAYS OBSERVATION_TTL_DAYS
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

# --- rolling observation ledger (2026-08-06) --------------------------------
# STRUCTURAL FIX for the tiny-denominator trap that retired the OLD design and
# nearly retired this one too: this box deliberately runs the qBit completed
# pool near-empty (torrent-janitor purges *arr-untracked leftovers daily;
# qBit own ratio cleanup removes seeds). A single run rarely sees
# MIN_SAMPLE torrents coexisting, so requiring that many CONCURRENT torrents
# made the guard permanently blind — not occasionally, structurally, forever.
#
# The fix: stop requiring torrents to coexist. Accumulate a verdict per
# TORRENT — keyed by the qBit info-hash, stable for that torrent lifetime —
# across MANY RUNS, and assert once enough DISTINCT torrents have been
# observed over time. The pool holds a handful at once but many pass through
# it across a week, so the sample fills even though the snapshot never does.
#
# Orphans (benign, no size match — see the module header) are NEVER recorded
# here, exactly as they are excluded from the count today: they doubled
# nothing, so they carry no evidence either way.
#
# A recorded verdict is TRUE FOR THE MOMENT IT WAS TAKEN, not a live status.
# If qBit later deletes that torrent source (ratio hit, janitor sweep), the
# torrent simply stops appearing in the completed list and this canary stops
# observing it — the stored verdict is left exactly as it was. That is
# CORRECT and INTENTIONAL, not stale data: the claim recorded is "at the
# moment checked, this torrent was, or was not, hardlinked", and that fact
# does not retroactively change because qBit later reaped the seed. Do not "fix"
# this later by deleting entries whose torrent has disappeared from qBit —
# that would silently shrink the sample back toward the exact blindness this
# rewrite exists to escape. The TTL prune below is the only removal path, and
# it is time-based (last_seen age), not presence-based.
#
# A re-observation of the SAME hash overwrites its verdict with whatever this
# run just saw — explicitly, deliberately, not by accident. That is real new
# evidence for that torrent: an operator fix genuinely flips detached ->
# hardlinked and the ledger must be able to show that. last_seen always
# advances on re-observation; first_seen is stamped once and never moved, so
# "how long has this torrent been under observation" stays meaningful even
# after a verdict flip.
OBS_STATE_DIR = os.path.expanduser("~/.opt/maint/hardlink-integrity")
OBS_STATE_PATH = os.path.join(OBS_STATE_DIR, "observations.json")
OBSERVATION_TTL_DAYS = float(os.environ.get("OBSERVATION_TTL_DAYS", "14"))
NOW = int(time.time())


def _load_observations():
    """Best-effort read -> {hash: {verdict, first_seen, last_seen}}. ANY
    failure (missing file, corrupt JSON, wrong shape, a poisoned single
    record) degrades to an EMPTY ledger rather than crashing the canary — a
    monitor that dies on its own state file is worse than one that starts
    blind again; losing the ledger just means re-accumulating from zero, not
    losing the ability to ever assert again."""
    try:
        with open(OBS_STATE_PATH) as fh:
            data = json.load(fh)
        raw = data.get("observations")
        if not isinstance(raw, dict):
            return {}
        clean = {}
        for h, rec in raw.items():
            if (isinstance(rec, dict)
                    and rec.get("verdict") in ("hardlinked", "detached")
                    and isinstance(rec.get("first_seen"), (int, float))
                    and isinstance(rec.get("last_seen"), (int, float))):
                clean[h] = {"verdict": rec["verdict"],
                            "first_seen": rec["first_seen"],
                            "last_seen": rec["last_seen"]}
        return clean
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        sys.stderr.write("note: observation ledger unreadable (%s: %s), "
                         "starting empty\n" % (type(exc).__name__, exc))
        return {}


def _save_observations(obs):
    """Best-effort write. Never raises — a write failure just means this
    run observations do not carry forward, not that the canary fails."""
    try:
        os.makedirs(OBS_STATE_DIR, exist_ok=True)
        with open(OBS_STATE_PATH, "w") as fh:
            json.dump({"observations": obs}, fh)
    except OSError as exc:
        sys.stderr.write("note: observation ledger write failed (%s) — this "
                         "run observations will not carry forward\n" % exc)


def _prune_observations(obs, now, ttl_days):
    """Drop entries not refreshed within ttl_days — a ROLLING window, not a
    permanent memory. This is the only removal path (see the note above): a
    torrent that stops appearing in the qBit completed list simply stops being
    refreshed and ages out on its own schedule, so a long-fixed regression
    cannot haunt the accumulator forever. Returns (kept, pruned_count)."""
    cutoff = now - ttl_days * 86400.0
    kept = {h: rec for h, rec in obs.items() if rec["last_seen"] >= cutoff}
    return kept, len(obs) - len(kept)


def _record_observation(observations, h, verdict, now):
    """Merge ONE classification into the ledger for hash h, in place. Keyed by
    hash so re-observing the same torrent across runs updates one entry
    instead of appending a duplicate -- accumulation counts DISTINCT torrents,
    not DISTINCT observations. verdict is OVERWRITTEN every call -- this is
    the explicit, deliberate re-observation behaviour described above, not a
    bug: a fresh reading is real evidence for this moment. first_seen is
    stamped only the first time a hash is seen and never moved afterward;
    last_seen advances on every call. Returns observations for convenience."""
    rec = observations.get(h, {})
    observations[h] = {
        "verdict": verdict,
        "first_seen": rec.get("first_seen", now),
        "last_seen": now,
    }
    return observations


observations, pruned_n = _prune_observations(_load_observations(), NOW,
                                             OBSERVATION_TTL_DAYS)
# The live pool seen by this run may legitimately be empty (torrent janitor /
# ratio cleanup) — that no longer means "nothing to assert" the way it used
# to. It just means this particular run adds zero NEW observations; the
# accumulator from prior runs is still evaluated below.
pool_empty_this_run = not torrents

# present and resolved are DELIBERATELY different counters (2026-08-19 — see the
# module header). present = the torrent content path exists on disk, which is
# the storage fact qbit-no-completed is actually about. resolved = present AND a
# VIDEO_EXTS file was found inside it, which is merely whether this run had
# anything classifiable to say.
present = 0                   # THIS RUN torrents whose content_path is on disk
resolved = 0                  # THIS RUN torrents that also yielded a video file
run_detached_samples = []     # THIS RUN (category, name) detached samples, for the failure msg
orphans = 0                   # THIS RUN benign orphans (never recorded to the ledger)

if not pool_empty_this_run:
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
    # DO NOT TRIM. One constant, TWO consumers: the library index below and
    # the torrent target walk further down. An extension missing here does not
    # skip a check — it silently drops that torrent from the sample and, before
    # the 2026-08-19 split of present-vs-resolved, converted it into a red.
    # .m2ts was the 2026-08-19 incident: the sole torrent in the pool was a BDMV rip
    # whose lone video file is BDMV/STREAM/00000.m2ts, so resolved=0 and monitor
    # #90 paged every 30 min for six hours on a completely healthy box.
    # .ts (MPEG transport stream) is listed alongside it — same disc/broadcast
    # family, same trap. Largest-file selection makes the TypeScript-source
    # collision harmless: the .ts files in a code torrent are kilobytes.
    VIDEO_EXTS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".m2ts", ".ts")
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
    for t in torrents:
        cp = t.get("content_path", "")
        if not cp or not os.path.exists(cp):
            continue
        present += 1
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
            verdict = "hardlinked"
        else:
            # No inode twin. A byte-identical library file at a DIFFERENT inode
            # is a copy-mode import (storage doubled). No size match → benign
            # orphan seed (its library counterpart, if any, is a different
            # release at a different byte size) — NOT evidence of a regression,
            # and NEVER recorded to the ledger.
            copies = [p for (d, i, p) in by_size.get(st.st_size, [])
                      if (d, i) != key and not p.startswith(DOWNLOADS)]
            if copies:
                verdict = "detached"
                run_detached_samples.append((t.get("category", "?"), t.get("name", "?")[:60]))
            else:
                orphans += 1
                continue

        h = t.get("hash")
        if not h:
            # No stable identity to key the ledger on. Still counted toward
            # `resolved` above (so qbit-no-completed sanity still works), but
            # skip accumulation rather than risk merging distinct torrents
            # under a falsy key.
            continue
        _record_observation(observations, h, verdict, NOW)

    if present == 0:
        # qBit reported completed torrents but NOT ONE content path exists on
        # disk — the qBit data dir was nuked, a remote mount evaporated, or
        # someone moved the downloads tree out from under qBit. All suspicious,
        # and all genuinely storage. This tested `resolved` until 2026-08-19,
        # which folded "every torrent is a shape VIDEO_EXTS does not list" into
        # the same red; see the header.
        sys.stderr.write("STAGE=qbit-no-completed msg=zero-content-paths-on-disk"
                         " torrents=%d\n" % len(torrents))
        sys.exit(1)
    if resolved == 0:
        # Paths are on disk, nothing inside is classifiable (disc rip, music,
        # ISO, software grab). Not an incident — this run just contributes no
        # evidence, exactly like an empty pool. Fall through to the accumulated
        # ledger; the vacuity clock is what notices if this keeps up.
        print("note: %d completed torrent(s) present on disk, none holding a "
              "VIDEO_EXTS file — no new observations this run" % present)

# Persist regardless of whether this run added anything new — a run that only
# pruned stale entries still needs that pruning to stick.
_save_observations(observations)

total = len(observations)
hardlinked_total = sum(1 for r in observations.values() if r["verdict"] == "hardlinked")
detached_total = total - hardlinked_total
min_sample = int(os.environ.get("MIN_SAMPLE", "5"))
span_days = ((NOW - min(r["first_seen"] for r in observations.values())) / 86400.0
             if observations else 0.0)
summary = (f"observed={total}/{min_sample} over {span_days:.0f}d "
          f"(hardlinked={hardlinked_total} detached={detached_total}) "
          f"pruned={pruned_n} this_run=present:{present}/resolved:{resolved}")

if total < min_sample:
    # Too few DISTINCT torrents have been observed, accumulated across runs, to
    # assert a systemic copy-mode regression (a real one shows up as a HIGH
    # copy fraction across MANY imports, not 1-2 in a near-empty pool). Pass as
    # inconclusive rather than firing on small-sample noise — the failure mode
    # that produced the 2026-07-10 false positive, and the same failure mode
    # this rewrite exists to keep from happening at the (now unreachable)
    # concurrent-pool level.
    #
    # Timed on the same clock either way: an accumulator that never grows past
    # min_sample blinds this guard just as completely as an empty one.
    # Three distinct ways to be inconclusive, all timed on the same clock. The
    # third (2026-08-19) is named so the operator can tell "the pool holds only
    # shapes I cannot classify" apart from "the pool is just small" — the two
    # have different remedies (extend VIDEO_EXTS vs wait for imports).
    if pool_empty_this_run:
        reason = "empty-pool"
    elif resolved == 0:
        reason = "no-classifiable-torrents"
    else:
        reason = "below-min-sample"
    _vacuous_exit(reason, summary)

detached_pct = 100.0 * detached_total / total
max_n = int(os.environ.get("MAX_DETACHED", "2"))
max_pct = float(os.environ.get("MAX_DETACHED_PCT", "5"))

# Past this point the real assertion RAN, so the blind-streak is over — reset it
# on the failure path too. The clock measures "did this guard evaluate", not
# "did it like what it saw"; a firing canary is the opposite of a blind one.
_clear_vacuity()

if detached_total >= max_n and detached_pct >= max_pct:
    samples = ";".join(f"{c}:{n}" for c, n in run_detached_samples[:3])[:80]
    sys.stderr.write(
        f"STAGE=hardlink-regression msg=detached={detached_total}/{total} "
        f"pct={detached_pct:.1f}% orphans_this_run={orphans} {summary} "
        f"samples={samples}\n"
    )
    sys.exit(1)

print(f"PASS: hardlink-integrity — {summary} detached_pct={detached_pct:.1f}% "
      f"(threshold={max_n}n@{max_pct}%) orphans_this_run={orphans}")
sys.exit(0)
PYEND
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

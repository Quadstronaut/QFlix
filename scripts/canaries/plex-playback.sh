#!/usr/bin/env bash
# Plex-playback canary: assert Plex can actually PRODUCE TRANSCODED VIDEO, FAST
# ENOUGH TO PLAY, for the worst item in the Movies library -- not merely that its
# transcode APIs answer.
#
# WHY THIS EXISTS
# plex-transcoder.sh curls /identity, /transcode/sessions and /:/prefs for HTTP
# 200 in under 10s. Every one of those is a liveness probe on an HTTP handler.
# None of them decodes a frame, spawns Plex Transcoder, runs EasyAudioEncoder,
# or moves one byte of output. So during the 26-day window in which every movie
# failed to play for a real member, that canary was green on every single tick --
# correctly, because everything it measures was genuinely fine. It is not
# mistuned; it is STRUCTURALLY INCAPABLE of seeing a playback failure, in the
# same way tdarr-healthcheck.sh could not see a 0% health-check rate by watching
# transcodes, and mobile-ux.sh could not see a dead dashboard shell by counting a
# server-rendered marker. The member-visible failure and the measured signal were
# orthogonal, and nothing in the fleet closed the gap.
#
# This canary closes it the only way that works: it performs the failing action.
# Decision, HLS session, real MPEG-TS segment bytes, measured throughput, cleanup.
#
# WHY THROUGHPUT IS A LEG AND NOT A PRINTED NUMBER (added 2026-08-19, review 2)
# The first draft captured segment wall time and printed it, and asserted only
# ">= 32768 bytes inside a 90s cap". That is not a test of the incident it was
# built for. The member-visible fault was "the transcoder never keeps up": bytes
# WERE being produced, just far slower than the 8 seconds of video each segment
# represents, so every client rebuffered forever. A byte floor cannot see that,
# and 32768 could not see much of anything -- at the 720 kbps this probe forces,
# 32768 bytes is 0.36 SECONDS of an 8-second segment, i.e. a transcoder emitting
# 4.5% of the required output still passed.
#
# So two thresholds now derive from the playlist itself rather than from a
# constant:
#   * the byte floor is QP_MIN_PCT percent of kbps * EXTINF / 8, where EXTINF is
#     the segment duration Plex declares in the sub-playlist it just handed us.
#     Measured live on 2026-08-19: EXTINF=8, cap 720 kbps -> 720000 B of headroom,
#     actual 434844 B (60% of cap, because maxVideoBitrate is a ceiling and dark
#     frames encode small). A 25% floor is 180000 B: 2.4x below the real value so
#     content variance cannot flap it, and 5.5x stronger than the old constant.
#   * REALTIME RATIO = EXTINF / segment wall time, as a percent. Measured live:
#     8s of video delivered in 0.903s = 886%. Anything under 100% cannot sustain
#     playback at all; the floor is 80% so a loaded shared slot has room, and the
#     26-day fault would have sat far under it.
# A curl timeout on the segment (code 000) is reported as segment-too-slow rather
# than segment-http for the same reason: 90 seconds to not finish 8 seconds of
# video IS the throughput fault, not a transport error.
#
# WHY THE WORST-CASE ITEM, PICKED FRESHLY EVERY RUN
# The profile that broke is a high-bitrate 1080p H.264 file whose default audio
# track is lossless multichannel (TrueHD / DTS-HD MA) or EAC3. Those are the files
# that force BOTH heavy legs at once: a full software video downscale AND an
# EasyAudioEncoder downmix to 2-channel AAC. A cheap AAC-stereo item exercises
# neither and would have stayed green through the outage exactly like the API
# probe did. The item is chosen at RUN TIME from the live library, ranked by audio
# tier then by bitrate, and NEVER hardcoded: qflix-reaper deletes movies on a
# 60-day add-date rule (memory reaper-maxpct-cap-disabled), so a pinned ratingKey
# becomes a permanent red the day that title ages out -- and a canary that reds
# for a reason unrelated to what it watches gets muted, which is worse than no
# canary at all.
#
# WHY TIER DEGRADATION IS NOW A FAILURE, NOT A FOOTNOTE (review 2)
# The first draft printed the chosen tier and passed regardless. That is a probe
# that can silently turn into the cheap-AAC probe it exists to replace: if the
# reaper ages out every lossless and every lossy-multichannel title, selection
# falls through to tier 3 or 4, the heavy path is never exercised again, and the
# monitor stays green forever on a test of nothing. QP_MIN_TIER (default 2) is
# the floor, checked BEFORE any CPU is burned -- there is no point transcoding an
# item that cannot answer the question. Raising it to 3 or 4 is a deliberate,
# written-down weakening, which is the only kind that should be possible.
#
# WHAT IT ASSERTS, IN ORDER (each leg is a distinct STAGE)
#   1. Plex answers /identity.
#   2. The Movies section exists and is non-empty (and the token is accepted).
#   3. A worst-case item is selectable, at or above QP_MIN_TIER.
#   4. /video/:/transcode/universal/decision returns HTTP 200 AND
#      transcodeDecisionCode=1001 ("Direct play not available; Conversion OK")
#      AND a Part carrying decision="transcode".
#   5. start.m3u8 returns a master playlist naming a session sub-playlist.
#   6. That sub-playlist returns at least one .ts segment URI AND declares an
#      #EXTINF duration for it -- without the duration there is no throughput
#      question to ask, so a playlist that omits it is a failed playlist.
#   7. /transcode/sessions shows OUR session with videoDecision=transcode AND
#      audioDecision=transcode AND error=0 -- i.e. Plex really chose the heavy
#      path and did not quietly direct-stream what we asked it to convert.
#   8. The first segment returns HTTP 200, at least the derived byte floor, and
#      starts with the MPEG-TS sync byte 0x47.
#   9. That segment arrived at or above QP_MIN_RT_PCT percent of realtime. This
#      is the leg with teeth: it is the only one that cannot be satisfied by a
#      healthy HTTP handler sitting in front of a transcoder that has fallen
#      behind, which is precisely what the 26-day outage looked like.
#  10. The session is stopped and CONFIRMED GONE, polled rather than sampled
#      once: on a loaded shared slot Plex reaps at its own pace, and a single
#      read two seconds after the stop turns ordinary reaping latency into a
#      fake leak report. Up to five 2s polls, so ~10s of grace.
#
# WHY THE DECISION LEG IS KEPT EVEN THOUGH LEGS 8-9 ARE STRONGER
# Two reasons, both load-bearing. It separates "Plex refuses to convert this item"
# from "Plex agreed to convert and then produced nothing" -- different faults,
# different operator action. And empirically the decision call must come FIRST:
# start.m3u8 issued without a preceding decision for the same session returned an
# EMPTY body (then 404 on the sub-playlist) during development on 2026-08-19,
# while five consecutive decision-first runs succeeded. The decision call
# registers the resourceSession that start.m3u8 then resolves against -- which is
# also why STARTED=1 is armed BEFORE the decision curl and not after it: the
# decision is the call that creates remote state, so a failure between it and
# start.m3u8 must still run cleanup.
#
# WHY THE SUB-PLAYLIST IS FETCHED RATHER THAN THE SEGMENT URL GUESSED
# Also measured on 2026-08-19: requesting session/<id>/base/00000.ts directly --
# without first GETting session/<id>/base/index.m3u8 -- returns 404 with an
# 85-byte HTML error body. Guessing the segment path produces a canary that is red
# for a reason that has nothing to do with playback. Follow the playlist Plex
# hands back, exactly as a client does. That GET is now doing double duty: it is
# also where the #EXTINF that scales both thresholds comes from.
#
# WHY ITS OWN MODULE, TIMER AND MONITOR
# Operator design law (memory qflix-compartmentalize-for-migration): a distinct
# concern gets its own module, its own timer and its own Kuma check, so it stays
# independently tunable and swappable across a server migration. This probe is
# heavier than the API canary (it burns real CPU), wants a slower cadence, and
# fails for entirely different reasons -- folding it into plex-transcoder.sh would
# couple a 10-minute liveness probe to a 30-minute load test and leave the
# operator one monitor that means two things.
#
# WHAT IT COSTS, AND WHY THAT IS ACCEPTABLE
# One ~8-second HLS segment transcoded at 640x360 / 720 kbps, measured at 0.9-2.0s
# wall time for ~435 KB of output. The low target bitrate is deliberate: it is
# what forces the full downscale + downmix while keeping the burn trivial on a
# shared slot with a 2000-task ulimit (memory seedbox-thread-cap-gomaxprocs). The
# probe never appears in /status/sessions -- verified 2026-08-19, size=0
# throughout a live probe -- so Tautulli records no play, no member-visible
# activity is created, and the operator privacy principle (memory
# qflix-privacy-no-member-activity) is untouched.
#
# MAINTENANCE WINDOW
# Suppressed during the Monday 11:00-15:00 UTC window. `manitoba-maint canary
# push` already suppresses on the window LOCK (lib/cli.py), but that is a bare
# lockfile-existence check; this adds the independent wall-clock leg the same way
# dash-asset-integrity.sh does, so a window whose lock failed to write still does
# not get a false red from a Plex that is being restarted on purpose. Reported as
# a named SKIP, never as a silent pass.
#
# Stage labels (stderr -> Kuma msg=):
#   STAGE=plex-secret-missing     ~/secrets/plex.{host,port,token} unreadable
#   STAGE=plex-up-fail            /identity non-200. Playback IS broken; the Plex
#                                 app monitor reds too and that is correct -- this
#                                 canary must never report green when it could not
#                                 assert (same choice plex-transcoder.sh makes)
#   STAGE=plex-auth-fail          the library API answered 401/403: the token in
#                                 ~/secrets/plex.token is expired, revoked or
#                                 wrong. Its own label because an expired token
#                                 previously surfaced as item-select-fail
#                                 "sections-unreadable-HTTPError", which reads as
#                                 a broken library and sends the operator to the
#                                 wrong place entirely
#   STAGE=section-missing         no movie section titled $SECTION_TITLE
#   STAGE=library-empty           the Movies section holds zero items: nothing to
#                                 play, which is a member-visible outage, not an
#                                 inconclusive skip
#   STAGE=item-select-fail        listing unreadable / no item carries a usable
#                                 ratingKey
#   STAGE=tier-below-minimum      the best item in the library is weaker than
#                                 QP_MIN_TIER, so the heavy path cannot be
#                                 exercised and a pass would mean nothing
#   STAGE=decision-http           decision endpoint non-200. A malformed probe
#                                 also lands here -- 400s were reproduced on
#                                 2026-08-19 by dropping the client-profile
#                                 parameters -- so this label means "our request
#                                 or their handler", never "the item is bad"
#   STAGE=decision-refused        200 but transcodeDecisionCode != 1001
#   STAGE=decision-not-transcode  200/1001 but no Part decision=transcode
#   STAGE=hls-start-fail          start.m3u8 non-200 or named no sub-playlist
#   STAGE=hls-playlist-fail       sub-playlist non-200, listed no .ts segment, or
#                                 declared no #EXTINF duration for it
#   STAGE=session-missing         /transcode/sessions does not know our session
#   STAGE=session-not-transcoding session exists but did not choose transcode for
#                                 video AND audio: a silent direct-stream means
#                                 the heavy path was never exercised
#   STAGE=session-error           Plex flagged the session error=1
#   STAGE=segment-http            first segment non-200
#   STAGE=segment-empty           segment smaller than the derived byte floor:
#                                 the transcoder produced nothing usable
#   STAGE=segment-not-mpegts      body does not start with 0x47: not a TS stream
#   STAGE=segment-too-slow        the segment arrived, but slower than
#                                 QP_MIN_RT_PCT percent of realtime (or not at all
#                                 inside the 90s cap). THE incident label: bytes
#                                 are being produced and playback still fails
#   STAGE=session-leak            probe passed but the session survived stop after
#                                 ~10s of polling. A canary that leaks a transcode
#                                 every 30 minutes on a shared slot becomes the
#                                 outage it watches
#
# Exits:
#   0 - a real transcoded segment was produced at speed (or a named SKIP)
#   2 - any stage above failed
#
# Env overrides (they exist so the branches are reachable in a test, not so the
# thresholds can be tuned by guess):
#   QFLIX_CANARY_PLAYBACK_SECTION       movie section title, default "QFlix - Movies"
#   QFLIX_CANARY_PLAYBACK_MIN_BYTES     ABSOLUTE byte floor, default 32768. The
#                                       effective floor is max(this, the derived
#                                       percent-of-bitrate one)
#   QFLIX_CANARY_PLAYBACK_MIN_BYTES_PCT percent of kbps*EXTINF/8 required, def 25
#   QFLIX_CANARY_PLAYBACK_MIN_RT_PCT    minimum realtime percent, default 80
#   QFLIX_CANARY_PLAYBACK_MIN_TIER      worst acceptable audio tier, default 2
#   QFLIX_CANARY_PLAYBACK_BITRATE       target kbps forced on the transcode, def 720
#   QFLIX_CANARY_PLAYBACK_RES           target resolution, default 640x360
#   QFLIX_CANARY_PLAYBACK_FORCE_WINDOW  1 = force in-window, 0 = force out
#   QFLIX_CANARY_PLAYBACK_TOKEN         test-only: use this Plex token instead of
#                                       ~/secrets/plex.token, so the 401 branch is
#                                       reachable without touching the secret
#   QFLIX_CANARY_PLAYBACK_SKIP_STOP     test-only: 1 = do not issue the stop, so
#                                       the leak poll is reachable. The EXIT trap
#                                       still cleans the session up
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

SECTION_TITLE=${QFLIX_CANARY_PLAYBACK_SECTION:-QFlix - Movies}
MIN_SEG_BYTES=${QFLIX_CANARY_PLAYBACK_MIN_BYTES:-32768}
MIN_BYTES_PCT=${QFLIX_CANARY_PLAYBACK_MIN_BYTES_PCT:-25}
MIN_RT_PCT=${QFLIX_CANARY_PLAYBACK_MIN_RT_PCT:-80}
MIN_TIER=${QFLIX_CANARY_PLAYBACK_MIN_TIER:-2}
TARGET_KBPS=${QFLIX_CANARY_PLAYBACK_BITRATE:-720}
TARGET_RES=${QFLIX_CANARY_PLAYBACK_RES:-640x360}
FORCE_WINDOW=${QFLIX_CANARY_PLAYBACK_FORCE_WINDOW:-}
TOKEN_OVERRIDE=${QFLIX_CANARY_PLAYBACK_TOKEN:-}
SKIP_STOP=${QFLIX_CANARY_PLAYBACK_SKIP_STOP:-}

# Config is interpolated in the double-quoted half; the probe body is single
# quoted so its regexes need no backslash gymnastics. The embedded python heredoc
# delimiter is QUOTED (<<"PYEOF") so the remote shell performs no expansion inside
# the python source -- unlike tdarr-healthcheck.sh, which uses an unquoted one and
# has to keep $ and backticks out of its python body by hand.
#
# This two-part shape (double-quoted prelude, single-quoted body) is covered by
# tests/unit/test_canary_sshm_quoting.py, which was extended on 2026-08-19 to
# recognise it -- before that the extractor matched only a bare `sshm '` and this
# canary escaped the shipped-string parse gate entirely.
RES=$(sshm "
set -uo pipefail
export QP_SECTION='${SECTION_TITLE}'
export QP_MIN_BYTES='${MIN_SEG_BYTES}'
export QP_MIN_PCT='${MIN_BYTES_PCT}'
export QP_MIN_RT_PCT='${MIN_RT_PCT}'
export QP_MIN_TIER='${MIN_TIER}'
export QP_KBPS='${TARGET_KBPS}'
export QP_RES='${TARGET_RES}'
export QP_FORCE_WINDOW='${FORCE_WINDOW}'
export QP_TOKEN='${TOKEN_OVERRIDE}'
export QP_SKIP_STOP='${SKIP_STOP}'
"'
fail() { printf "STAGE=%s msg=%s\n" "$1" "$2" >&2; exit 2; }

# ---- maintenance window ---------------------------------------------------
# Two OR-ed legs, mirroring dash-asset-integrity.sh: the UTC wall clock (which
# depends on nothing having been written correctly) and a LIVE window lock whose
# owning pid is still alive. A leaked lock whose owner is gone must NOT suppress
# this canary forever -- that is how a probe goes quiet without anyone deciding.
in_window() {
  [ "$QP_FORCE_WINDOW" = "1" ] && { echo "forced-on"; return 0; }
  [ "$QP_FORCE_WINDOW" = "0" ] && return 1
  DOW=$(date -u +%u); HOUR=$(date -u +%H); HOUR=${HOUR#0}
  if [ "$DOW" = "1" ] && [ "${HOUR:-0}" -ge 11 ] && [ "${HOUR:-0}" -lt 15 ]; then
    echo "wallclock-mon-1100-1500-utc"; return 0
  fi
  LOCK=${MANITOBA_STATE_DIR:-$HOME/.opt/maint}/lock
  if [ -f "$LOCK" ]; then
    PID=$(head -1 "$LOCK" 2>/dev/null | tr -dc 0-9)
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
      echo "window-lock-held-pid-$PID"; return 0
    fi
  fi
  return 1
}
WHY=$(in_window) && {
  echo "PASS: plex-playback - SKIP: maintenance window ($WHY) - Plex may be stopped on purpose"
  exit 0
}

# ---- secrets --------------------------------------------------------------
PLEX_HOST=$(cat ~/secrets/plex.host 2>/dev/null)
PLEX_PORT=$(cat ~/secrets/plex.port 2>/dev/null)
TOKEN=$(cat ~/secrets/plex.token 2>/dev/null)
[ -n "$PLEX_HOST" ] && [ -n "$PLEX_PORT" ] && [ -n "$TOKEN" ] \
  || fail plex-secret-missing "cannot-read-secrets-plex-host-port-token"
# Test hook only. The real secret still has to be readable above, so this can
# never turn a box with missing secrets into a green one.
[ -n "$QP_TOKEN" ] && TOKEN=$QP_TOKEN
BASE="http://${PLEX_HOST}:${PLEX_PORT}"
UNI="${BASE}/video/:/transcode/universal"

# ---- leg 1: server up -----------------------------------------------------
ID_CODE=$(curl -sk -m 10 -o /dev/null -w "%{http_code}" "${BASE}/identity")
[ "$ID_CODE" = "200" ] || fail plex-up-fail "identity=$ID_CODE-cannot-assert-playback"

# ---- legs 2+3: pick the worst-case item -----------------------------------
# Selection is client-side over the full listing, deliberately. Plex server-side
# filters were measured on 2026-08-19 and are NOT discriminating enough to trust:
# audioCodec=aac returned all 46 movies and audioChannels=8 returned all 46, while
# audioCodec=truehd returned 10. A selector that silently degrades to "the
# highest-bitrate stereo AC3 file" would reintroduce the exact blind spot this
# canary exists to remove, so the ranking is done here where it can be read.
PICK=$(python3 - <<"PYEOF"
import json, os, sys, urllib.error, urllib.parse, urllib.request

host = open(os.path.expanduser("~/secrets/plex.host")).read().strip()
port = open(os.path.expanduser("~/secrets/plex.port")).read().strip()
token = (os.environ.get("QP_TOKEN")
         or open(os.path.expanduser("~/secrets/plex.token")).read().strip())
base = "http://%s:%s" % (host, port)
want = os.environ.get("QP_SECTION") or "QFlix - Movies"

def fail(stage, msg):
    sys.stderr.write("STAGE=%s msg=%s\n" % (stage, msg))
    sys.exit(2)

def get(path):
    req = urllib.request.Request(base + path,
                                 headers={"Accept": "application/json",
                                          "X-Plex-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        # An expired or revoked token is NOT an unreadable library. It gets its
        # own stage so the operator is sent to plex.token instead of to a library
        # that is perfectly fine -- before review 2 this surfaced as
        # item-select-fail "sections-unreadable-HTTPError".
        if exc.code in (401, 403):
            fail("plex-auth-fail",
                 "plex-rejected-the-token-http-%d-on-%s-rotate-secrets-plex-token"
                 % (exc.code, path.split("?")[0]))
        raise

try:
    secs = get("/library/sections")["MediaContainer"].get("Directory", [])
except Exception as exc:
    fail("item-select-fail", "sections-unreadable-" + type(exc).__name__)

sec = None
for d in secs:
    if d.get("type") == "movie" and d.get("title") == want:
        sec = d.get("key")
        break
if sec is None:
    fail("section-missing", "no-movie-section-titled-" + want.replace(" ", "-"))

try:
    doc = get("/library/sections/%s/all?%s" % (
        sec, urllib.parse.urlencode({"X-Plex-Container-Start": 0,
                                     "X-Plex-Container-Size": 1000})))
except Exception as exc:
    fail("item-select-fail", "listing-unreadable-" + type(exc).__name__)

mc = doc.get("MediaContainer", {})
items = mc.get("Metadata") or []
if not items:
    fail("library-empty", "section-%s-holds-zero-items-nothing-to-play" % sec)

# Audio tiers, worst-case first. Tier 1 is the profile that broke: lossless
# multichannel, which forces an EasyAudioEncoder downmix on top of the video
# downscale. Tier 2 is lossy multichannel -- still a downmix, one less decoder.
# Tier 3 is any non-AAC. Tier 4 is whatever is biggest, and says so loudly.
# The leading digit is LOAD-BEARING: the shell reads it back and enforces
# QP_MIN_TIER against it, so renaming a tier without keeping the digit turns the
# floor off silently.
TIERS = [
    ("1-lossless-multichannel", ("truehd", "dca-ma", "dts-hd", "dtshd")),
    ("2-lossy-multichannel", ("eac3", "dca", "dts", "ac3")),
]

def rows():
    for m in items:
        rk = m.get("ratingKey")
        if not rk:
            continue
        for md in m.get("Media", []):
            yield (int(md.get("bitrate") or 0),
                   str(md.get("audioCodec") or "").lower(),
                   int(md.get("audioChannels") or 0),
                   str(md.get("videoCodec") or "?"),
                   int(md.get("height") or 0),
                   str(rk))

allrows = sorted(rows(), reverse=True)
if not allrows:
    fail("item-select-fail", "no-item-carries-both-a-ratingkey-and-media")

chosen = None
tier = None
for name, codecs in TIERS:
    for r in allrows:
        if r[1] in codecs and r[2] >= 6:
            chosen, tier = r, name
            break
    if chosen:
        break
if chosen is None:
    for r in allrows:
        if r[1] != "aac" and r[2] >= 2:
            chosen, tier = r, "3-nonaac-DEGRADED"
            break
if chosen is None:
    chosen, tier = allrows[0], "4-any-DEGRADED-no-nonaac-title-in-library"

br, ac, ch, vc, ht, rk = chosen
print("SEC=%s" % sec)
print("TOTAL=%s" % (mc.get("totalSize") or len(items)))
print("RK=%s" % rk)
print("TIER=%s" % tier)
print("PROFILE=%dkbps-%s-%dch-%s-%dp" % (br, ac, ch, vc, ht))
PYEOF
) || exit 2

RK=$(printf "%s\n" "$PICK" | sed -n "s/^RK=//p")
TIER=$(printf "%s\n" "$PICK" | sed -n "s/^TIER=//p")
PROFILE=$(printf "%s\n" "$PICK" | sed -n "s/^PROFILE=//p")
TOTAL=$(printf "%s\n" "$PICK" | sed -n "s/^TOTAL=//p")
[ -n "$RK" ] || fail item-select-fail "selector-returned-no-ratingkey"

# ---- leg 3b: the chosen item must be worth probing ------------------------
# Checked BEFORE the transcode starts: burning CPU on an item that cannot answer
# the question buys nothing, and a green on a DEGRADED tier is a green on the
# cheap-AAC probe this canary was built to replace.
TIER_N=$(printf "%s" "$TIER" | cut -d- -f1)
[ -n "$TIER_N" ] || TIER_N=9
case "$TIER_N" in
  *[!0-9]*) TIER_N=9 ;;
esac
[ "$TIER_N" -le "$QP_MIN_TIER" ] \
  || fail tier-below-minimum "tier=$TIER-numeric=$TIER_N-worse-than-min=$QP_MIN_TIER-library-holds-no-worst-case-title-probe-would-silently-become-the-cheap-aac-probe-it-replaced"

# ---- probe parameters -----------------------------------------------------
# A full Plex-Web client-profile parameter set. This is not decoration: dropping
# it was reproduced on 2026-08-19 as a bare HTTP 400 from the decision endpoint,
# which is what an earlier malformed probe misread as "the item is bad".
# directPlay / directStream / directStreamAudio are all 0 and the target is
# deliberately small, which is what forces the FULL software path -- video
# downscale plus audio downmix -- rather than a cheap remux.
SESS="qflix-playback-canary-$$-$(date -u +%s)"
CID="qflix-playback-canary"
Q="hasMDE=1&path=%2Flibrary%2Fmetadata%2F${RK}&mediaIndex=0&partIndex=0"
Q="$Q&protocol=hls&fastSeek=1&offset=0"
Q="$Q&directPlay=0&directStream=0&directStreamAudio=0"
Q="$Q&subtitles=none&subtitleSize=100&audioBoost=100&copyts=1"
Q="$Q&location=lan&addDebugOverlay=0&autoAdjustQuality=0&mediaBufferSize=102400"
Q="$Q&session=${SESS}&Accept-Language=en"
Q="$Q&videoResolution=${QP_RES}&maxVideoBitrate=${QP_KBPS}&videoQuality=30"
Q="$Q&X-Plex-Session-Identifier=${SESS}&X-Plex-Incomplete-Segments=1"
Q="$Q&X-Plex-Product=Plex%20Web&X-Plex-Version=4.145.1"
Q="$Q&X-Plex-Client-Identifier=${CID}"
Q="$Q&X-Plex-Platform=Chrome&X-Plex-Platform-Version=139.0"
Q="$Q&X-Plex-Features=external-media%2Cindirect-media%2Chub-style-list"
Q="$Q&X-Plex-Model=standalone&X-Plex-Device=Linux&X-Plex-Device-Name=Chrome"
Q="$Q&X-Plex-Device-Screen-Resolution=1920x1080%2C1920x1080&X-Plex-Language=en"

# Cleanup runs on EVERY exit path, including the failing ones.
STARTED=0
cleanup() {
  [ "$STARTED" = "1" ] || return 0
  curl -sk -m 15 -o /dev/null -H "X-Plex-Token: $TOKEN" \
    "${UNI}/stop?session=${SESS}&X-Plex-Client-Identifier=${CID}" || true
}
trap cleanup EXIT

# ---- leg 4: transcode decision -------------------------------------------
# STARTED is armed HERE, not before start.m3u8. /decision is the call that
# REGISTERS the resourceSession -- the header says so and the sequencing was
# measured on 2026-08-19 -- so a decision-* failure has already created remote
# state and must run cleanup. Arming it one leg later (the shape shipped in
# review 1) meant every decision-refused / decision-not-transcode exit skipped
# the stop and left the session behind.
STARTED=1
DEC=$(curl -sk -m 25 -w "\nHTTPCODE=%{http_code}" -H "X-Plex-Token: $TOKEN" "${UNI}/decision?$Q")
DEC_CODE=$(printf "%s" "$DEC" | sed -n "s/.*HTTPCODE=//p" | tail -1)
[ "$DEC_CODE" = "200" ] || fail decision-http "decision-http=$DEC_CODE-rk=$RK-our-request-or-their-handler-not-the-item"
TDC=$(printf "%s" "$DEC" | grep -o "transcodeDecisionCode=\"[0-9]*\"" | head -1 | tr -dc 0-9)
if [ "$TDC" != "1001" ]; then
  TXT=$(printf "%s" "$DEC" | grep -o "generalDecisionText=\"[^\"]*\"" | head -1 | tr " " "-")
  fail decision-refused "transcodeDecisionCode=${TDC:-none}-want-1001-rk=$RK-$TXT"
fi
printf "%s" "$DEC" | grep -q "decision=\"transcode\"" \
  || fail decision-not-transcode "no-part-decision-transcode-rk=$RK-plex-agreed-then-declined-the-work"

# ---- leg 5: HLS master playlist ------------------------------------------
M3U=$(curl -sk -m 40 -w "\nHTTPCODE=%{http_code}" -H "X-Plex-Token: $TOKEN" "${UNI}/start.m3u8?$Q")
M3U_CODE=$(printf "%s" "$M3U" | sed -n "s/.*HTTPCODE=//p" | tail -1)
SUB=$(printf "%s\n" "$M3U" | grep -v "^#" | grep -m1 "m3u8")
[ "$M3U_CODE" = "200" ] && [ -n "$SUB" ] \
  || fail hls-start-fail "start.m3u8=$M3U_CODE-subplaylist=${SUB:-none}-rk=$RK"

# ---- leg 6: media playlist ------------------------------------------------
# Follow what Plex named. Guessing base/00000.ts without this GET returns 404.
# The #EXTINF here is what BOTH segment thresholds are derived from, so a
# playlist without one is a failed playlist: there is no realtime to compare to.
PL=$(curl -sk -m 45 -w "\nHTTPCODE=%{http_code}" -H "X-Plex-Token: $TOKEN" "${UNI}/${SUB}")
PL_CODE=$(printf "%s" "$PL" | sed -n "s/.*HTTPCODE=//p" | tail -1)
SEG=$(printf "%s\n" "$PL" | grep -v "^#" | grep -m1 "\.ts")
[ "$PL_CODE" = "200" ] && [ -n "$SEG" ] \
  || fail hls-playlist-fail "playlist=$PL_CODE-segments=${SEG:-none}-rk=$RK-session-listed-no-segments"
EXTINF=$(printf "%s\n" "$PL" | grep -o "#EXTINF:[0-9][0-9.]*" | head -1 | cut -d: -f2)
[ -n "$EXTINF" ] \
  || fail hls-playlist-fail "playlist=$PL_CODE-declared-no-extinf-duration-rk=$RK-cannot-derive-a-throughput-target"
SUBDIR=$(dirname "${SUB%%\?*}")

# ---- leg 7: the session really chose the heavy path -----------------------
TSX=$(curl -sk -m 15 -H "X-Plex-Token: $TOKEN" "${BASE}/transcode/sessions" | tr -d "\n")
OURS=$(printf "%s" "$TSX" | grep -o "<TranscodeSession[^>]*key=\"${SESS}\"[^>]*>")
[ -n "$OURS" ] || fail session-missing "no-transcodesession-keyed-$SESS-rk=$RK"
printf "%s" "$OURS" | grep -q "error=\"0\"" \
  || fail session-error "plex-flagged-session-error-rk=$RK-$(printf %s "$OURS" | grep -o "error=\"[0-9]*\"")"
VD=$(printf "%s" "$OURS" | grep -o "videoDecision=\"[a-z]*\"" | cut -d\" -f2)
AD=$(printf "%s" "$OURS" | grep -o "audioDecision=\"[a-z]*\"" | cut -d\" -f2)
SRCA=$(printf "%s" "$OURS" | grep -o "sourceAudioCodec=\"[a-z0-9-]*\"" | cut -d\" -f2)
[ "$VD" = "transcode" ] && [ "$AD" = "transcode" ] \
  || fail session-not-transcoding "video=$VD-audio=$AD-want-both-transcode-rk=$RK-heavy-path-never-exercised"

# ---- legs 8+9: REAL SEGMENT BYTES, AT REAL SPEED (the legs with teeth) ----
# Both thresholds come from the playlist Plex just handed us, never from a
# constant: MIN_BYTES = QP_MIN_PCT% of kbps*EXTINF/8, and the realtime ratio is
# EXTINF/wall expressed as a percent. awk does the arithmetic because bash has no
# floats and both EXTINF and time_total are decimals.
OUT=$(mktemp)
SEGR=$(curl -sk -m 90 -o "$OUT" -w "%{http_code} %{size_download} %{time_total}" \
  -H "X-Plex-Token: $TOKEN" "${UNI}/${SUBDIR}/${SEG}")
SEG_CODE=$(echo "$SEGR" | awk "{print \$1}")
SEG_BYTES=$(echo "$SEGR" | awk "{print \$2}")
SEG_TIME=$(echo "$SEGR" | awk "{print \$3}")
SYNC=$(head -c 1 "$OUT" 2>/dev/null | od -An -tx1 | tr -d " \n")
rm -f "$OUT"

MIN_BYTES=$(awk -v k="$QP_KBPS" -v e="$EXTINF" -v p="$QP_MIN_PCT" -v f="$QP_MIN_BYTES" \
  "BEGIN{v=int(k*1000*e/8*p/100); if(v<f)v=f; print v}")
RT_PCT=$(awk -v e="$EXTINF" -v t="$SEG_TIME" \
  "BEGIN{if(t<=0)t=0.001; print int((e/t)*100)}")

# curl code 000 is a TIMEOUT, not a transport error: ninety seconds spent not
# finishing EXTINF seconds of video IS the throughput fault, and labelling it
# segment-http would send the operator hunting a broken HTTP handler.
[ "$SEG_CODE" = "000" ] \
  && fail segment-too-slow "segment-did-not-complete-inside-90s-for-${EXTINF}s-of-video-rk=$RK-transcoder-cannot-keep-up"
[ "$SEG_CODE" = "200" ] || fail segment-http "segment=$SEG_CODE-rk=$RK-$SEG"
[ "${SEG_BYTES:-0}" -ge "$MIN_BYTES" ] \
  || fail segment-empty "segment-bytes=$SEG_BYTES-min=$MIN_BYTES-derived-${QP_MIN_PCT}pct-of-${QP_KBPS}kbps-x-${EXTINF}s-rk=$RK-TRANSCODER-PRODUCED-NOTHING"
[ "$SYNC" = "47" ] \
  || fail segment-not-mpegts "first-byte=0x${SYNC:-none}-want-0x47-rk=$RK-body-is-not-a-ts-stream"
[ "${RT_PCT:-0}" -ge "$QP_MIN_RT_PCT" ] \
  || fail segment-too-slow "realtime=${RT_PCT}pct-min=${QP_MIN_RT_PCT}pct-${EXTINF}s-of-video-took-${SEG_TIME}s-rk=$RK-bytes-flowed-but-playback-cannot-sustain"

# ---- leg 10: cleanup must actually clean ----------------------------------
# Polled, not sampled once. A single read two seconds after the stop reports a
# leak whenever a loaded shared slot reaps slowly, which is a red for a reason
# that has nothing to do with playback. Five 2s polls = ~10s of grace, and
# STARTED stays armed until the session is CONFIRMED gone so the EXIT trap still
# issues a stop on the leak path.
if [ "$QP_SKIP_STOP" != "1" ]; then
  curl -sk -m 15 -o /dev/null -H "X-Plex-Token: $TOKEN" \
    "${UNI}/stop?session=${SESS}&X-Plex-Client-Identifier=${CID}"
fi
LEAK=1
POLLS=0
while [ "$POLLS" -lt 5 ]; do
  POLLS=$((POLLS + 1))
  sleep 2
  if ! curl -sk -m 15 -H "X-Plex-Token: $TOKEN" "${BASE}/transcode/sessions" \
       | tr -d "\n" | grep -q "key=\"${SESS}\""; then
    LEAK=0
    break
  fi
done
[ "$LEAK" = "0" ] \
  || fail session-leak "session-$SESS-survived-stop-through-${POLLS}-polls-over-10s-rk=$RK-canary-is-leaking-transcodes"
STARTED=0

printf "PASS: plex-playback - rk=%s tier=%s profile=%s src_audio=%s -> %s@%skbps segment=%sB (min %sB) in %ss = %s%% of realtime for %ss of video, session gone after %s poll(s) (library=%s items)\n" \
  "$RK" "$TIER" "$PROFILE" "${SRCA:-?}" "$QP_RES" "$QP_KBPS" "$SEG_BYTES" "$MIN_BYTES" \
  "$SEG_TIME" "$RT_PCT" "$EXTINF" "$POLLS" "$TOTAL"
') || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

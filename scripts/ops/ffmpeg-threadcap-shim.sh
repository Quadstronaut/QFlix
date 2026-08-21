#!/usr/bin/env bash
# QFLIX-FFMPEG-THREADCAP
# ffmpeg thread-cap shim — installed AS Tdarr's ffmpeg binary.
#
# The marker on line 2 is the INSTALLED-ness token, and it is deliberately a
# single unbroken word. smoke-test.sh and 50-tdarr-install.sh both grep it to
# tell "shim present" from "Tdarr upgrade overwrote it with the real binary".
# The first version of that check grepped `threadcap` against prose that reads
# "thread-cap", so it reported MISSING against a perfectly healthy shim — a
# check that cannot tell present from absent is worse than no check, because it
# trains you to ignore it. Same law as the worker1.js QFLIX-WORKER2-EXIT-
# NULLGUARD marker. Do not reword this line; a test pins it.
#
# WHY THIS EXISTS
# ---------------
# This slot's binding constraint is `ulimit -u 2000` TASKS, not CPU. ffmpeg
# threads to core count, and this is a 128-core box, so a single job holds
# 129-273 threads. Measured 2026-08-20: node off 962 tasks, ONE transcode
# worker 1411 (70.5%, past the thread-ceiling canary's 65% warn), TWO workers
# 2000 and wedged — bash could not fork, which takes cron, every canary and
# every *arr down with it, not just Tdarr.
#
# So "run a couple of files at once" was unaffordable at any worker count until
# ffmpeg itself was capped. Capped, two concurrent transcodes cost far less
# than one uncapped job does.
#
# WHY A BINARY SHIM AND NOT CONFIG
# --------------------------------
# There are two ffmpeg callers and only one is reachable from Tdarr's config:
#   * the TRANSCODE is flow-driven, so the flow's ffmpegCommandCustomArguments
#     node could cap it (129 threads);
#   * the HEALTH CHECK is built inside Tdarr_Node's obfuscated `srcug` bundle
#     (`-f null` full decode, 273 threads) and takes no configuration at all.
# A shim in front of the binary is the only lever that reaches both, and it
# also covers any ffmpeg call a future Tdarr version adds.
#
# COST, STATED PLAINLY: a Tdarr upgrade unzips over node_modules and removes
# this. That is the same fragility as the worker1.js exit-handler null-guard
# patch, which 50-tdarr-install.sh re-applies and smoke-test.sh asserts. This
# shim is installed and verified the same way. If it goes missing, the box does
# not break — it silently returns to 70% of its task ceiling at one worker,
# which is why the assertion matters more than the patch.
#
# CONTRACT
#   * Idempotent: refuses to wrap an already-wrapped binary.
#   * Never overrides an explicit -threads the caller supplied.
#   * Passes informational invocations (-version, -encoders, …) through
#     untouched — Tdarr runs "Binary test: ffmpegPath working" at every start
#     and a shim that breaks those bricks the node.
#   * Injects at BOTH ends: before the inputs (decoder threads — what the
#     health-check full-decode actually spends) and before the output target
#     (encoder threads — what libx264 will spend once hevc/av1 files convert).
#     Capping only one end leaves the other at core count.
set -u

REAL="$(dirname "$0")/ffmpeg.real"
N="${TDARR_FFMPEG_THREADS:-8}"

# No real binary → do not silently do nothing; a missing ffmpeg must look like
# a missing ffmpeg, not like a shim that swallowed the call.
[ -x "$REAL" ] || { echo "ffmpeg-threadcap: $REAL missing or not executable" >&2; exit 127; }

[ "$#" -eq 0 ] && exec "$REAL"

for a in "$@"; do
  case "$a" in
    # Caller knows what it wants — never second-guess an explicit cap.
    -threads|-threads:*) exec "$REAL" "$@" ;;
    # Informational / self-test invocations: pass through verbatim.
    -version|-buildconf|-formats|-codecs|-encoders|-decoders|-filters|-h|-help|-hide_banner)
      exec "$REAL" "$@" ;;
  esac
done

n=$#
last="${!n}"
if [ "$n" -eq 1 ]; then
  exec "$REAL" -threads "$N" "$last"
fi
head=( "${@:1:n-1}" )
exec "$REAL" -threads "$N" "${head[@]}" -threads "$N" "$last"

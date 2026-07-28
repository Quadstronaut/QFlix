#!/usr/bin/env bash
# Tdarr scanner-probe canary: assert Tdarr's own startup self-test still reports
# a working FFprobe + Exiftool, and track the two probes we KNOW are down.
#
# WHY: Tdarr runs four scanner self-tests at server start and logs one line each:
#   Scanner test 1: FFprobe working
#   Scanner test 2: Exiftool working
#   Scanner test 3: Mediainfo not working:"WebAssembly.instantiate(): Out of memory: wasm memory"
#   Scanner test 4: CCExtractor not working:"..."
# Tests 3 and 4 have been failing for as long as the retained logs go back, and
# REA never surfaced it: the tdarr source tailed .err files that are ~90% express
# stack-trace noise, so 858 WASM-OOM hits across 2026-07-24..28 stayed invisible
# (fixed 2026-07-28 in qflix-rea.ps1; this canary is the durable half).
#
# Mediainfo is UNFIXABLE at our layer, verified 2026-07-28 — do not retry:
#   - The slot caps address space at ulimit -v 10GB. Node reserves a ~8GB
#     trap-guard region per WebAssembly.Memory, so even
#     `new WebAssembly.Memory({initial:1})` (64KB!) fails on this box.
#   - The documented cure, NODE_OPTIONS=--disable-wasm-trap-handler (the
#     node-ultracc-wasm-fix that qflix-dash carries), is REJECTED by Tdarr's
#     bundled Node v18 runtime: "--disable-wasm-trap-handler is not allowed in
#     NODE_OPTIONS". Deploying it crash-looped tdarr-server (exit 9) and was
#     rolled back. It is a CLI-only flag and the launcher spawns the runtime.
#   - NODE_OPTIONS=--max-old-space-size=512 IS accepted but does not help;
#     capping the heap does not free the guard region.
#   - LimitAS can't be raised: soft == hard == 10GB on a shared slot.
#   - /usr/bin/mediainfo (v24.12) exists natively, but Tdarr's scanner is
#     obfuscated (`srcug/`) with no path/toggle knob, and any patch would be
#     wiped by the next Tdarr update.
#
# CCExtractor is a SEPARATE, unrelated failure — do not conflate the two (this
# canary's own message did until 2026-07-28). It is not WASM/OOM at all:
#   Scanner test 4: CCExtractor not working:"…/ccextractor: error while loading
#   shared libraries: libtesseract.so.4: cannot open shared object file"
# i.e. Tdarr's bundled ccextractor is dynamically linked against a libtesseract
# the host doesn't ship, and installing one needs root we don't have on a shared
# slot. Also supplementary (closed-caption extraction only), so it stays in the
# accepted-down baseline rather than parking a permanent red.
# Impact is bounded: FFprobe and Exiftool carry the pipeline and scans complete
# normally. Mediainfo is a supplementary probe. So this canary must NOT sit red
# forever over a known, accepted, unfixable condition — that is the exact noise
# this whole 2026-07-28 exercise removed. It reds only on a REGRESSION.
#
# Stage labels (printed to stderr on failure -> Kuma `msg=`):
#   STAGE=tdarr-server-inactive     — the unit is not running/crash-looping
#   STAGE=tdarr-scanner-regression  — FFprobe or Exiftool broke: pipeline-blocking
#
# Exit:
#   0 — core probes up. PASS-WARN while the known-degraded set is still down;
#       plain PASS (with a RECOVERED note) if one of them ever comes back, which
#       is the signal to close the known issue and drop it from the baseline.
#   1 — regression or the server is down.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

# Probes accepted as down. Shrink this as they get fixed — never grow it to
# silence a new failure.
BASELINE_DOWN=${QFLIX_CANARY_TDARR_BASELINE_DOWN:-"Mediainfo CCExtractor"}
# Log path is overridable so the regression/recovery/indeterminate branches can
# be exercised against fixtures instead of only ever being observed in the one
# state the live box happens to be in.
LOG_PATH=${QFLIX_CANARY_TDARR_LOG:-}

RES=$(sshm "
set -uo pipefail
BASELINE_DOWN='${BASELINE_DOWN}'
LOG='${LOG_PATH}'
[ -n \"\$LOG\" ] || LOG=\$HOME/.apps/tdarr/logs/Tdarr_Server_Log.txt

STATE=\$(systemctl --user is-active tdarr-server.service 2>/dev/null)
if [ \"\$STATE\" != 'active' ]; then
  echo \"STAGE=tdarr-server-inactive msg=tdarr-server-is-\${STATE:-unknown}\" >&2
  exit 1
fi

if [ ! -f \"\$LOG\" ]; then
  # No log to read is indeterminate, not a fault — stay UP and say so.
  echo 'PASS-WARN: tdarr-scanner-log-missing-state-indeterminate'
  exit 0
fi

# Tdarr colours its log lines; strip ANSI before matching. Take the LAST line
# per probe rather than a fixed tail -4, so a future Tdarr adding a fifth
# scanner test cannot silently shift the block out from under us.
CLEAN=\$(grep -a 'Scanner test' \"\$LOG\" 2>/dev/null | sed -E 's/\x1b\[[0-9;]*m//g')

probe_state() {
  local line
  line=\$(printf '%s\n' \"\$CLEAN\" | grep -F \"\$1\" | tail -n1)
  if [ -z \"\$line\" ]; then echo unknown; return; fi
  case \"\$line\" in *'not working'*) echo down;; *) echo up;; esac
}

FFPROBE=\$(probe_state FFprobe)
EXIFTOOL=\$(probe_state Exiftool)
MEDIAINFO=\$(probe_state Mediainfo)
CCEXTRACTOR=\$(probe_state CCExtractor)

if [ \"\$FFPROBE\" = unknown ] && [ \"\$EXIFTOOL\" = unknown ]; then
  # Block rotated out of the retained log (tdarr can run for weeks between
  # restarts). Indeterminate is NOT a fault — a red here would be pure noise.
  echo 'PASS-WARN: tdarr-scanner-test-block-not-in-retained-log-state-indeterminate'
  exit 0
fi

# FFprobe and Exiftool are load-bearing: without them Tdarr cannot read a file
# at all, and the whole transcode pipeline stalls silently.
if [ \"\$FFPROBE\" = down ] || [ \"\$EXIFTOOL\" = down ]; then
  echo \"STAGE=tdarr-scanner-regression msg=ffprobe-\${FFPROBE}-exiftool-\${EXIFTOOL}-PIPELINE-BLOCKING-mediainfo-\${MEDIAINFO}-ccextractor-\${CCEXTRACTOR}\" >&2
  exit 1
fi

# Fresh WASM-OOM volume, reported for context only — it is a symptom of the
# known mediainfo failure, never a trigger on its own.
OOM=\$(grep -ac 'Out of memory: wasm memory' \"\$LOG\" 2>/dev/null || echo 0)

# Did anything in the accepted-down baseline come back?
RECOVERED=''
for p in \$BASELINE_DOWN; do
  case \"\$p\" in
    Mediainfo)   [ \"\$MEDIAINFO\"   = up ] && RECOVERED=\"\$RECOVERED Mediainfo\" ;;
    CCExtractor) [ \"\$CCEXTRACTOR\" = up ] && RECOVERED=\"\$RECOVERED CCExtractor\" ;;
  esac
done

if [ -n \"\$RECOVERED\" ]; then
  # Good news is actionable too: drop it from BASELINE_DOWN so a later relapse reds.
  echo \"PASS: tdarr-scanner-RECOVERED-\$(echo \$RECOVERED | tr ' ' '-')-remove-from-baseline-ffprobe-\${FFPROBE}-exiftool-\${EXIFTOOL}\"
  exit 0
fi

echo \"PASS-WARN: ffprobe=\${FFPROBE}-exiftool=\${EXIFTOOL}-known-down=mediainfo(wasm-oom-unfixable)+ccextractor(missing-libtesseract.so.4)-wasm-oom-lines=\${OOM}\"
exit 0
") || RC=$?
RC=${RC:-0}
echo "$RES"
exit $RC

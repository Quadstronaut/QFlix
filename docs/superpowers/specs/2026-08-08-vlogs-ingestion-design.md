# VLogs ingestion redesign — per-source cursors, bounded everywhere

**Status:** design. Council-adjudicated 2026-08-07 (5 blockers returned, all
resolved below and binding on the implementation). Awaiting operator review
before an implementation plan is written. Nothing in this document is deployed.

## Goal

Everything on the box routes durable logs into VictoriaLogs
(`-retentionPeriod=90d`), and the ingester stops re-shipping what it already
shipped. Measured duplication in the current `qflix-vlogs-ingest.py` is
**~20× to ~386× per file**, because each 5-minute cycle re-reads whole files
and the only thing standing between a chatty file and the wire is
`_file_is_dormant()` — a heuristic skip that is simultaneously the sole
backpressure mechanism and a coverage hole (a dormant-classified file that
wakes up ships its entire history again).

Three deliverables:

1. **Per-source persisted cursors** — read only bytes not yet shipped.
   `_file_is_dormant()` is deleted; its two jobs (skip-quiet-files,
   throttle-noisy-files) are replaced by the cursor itself and by explicit
   per-cycle caps (Blocker 4).
2. **Manifest-driven source map** — the list of files to ingest moves out of
   code into `manifest/vlogs-sources.yaml`, one entry per source with path
   glob, parser hint, and app attribution, so adding an app to the box carries
   an obligation to declare its logs (and the canary can demand it).
3. **Bidirectional coverage canary** — `vlogs-coverage.sh`: every declared
   source is being read (declared-but-silent = finding, with the named BROKEN
   states below), and every log-writing app in `manifest/apps.yaml` has a
   declaration (writing-but-undeclared = finding). The prowlarr-app-sync
   shape: both directions, so the next added app cannot become an invisible
   gap.

## Cursor state — the schema that resolves Blockers 1, 2 and 5

One JSON document per source at `~/.opt/maint/vlogs-ingest/cursors/<id>.json`:

```json
{
  "identity": {"dev": 2049, "ino": 9437285, "head_sha256": "…first 256 bytes…"},
  "offset": 184320,
  "last_ts": "2026-08-08T04:59:35.215Z",
  "consecutive_defers": 0,
  "capped_streak": 0
}
```

- **Identity is the triple `(st_dev, st_ino, sha256(first 256 bytes))`** —
  never inode alone, never `size < offset` alone (Blocker 1). `copytruncate`
  keeps the inode and can regrow past the old offset between ticks, so either
  single-field test resumes mid-stream at a non-line-boundary: silent loss
  plus a permanently misaligned parse. Any change to the triple is a
  rotation.
- **`last_ts` is persisted, not per-call** (Blocker 5). `collect_for()`
  carries `last_ts` forward so untimestamped continuation lines (stack
  traces) inherit their parent's timestamp — added precisely because
  re-tailed exception blocks once resurfaced as phantom "recent" errors and
  misled a council (`test_mcp_logs.py:48`). With cursors, a batch can begin
  mid-trace; if `last_ts` restarts at None the record ships with `_time: ""`
  and VictoriaLogs stamps it NOW — the same phantom-error defect, resurrected
  at every chunk boundary.

## Blocker resolutions (binding)

### B1 — rotation: identity triple, nothing less
Covered above. Both proposed legs ("inode change" / "size < offset") are
individually defeated by `copytruncate`; the triple is not.

### B2 — resume-from-unknown is tail-bounded, always
"Restart at 0 on rotation" is an unbounded amplifier: a truncate-and-regrow
of a 28 MB file — or one corrupt `cursors/<id>.json` — re-ships the whole
file, which is the 386× problem re-entering through the recovery path.
**Every** resume-from-unknown (first sight, rotation, corrupt state,
fingerprint mismatch) reads at most `backfill_max_bytes` (default **2 MiB**)
from the **tail**, aligned forward to the first `0x0A`. Every such event is
counted, named in the run summary, and tagged on the wire
(`resume: "rotation"` etc.) so a backfill is distinguishable from a live
tail in queries.

### B3 — poison-batch escape: classify, quarantine, advance
"Cursor advances only after 2xx, retry same bytes on failure" has no
termination: one oversized line (VictoriaLogs `-insert.maxLineSizeBytes`
default 256 KB), one malformed record, one 4xx, and the source never advances
again — a new permanent blind spot with exactly the shape of the one being
fixed, invisible because the run exits 0. POST outcomes are classified:

| outcome | action |
|---|---|
| `ok` | advance cursor |
| `transport` / `http-5xx` | defer; cursor unmoved; counted (`consecutive_defers`) — the normal shared-box I/O-stall path (`ulimit -u 2000` makes it common) |
| `http-4xx` | payload defect: **bisect** the batch to isolate the offending record(s), quarantine them to `~/.opt/maint/vlogs-ingest/quarantine/YYYY-MM-DD.jsonl`, **advance past them**, increment `quarantined` in the run summary |

A source with `consecutive_defers >= 12` (one hour at the 5-minute cadence)
is reported by the coverage canary as a **named BROKEN state**
(`vlogs-source-deferred`), never as silence.

### B4 — backpressure replaces the dormant skip, explicitly
The dormant skip is a bad brake but it is the only brake; deleting it with no
replacement sends an app error-looping at 100 MB/hour straight to the wire,
and the first thing that notices is the quota canary. Per-source caps:
`max_lines_per_cycle` (default **20 000**) and `max_bytes_per_cycle` (default
**8 MiB**). On cap: ship what fits, **advance the cursor by exactly what was
shipped**, record `capped: true` plus the residual byte count. A source
capped on **3 consecutive cycles** is a canary finding
(`vlogs-coverage-source-backlogged`) — loud, never silent, and never a reason
to stop shipping the head of the backlog.

### B5 — continuation-line timestamps survive chunk boundaries
Covered by the persisted `last_ts` above.

## Failure vocabulary

`vlogs-source-deferred` · `vlogs-coverage-source-backlogged` ·
`vlogs-coverage-declared-missing` · `vlogs-coverage-unclaimed` ·
exit 0 / 1 finding / **2 could-not-assert** (unreadable cursor dir, zero
declared sources — an empty declaration set must never read as clean, the
cron-liveness precedent).

## Testing

- Rotation matrix: rename-rotate, copytruncate-with-regrow (size > offset),
  copytruncate-with-shrink, first-sight, corrupt cursor JSON → every path
  asserts tail-bounded backfill, byte-for-byte no-duplication, no-loss within
  the backfill window.
- Poison batch: an oversized line mid-batch → bisection quarantines exactly
  that record, cursor lands after it, quarantine file carries it.
- Cap behaviour: a 50 000-line burst → 20 000 shipped, cursor advanced
  exactly, `capped: true`, residual correct; three cycles → the canary
  finding fires (mutation-check: break the streak counter and the test must
  fail).
- Continuation lines: a batch that opens mid-stack-trace inherits the
  persisted `last_ts`; `_time` is never empty on the wire.

## Out of scope

Journald ingestion (already routed via the existing vector), retention
tuning, VictoriaLogs upgrades, and any change to what the apps themselves
log. The reacquire-guard respec is a separate document.

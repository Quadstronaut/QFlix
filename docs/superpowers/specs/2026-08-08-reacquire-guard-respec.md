# Anti-reacquisition guard — Stage-0 respec

**Status:** design respec. The 2026-08-07 arch-tier council on this guard
returned `route_back → stage0`: 20 verdicts, 4 passes, **six blockers spread
across all four candidates**, no override available (ledger `620077b`, 37
rows). The arbiter left two spec ambiguities explicitly unresolved and named
them as the reason four competent attempts diverged rather than converged.
This document resolves both and carries every council-found blocker forward
as a binding requirement. Awaiting operator review before implementation; the
regeneration round runs at `feature` tier against THIS spec.

## Problem (unchanged)

The reaper deletes by pure add-date retention while Sonarr still monitors the
deleted episodes, so the *arrs re-grab what was just reaped — a
delete/re-download loop that burns bandwidth and refills the disk. The guard's
job: after a reaper deletion, unmonitor ("fuse") exactly what the reaper
deleted, so it stays gone until a human re-requests it.

## Ruling 1 — what `--max-total` counts

**The cap counts actionable mutations planned by THIS run** — the set of
(series/season/episode, fuse-action) pairs the run is about to write — not
the raw pre-classification candidate population.

The rejected reading is disqualified by measurement: counted against the raw
population, the live fleet volume (212 candidates fleet-wide on 2026-08-07)
exceeds any sane cap on every scheduled run, so the guard exits 2 forever — a
module that can never do its job. A tripwire must bound what the run *does*,
not describe the backlog it sees; this is the reaper's own `--max-items`
semantics, and the two caps should read identically. Exceeding the cap
defers the excess to the next run (oldest-first), reports the deferral count,
and still processes up to the cap — the aab9e87 defer-oldest-N precedent, not
an abort.

## Ruling 2 — the reaper manifest is CORROBORATION

Neither gate nor audit-only. The reaper's per-run audit manifest
(`~/.opt/maint/reaper/manifest-*.json`) answers one question the *arr queue
cannot: *was this disappearance reaper-intentioned?* The guard fuses only
items the manifest corroborates as reaper-deleted; anything missing from the
manifest — an out-of-band deletion, a hardlink loss, an operator `rm` — is
**reported, never fused** (fusing it would hide a real incident behind the
guard's own bookkeeping).

- Not a **gate**: an unreadable or missing manifest downgrades the run to
  report-only and says so loudly. It never blocks or aborts the run wholesale
  — a missing file is a data-quality signal, and overloading it as an
  interlock is the exact shape the operator has already corrected once
  (never-use-missing-data-as-an-interlock, 2026-08-07).
- Not **audit-only**: it actively discriminates the fuse set. Removing it
  from the decision path would fuse out-of-band losses, which is how a disk
  incident gets silently converted into "intentionally not monitored".

Absence freezes, presence acts — the same asymmetry the entitlement gate
encodes: missing evidence must never cause the destructive direction.

## Council blockers carried forward as requirements

1. **Checked writes, both PUTs, no fuse on failure.** The worst candidate
   gated the episode PUT correctly, then set `seasons_cleared`
   unconditionally — a 500 on the season-flag PUT yielded exit 0, status ok,
   episodes fused permanently: the guard would have *created* the
   resurrection loop it exists to prevent, and reported green. Requirement:
   every write is return-code-gated; `season_flag_failures` recorded
   separately; any failed write → `status: partial-write-failure`, exit 1,
   and **no fuse entry for the failed item** (an unfused item retries next
   run; a wrongly-fused item is permanent).
2. **Lock contention is visible.** Whole-run `flock` scope; contention must
   not exit 0 silently, and must not exit 2 with an empty body under
   `--dry-run`/`--emit-json` — both consumers parse the body.
3. **Series-size-unknown → refusal.** If the *arr cannot state what the
   series should contain, the guard refuses that item (exit-2 vocabulary),
   never guesses.
4. **Arbiter's merge plan as the starting base**: gen-opus-1 (clean
   spec-conformance + boundaries, correct size-unknown refusal, measured
   `--max-total` semantics, whole-run flock) with gen-opus-2's
   `apply_instance()` checked-write discipline grafted onto
   `apply_tv()`/`apply_movies()`.

## Interlocks (inherited house pattern)

Ships inert: dry-run default, `--execute` armed only by an on-box systemd
drop-in; Monday-window suppression; `--max-total` as ruled above; every fuse
written to a durable per-run manifest of its own so the guard's actions are
themselves auditable and revertable.

## Canary

The guard's dead-man follows the unstick-rate precedent — watch the
**action**, not the rule: a run that fuses more than the cap wants, or a
parked `partial-write-failure` streak, is a finding. Wiring: all five
installer points, `manifest/apps.yaml`, `manifest/jobs.yaml`, doc counts.

## Out of scope

Retention-window tuning (60-day add-date design is operator-confirmed),
watch-awareness, Sonarr per-series quality profiles (rejected 2026-08-03),
and the vlogs redesign (separate spec).

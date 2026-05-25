# Maintenance-aware alerting — Sub-project C: correlated-alert collapse (fleet dead-man)

**Date:** 2026-05-24
**Status:** Design (full-auto build authorized; pending parallel implementation)
**Part of:** the A→D maintenance-aware alerting effort. **Independent of A** (no
`ucc.py` dependency), but built on integration branch `feat/maint-aware-alerting`
so it merges cleanly with B/D.

## Context

When the host degrades (UCC maintenance, a load spike, or a pusher crashloop),
many monitors flip DOWN at once. The operator gets N near-simultaneous pages for
what is really **one** correlated event — "storms are always confusing at first
glance." C collapses correlated mass-down into a single aggregate signal so a storm
reads as one clear alert, not thirty.

This is independent of *why* the host is degraded — it is a general dead-man over
the whole fleet. (B handles the UCC-specific recovery suppression; C handles the
operator-facing signal.)

## Design

One new aggregate Kuma monitor plus storm detection in the pusher.

### C1 — "QFlix Fleet" aggregate PUSH monitor

A dead-man monitor the pusher feeds once per cycle, mirroring the existing
"Manitoba Pusher" self-heartbeat.

- `scripts/maint/bootstrap-kuma-monitors.py`: add a step (mirroring the
  "Manitoba Pusher" block) that idempotently ensures a PUSH monitor named
  **"QFlix Fleet"** exists and captures its token into
  `secrets/kuma-push-tokens.json` under key `"qflix-fleet"`. Add `"QFlix Fleet"`
  to the expected-monitor set in `lib/kuma.py` `audit_monitors` (alongside the
  existing `"Manitoba Pusher"` addition) so drift audit doesn't flag it as orphaned.

### C2 — Storm detection + collapse (`lib/fleet.py`, new + `pusher.py` edits)

`lib/fleet.py`:
```python
FLEET_STORM_THRESHOLD = int(os.environ.get("MANITOBA_FLEET_STORM_THRESHOLD", "8"))

def evaluate(results: dict[str, str], *, probe_ok: dict[str, bool],
             state_path=None) -> dict:
    """Given this cycle's per-app health (probe_ok: app->ok bool), update
    fleet-window.json and return:
      {"down_count", "total", "storm_active", "edge"}  # edge in {None,"onset","clear"}
    Storm is active when down_count >= FLEET_STORM_THRESHOLD. Edge fires only on
    transition (non-storm->storm = 'onset', storm->non-storm = 'clear')."""
```
- State file `~/.opt/maint/fleet-window.json` (atomic write, same idiom as
  `ucc.write_state`): `{storm_active, down_count, total, since, last_eval_at}`.
  Survives pusher restarts so onset/clear edges don't double-fire on a bounce.
- `down_count` = number of pushed apps whose probe was not ok this cycle.

`lib/pusher.py` edits (localized, after the per-app loop in `push_once` — a
separate region from B's edit inside the strike block):
- Build the `probe_ok` map during the existing loop.
- Call `fleet.evaluate(...)`.
- Push the **"QFlix Fleet"** monitor each cycle using `tokens.get("qflix-fleet")`:
  `status=down` with msg `"storm: N/M down"` when `storm_active`, else `status=up`
  with msg `"N/M down"`. (Dead-man: if the pusher dies entirely, no push arrives
  and Kuma flips "QFlix Fleet" down after its heartbeat — one unambiguous signal.)
- On `edge == "onset"`: emit **one** consolidated Discord alert via `notify.notify`
  (`level="warning"`): `"⚠ Fleet storm: N/M monitors down at once"` plus a short
  list (first ~8 names). On `edge == "clear"`: one `level="info"` "storm cleared
  (lasted Xm)". No per-cycle repeats — only on edges.

The per-app Kuma pushes are unchanged (the dashboard stays accurate per service);
C only adds the aggregate signal and the single storm alert. Recovery is untouched
(its semaphore + permanently-failed dedup already cap churn; UCC-specific
suppression is B's job).

### Threshold rationale

Fleet is ~33 pushed app monitors. A default of 8 simultaneously-down (~25%)
distinguishes a correlated storm from a handful of independent outages, and is
env-overridable for tuning. Documented in `fleet.py`.

## Error handling

- `fleet.evaluate` never raises; state read/write best-effort (corrupt/missing →
  treat as no prior storm).
- The aggregate push and the storm notify are best-effort and must not break the
  pusher loop (same guard style as the existing self-heartbeat push).
- If `"qflix-fleet"` token is absent (monitor not bootstrapped yet), skip the
  aggregate push silently — code path is a no-op until the operator re-runs
  bootstrap, exactly like the pusher self-heartbeat gate.

## Testing (TDD)

- `tests/unit/test_fleet.py`: threshold boundary (7 down = no storm, 8 = storm);
  edge detection across cycles (onset fires once, stays active without re-firing,
  clear fires once); state round-trip + corrupt-state fresh start; `down_count`/
  `total` math.
- `tests/unit/test_pusher.py` (extend): with a token map containing `"qflix-fleet"`,
  a cycle with ≥threshold failing probes pushes the aggregate monitor `down` and
  emits exactly one storm notify (mock `notify`); a healthy cycle pushes it `up`
  and emits no notify; absent token → no aggregate push, no crash.
- Bootstrap is not unit-tested live (socket), consistent with existing bootstrap
  having no unit tests; keep the new block structurally identical to the proven
  "Manitoba Pusher" block.

## Out of scope

UCC detection (A), recovery suppression (B), deep-heal (D). C may share the host
with a UCC maintenance window; during one, a storm alert firing once (not N pages)
is the intended, acceptable behavior.

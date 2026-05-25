# Maintenance-aware alerting — Sub-project D: post-window deep-check autoheal

**Date:** 2026-05-24
**Status:** Design (full-auto build authorized; pending parallel implementation)
**Part of:** the A→D maintenance-aware alerting effort. **Loosely depends on A**
(invoked on A's `active→clear` edge by B) and edits `window.py`.
**Built on:** integration branch `feat/maint-aware-alerting`.

## Context

Two windows leave the fleet in a state that needs an explicit re-heal sweep when
they close:

1. **QFlix's own window** — during it, the `lock` file makes the Kuma webhook queue
   down-events instead of recovering (see `kuma._handle_event`). On close, those
   queued events are not replayed; a service that died mid-window may sit down.
2. **UCC upstream window** — B suppresses `ucc`-class recovery for the whole window.
   On clear, those apps may still be down (or were left stopped by the gate).

D is the safety net: a single deep-check that probes every app and recovers
anything still down, run on **either** window's close. It is what makes B's blanket
ucc-class suppression safe.

## Design

### D1 — The deep-check (`lib/deep_check.py`, new)

```python
def run_deep_check(*, reason: str, manifest=None,
                   recover: bool = True) -> dict:
    """Probe every manifest app; for each that is down, trigger recovery
    (now ungated). Best-effort; never raises.

    Returns {"reason", "ts", "checked", "down": [names],
             "recovery_triggered": {name: decision}, "skipped": [...]}.
    """
```
- Loads the manifest (default via the same resolution as `recovery._load_default_manifest`)
  unless one is passed.
- Probes each app with `health.probe`.
- For each down app, calls `recovery.trigger_async(app, manifest=manifest)` and
  records the returned decision (`started` / `already_running` / `permanently_failed`
  / `not_recoverable` / …). Uses `trigger_async` (not synchronous `run`) so the
  sweep doesn't block on 3-attempt loops and reuses the existing per-app lock +
  semaphore + dedup — D will never double-recover something already in flight.
- Emits one summary `notify.notify` (`level="info"` if nothing was down, `"warning"`
  if it triggered recoveries) and appends a line to a `deep-check.jsonl` log under
  `MANITOBA_STATE_DIR`.
- **This is the exact signature B's responder calls on the UCC clear edge**
  (`deep_check.run_deep_check(reason="ucc-clear")`). The keyword-only `reason` and
  best-effort/never-raise contract are pinned so B and D merge cleanly.

### D2 — Triggers (two call sites)

- **QFlix window close** — `lib/window.py` `WindowOrchestrator.run()`: after
  `self.close()` and the post-maint notify/email, call
  `deep_check.run_deep_check(reason="qflix-window", manifest=self._manifest)`
  inside try/except (best-effort; a deep-check failure must not fail the window).
  Skip in `dry_run`. *(window.py is edited only by D; no overlap with B/C.)*
- **UCC window clear** — invoked by B's responder on `active→clear` via the pinned
  seam above. D defines the function; B calls it. (D does not itself watch UCC
  state — keeps the dependency one-directional and the modules testable in
  isolation.)

### D3 — CLI

`manitoba-maint deep-check [--reason <str>]` in `lib/cli.py` `_build_parser` +
dispatch (a new top-level subcommand — a different region of `_build_parser` than
A's `ucc` block and B's edit inside `_cmd_ucc_detect`, so the three merge cleanly).
Prints the summary dict; exit 0 unless the probe run itself couldn't start.
Lets the operator run a manual sweep and gives D a timer entrypoint if ever wanted
(no timer is installed now — triggers are the two window closes).

## Error handling

- `run_deep_check` never raises; per-app probe/recovery failures are captured in the
  result dict, not propagated.
- A down app that is `not_recoverable` (library/cron-without-unit) is recorded and
  skipped, not retried.
- Manifest load failure → return a result with an `error` field and an empty sweep;
  notify `warning`; never crash the caller (window close / B responder).

## Testing (TDD)

- `tests/unit/test_deep_check.py`: mixed up/down fleet → only down apps get
  `trigger_async` (mock it), summary dict shape correct, nothing-down path emits the
  info notify and triggers no recovery, manifest-load failure returns the error
  result without raising, `not_recoverable` decisions recorded.
- `tests/unit/test_window.py` (extend): `run()` calls
  `deep_check.run_deep_check(reason="qflix-window", …)` after close (mock it);
  dry_run skips it; a raising deep_check does not fail `run()`.
- `tests/unit/test_cli.py` (extend): `deep-check` subcommand dispatches to
  `run_deep_check` and prints the summary (mock it).

## Out of scope

UCC detection (A), suppression/incident/email (B), storm collapse (C). D only
provides `run_deep_check` and wires it to the two window-close triggers.

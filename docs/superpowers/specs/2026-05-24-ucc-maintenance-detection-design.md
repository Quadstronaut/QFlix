# UCC (upstream) maintenance detection + state — Sub-project A

**Date:** 2026-05-24
**Status:** Design (approved, pending spec review)
**Part of:** the maintenance-aware alerting effort (A→B→C→D). This spec covers **A only**.

## Context

QFlix runs on a shared Ultra.cc (UCC) seedbox (`seedbox.example.com`). During UCC's own
host maintenance, the `app-<name> <op>` lifecycle CLI is **gated**: every lifecycle
operation returns, regardless of the app,

```json
{"data": {"message": "The 'start' operation is no longer available due to maintenance."}, "result": false}
```

This gating (observed live 2026-05-24) is the root cause of the alert storm that
prompted this work: services stay up, but the monitoring/lifecycle plane degrades,
and ~30 Kuma push-monitors flap as the overloaded host intermittently stalls.

Today the codebase has **no concept of upstream/UCC maintenance** — all "maintenance"
logic refers to QFlix's *own* weekly window (a lockfile at `~/.opt/maint/lock`, see
`scripts/maint/lib/window.py`). Sub-projects B/C/D need a reliable "is UCC in
maintenance right now?" signal. A provides it.

### What downstream sub-projects will consume (not built here)

- **B** — suppress pusher recovery triggers, pin a user-facing Kuma status-page
  incident, and fire a new "upstream maintenance" customer email, all keyed off A's state.
- **C** — correlated-alert collapse (dead-man aggregate); independent of A but related.
- **D** — post-window deep-check autoheal on *any* window close (QFlix or UCC),
  keyed partly off A's `active → clear` edge.

## Findings that constrain the design (empirical, 2026-05-24)

1. **No passive maintenance signal.** No flag file, no maintenance MOTD
   (`/etc/motd` dates to 2019), and there is **no `status` subcommand** on the app
   wrapper (`app-kavita status` → `Unknown command: status`). Detection MUST use a
   lifecycle op.
2. **The gate message is stable and parseable**: `result == false` AND message
   matching `/no longer available due to maintenance/i`.
3. **`start` is the safe probe op**: gated → returns the message and does nothing;
   ungated → idempotent on an already-running app. (`stop`/`restart` are destructive
   ungated and must NOT be used as the periodic probe.)

## Design

### New module: `scripts/maint/lib/ucc.py`

Single responsibility: probe the UCC gate, maintain `ucc-window.json`, emit edge
transitions. It does **not** send email, pin Kuma incidents, or run heals.

#### Probe

- Command: `app-<probe_app> start`, stdout captured, short timeout (e.g. 15s).
- `probe_app` resolution order: `secrets/ucc.probe_app` → default bootstrap
  (`kavita`, known-installed, low-stakes). **Post-maintenance, set to `qui`** (see
  Testing).
- Classification of probe result:
  - **`gated`** — JSON `result == false` and message matches `due to maintenance`.
  - **`clear`** — JSON `result == true`, or output indicates already-running /
    started successfully.
  - **`probe-error`** — timeout, non-JSON, SSH/host stall, **or** an
    unknown-app / "not installed" response (guards against a mis-set `probe_app`).
    Never changes state.

#### State file: `~/.opt/maint/ucc-window.json` (under `MANITOBA_STATE_DIR`)

```json
{
  "active": true,
  "first_detected_at": "2026-05-24T21:30:00Z",
  "last_confirmed_at": "2026-05-24T23:55:00Z",
  "last_probe_at":     "2026-05-24T23:55:00Z",
  "last_probe_result": "gated",
  "probe_op":          "app-qui start",
  "consecutive_clear": 0,
  "consecutive_error": 0
}
```

Written atomically (temp file + rename), mirroring `window.py`'s state handling.

#### Transitions (debounced to survive host-load flapping)

- **`clear → active`**: a single `gated` probe flips immediately. We want to start
  suppressing fast; a false positive only means "we briefly thought UCC was in
  maintenance," which is low-harm.
- **`active → clear`**: requires **N consecutive `clear` probes** (default
  `UCC_CLEAR_DEBOUNCE = 3`, ≈15 min at the 5-min cadence). A single probe sneaking
  through during host overload must not prematurely end the window and un-suppress.
- **`probe-error`**: hold last state, increment `consecutive_error`, reset nothing.
  Persistent errors are a separate (host-down) concern surfaced via Discord, not a
  state flip.
- On any edge (`clear→active`, `active→clear`): call `notify.notify(...)` and append
  a transition record (timestamp, from→to, probe_op) to a transitions log for B/D.

### Wiring

- **CLI** (`scripts/maint/lib/cli.py`, mirroring `window`/`pusher`):
  - `manitoba-maint ucc detect` — run one probe + state update (timer entrypoint).
  - `manitoba-maint ucc status` — read-only print of current state (humans + B/C/D).
- **systemd**: `manitoba-maint-ucc-detect.{service,timer}`, `OnUnitActiveSec=5min`
  (gate changes slowly; matches the throwaway `tautulli-gate-watch.sh` cadence).

### Reused infrastructure

- `MANITOBA_STATE_DIR` (`~/.opt/maint`) for state.
- `lib/notify.py` for Discord edge notifications.
- `lib/secrets.py` for `ucc.probe_app`.

## Error handling

- Probe subprocess timeout / non-zero exit / non-JSON → `probe-error`, no flip.
- Unknown-app / not-installed response → `probe-error` (prevents a mis-set
  `probe_app` from being read as `clear` and ending a window).
- State file unreadable/corrupt → treat as "no prior state" (fresh start), log a
  warning; never crash the timer.
- All edge side-effects (notify) are best-effort and must not abort the state write.

## Testing

- **Unit** (mock the probe subprocess output): assert classification of gated /
  clear / error outputs, and the full state machine including the `active→clear`
  3-probe debounce and `probe-error` hold behavior.
- **Live, now**: with UCC currently gated, `manitoba-maint ucc detect` must record
  `active`. When UCC lifts the gate, watch the real `active→clear` debounce fire.
- **Destructive validation on `qui` (post-maintenance):** once the gate lifts,
  install `qui` (a disposable qBittorrent web UI app) and set `probe_app = qui`.
  Then, on the live ungated box, confirm:
  - `app-qui start` on a running `qui` is a true no-op (classified `clear`, app not
    restarted) — validates the periodic probe is non-disruptive.
  - capture exact ungated `stop`/`restart` responses for documentation (we may
    `stop`/`restart` `qui` freely since nothing depends on it).
  If `start` ungated turns out NOT to be a no-op, fall back to a different installed
  low-stakes `probe_app`.

## Out of scope (later sub-projects)

Suppression, Kuma user incident, customer email, alert collapse, and post-window
deep-heal are B/C/D. A only detects and records.

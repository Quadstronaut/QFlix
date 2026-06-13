# Council v2 — Tdarr quiet-hours / pusher false-recovery

**Date:** 2026-06-12 · **Arbiter:** Claude (Opus 4.8) · **Tier (§8):** feature / multi-file → 3 candidates, spec + 3 lenses, 1 cross-vendor

---

## Stage 0 — Spec

### Problem (root cause, grounded)

`tdarr-node-pause.timer` intentionally **stops** `tdarr-node.service` daily at **18:00 UTC**
(fair-use quiet hours — keep transcode workers off during streaming peak; `tdarr-node-resume.timer`
restarts it at 23:00 UTC). See `scripts/configure/50c-tdarr-quiet-hours.sh`.

The **pusher** (`scripts/maint/lib/pusher.py`, 60 s loop) probes `tdarr-node` via `systemd_only`,
sees `inactive`, accrues 3 strikes (~180 s), and fires `recovery.trigger_async` →
`lifecycle.start(tdarr-node)` → the node restarts at ~18:02 UTC → `recovery.py` emits
`✓ tdarr-node recovered after 1 attempt(s)` to Discord at ~18:04 UTC.

This (a) spams a **false daily recovery alert**, and (b) **defeats the 5-hour fair-use pause** —
the node transcodes 18:02–23:00 UTC instead of staying quiet, competing with streamers, which is
exactly what the quiet-hours feature exists to prevent.

The heartbeat cron (`scripts/ops/heartbeat-tdarr-node.sh`) was patched 2026-05-30 to skip 18:00–23:00,
but the **pusher is a second, independent watchdog that was never given the same guard.**

### Evidence (artifacts)

- `~/.opt/maint/notify.log` — 7/7 tdarr lines are `✓ tdarr-node recovered after 1 attempt(s)`,
  one per day at 18:03–18:04 UTC (2026-06-06 … 2026-06-12). No other tdarr event. No genuine failure.
- `systemctl --user show tdarr-node`: `NRestarts=0`, `ExecMainStartTimestamp=18:02:06 UTC` — an
  external `systemctl start` of a healthy-but-paused unit, not a crash/auto-restart.
- `systemctl --user show tdarr-server`: `NRestarts=0`, up since 2026-05-21 (23 d) — **stable; not involved.**
- The `✓ … recovered` string exists **only** in `recovery.py`, reachable **only** via the pusher
  auto-heal (Kuma webhook can't reach host loopback). Heartbeat scripts never call `notify`.

### Goal / contract

During an app's declared **pause window**, the pusher MUST:
1. NOT trigger recovery (no restart of the intentionally-stopped unit).
2. NOT push the monitor `down` (keep Kuma green with a clear `[paused: …]` note).
3. NOT accrue auto-heal strikes (clear any stale count).
4. Behave **identically to today outside the window** (zero regression).

### Interface

Declarative `pause_window` on the app in `manifest/apps.yaml`, UTC hour granularity (matches the
systemd `OnCalendar` and the heartbeat's `HOUR_UTC` guard exactly):

```yaml
pause_window:
  start_hour_utc: 18   # inclusive
  end_hour_utc: 23     # exclusive  → paused for hours 18,19,20,21,22
```

Semantics: window is `[start, end)` in UTC hours; supports wrap-around (`start > end`, spans midnight);
`start == end` → never paused; absent field → no window (unchanged behavior). Invalid/out-of-range →
`ManifestError` at load (fail loud, like the existing health-kind validation).

### Acceptance tests (executable, must exist + fail first)

1. `manifest.load` parses `pause_window`; app without it → `pause_window is None`.
2. Invalid pause_window (missing key / hour out of 0..23) → `ManifestError`.
3. `PauseWindow.contains`: `[18,23)` → True at 18 and 22, False at 17 and 23.
4. Wrap-around `[22,6)` → True at 22, 23, 5; False at 6, 12, 21. `start==end` → always False.
5. pusher `push_once`, app in window → pushed `status=up` with `[paused…]` msg, `health.probe`
   NOT called, `recovery.trigger_async` NOT called, strike counter cleared.
6. pusher `push_once`, app outside window → probe + recovery path unchanged.
7. Full existing suite stays green (58 → 58+).

### Invariants

- Pusher loop never raises (fail toward normal probing on any pause-window error).
- UTC only — the box's pause timers are UTC.

### Out of scope

systemd pause/resume timers (work correctly), tdarr-server (stable), heartbeat guard (already correct;
becomes belt-and-braces). The pusher-INFO-not-in-journald observability gap is noted separately, not fixed here.

---

## Stage 2 — Verdicts

4 adversarial lens reviewers (Sonnet, isolated) + 1 cross-vendor seat (Gemini 3.5 Flash, on the
abstracted hour-math only — privacy gate satisfied). All reviewers required an executable artifact.

| Reviewer | Verdict | Sev | Artifact | Result |
|---|---|---|---|---|
| spec-conformance | **pass** | none | `_council_spec.py` (31 tests) | all 7 acceptance criteria confirmed; daily revival stopped |
| boundaries / time-math | **pass** | none | `_council_bounds.py` (25 tests) | 23:00 exclusive ✓, tz/UTC ✓; flagged naive-datetime hazard (minor, non-prod) |
| failure-recovery | **pass** | none | `_council_failrec.py` (11 tests) | resume path ✓, strikes cleared ✓, heartbeat guard semantically identical (no conflict) |
| independence | **pass** | none | `_council_indep.py` (7 tests) | full design convergence; rejected timer-driven-suppress alt; added config-drift lock |
| **cross-vendor (Gemini 3.5)** | math correct | minor | abstracted snippet | only finding = naive-datetime shift on CEST host; self-refuted its 2nd "bug" |

**Quorum (§7):** unanimous `pass` across all active lenses, zero blockers → gate open.

## Stage 3 — Arbitration

Two convergent **minor** findings folded in before commit (none were gate-blocking):

1. **Naive-datetime hardening** (Gemini + boundaries). The math was correct; the risk was API misuse
   (`_in_pause_window(now=<naive>)` would shift by the host offset). Resolved by **centralizing** the
   predicate as `suppression.in_pause_window(app, now=None)` which treats a naive datetime as UTC.
   Production was always safe (aware `_utcnow()`), but the footgun is now removed.

2. **Chokepoint guard** (arbiter diligence — *not* in any single lens's mandate). `deep_check.run_deep_check`
   and the Kuma webhook are a **second** recovery entry point that bypassed the pusher-only guard. They
   never fired during the pause in practice (window timer is Mon 13:00 UTC, no overlap — confirmed by
   notify.log showing *only* pusher 18:0x recoveries), but a maintenance window overlapping 18:00–23:00
   would have re-introduced the symptom. Guarded the **chokepoint** `recovery.trigger_async` → returns
   `"paused"` so *every* caller (pusher, deep_check, webhook) is covered at the source (defense-in-depth).

Final shape: one canonical predicate (`suppression.in_pause_window`) consumed by both the pusher's
push-up branch (Kuma green + skip probe) and `recovery.trigger_async` (skip restart). Config-drift
between the manifest window, the 50c systemd timers, and the heartbeat guard is now **machine-enforced**
by `test_pause_window_chokepoint.py::TestQuietHoursSourcesInSync`.

**Gate:** full suite **680 passed, 5 skipped** (was 672 pre-arbitration; +8 new tests). Committed.

## §9 Dissent Ledger

- **Defect caught (boundaries + cross-vendor, minor):** naive-datetime offset shift in the pause
  predicate. Artifact: `_council_bounds.py` UserWarning on UTC-7 host. Ruling: hardened (treat naive as
  UTC) in the centralized predicate. Not a blocker (no prod path).
- **Defect caught (arbiter, major-latent):** second recovery entry point (`deep_check`/webhook)
  bypassed the pusher-only guard. Ruling: guard the `recovery.trigger_async` chokepoint. Grounded by
  `test_pause_window_chokepoint.py::TestRecoveryChokepointHonorsPause`.
- **Unresolved dissent:** none. All lenses unanimous `pass`; arbiter additions were hardenings, not reversals.
- **Arbiter override:** none required (no blocker overridden).
- **Process note:** Stage 1 "blind generation" was right-sized to 1 independent design reviewer
  (the independence seat) rather than N worktree generators — per §2.6/§8 ("accuracy lives in
  independence + grounding, not headcount") and because the tree already held the reference candidate.
  It reported full design convergence.

### → Promote to CLAUDE.md / Stage-0 templates (feedback loop)

- **Gate:** "A service with an intentional pause/maintenance schedule must declare it in ONE place that
  *every* watchdog consults. Adding a second watchdog without porting the first's guards is the recurring
  failure mode here (heartbeat fixed 2026-05-30, pusher missed until 2026-06-12)."
- **Stage-0 template line:** "Enumerate ALL recovery/restart entry points (pusher, deep_check, webhook,
  heartbeat crons) and confirm the fix covers the chokepoint, not just the observed path."

# Maintenance-aware alerting — Sub-project B: suppression + user incident + upstream-maintenance email

**Date:** 2026-05-24
**Status:** Design (full-auto build authorized; pending parallel implementation)
**Part of:** the A→D maintenance-aware alerting effort. **Depends on A** (`lib/ucc.py`).
**Built on:** integration branch `feat/maint-aware-alerting` (contains A + the QuadstroNot status work).

## Context

A (`lib/ucc.py`) detects UCC upstream maintenance and maintains `ucc-window.json`
with an `active` boolean, flipping on debounced edges and logging them to
`ucc-transitions.jsonl`. A is detection-only: it does **not** suppress recovery,
touch Kuma incidents, or email customers. B is the responder that does all three,
keyed off A's state.

The storm that motivated this work is dominated by **operator-page noise**: while
UCC gates `app-<name> <op>`, recovery of a `ucc`-class app runs its 3-attempt loop
→ every attempt's `app-* start` is gated → loop exhausts → `_mark_permanently_failed`
→ `_emit("failed", … level="error")` → Discord operator ping. Multiplied across
every flapping ucc-class monitor, that is the page storm. Suppressing ucc-class
recovery while UCC is active kills it at the source; D's post-clear deep-check
re-heals anything left down.

## Design

Three independent pieces, all reading A's state via `ucc.status()`.

### B1 — Recovery suppression (`lib/suppression.py`, new)

A single shared predicate both recovery entry points consult.

```python
# lib/suppression.py
def ucc_active(*, state_path=None) -> bool:
    """True iff A's ucc-window.json says active. Best-effort; False on any error."""

def recovery_suppressed(app) -> bool:
    """True iff recovery for `app` should be skipped right now.
    Currently: app.class_ == 'ucc' AND ucc_active().
    ucc-class apps can't be started while the gate is up (app-* start is gated),
    so recovery would only churn to permanently-failed and page the operator."""
```

Rationale for scoping to `ucc`-class only: `systemd`/`cron` apps use
`systemctl --user` (not the gated `app-*` wrapper), so their recovery still works
during UCC maintenance and stays low-noise. Suppressing them too would needlessly
delay legitimate heals. (Documented tradeoff: a genuinely-down ucc-class app won't
auto-heal until the gate lifts — D's deep-check is the safety net.)

**Wiring (two call sites, minimal edits):**

- `lib/pusher.py` `push_once`: in the strike→`recovery_mod.trigger_async` block
  (~line 168), skip the trigger when `suppression.recovery_suppressed(app)` is
  True. Still push the real health status to Kuma (do **not** hide down state) and
  annotate the Kuma `msg` (e.g. `[ucc-maint: recovery suppressed]`) so the
  dashboard explains the held state. Do not increment toward permanent-failure.
- `lib/kuma.py` `KumaWebhookHandler._handle_event` (status==0 path): after the
  existing QFlix-window `lock` check and before `recovery.trigger_async`, skip
  recovery when `suppression.recovery_suppressed(app)` is True; record a
  `state.record(..., event="ucc_maint_recovery_suppressed")` and return.

### B2 — User-facing Kuma status-page incident

On `clear→active`, pin a public incident so subscribers see "upstream provider
maintenance — monitoring may be degraded." On `active→clear`, unpin it.

Mechanism mirrors the proven `scripts/ops/tautulli-gate-watch.sh` approach: the
`uptime_kuma_api` raw socket emit, which persists server-side without triggering
the buggy whole-status-page re-save on this Kuma version.
- Pin: `api._call("postIncident", slug, {"title", "content", "style": "warning"})`
  then `api._call("pinIncident", slug, <incident_id>)` if a separate pin call is
  required by this Kuma version (verify live — see Open items).
- Unpin: `api._call("unpinIncident", slug)` (exactly as the watcher does).
- Status-page slug: `"public"` (matches the watcher's `KUMA_SLUG`).
- Login: `UptimeKumaApi("http://127.0.0.1:<uptimekuma.port>")`,
  `api.login("quadstronaut", <htpasswd.password>)`. Best-effort, wrapped, logged;
  failure must never abort B's other side-effects.

Put this in `lib/ucc_incident.py` (new): `pin_maintenance_incident()` and
`clear_maintenance_incident()`, each self-contained and best-effort.

### B3 — Upstream-maintenance customer email

Two new Jinja templates mirroring the existing `maint-start`/`maint-complete`
pair, fired via the existing `listmonk.fire_template_campaign`.

- New files under `scripts/qflix-newsletter/qflix_newsletter/templates/`:
  `upstream-maint-start.html.j2`, `upstream-maint-complete.html.j2`. Copy the
  structure/styling of `maint-start.html.j2` / `maint-complete.html.j2` but reword
  for *upstream provider* maintenance ("our host is performing maintenance; some
  monitoring may look degraded; Plex/your requests should keep working"). Same
  `ctx` fields (`ctx.subject`, `ctx.public_host`, `ctx.kuma_public_host`,
  unsubscribe/browser links) so `sync.render_preview` renders them unchanged.
- Register in `sync.py` `TEMPLATE_TITLES`:
  `"upstream-maint-start.html.j2": "Upstream Maintenance Start"`,
  `"upstream-maint-complete.html.j2": "Upstream Maintenance Complete"`.
- Fire from the responder: on `clear→active`,
  `listmonk.fire_template_campaign(template_title="Upstream Maintenance Start",
  subject="QFlix — upstream provider maintenance in progress")`; on
  `active→clear`, the `"Upstream Maintenance Complete"` title. Fire-and-forget
  (listmonk already logs failures to `notify-fail.log` and returns False).

### B0 — The responder + edge wiring

`lib/ucc_response.py` (new): `respond(state, *, response_state_path=None) -> dict`.
- Keeps its own cursor file `ucc-response-state.json` (atomic write, same idiom as
  `ucc.write_state`) recording the last `active` value it acted on.
- Compares `state["active"]` to the cursor:
  - cursor False/absent → state active True  ⇒ **clear→active**: pin incident (B2)
    + fire "Upstream Maintenance Start" email (B3) + Discord notify.
  - cursor True → state active False ⇒ **active→clear**: unpin incident (B2) +
    fire "Upstream Maintenance Complete" email (B3) + Discord notify + **trigger D**:
    `from lib import deep_check; deep_check.run_deep_check(reason="ucc-clear")`
    inside try/except (ImportError or any exception → log + continue). *(This is the
    pinned B→D seam; D's spec defines the exact signature.)*
  - no change ⇒ no-op (idempotent — safe to call every cycle).
- Every side-effect best-effort; the cursor is written only after attempting them,
  so a transient failure retries next cycle.

**Trigger:** the existing `manitoba-maint ucc detect` path (A's `_cmd_ucc_detect`
in `cli.py`) calls `ucc_response.respond(state)` immediately after `ucc.detect()`
returns. B1's suppression is passive (read on each pusher/webhook cycle) and needs
no timer. No new systemd unit — B rides A's 5-min `manitoba-maint-ucc-detect.timer`.

## Error handling

- Every external effect (Kuma socket, listmonk, notify, deep_check) is wrapped;
  none aborts the others or the cursor write.
- Suppression predicate returns False on any read error (fail toward normal
  recovery, not toward silent suppression).
- Cursor file corrupt/missing → treat as "no prior action" (cursor active=False).

## Testing (TDD)

- `tests/unit/test_suppression.py`: `recovery_suppressed` True only for ucc-class +
  ucc_active; False for systemd/cron; False when state unreadable. Mock `ucc.status`.
- `tests/unit/test_ucc_response.py`: edge detection from cursor vs state (both
  edges + no-op), idempotency, and that each edge calls the right effects (mock
  `ucc_incident`, `listmonk.fire_template_campaign`, `notify`, `deep_check`); a
  failing effect doesn't block the others or the cursor write; the clear edge calls
  `deep_check.run_deep_check(reason="ucc-clear")`.
- `tests/unit/test_cli.py`: `ucc detect` invokes `ucc_response.respond` with the
  detect result (mock both).
- `tests/unit/test_pusher.py` / `test_kuma.py` (extend): with `recovery_suppressed`
  patched True, the strike-threshold path / webhook down path does **not** call
  `trigger_async`, still pushes status, and records the suppression event.
- Newsletter render test: the two new `.j2` templates render without error against
  the preview context (mirror the existing maint-template render test).
- Kuma incident pin/unpin: socket calls are mocked (no live Kuma in unit tests).

## Open items (live verification, post-merge)

- Confirm the exact Kuma socket events for *pinning* an incident on this Kuma
  version (`postIncident` + whether a separate `pinIncident` is needed). The unpin
  path is already proven by the watcher. If the API differs, adjust `ucc_incident.py`;
  the unit tests mock the seam so they stay valid.

## Out of scope

Alert collapse (C) and post-window deep-heal internals (D) — B only *invokes* D's
`run_deep_check` on the clear edge.

# QFlix Admin — Android remote-operations app (design)

**Date:** 2026-08-03 · **Status:** DESIGN
**Supersedes:** `docs/superpowers/specs/2026-07-15-heartbeat-android-design.md`
(QFlix Heartbeat v2 — read-only health dashboard, shipped 2026-07-16)

## Purpose

The operator no longer touches the stack day to day. When something does break,
the fix must not require being at the house. Heartbeat v2 answers *"is it
broken?"* from a phone; it cannot answer *"fix it."* This adds the fixing.

Heartbeat's Dashboard survives unchanged as one page of a larger app.

## Product constraint: QFlix is privacy-focused

**No member viewing activity is exposed in this app.** A "who is watching now"
tile was proposed and REJECTED by the operator on 2026-08-03. Plex and Tautulli
both record sessions; that is their business, and it is not replicated into an
admin tool that lives in a pocket.

This is enforced structurally, not by convention: **the dispatcher defines no
verb that returns session, watch-history, or per-member data.** The capability
is absent from the wire protocol, not merely unused by the UI. `scripts/mcp/
plex.py` supports a sessions snapshot; that mode is deliberately not reachable
through the dispatcher.

The stARR library view reports *content presence* ("do we have this episode"),
never *consumption* ("did anyone watch it").

**This constraint bites the `status` verb, and the spec originally contradicted
itself about it.** `app_status.py` emits five sections, and `top5` is
per-member by name: `top5_watch` returns `{"user": <friendly name>, "hours",
"plays"}` from Tautulli, and `top5_requests` returns `{"user": <display name>,
"count"}` from Seerr. An earlier draft of this table called `status` an
"unchanged contract" passthrough, which would have shipped exactly the data
this section forbids. `status` therefore requests
`--sections quota,kuma,streams,downloads` and never `top5`. The `streams`
section stays: it is aggregate counts (`streams`, `users`, `transcodes`,
`wan_kbps`), not identities.

**Pre-existing exposure, not introduced here:** Heartbeat v2's forced command
is `app-status.py` with no arguments, so the app already installed on the
operator's phone receives `top5` today. Reminting the key onto the dispatcher
(Task 9) is what actually closes that, because the dispatcher is the only
caller that filters.

Any future verb that would surface member activity is a spec change, not an
implementation detail.

## Blast radius — an explicit operator decision

Heartbeat v2's key was read-only **by construction**: `authorized_keys` forced a
single read-only command, so a stolen phone could emit health JSON and nothing
else.

That guarantee necessarily changes when the app gains start/stop/restart. The
operator was asked directly what a stolen, unlocked phone should be able to do
and answered **full blast radius accepted, 2026-08-03**, with the note "I know
what I'm asking."

The dispatcher pattern is retained anyway, because it was independently
requested ("all of the actions the phone app can initiate linked to specific
scripts on the server") and because it remains the single place where the set of
possible actions is written down. It is a manifest, not a security boundary.

## Architecture

```
[QFlix Admin]  --ssh ed25519, forced command-->  [scripts/mcp/dispatch.py]
                                                        |
                       +--------------------------------+-------------------+
                       |                |               |                   |
                  app_status.py    missing.py      unstick.py           logs.py
                  (dashboard)      (search all)    (fix stuck)          (output)
                       |                |               |                   |
                  arr_library_peek.py   arr_disk_usage.py    app-<slug> start|stop|restart
                                                             systemctl --user <unit>
```

**Transport:** unchanged from Heartbeat v2 — sshj, dedicated ed25519 keypair,
strict host-key pin, provisioning bundle in app-private storage. Nothing is
baked into the APK.

**The one change to `authorized_keys`:**

```
command="~/scripts/mcp/dispatch.py",no-pty,no-X11-forwarding,
no-agent-forwarding,no-port-forwarding,restrict ssh-ed25519 AAAA...
```

`restrict` and the `no-*` flags are kept. Only the forced command changes.

## `scripts/mcp/dispatch.py`

The single entry point. Reads `$SSH_ORIGINAL_COMMAND`, parses `<verb> [args]`,
dispatches, and emits one JSON envelope.

**Envelope (every verb, success or failure):**

```json
{
  "ok": true,
  "verb": "app.restart",
  "target": "sonarr",
  "verdict": "restarted sonarr (UCC app-sonarr restart)",
  "lines": ["...", "..."],
  "elapsed_s": 4.2
}
```

`lines` is capped (default 20, `--tail N` up to 200) so a mobile connection is
never asked to carry an unbounded log. `verdict` is always a single human
sentence — it is what the toast shows; `lines` is what the expandable panel
shows. This satisfies "verdict + last lines on demand."

**Verb table.** An unknown verb is an error with the full verb list in `lines`,
so the app can never silently no-op.

| Verb | Backing action |
|---|---|
| `status` | `app_status.py --sections quota,kuma,streams,downloads` — the Dashboard doc MINUS `top5` |
| `app.list` | the 24 lifecycle apps + their class |
| `app.start\|stop\|restart <slug>` | UCC → `app-<slug> <verb>`; systemd → `systemctl --user <verb> <unit>` |
| `arr.search_wanted <slug>` | `missing.py --slug <slug> --emit-json` |
| `starr` | **all four rows in one call** — `arr_library_peek.py` + `arr_disk_usage.py` for every *arr |
| `unstick <slug> <queue-id>` | `unstick.py --slug --queue-id --emit-json` |
| `logs <slug> [--tail N]` | `logs.py --app <slug> --emit-json` |
| `quota` | disk headroom for the Dashboard tile |

**One page, one round trip.** `starr` returns all four instances rather than
exposing per-instance `arr.peek` / `arr.disk` verbs. Per-row verbs would mean
eight SSH connections to paint one screen, each with full handshake cost, on
exactly the flaky mobile link this app exists to work over. Same reasoning keeps
`app.list` a single call for all 24 rows. Actions stay per-target because the
operator fires them one at a time and wants that verdict alone.

There is deliberately **no `cron.run` verb.** The Apps page lists 24 lifecycle
apps and does not show the 10 timer-driven jobs, so a cron verb would be
dispatcher surface no UI can reach — an untested path that rots. If cron
force-runs are wanted later, they arrive with the page that shows them.

**Deliberately absent:** any verb returning Plex sessions, watch history, or
per-member data. See the privacy constraint above.

**Lifecycle class is data, not a branch in the app.** `app.list` returns each
app's class (`ucc` / `systemd`), and the Apps page renders that class on the
row. The phone never decides how to start something — the dispatcher does, from
`manifest/apps.yaml`. This is what makes "if a UCC app it must use the approved
Ultra commands only" true rather than hoped for.

## Android app

**Identity:** `applicationId` `com.qflix.heartbeat` → `com.qflix.admin`,
`android:label` → "QFlix Admin". A changed applicationId means Android installs
this as a new app; the old one is uninstalled, not upgraded — the same flow used
when Heartbeat v2 replaced `com.qflix.heartbeat.debug`.

**Navigation:** a hideable drawer (Material 3 `ModalNavigationDrawer`) with three
destinations. Default destination is Dashboard, so existing muscle memory is
preserved.

### Dashboard

Heartbeat v2's view, plus two additions:

- **Reds first.** Any Kuma monitor currently DOWN is pinned to the top, and each
  red links directly to the action most likely to clear it (a wedged app → its
  restart; a stuck queue → unstick). Alert to fix in two taps.
- **Quota tile.** Used / total GB and percent. The box sits near 78%; add-date
  retention is what keeps it alive, so the number is worth seeing.

### Apps

24 rows — 18 UCC + 6 systemd. Per row: name, class badge (`UCC` / `systemd`),
current state, and start / stop / restart. Tapping an action shows a verdict
toast; the row expands to the last 20 lines on demand.

Cron and library/canary entries are **not** listed. They have no lifecycle, and
a page full of rows with nothing to press is worse than a shorter honest one.

### stARR

Four rows: `sonarr`, `sonarr2`, `radarr`, `radarr2`, all painted from a single
`starr` call. Per row:

- **Search all wanted** — fires `missing.py` for that instance.
- **Library peek** — coarse, deliberately: `Show X 12/30`, `Movie Y ✓`. Presence
  and counts only. Not full library statistics; the operator asked for "just a
  peek into the status."
- **Disk consumed** — bytes managed by that instance.
- **Open in browser** — plain URL launch, no credential replay. Autologin was
  considered and rejected: *arr uses forms auth, so it would mean storing
  credentials and replaying a POST, which breaks on every upgrade.

## New server-side scripts

| Script | Job |
|---|---|
| `scripts/mcp/dispatch.py` | verb router + JSON envelope; the forced command |
| `scripts/mcp/arr_library_peek.py` | per-title presence/counts, coarse, one *arr |
| `scripts/mcp/arr_disk_usage.py` | bytes managed by one *arr |

Lifecycle wrapping lives inside `dispatch.py` — it is a `manifest/apps.yaml`
lookup plus one subprocess, not a module.

Each follows the existing MCP conventions: stdlib-only where possible,
`--emit-json`, per-section failure isolation, and a non-zero exit that is
distinguishable from an empty-but-healthy result.

## Testing

- **Dispatcher:** unit tests per verb — happy path, unknown verb, unknown slug,
  wrong app class, backing script non-zero. A test asserting the verb table
  contains **no** session/watch verb, so the privacy constraint fails CI if
  someone adds one.
- **Lifecycle routing:** a UCC slug must produce an `app-<slug>` call and a
  systemd slug a `systemctl --user` call — asserted against a fake runner, so
  the "approved commands only" rule is enforced by test, not by comment.
- **Android:** existing JVM unit tests extend to the new view states. Transport
  stays behind `StatusTransport`, so pages are testable against `FakeTransport`
  with no device.
- **Provisioning:** re-runnable script mints the key, patches `authorized_keys`,
  and pins the host key over the already-authenticated channel.

## Out of scope

- Autologin to *arr web UIs.
- Any member viewing activity (see the privacy constraint).
- Push notifications — Kuma already pages via Discord; duplicating that is a
  second alert path to keep in sync.
- Editing *arr settings from the phone. Read, trigger, and lifecycle only.
- Tdarr / Plex library management beyond what the Dashboard already reports.

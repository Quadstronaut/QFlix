# `manitoba-maint status --json` — QuadstroNot status contract

**Date:** 2026-05-24
**Status:** Design (contract supplied fixed; pending spec review)
**Scope:** Add a `--json` output mode to the `status` subcommand. This is a
**cross-repo interface**, consumed by QuadstroNot. Treat the JSON shape as the
contract; field names do not change without a `schema_version` bump.

## Context

QuadstroNot (the Discord bot on `quadstronix.dev`) is getting an on-demand
"is the QFlix stack up?" command. Architecture is **thin client**: QuadstroNot
SSHes into `quadstronaut@seedbox.example.com`, runs
`manitoba-maint status --all --json`, parses stdout, and renders a Discord
dashboard. The seedbox stays the single source of truth — QuadstroNot copies no
QFlix code, ships no QFlix manifest. The only thing QFlix owes it is a stable,
machine-readable status payload.

This is purely additive to `_cmd_status` (`scripts/maint/lib/cli.py`). The
existing human table path is unchanged.

## What already exists (grounding, read 2026-05-24)

`_cmd_status` (cli.py ~line 92) already:

- resolves a single app (`manifest.app(name)`) or all apps (`manifest.apps()`,
  33 apps — **canaries excluded**, which matches the contract);
- probes every app in parallel (`ThreadPoolExecutor`, `health.probe(app)`);
- builds rows `{app, class_, ok, latency_ms, last_recovery}`;
- formats `last_recovery` as `f"{event} ({updated_at[:10]})"` from
  `state.json` (`""` when no recovery on record) — already the contract's
  `"restart (2026-05-20)"` shape;
- sorts rows by app name (deterministic);
- returns exit 0 unconditionally; single unknown app returns 1.

`health.HealthResult.latency_ms` is already `None` for non-HTTP probe kinds
(`systemd_only`, `systemd_oneshot`, `import_check`, `process_pattern`).

`App` (manifest.py) carries `class_: str`, `kuma_monitor: Optional[str]`, and
`health: HealthConfig` with `health.kind: str`. `health.kind` is one of **seven**
values: `http_api`, `http_root`, `systemd_only`, `systemd_oneshot`,
`port_listen`, `import_check`, `process_pattern`.

## Design

### Flag

Add `--json` to the `status` subparser. Applies to both forms:

- `manitoba-maint status --all --json` → all 33 manifest apps.
- `manitoba-maint status <app> --json` → that one app (summary totals = 1).

When `--json` is set, `_cmd_status` emits **only** the JSON object to stdout and
suppresses `_render_status_table`. Nothing else may touch stdout in that path —
SSH stdout must be pure parseable JSON. Any diagnostics go to **stderr**.

### Row → JSON field mapping

The probe loop already yields the row dict. Extend `_probe_one` to also capture
`display` and `probe_kind`, then serialize:

| JSON field      | Source                                                        |
|-----------------|---------------------------------------------------------------|
| `app`           | `app.name`                                                    |
| `display`       | `app.kuma_monitor` if set, else `app.name` (fallback)         |
| `class`         | `app.class_` (Python keyword → JSON key `class`)              |
| `probe_kind`    | `app.health.kind` (verbatim; all 7 kinds, no enum gate)       |
| `ok`            | `result.ok`                                                   |
| `latency_ms`    | `result.latency_ms` (`null` for non-HTTP probes)              |
| `last_recovery` | existing formatted string (`""` when none)                    |

### Exact contract (`schema_version` 1)

```json
{
  "schema_version": 1,
  "captured_at": "2026-05-24T18:03:11Z",
  "summary": { "total": 33, "up": 31, "down": 2 },
  "apps": [
    {
      "app": "sonarr2",
      "display": "Sonarr Anime",
      "class": "ucc",
      "probe_kind": "http_api",
      "ok": true,
      "latency_ms": 42,
      "last_recovery": "restart (2026-05-20)"
    }
  ]
}
```

- `captured_at` — UTC, ISO-8601 with trailing `Z`, captured once at the start of
  the probe run.
- `summary.total` = `len(apps)`; `summary.up` = count of `ok == true`;
  `summary.down` = `total - up`.
- `apps` sorted by `app` key (existing deterministic order).
- `display` falls back to the app key when `kuma_monitor` is `null`.
- `latency_ms` is `null` for non-HTTP probes.
- `last_recovery` is `""` when none.
- Serialize with `json.dumps(..., sort_keys=False)` and an explicit field order
  (or build an ordered dict) so the top-level key order matches the contract for
  readability; QuadstroNot parses by key, not position, so order is cosmetic.

### Exit code

- **0** whenever the probe run completed — up/down lives in the payload, never in
  the exit code. (Current behavior; unchanged.)
- **Non-zero** only when the probe could not run at all:
  - unknown app name (single-app form) → `1` (existing behavior; in `--json`
    mode the error message still goes to **stderr**, stdout stays empty).
  - manifest load failure → existing non-zero from `main()`.

  Per-app probe failures never raise (`health.probe` catches all network/
  subprocess errors), so once the manifest loads, `--all` always exits 0.

## Error handling

- Manifest unavailable: `main()` already fails before `_cmd_status` runs; no JSON
  is emitted. The bot treats empty/non-JSON stdout + non-zero exit as "couldn't
  reach the stack."
- A single app's probe erroring is normal data: `ok == false` with a `null`
  latency. It does not change the exit code.
- `--json` must never let a stray `print` leak into stdout. Audit the status path
  for incidental prints; route any to stderr.

## Testing

Extend the existing status/CLI test suite with a `--json` case asserting:

- top-level keys exactly `{schema_version, captured_at, summary, apps}`;
- `schema_version == 1`;
- `summary.total == len(apps)` and `summary.up + summary.down == total`, with
  `up` matching the count of `ok` rows (use a fixture with a known up/down mix);
- `display` falls back to `app` when `kuma_monitor` is `null`;
- `probe_kind` is carried through verbatim (include a non-HTTP app so a `null`
  `latency_ms` is exercised);
- `last_recovery` is `""` for an app with no recovery and the
  `"event (YYYY-MM-DD)"` form for one with state;
- stdout parses as JSON and contains **no** table text (no `APP`/`LAST RECOVERY`
  header, no `✓`/`✗`) when `--json` is set;
- single-app form (`status <app> --json`) yields `summary.total == 1`.

Probes are mocked (as in the existing suite); no live network.

## Out of scope / future

- **UCC maintenance state** is deliberately **not** in `schema_version` 1. Once
  sub-project A lands `ucc-window.json`, "stack is up but the host is in
  upstream maintenance" is exactly what the bot will want to show — that becomes
  a `schema_version` 2 addition (e.g. a top-level `upstream_maintenance` block),
  flagged to QuadstroNot as a contract change. Not built here.
- Canaries stay out of `status --all` (unchanged); no canary rows in the payload.
- No new transport, auth, or endpoint — QuadstroNot's SSH access already exists.

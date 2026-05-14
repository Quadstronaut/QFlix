# QFlix MCP — diagnostics, log fix, and unstick hardening

Date: 2026-05-13
Status: Approved for implementation planning

## Problem

During a routine farm scour, three issues blocked diagnosis from inside the MCP envelope:

1. **`qflix_unstick_torrent` hangs at 60s SSH timeout** for every invocation — including `--dry-run`. The autonomous collector exhibits the same symptom (3 candidates flagged ≥3h with `acted_on_at: null`, 0 actions in 24h). The 3 candidates as of 2026-05-13: `bdb9fa863641e6dba1f1f4db6961fdf41b8e53fe` (Bob's Burgers Movie / Radarr), `62ccd57824da9cd96444759142df13361cccc5df` (Blue Mountain State S03E01 / Sonarr), `b1ae88c9451923f61f7b9bd28029d7a2dea8d3c0` (Blue Mountain State S03 pack / Sonarr).
2. **`qflix_get_logs` returns empty for every app slug.** It reads from `B:\QFlix\data\logs\<date>\<app>.log`, a directory that is never populated. The real `scripts/mcp/logs.py` on the seedbox knows how to tail real logs (file routes + journalctl routes) but no MCP tool invokes it.
3. **There is no MCP tool to discover valid log slugs** or to probe `unstick.py`'s pre-flight phases.

Five additional torrents are stuck in `metaDL` state with `size=0, sizeleft=0` for >24h ("qBittorrent is downloading metadata"). The current stale-rule engine only matches `stalledDL`, so these are not autonomously cleared.

## Goals

- Make the unstick path actually fire (find and fix the hang).
- Give the MCP server enough diagnostic surface that the next outage can be triaged without leaving the envelope.
- Auto-handle the dead-magnet (`metaDL>24h`) class going forward.
- Keep the existing successful paths untouched.

## Non-goals

- Backfilling the dead `B:\QFlix\data\logs` cache directory (the rewrite makes it irrelevant).
- Touching the other slow-but-progressing downloads (Mayfair Witches, Happy Face — those are working as designed).
- Any restructure of `collect.py` beyond adding one stale rule.
- Any change to `lib/arr_client.py` beyond exposing a configurable timeout.

## Architecture

No new layers. Two-layer model preserved:

- **Local (Windows host)** — `scripts/local/qflix-mcp/qflix_mcp.py` registers FastMCP tools; `lib/ssh.py` wraps subprocess SSH.
- **Remote (manitoba seedbox)** — `scripts/mcp/*.py` runs via SSH with `--emit-json`.

Two new MCP tools, three modified MCP tools, one hardening change to the SSH wrapper, two new flags on existing remote scripts, one refactor of `unstick.py`, and one new stale rule in `collect.py`.

## Components

### 1. `scripts/local/qflix-mcp/lib/ssh.py` — timeout hardening

Wrap `subprocess.run` in `try/except subprocess.TimeoutExpired`. On timeout return a synthetic `CompletedProcess` with:
- `returncode = 124`
- `stdout = ""`
- `stderr = f"ssh-timeout after {timeout}s"`

Signature unchanged. All current callers continue to work; they just stop crashing on slow remote commands.

### 2. `scripts/local/qflix-mcp/qflix_mcp.py` — tool changes

**Rewrite `qflix_get_logs`** to SSH-invoke the real script:

```
def qflix_get_logs(app: str, since: str = "24h", tail: int = 500,
                   grep: Optional[str] = None) -> dict:
```

- Drops the `date` parameter (replaced by `since`, matching `logs.py`'s flag).
- SSH-invokes `python3 ~/scripts/mcp/logs.py --emit-json --app <app> --since <since> --tail <tail>`.
- Parses the JSON response. Applies `grep` client-side over `line.message`.
- Returns the full structured result `{app, source, lines}` — not a bare list, so callers can see which file/unit was tailed.

**New `qflix_list_log_apps()`**:

- SSH-invokes `python3 ~/scripts/mcp/logs.py --emit-json --list-apps`.
- Returns `{file_apps: [...], systemd_apps: [...]}`.

**New `qflix_diagnose_unstick(slug, hash_)`**:

- SSH-invokes `python3 ~/scripts/mcp/unstick.py --emit-json --diagnose --slug <s> --hash <h>` with `ssh_call(..., timeout=180)`.
- Returns the structured phase timings (see §4).

**Modify `qflix_unstick_torrent`**:

- Add `timeout: int = 120` parameter (was hardcoded 60 in `ssh_call`).
- Default raised from 60 → 120.

**All three write tools** (`unstick`, `trigger_missing_search`, `refresh_collect`):

- When `proc.returncode == 124`, return `{status: "ssh-timeout", timeout_s: N}` instead of the current `ssh-failed` shape.

### 3. `scripts/mcp/logs.py` — add `--list-apps`

New mutually-exclusive flag alongside `--emit-json` / `--cron`. When passed:

- Print `{"file_apps": sorted(_FILE_LOGS.keys()), "systemd_apps": sorted(_SYSTEMD_LOGS.keys())}` to stdout.
- Exit 0.

No other changes to the script.

### 4. `scripts/mcp/unstick.py` — `--diagnose` + structural refactor

**Split `run()`** into four functions, each with one responsibility:

- `_preflight(slug, state_file, max_actions_per_day) -> Optional[dict]` — returns a refusal dict if any guard trips, else `None`. Encapsulates the `is_arr_red` check and the per-day cap.
- `_resolve_queue_item(c, *, hash_, queue_id) -> dict` — returns one of: `{found, item}` / `{already_removed}` / `{queue_fetch_failed, code}`.
- `_execute_delete(c, queue_id, dry_run) -> dict` — returns `{status, code?}`. The single point where the destructive DELETE lives.
- `_record_event(...)` — always called, even on refused/timed-out paths. Today it's only called in the success branch of `run()`.

`run()` becomes a thin orchestrator that calls these in order and emits the event regardless of which branch returns.

**Fix the lookup query** in `_resolve_queue_item`:

The current code calls `c.get("/queue", query="pageSize=500&includeUnknownSeriesItems=true")`. The working `qflix_arr_queue` cache uses the *arr default query shape and returns fast. Drop the extra query params; iterate paginated results if `totalRecords > pageSize` is observed. This is the strongest candidate for the root cause.

**Add `--diagnose`** as a separate flag (not part of the existing `--emit-json` / `--cron` mode group; it is passed alongside `--emit-json` from the MCP path):

When `--diagnose` is passed, instead of running the action:

1. Time `is_arr_red(slug)` → `phases.state_read_ms`.
2. Time the slow current-shape query `c.get("/queue", query="pageSize=500&includeUnknownSeriesItems=true")` → `phases.queue_lookup_paged_ms`, `queue_size_paged`.
3. Time the default-shape query `c.get("/queue")` → `phases.queue_lookup_default_ms`, `queue_size_default`.
4. Time `_resolve_queue_item(c, hash_=hash)` against the (now-default) query → `phases.hash_match_ms`.
5. Print `{status: "diagnose", slug, hash, phases, queue_size_paged, queue_size_default}`.

No DELETE. No event row written.

**Surface `ArrClient` timeout**:

Add a `timeout: int = 30` kwarg to `ArrClient.__init__`. The client stores it and uses it for both `.get` and `.delete`. Default 30s. `unstick.py` instantiates `ArrClient(slug, version, timeout=15)` so a slow *arr GET fails fast rather than bleeding into the SSH window.

### 5. `scripts/mcp/collect.py` — new stale rule

Existing stale-rule engine matches `stalledDL` after 3 consecutive zero-movement hours. Add a parallel rule:

- **Rule name**: `meta-stuck`
- **Match**: torrent state `metaDL` AND `size == 0` AND `sizeleft == 0` for ≥ 24 consecutive hourly samples (i.e. one full day).
- **Outcome**: marks `candidate_for_unstick = true` with `rule_matched = "meta-stuck"`.

The existing autonomous DELETE path then handles them. The 5 current entries (as of 2026-05-13) can be unstuck manually via `qflix_unstick_torrent` once the hang fix lands:
- Radarr: `ec47ce4115d23d287b7b387706360342057a4ca1` (Dragonkeeper 2024)
- Radarr: `64875cd622d574bc9763d222bf863ee3221b12c3` (hash-named movieId 340)
- Sonarr: `a2c1277f2aba2a522925bf7c6de952816827a674` (NYPD Blue S01)
- Sonarr: `8bb3bb41b8a9ce51d294cbae1b57d22c587b307b` (NYPD Blue S01E14)
- Sonarr: `66e908daa8e60dcfee477dedee55bcefe41f1e08` (NYPD Blue S01)

## Data flow

Unchanged. Three more remote-script invocations (`logs.py` for real, `logs.py --list-apps`, `unstick.py --diagnose`) join the existing set.

## Error handling

- All SSH timeouts now structured as `{status: "ssh-timeout", timeout_s: N}`. No bare exceptions to the MCP framework.
- Logs tool: unknown app returns `{error: "unsupported", lines: []}` from the host script; the MCP wrapper passes it through. Discovery via `qflix_list_log_apps`.
- Diagnose tool returns `status: "diagnose"` — distinct from any action outcome, so consumers can't mistake a probe for a real unstick.
- `unstick.py` event row is always written, so `qflix_recent_events` reflects every attempt including refusals and timeouts.

## Testing

End-to-end against the live host (no automated tests; this is operational tooling):

1. **Hardening check**: `qflix_list_log_apps()` → returns non-empty list.
2. **Logs fix**: `qflix_get_logs(app="qbittorrent", since="6h", tail=50)` → returns parsed lines from `~/.apps/qbittorrent/.../qbittorrent.log`.
3. **Diagnose**: `qflix_diagnose_unstick(slug="radarr", hash="bdb9fa…")` → returns timings in < 60s. Expect `queue_lookup_paged_ms >> queue_lookup_default_ms` if hypothesis is correct.
4. **Hang fix**: trigger `qflix_unstick_torrent` on all 3 stale hashes — all return `deleted+blocklisted` within the bumped 120s window.
5. **Verify**: `qflix_list_stale` shows `acted_on_at` populated for the 3 hashes.
6. **Metadata rule**: trigger `qflix_refresh_collect` after the rule lands; verify the 5 metadata-stuck magnets either show up as candidates (if 24h has elapsed) or are otherwise unaffected. Manually unstick the 5 via `qflix_unstick_torrent` regardless.
7. **Event coverage**: deliberately call `qflix_unstick_torrent` with a bogus hash; verify a refused-row appears in `qflix_recent_events`.

## Risk

- `--diagnose` and `--list-apps` are pure-read additions; zero risk to existing paths.
- The `unstick.py` refactor is the highest-risk change. Mitigation: the new structure is a pure decomposition of the current single function; the lookup-query fix is the only behavioral change, and it's testable in isolation via `--diagnose`.
- The `ArrClient` timeout addition could mask a previously-silent slow path if set too low. 15s is generous compared to a healthy *arr (<1s typical).
- The new stale rule is gated on a strict 24-consecutive-hour condition; it will not fire on transient `metaDL` states.

## Implementation order

1. `lib/ssh.py` timeout catch (no callers see breakage).
2. `logs.py --list-apps` + `qflix_list_log_apps` + rewrite `qflix_get_logs` (independent of the unstick work).
3. `unstick.py` refactor + `--diagnose` + `ArrClient` timeout + `qflix_diagnose_unstick`.
4. Run diagnose against live host; confirm root cause.
5. Apply the query-shape fix; bump unstick MCP timeout; verify 3 stale unsticks fire.
6. `collect.py` `meta-stuck` rule; manual unstick of the 5 current dead magnets.

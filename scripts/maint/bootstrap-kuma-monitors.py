#!/usr/bin/env python3
"""bootstrap-kuma-monitors — one-shot: log in to Kuma over a tunneled
socket.io connection and create one PUSH monitor per `kuma_monitor` value
in manifest/apps.yaml. Push tokens are extracted and saved into
secrets/kuma-push-tokens.json so a maintenance-side push-loop can find
them.

WHY push instead of HTTP: Uptime Kuma 2.x runs in a separate net
namespace on Ultra.cc; from inside Kuma, `127.0.0.1` is the container's
loopback, NOT the host's, so it can't reach apps that bind to host
loopback. Push monitors invert the flow — `manitoba-maint` (host
netns) probes apps and POSTs results to Kuma's `/api/push/<token>`.

Idempotent: skips apps whose Kuma monitor already exists; for the new
ones, captures the freshly-generated push token.

Tunnel must already be open: `ssh -fN -L 42005:127.0.0.1:42005 manitoba`.

Usage:
  PYTHONPATH=scripts/maint tests/.venv/Scripts/python.exe \\
      scripts/maint/bootstrap-kuma-monitors.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "maint"))
# When running on the seedbox the script lives at ~/scripts/maint/ — parents[2]
# resolves to ~/, where there is no checked-out repo. Also add ~/scripts/maint
# directly so `from lib.*` works without a manifest/ sibling.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.manifest import load as manifest_load  # noqa: E402


def _resolve_manifest_path() -> Path:
    """Honor MANITOBA_MANIFEST; otherwise prefer the deployed copy
    (~/.opt/maint/apps.yaml) before falling back to the in-repo location."""
    env = os.environ.get("MANITOBA_MANIFEST")
    if env:
        return Path(env).expanduser()
    deployed = Path("~/.opt/maint/apps.yaml").expanduser()
    if deployed.is_file():
        return deployed
    return REPO_ROOT / "manifest" / "apps.yaml"


KUMA_URL = os.environ.get("KUMA_URL", "http://127.0.0.1:42005")
USER = "quadstronaut"


def _read_secret(name: str) -> str:
    return (REPO_ROOT / "secrets" / name).read_text().strip()


# Apps whose manifest health uses `port_source` (env_file/json_file) but
# which we have a static fallback secret for. The static port matches the
# port_source target at install time — fine for monitor URLs.
# (Conjurr + Newsletterr removed 2026-05-15 — both apps purged 2026-05-11.)
PORT_SOURCE_FALLBACK = {
    "tdarr-server": "tdarr.server_port",
}


def _resolve_port_for_app(app) -> int | None:
    """Return the loopback port the monitor should probe for `app`.
    Mirrors lib/health.py — reads port_secret from manifest's raw health
    config, with a small static fallback for port_source-driven apps."""
    raw = app.health.raw if hasattr(app.health, "raw") else {}
    port_secret = raw.get("port_secret")
    if port_secret:
        try:
            return int(_read_secret(port_secret))
        except (FileNotFoundError, ValueError):
            return None
    # port_source fallback (env_file / json_file) — use the same value
    # captured at install time in secrets/*
    fallback = PORT_SOURCE_FALLBACK.get(app.name)
    if fallback:
        try:
            return int(_read_secret(fallback))
        except (FileNotFoundError, ValueError):
            return None
    return None


def _build_monitor_url(app, port: int) -> str:
    """Build a useful HTTP URL Kuma should probe."""
    raw = app.health.raw if hasattr(app.health, "raw") else {}
    kind = raw.get("kind", "")
    path_template = raw.get("path_template", "")
    urlbase_secret = raw.get("urlbase_secret")
    urlbase = ""
    if urlbase_secret:
        try:
            urlbase = _read_secret(urlbase_secret)
        except FileNotFoundError:
            urlbase = ""
    if kind == "http_api" and path_template:
        # Substitute {urlbase} and clean up duplicate slashes (but preserve
        # the leading "//" of e.g. http:// — we already prefixed
        # http://host:port so it's safe).
        path = path_template.replace("{urlbase}", urlbase)
        while "//" in path:
            path = path.replace("//", "/")
        if not path.startswith("/"):
            path = "/" + path
        return f"http://127.0.0.1:{port}{path}"
    return f"http://127.0.0.1:{port}/"


def _expected_status(app) -> list[str]:
    raw = app.health.raw if hasattr(app.health, "raw") else {}
    expect = raw.get("expect_status")
    if expect:
        return [str(expect)]
    # http_root probes accept 200/302/401 in our health.py — Kuma needs
    # ranges like "401-401" for individual codes outside 200-299.
    return ["200-299", "301-301", "302-302", "401-401"]


def _login(api, candidates):
    last_err = None
    for pw_name, pw in candidates:
        try:
            api.login(USER, pw)
            return pw_name
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"all logins failed; last error: {last_err}")


# Heartbeat (interval) per monitor, in seconds. Kuma flips a PUSH monitor
# DOWN if no ping arrives within `interval` seconds. App monitors push every
# 60s via manitoba-maint-pusher → 90s buffer is right. Canary monitors fire
# on schedule (hourly / 15-min / daily) so the heartbeat must be ≥ schedule
# + a buffer or Kuma will mark them down between scheduled runs.
APP_HEARTBEAT_S = 90

# Heartbeat MUST exceed (schedule period + the timer's RandomizedDelaySec) or Kuma
# flips the monitor to a false "no heartbeat" DOWN whenever the jitter grows between
# two runs. Retuned 2026-06-27 from 40 days of beat history: the old 60s buffers were
# smaller than the 120-240s timer jitter and caused ~40% false-down flapping (e.g.
# Stale-Log Watchdog + Kometa Libraries were 100% missed-heartbeat, ZERO real fails).
# every-30min / every-10min were also missing entirely (defaulted to the hourly buffer).
CANARY_HEARTBEAT_S_BY_SCHEDULE = {
    "every-10min": 900,    # 10min + jitter + margin
    "every-15min": 1500,   # 15min + jitter + margin
    "every-30min": 2400,   # 30min + jitter + margin
    "hourly": 4200,        # 1h + RandomizedDelaySec (<=240s) + margin
    "daily-0430": 90000,   # 24h + buffer
    "daily": 90000,        # 24h + RandomizedDelaySec + buffer (arr-plex-parity)
    # weekly-mon-send: the newsletter-digest canary pushes only on Mondays (3x:
    # 14:20/14:50/15:20 UTC). The gap between the last push of one Monday and the
    # first of the next is ~6d23h (601140s), so a 4200s default flips Kuma to a
    # false "No heartbeat in the time window" DOWN every non-Monday (the 2026-07-13
    # finding). 8 days (691200s) clears the weekly gap with a 1-day margin while
    # still surfacing a genuinely-missed Monday within ~1 day.
    "weekly-mon-send": 691200,  # 7d gap + 1d buffer
}


def _heartbeat_for_canary(canary) -> int:
    return CANARY_HEARTBEAT_S_BY_SCHEDULE.get(canary.schedule, 4200)


def _add_push_monitor(api, name: str, interval: int = APP_HEARTBEAT_S) -> str:
    """Create a Kuma PUSH monitor named `name`. Returns the push token.

    Replicates UptimeKumaApi.add_monitor's body but splices in the
    `conditions=[]` field that Kuma 2.3.x requires (uptime-kuma-api 1.2.1
    doesn't pass it on its own)."""
    from uptime_kuma_api.api import _convert_monitor_input, _check_arguments_monitor
    from uptime_kuma_api.event import Event
    from uptime_kuma_api import MonitorType

    data = api._build_monitor_data(
        type=MonitorType.PUSH,
        name=name,
        interval=interval,
        maxretries=0,
    )
    data["conditions"] = []
    _convert_monitor_input(data)
    _check_arguments_monitor(data)
    with api.wait_for_event(Event.MONITOR_LIST):
        api._call("add", data)
    # Re-fetch the monitor to grab the auto-generated pushToken.
    for m in api.get_monitors():
        if m["name"] == name and m.get("type") and str(m["type"]).lower().endswith("push"):
            return m.get("pushToken", "")
    return ""


def _ensure_monitor_interval(api, monitor: dict, expected_interval: int) -> bool:
    """If `monitor`'s interval != expected, edit it. Returns True if changed."""
    cur = monitor.get("interval")
    if cur == expected_interval:
        return False
    try:
        # uptime-kuma-api 1.2.1: edit_monitor(id, **fields) — pass interval only.
        api.edit_monitor(monitor["id"], interval=expected_interval)
        print(f"  [edit]{monitor['name']:25s} interval {cur}s → {expected_interval}s")
        return True
    except Exception as exc:
        print(f"  [edit-FAIL]{monitor['name']:25s} {exc}")
        return False


_AUTOHEAL_WEBHOOK_NAME = "Manitoba auto-heal webhook"


def _autoheal_webhook_url() -> str:
    """Build the webhook URL the maintenance receiver listens on. Reads
    secrets/maintenance.port locally; defaults to 42017 if absent."""
    try:
        port = _read_secret("maintenance.port")
    except FileNotFoundError:
        port = "42017"
    return f"http://127.0.0.1:{port}/kuma"


def _ensure_autoheal_webhook(api) -> None:
    """Idempotent: ensure the maintenance auto-heal webhook notification
    exists, points at the right URL, and is attached to every monitor.
    Without this, Kuma down events never reach manitoba-maint-webhook,
    so lib.recovery never fires for app outages."""
    from uptime_kuma_api import NotificationType

    url = _autoheal_webhook_url()
    existing = [n for n in api.get_notifications() if n.get("name") == _AUTOHEAL_WEBHOOK_NAME]
    if existing:
        print(f"  [skip]{_AUTOHEAL_WEBHOOK_NAME} (id={existing[0].get('id')})")
        return

    res = api.add_notification(
        name=_AUTOHEAL_WEBHOOK_NAME,
        type=NotificationType.WEBHOOK,
        webhookURL=url,
        webhookContentType="application/json",
        applyExisting=True,
        isDefault=True,
    )
    print(f"  [add]{_AUTOHEAL_WEBHOOK_NAME} → {url} (id={res.get('id')})")


def _ensure_notifications_attached(api) -> int:
    """Attach the standard notification set to every active monitor missing it.

    WHY this exists (2026-07-28): `_ensure_autoheal_webhook` attaches itself with
    `applyExisting=True`, but ONLY on the run that first creates it. Every later
    run hits the `[skip]` branch, so every monitor created AFTER that day was born
    with zero notifications — no Discord alert AND no auto-heal webhook, which is
    the exact thing that webhook's own docstring warns about ("Kuma down events
    never reach manitoba-maint-webhook, so lib.recovery never fires").

    By 2026-07-28 that was 32 of 60 active monitors — including 15 of 18 canaries,
    the `QFlix Fleet` storm aggregate, the `Manitoba Pusher` self-heartbeat, and
    all four self-pushing janitors. They could go red in total silence. Found while
    verifying that the new tdarr-healthcheck canary could actually reach the
    operator; it could not.

    Reconciles on EVERY run instead of only at creation, so a monitor added later
    can never again be born mute. Idempotent: monitors already carrying the full
    set are skipped."""
    try:
        notifications = api.get_notifications()
    except Exception as exc:
        print(f"  [warn] could not list notifications: {exc}")
        return 0
    # The standard set is every default channel (Discord + auto-heal webhook).
    # Derived, not hardcoded, so adding a default channel in the UI propagates.
    default_ids = sorted(n["id"] for n in notifications if n.get("isDefault"))
    if not default_ids:
        default_ids = sorted(
            n["id"] for n in notifications
            if n.get("name") in ("Mission Control - QFlix", _AUTOHEAL_WEBHOOK_NAME)
        )
    if not default_ids:
        print("  [warn] no default notification channels found — nothing to attach")
        return 0

    fixed = 0
    for m in api.get_monitors():
        if not m.get("active", True):
            continue
        current = set(m.get("notificationIDList") or {})
        current = {int(k) for k, v in (m.get("notificationIDList") or {}).items() if v} \
            if isinstance(m.get("notificationIDList"), dict) else set(current)
        missing = [i for i in default_ids if i not in current]
        if not missing:
            continue
        merged = {str(i): True for i in sorted(set(default_ids) | current)}
        try:
            api.edit_monitor(m["id"], notificationIDList=merged)
            print(f"  [notify]{m['name']:32s} + channels {missing} (was {sorted(current) or 'NONE'})")
            fixed += 1
        except Exception as exc:
            print(f"  [notify-fail]{m['name']:32s} {exc}")
    return fixed


def _live_push_token(api, monitor):
    """Ask the SERVER for `monitor`'s pushToken. None if it cannot be had.

    Same cache problem as _confirm_mute_live, different casualty: on the run
    that CREATES a monitor, `get_monitors()` returns it without a pushToken, so
    the token map was written with that key absent and the run ended FATAL. The
    documented remedy was "re-run this script" — every new canary therefore
    needed two installer passes, and an operator who ran it once and stopped
    shipped exactly the tokenless canary the FATAL exists to prevent (a push to
    a tokenless monitor exits 0 while the monitor sits DOWN forever).

    A single `getMonitor` round trip has the token the moment it exists."""
    mid = (monitor or {}).get("id")
    if mid is None:
        return None
    try:
        return api.get_monitor(mid).get("pushToken") or None
    except Exception as exc:
        print(f"  [warn] could not read pushToken for id={mid} live: {exc}")
        return None


def _resolve_token(api, fresh, created_tokens, mon_name):
    """Push token for `mon_name`, cheapest source first: cached list, then the
    add-response captured this run, then a live server read. None if all three
    come up empty — which is a real missing token, not cache lag."""
    m = fresh.get(mon_name)
    if m and m.get("pushToken"):
        return m["pushToken"]
    if created_tokens.get(mon_name):
        return created_tokens[mon_name]
    return _live_push_token(api, m)


def _channel_ids(monitor) -> set:
    """The attached channel ids in either shape Kuma returns ({id: bool} or list)."""
    raw = monitor.get("notificationIDList") or {}
    if isinstance(raw, dict):
        return {int(k) for k, v in raw.items() if v}
    return set(raw)


def _confirm_mute_live(api, mid):
    """Ask the SERVER whether monitor `mid` really has no channels.

    Returns True (mute), False (has channels), or None (could not ask).

    WHY THIS EXISTS: `api.get_monitors()` is `_get_event_data(MONITOR_LIST)` —
    a client-side cache refreshed by socket events. `api.get_monitor(id)` is
    `_call('getMonitor', id)` — an actual round trip. On the run that CREATES a
    monitor, the cache does not reflect the notificationIDList that this same
    run just wrote, so the cache-only verifier below reported the monitor mute
    immediately after printing its own `[notify] ... + channels [1, 2]` success
    line. The retry then re-read the identical cache and could never clear it,
    so the run ended FATAL every single time a monitor was created — observed
    2026-08-03 on monitors 115/116/117, where kuma.db showed 2 channels each.

    A guard that cries wolf on exactly the run it exists to protect trains the
    operator to ignore it, which is how a genuinely mute monitor gets waved
    through. So the cache may only ACCUSE; the server convicts."""
    try:
        return not _channel_ids(api.get_monitor(mid))
    except Exception as exc:
        print(f"  [warn] could not confirm id={mid} against the server: {exc}")
        return None


def _mute_monitor_names(api, created_names=None) -> list:
    """Names of active monitors carrying ZERO notification channels.

    The post-reconcile assertion. Kept separate from
    _ensure_notifications_attached so the check is independent of the code that
    is supposed to have done the work — a verifier that reuses the actor's view
    of the world cannot catch the actor missing a monitor entirely, which is the
    failure it exists to catch.

    Enumeration comes from the cached list PLUS `created_names` — the monitors
    this very run created. That union is load-bearing, not belt-and-braces: the
    cache is refreshed over a socket, so a monitor created seconds ago may be
    ABSENT from it entirely, and an absent monitor is not checked, not accused,
    and not printed — it just silently does not exist as far as this verifier is
    concerned, which is how the function returns [] and the caller prints
    "verified: every active monitor has notification channels" over a monitor
    that has none.

    That is not hypothetical and it is not fixed by the re-read/retry above it:
    "Canary Dash Asset Integrity" was born mute on 2026-07-30 with this function
    already in place, and on 2026-08-07 "Canary Tdarr Pause Integrity" did it
    again — run 1 created it, attached NOTHING, and printed the verified line;
    run 2 reported `+ channels [1, 2] (was NONE)`. Third occurrence of one class.
    A verifier whose enumeration can miss the exact monitors most likely to be
    broken (the new ones) is not a verifier.

    A created-this-run monitor that the cache cannot show is UNCONFIRMABLE and
    is therefore ACCUSED, per this function's own standing rule: fail loud on
    what it cannot vouch for. Failing closed here costs one extra bootstrap run
    on a fresh monitor; failing open costs a monitor that pages nobody."""
    mute = []
    created_names = set(created_names or ())
    try:
        monitors = api.get_monitors()
    except Exception as exc:
        print(f"  [warn] could not re-read monitors to verify: {exc}")
        # Cannot vouch for anything — but we KNOW what we created, so those are
        # still accused rather than waved through on a failed read.
        return sorted(created_names)
    seen = {m.get("name") for m in monitors}
    for name in sorted(created_names - seen):
        print(f"  [unconfirmable]{name:30s} created this run but absent from the "
              f"monitor list — cannot verify its channels")
        mute.append(name)
    for m in monitors:
        if not m.get("active", True):
            continue
        if _channel_ids(m):
            continue
        name = m.get("name", f"id={m.get('id')}")
        mid = m.get("id")
        live = _confirm_mute_live(api, mid) if mid is not None else None
        if live is False:
            # Cache lag, not a mute monitor. Say so — silence here would look
            # like the check never ran.
            print(f"  [stale-cache]{name:32s} cache said mute; server says wired")
            continue
        mute.append(name)
    return sorted(mute)


def _delete_all_http_monitors(api):
    """Delete the 22 HTTP monitors I created in the previous pass.
    Idempotent — skips anything that's not HTTP-typed or that doesn't
    match a name from the manifest."""
    manifest_path = _resolve_manifest_path()
    manifest = manifest_load(manifest_path)
    target_names = {a.kuma_monitor for a in manifest.apps() if a.kuma_monitor}
    to_delete = []
    for m in api.get_monitors():
        mtype = str(m.get("type", "")).lower()
        if m["name"] in target_names and mtype.endswith("http"):
            to_delete.append((m["id"], m["name"]))
    for mid, mname in to_delete:
        try:
            api.delete_monitor(mid)
            print(f"  [del]{mname:25s} (was HTTP id={mid})")
        except Exception as exc:
            print(f"  [del-fail]{mname:25s} {exc}")
    return len(to_delete)


def main() -> int:
    try:
        from uptime_kuma_api import UptimeKumaApi
    except ImportError:
        print("ERROR: uptime-kuma-api not installed. pip install uptime-kuma-api", file=sys.stderr)
        return 2

    manifest_path = _resolve_manifest_path()
    manifest = manifest_load(manifest_path)

    candidates = []
    for pw_name in ("htpasswd.password", "shared-admin.password"):
        try:
            candidates.append((pw_name, _read_secret(pw_name)))
        except FileNotFoundError:
            pass
    if not candidates:
        print("ERROR: no candidate passwords", file=sys.stderr)
        return 2

    api = UptimeKumaApi(KUMA_URL)
    try:
        used = _login(api, candidates)
        print(f"login ok: user={USER} via secrets/{used}")
    except Exception as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        api.disconnect()
        return 3

    print("\n--- step 0: ensure auto-heal webhook notification + attach to all monitors ---")
    _ensure_autoheal_webhook(api)

    print("\n--- step 0b: ensure pusher self-heartbeat PUSH monitor exists ---")
    # "Manitoba Pusher" is the dead-man for the pusher itself. Without
    # this monitor, a pusher crashloop looks like every app went down at
    # once. Gives the operator a single unambiguous signal.
    pusher_monitor = "Manitoba Pusher"
    existing_now = {m["name"]: m for m in api.get_monitors()}
    pusher_create_token = ""
    if pusher_monitor not in existing_now:
        try:
            pusher_create_token = _add_push_monitor(api, pusher_monitor)
            print(f"  [add]{pusher_monitor:25s} PUSH (heartbeat-only) token={'yes' if pusher_create_token else 'pending'}")
        except Exception as exc:
            print(f"  [FAIL]{pusher_monitor:25s} {exc}")
    else:
        pusher_create_token = existing_now[pusher_monitor].get("pushToken", "")
        print(f"  [skip]{pusher_monitor:25s} (already exists)")

    print("\n--- step 0c: ensure fleet aggregate PUSH monitor exists ---")
    # "QFlix Fleet" is the dead-man for the whole pushed-app fleet. When many
    # monitors flip DOWN simultaneously (correlated storm), this single monitor
    # goes DOWN instead of N pages. The pusher feeds it each cycle (sub-project C).
    fleet_monitor = "QFlix Fleet"
    fleet_create_token = ""
    if fleet_monitor not in existing_now:
        try:
            fleet_create_token = _add_push_monitor(api, fleet_monitor)
            print(f"  [add]{fleet_monitor:25s} PUSH (heartbeat-only) token={'yes' if fleet_create_token else 'pending'}")
        except Exception as exc:
            print(f"  [FAIL]{fleet_monitor:25s} {exc}")
    else:
        fleet_create_token = existing_now[fleet_monitor].get("pushToken", "")
        print(f"  [skip]{fleet_monitor:25s} (already exists)")

    print("\n--- step 1: drop any existing HTTP monitors I created previously ---")
    deleted = _delete_all_http_monitors(api)
    print(f"deleted {deleted} stale HTTP monitor(s)")

    # Let Kuma's socketio MONITOR_LIST event propagate before we re-snapshot.
    import time
    time.sleep(2)
    existing = {m["name"]: m for m in api.get_monitors()}
    print(f"\n--- step 2: create PUSH monitors (currently {len(existing)} existing) ---")

    created = 0
    skipped = 0
    failed = 0

    # Push tokens captured AT CREATION, keyed by monitor name. `_add_push_monitor`
    # returns the token, but until 2026-07-30 only the pusher and fleet monitors
    # kept it — every app/canary/janitor discarded the return value and relied on
    # re-reading it from get_monitors() further down.
    #
    # That re-read races: the code below already documents that "get_monitors()
    # sometimes returns the PUSH monitor without the pushToken field even seconds
    # after creation", which is why pusher/fleet have a fallback. Canaries had
    # none, so creating one landed its key in `missing` — and `missing` was only
    # a [warn]. The run then reported success, the installer deployed a token file
    # with that canary absent, and `manitoba-maint canary push <name>` silently
    # exited 0 forever. dash-asset-integrity shipped exactly this way on
    # 2026-07-30: scheduled, running, exit 0, pushing nothing, monitor DOWN with
    # "No heartbeat in the time window" and zero real coverage.
    created_tokens: dict[str, str] = {}
    # EVERY create, whether or not its token came back. Tracked separately
    # from created_tokens on purpose: that dict only gains a key when the
    # token was captured, and a monitor created WITHOUT one is precisely the
    # case that ships mute (2026-08-07 - Canary Tdarr Pause Integrity was
    # created tokenless on run 1 and the verifier printed "verified" over it).
    # Keying the mute check off created_tokens would miss exactly the monitors
    # most likely to be broken.
    created_names: set = set()

    for app in manifest.apps():
        target = app.kuma_monitor
        if not target:
            continue
        if target in existing:
            print(f"  [skip]{target:25s} (already exists)")
            skipped += 1
            continue
        try:
            tok = _add_push_monitor(api, target)
            created_names.add(target)
            if tok:
                created_tokens[target] = tok
            print(f"  [add]{target:25s} PUSH (token={'yes' if tok else 'pending'})")
            created += 1
        except Exception as exc:
            print(f"  [FAIL]{target:25s} {exc}")
            failed += 1

    # Canaries — same idempotent create flow, keyed by `canary-<name>` in tokens.
    # Heartbeat varies per canary based on its schedule (must be ≥ schedule
    # interval, or Kuma flips DOWN between runs).
    for canary in manifest.canaries():
        target = canary.kuma_monitor
        if not target:
            continue
        hb = _heartbeat_for_canary(canary)
        if target in existing:
            _ensure_monitor_interval(api, existing[target], hb)
            print(f"  [skip]{target:25s} (already exists)")
            skipped += 1
            continue
        try:
            tok = _add_push_monitor(api, target, interval=hb)
            created_names.add(target)
            if tok:
                created_tokens[target] = tok
            print(f"  [add]{target:25s} PUSH (canary, hb={hb}s, "
                  f"token={'yes' if tok else 'pending'})")
            created += 1
        except Exception as exc:
            print(f"  [FAIL]{target:25s} {exc}")
            failed += 1

    # Standalone self-pushing maint monitors (reaper + janitors). These aren't
    # apps or canaries — each janitor self-pushes its own monitor per run — but
    # bootstrap still CREATES any that are missing so a newly-shipped janitor's
    # monitor + token exist without a manual Kuma click. Daily cadence, so a
    # large heartbeat (Kuma must not flip DOWN between daily runs). Names +
    # token keys come from lib.kuma — the single source of truth the drift audit
    # also reads (so audit and bootstrap can never disagree).
    # Both come from lib.kuma: the monitor set AND its per-monitor heartbeat
    # windows. Keeping the cadence beside the registration is deliberate -- a
    # window declared in this file and a job declared in that one is a pair that
    # drifts, and a dead-man wider than its job's cadence is green while the job
    # is dead.
    from lib.kuma import (STANDALONE_SELF_PUSH_MONITORS,
                          STANDALONE_SELF_PUSH_HEARTBEATS)
    _STANDALONE_HB_S = 90000  # 24h + buffer, matching the daily-0430 canary heartbeat

    for mon_name in STANDALONE_SELF_PUSH_MONITORS:
        if mon_name in existing:
            print(f"  [skip]{mon_name:25s} (already exists)")
            skipped += 1
            continue
        hb = STANDALONE_SELF_PUSH_HEARTBEATS.get(mon_name, _STANDALONE_HB_S)
        try:
            tok = _add_push_monitor(api, mon_name, interval=hb)
            created_names.add(mon_name)
            if tok:
                created_tokens[mon_name] = tok
            print(f"  [add]{mon_name:25s} PUSH (standalone janitor, "
                  f"hb={hb}s, token={'yes' if tok else 'pending'})")
            created += 1
        except Exception as exc:
            print(f"  [FAIL]{mon_name:25s} {exc}")
            failed += 1

    # Final token reconciliation — tokens populate server-side a beat after
    # add. Re-fetch and map kuma_monitor -> app.name -> pushToken.
    import time
    time.sleep(2)
    fresh = {m["name"]: m for m in api.get_monitors()}
    # Seed from the existing tokens file so any operator-placed keys we don't
    # touch below survive (e.g., manually-bootstrapped external PUSH tokens
    # written before the external-monitor sync below existed). Without this,
    # every bootstrap run wipes tokens.json down to only what we re-sync.
    out = REPO_ROOT / "secrets" / "kuma-push-tokens.json"
    try:
        tokens: dict[str, str] = json.loads(out.read_text())
        if not isinstance(tokens, dict):
            tokens = {}
    except (FileNotFoundError, ValueError):
        tokens = {}
    missing = []
    for app in manifest.apps():
        if not app.kuma_monitor:
            continue
        tok = _resolve_token(api, fresh, created_tokens, app.kuma_monitor)
        if tok:
            tokens[app.name] = tok
        else:
            missing.append((app.name, app.kuma_monitor))
    # Canary tokens use `canary-<name>` so cli.py canary-push can find them.
    for canary in manifest.canaries():
        if not canary.kuma_monitor:
            continue
        key = f"canary-{canary.name}"
        tok = _resolve_token(api, fresh, created_tokens, canary.kuma_monitor)
        if tok:
            tokens[key] = tok
        else:
            missing.append((key, canary.kuma_monitor))

    # Standalone self-pusher tokens — keyed by the janitor's KUMA_PUSH_KEY (the
    # VALUE in STANDALONE_SELF_PUSH_MONITORS), which is what each janitor reads
    # from kuma-push-tokens.json. Monitor NAME != token KEY for these (e.g.
    # "QFlix Reaper" -> "qflix-reaper"), so they're captured separately.
    for _mon_name, _token_key in STANDALONE_SELF_PUSH_MONITORS.items():
        tok = _resolve_token(api, fresh, created_tokens, _mon_name)
        if tok:
            tokens[_token_key] = tok
        else:
            missing.append((_token_key, _mon_name))

    # External PUSH monitors — entries in manifest.kuma_external_monitors
    # whose Kuma type is PUSH (e.g., "QFlix Collect (seedbox)"). The
    # bootstrap doesn't create these (they're operator-owned, out of
    # manitoba scope), but it CAN sync their tokens so consumers like
    # qflix-collect.ps1 stay healthy after a manual monitor regen. HTTP-type
    # externals (Quadstronix nodes) have no pushToken and are skipped.
    # The display-name is used as the key here — that's what Push-Kuma in
    # qflix-collect.ps1 looks up verbatim.
    for ext_name in manifest.external_monitors():
        m = fresh.get(ext_name)
        if not m:
            continue
        mtype = str(m.get("type", "")).lower()
        if not mtype.endswith("push"):
            continue
        tok = m.get("pushToken") or _live_push_token(api, m)
        if tok:
            tokens[ext_name] = tok
        else:
            missing.append((ext_name, ext_name))

    # Pusher self-heartbeat token — keyed "manitoba-pusher" so the
    # pusher loop can find it. The pusher pushes status=up here each
    # cycle; Kuma flips it down if no push arrives within 90s.
    pusher_m = fresh.get(pusher_monitor)
    if pusher_m and pusher_m.get("pushToken"):
        tokens["manitoba-pusher"] = pusher_m["pushToken"]
    elif pusher_create_token:
        # get_monitors() sometimes returns the PUSH monitor without the
        # pushToken field even seconds after creation. The token we captured
        # from _add_push_monitor's re-fetch is the authoritative copy.
        tokens["manitoba-pusher"] = pusher_create_token
    else:
        _tok = _live_push_token(api, pusher_m)
        if _tok:
            tokens["manitoba-pusher"] = _tok
        else:
            missing.append(("manitoba-pusher", pusher_monitor))

    # Fleet aggregate token — keyed "qflix-fleet" so push_once() can find it.
    # The pusher pushes status=up/down here each cycle; if no push arrives
    # within 90s Kuma flips DOWN as a dead-man for the whole fleet.
    fleet_m = fresh.get(fleet_monitor)
    if fleet_m and fleet_m.get("pushToken"):
        tokens["qflix-fleet"] = fleet_m["pushToken"]
    elif fleet_create_token:
        tokens["qflix-fleet"] = fleet_create_token
    else:
        _tok = _live_push_token(api, fleet_m)
        if _tok:
            tokens["qflix-fleet"] = _tok
        else:
            missing.append(("qflix-fleet", fleet_monitor))

    # A missing token is FATAL, not a warning. It used to print [warn] and let
    # the run report success; the installer then deployed a token file with that
    # key absent, and the consumer -- `manitoba-maint canary push <name>` --
    # SILENTLY EXITS 0 when it cannot find its token. The result is a canary that
    # is scheduled, runs, succeeds, and pushes nothing, while its Kuma monitor
    # sits DOWN on "No heartbeat in the time window". A warning nobody reads is
    # not a guard.
    token_failure = False
    if missing:
        token_failure = True
        print(f"\nFATAL: {len(missing)} monitor(s) have NO pushToken:")
        for app_name, mon_name in missing:
            print(f"  - {app_name} ({mon_name})")
        print("Their consumers would exit 0 while pushing nothing.")
        # Was "Re-run this script" — accurate when the only token source was the
        # racing monitor cache. _resolve_token now also asks the server directly,
        # so reaching here means the token genuinely is not there and a re-run is
        # a guess, not a fix. Say what to actually look at.
        print("The cached list, this run's create response AND a live getMonitor")
        print("all came up empty — check the monitor exists and is PUSH-typed in")
        print("the Kuma UI, then re-run.")

    # Persist tokens for the push-loop service.
    out.write_text(json.dumps(tokens, indent=2, sort_keys=True))
    try:
        os.chmod(out, 0o600)
    except OSError:
        pass
    print(f"\nwrote {len(tokens)} token(s) to secrets/kuma-push-tokens.json")
    print(f"created: {created}  skipped(existed): {skipped}  failed: {failed}")

    # Runs LAST, after every monitor exists, so anything created above is also
    # reconciled. A monitor with no notifications can go red in total silence —
    # see _ensure_notifications_attached for the 32/60 incident this closes.
    print("\n--- final: reconcile notification channels on every monitor ---")
    attached = _ensure_notifications_attached(api)
    print(f"notification sets repaired: {attached}")

    # VERIFY, don't assume. The reconcile above is correct in intent but reads
    # api.get_monitors(), which is a client-side cache the server refreshes over
    # the socket. A monitor CREATED moments earlier in this same run may not have
    # landed in that cache yet, so it is silently skipped — no [notify] line and
    # no [notify-fail] line, because the loop never sees it at all.
    #
    # That is exactly how "Canary Dash Asset Integrity" was born mute on
    # 2026-07-30 despite this function existing to prevent it: the creating run
    # missed it, and only the NEXT run repaired it. Reconcile-on-every-run fixes
    # a monitor eventually, but "eventually" means the guard ships deaf and stays
    # deaf until someone happens to re-run the bootstrap.
    #
    # So: re-read, retry once against a freshly fetched list, and if anything is
    # still mute, FAIL the run. A non-zero exit makes 240-maintenance-install.sh
    # print its "monitors may be incomplete" warning instead of reporting success
    # over a silent monitor.
    # created_names is every monitor this run CREATED, token or not. Passing it
    # in is what stops a cache miss from reading as "nothing to check" — see
    # _mute_monitor_names for the three times that silently shipped a mute guard.
    mute = _mute_monitor_names(api, created_names)
    if mute:
        print(f"  [retry] {len(mute)} monitor(s) still mute after reconcile: {', '.join(mute)}")
        attached += _ensure_notifications_attached(api)
        mute = _mute_monitor_names(api, created_names)
    if mute:
        print(f"\nFATAL: {len(mute)} active monitor(s) have NO notification channels:")
        for name in mute:
            print(f"  - {name}")
        print("These can go red in total silence — no Discord, no auto-heal webhook.")
        print("Re-run this script; if it persists, attach the channels in the Kuma UI.")
        api.disconnect()
        return 1
    print("verified: every active monitor has notification channels")

    api.disconnect()
    if token_failure:
        return 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import json
    sys.exit(main())

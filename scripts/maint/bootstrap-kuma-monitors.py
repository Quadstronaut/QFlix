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

    for app in manifest.apps():
        target = app.kuma_monitor
        if not target:
            continue
        if target in existing:
            print(f"  [skip]{target:25s} (already exists)")
            skipped += 1
            continue
        try:
            _add_push_monitor(api, target)
            print(f"  [add]{target:25s} PUSH (id pending)")
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
            _add_push_monitor(api, target, interval=hb)
            print(f"  [add]{target:25s} PUSH (canary, hb={hb}s)")
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
    from lib.kuma import STANDALONE_SELF_PUSH_MONITORS
    _STANDALONE_HB_S = 90000  # 24h + buffer, matching the daily-0430 canary heartbeat
    for mon_name in STANDALONE_SELF_PUSH_MONITORS:
        if mon_name in existing:
            print(f"  [skip]{mon_name:25s} (already exists)")
            skipped += 1
            continue
        try:
            _add_push_monitor(api, mon_name, interval=_STANDALONE_HB_S)
            print(f"  [add]{mon_name:25s} PUSH (standalone janitor, hb={_STANDALONE_HB_S}s)")
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
        m = fresh.get(app.kuma_monitor)
        if m and m.get("pushToken"):
            tokens[app.name] = m["pushToken"]
        else:
            missing.append((app.name, app.kuma_monitor))
    # Canary tokens use `canary-<name>` so cli.py canary-push can find them.
    for canary in manifest.canaries():
        if not canary.kuma_monitor:
            continue
        m = fresh.get(canary.kuma_monitor)
        key = f"canary-{canary.name}"
        if m and m.get("pushToken"):
            tokens[key] = m["pushToken"]
        else:
            missing.append((key, canary.kuma_monitor))

    # Standalone self-pusher tokens — keyed by the janitor's KUMA_PUSH_KEY (the
    # VALUE in STANDALONE_SELF_PUSH_MONITORS), which is what each janitor reads
    # from kuma-push-tokens.json. Monitor NAME != token KEY for these (e.g.
    # "QFlix Reaper" -> "qflix-reaper"), so they're captured separately.
    for _mon_name, _token_key in STANDALONE_SELF_PUSH_MONITORS.items():
        m = fresh.get(_mon_name)
        if m and m.get("pushToken"):
            tokens[_token_key] = m["pushToken"]
        else:
            missing.append((_token_key, _mon_name))

    # External PUSH monitors — entries in manifest.kuma_external_monitors
    # whose Kuma type is PUSH (e.g., "QFlix Collect (workstation)"). The
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
        if m.get("pushToken"):
            tokens[ext_name] = m["pushToken"]
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
        missing.append(("qflix-fleet", fleet_monitor))

    if missing:
        print(f"\n[warn] {len(missing)} monitor(s) lack a pushToken in get_monitors():")
        for app_name, mon_name in missing:
            print(f"  - {app_name} ({mon_name})")

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

    api.disconnect()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import json
    sys.exit(main())

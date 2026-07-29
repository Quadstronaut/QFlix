"""lib/kuma.py — Uptime Kuma integration.

Phase 5: client — query Kuma's /metrics endpoint for monitor status.
Phase 8: server — HTTP webhook receiver for Kuma down/up events.

Client endpoint strategy: GET <host>/metrics with Basic auth ("", api_key).
Prometheus-format response; parse `monitor_status{...monitor_name="<name>"...} <value>`.
Values: 1=up, 0=down, 2=pending, 3=maintenance → map 1→"up", 0→"down", else→"unknown".
Best-effort: any failure → "unknown".

Server: stdlib http.server.ThreadingHTTPServer on 127.0.0.1 (loopback only).
Routes:
  POST /kuma   — Kuma webhook payload
  GET  /health — returns 200 'ok\n'
"""
from __future__ import annotations

import http.server
import json
import logging
import os
import re
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, Type

import requests

from lib import recovery, state

_DEFAULT_KUMA_PORT = 3001
_DEFAULT_KUMA_HOST = f"http://127.0.0.1:{_DEFAULT_KUMA_PORT}"

# Matches: monitor_status{...monitor_name="<name>"...} <value>
_METRICS_RE = re.compile(
    r'^monitor_status\{[^}]*monitor_name="([^"]+)"[^}]*\}\s+(\d+)',
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Secrets resolution (delegates to lib.secrets — single source of truth)
# ---------------------------------------------------------------------------

from lib.secrets import secrets_dir as _secrets_dir  # noqa: E402, F401
from lib.secrets import read_secret as _secret_read  # noqa: E402


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------

def _kuma_host() -> str:
    try:
        return _secret_read("uptimekuma.host")
    except FileNotFoundError:
        pass
    try:
        port = _secret_read("uptimekuma.port")
        return f"http://127.0.0.1:{port}"
    except FileNotFoundError:
        pass
    return _DEFAULT_KUMA_HOST


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def monitor_status(
    name: str,
    *,
    timeout_s: float = 5.0,
) -> Literal["up", "down", "unknown"]:
    """Query Uptime Kuma's /metrics endpoint for the status of a monitor by name.

    Returns 'up', 'down', or 'unknown' (network error / monitor not found /
    parse error). Never raises — best-effort; caller decides what to do with
    'unknown'.
    """
    try:
        api_key = _secret_read("uptimekuma.key")
    except FileNotFoundError:
        api_key = ""

    host = _kuma_host()
    url = host.rstrip("/") + "/metrics"

    try:
        resp = requests.get(url, auth=("", api_key), timeout=timeout_s)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return "unknown"

    for match in _METRICS_RE.finditer(text):
        monitor_name = match.group(1)
        value_str = match.group(2)
        if monitor_name == name:
            value = int(value_str)
            if value == 1:
                return "up"
            if value == 0:
                return "down"
            return "unknown"

    return "unknown"


def monitors_status(
    names: list[str],
    *,
    timeout_s: float = 5.0,
) -> dict[str, Literal["up", "down", "unknown"]]:
    """Batched variant: one /metrics fetch, return {name: status} for every
    requested name. Names not present in the Kuma response come back as
    'unknown'. Same best-effort semantics as monitor_status — network /
    auth / parse failures map every requested name to 'unknown'."""
    if not names:
        return {}

    try:
        api_key = _secret_read("uptimekuma.key")
    except FileNotFoundError:
        api_key = ""

    host = _kuma_host()
    url = host.rstrip("/") + "/metrics"

    try:
        resp = requests.get(url, auth=("", api_key), timeout=timeout_s)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return {n: "unknown" for n in names}

    wanted = set(names)
    result: dict[str, Literal["up", "down", "unknown"]] = {n: "unknown" for n in names}
    for match in _METRICS_RE.finditer(text):
        monitor_name = match.group(1)
        if monitor_name not in wanted:
            continue
        value = int(match.group(2))
        if value == 1:
            result[monitor_name] = "up"
        elif value == 0:
            result[monitor_name] = "down"
        # value 2 (pending) / 3 (maintenance) → leave as 'unknown'
    return result


# ---------------------------------------------------------------------------
# Audit — drift between manifest kuma_monitor names and live Kuma monitors
# ---------------------------------------------------------------------------

# Self-pushing maintenance oneshots (reaper + janitors): each self-pushes its
# own Kuma monitor on every run rather than being pusher-probed (they're timer
# oneshots, not long-lived apps), so they are NOT in manifest apps/canaries.
# Register them here — this is the single source of truth consumed by BOTH the
# drift audit below (so they count as expected, not orphan) AND
# bootstrap-kuma-monitors.py (so a missing monitor is created + its token
# captured). Keys are token keys under secrets/kuma-push-tokens.json, matching
# each janitor's KUMA_PUSH_KEY. Adding a new self-pushing janitor? Add it here,
# nowhere else — the 2026-07-27 audit found audio-disposition + anime-janitor
# flagged as orphan drift precisely because they shipped without this line.
STANDALONE_SELF_PUSH_MONITORS = {
    "QFlix Reaper": "qflix-reaper",
    "QFlix Audio Disposition": "qflix-audio-disposition",
    "qflix-anime-janitor": "qflix-anime-janitor",
    "QFlix Torrent Janitor": "qflix-torrent-janitor",
    # Moved here from manifest.kuma_external_monitors 2026-07-29: this job
    # migrated off the operator's Windows scheduled task onto the box itself
    # 2026-07-09 (qflix-collect.py + qflix-collect.timer — see memory
    # collect-migrated-to-box) and now self-pushes just like the janitors
    # above, so it belongs in the audit's expected set instead of being
    # invisible to drift. Its token KEY equals its monitor NAME (unlike the
    # other entries here, where the value is a short slug) because
    # qflix-collect.py's KUMA_PUSH_KEY constant reads
    # secrets/kuma-push-tokens.json["QFlix Collect (workstation)"] verbatim —
    # that literal string is baked into the already-deployed box script and
    # the box is read-only from here, so the name stays as-is even though
    # "(workstation)" is now cosmetically stale. Renaming it live (in Kuma
    # AND in qflix-collect.py's default) is a coordinated change for the
    # operator, not something this dict can paper over.
    "QFlix Collect (workstation)": "QFlix Collect (workstation)",
}


def _kuma_db_path() -> Path:
    return Path(os.environ.get(
        "QFLIX_KUMA_DB", str(Path.home() / ".apps" / "uptimekuma" / "kuma.db")))


def _live_monitors_from_db():
    """Authoritative set of ACTIVE monitor names from Kuma's SQLite DB, or None
    if the DB isn't readable locally (e.g. the audit is run from the
    workstation). Preferred over /metrics for the existence check: /metrics only
    emits a monitor_status line within a recent-heartbeat window, so a daily
    self-pusher (reaper + the janitors, interval 90000s) drops out of /metrics
    for ~22h/day between beats and the /metrics-based audit then false-flags it
    as drift. The DB is the source of truth for whether a monitor EXISTS."""
    db = _kuma_db_path()
    if not db.exists():
        return None
    try:
        import sqlite3
        con = sqlite3.connect("file:{}?mode=ro".format(db), uri=True, timeout=3)
        try:
            rows = con.execute("SELECT name FROM monitor WHERE active=1").fetchall()
        finally:
            con.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return None


def audit_monitors(manifest, *, kuma_url: str | None = None,
                   api_key: str | None = None,
                   timeout_s: float = 5.0) -> dict:
    """Compare manifest kuma_monitor names vs Kuma's live monitor list.

    Returns:
      {
        "matched":       [<name>, ...],   # in manifest AND in Kuma
        "manifest_only": [<name>, ...],   # in manifest, NOT in Kuma  (need bootstrap)
        "kuma_only":     [<name>, ...],   # in Kuma, NOT in manifest  (orphaned)
        "live_count":    int,             # total monitors in Kuma
        "manifest_count":int,             # total kuma_monitor names in manifest
      }

    Read-only: uses Kuma's /metrics endpoint with the API key in
    secrets/uptimekuma.key. No socket.io login needed."""
    manifest_monitors = {a.kuma_monitor for a in manifest.apps() if a.kuma_monitor}
    # Include canary monitors in drift audit
    try:
        for c in manifest.canaries():
            manifest_monitors.add(c.kuma_monitor)
    except AttributeError:
        pass
    # The daemon's own self-heartbeat monitor — created by
    # bootstrap-kuma-monitors.py and fed by manitoba-maint-pusher.service.
    # Not tied to any app, but always part of the expected monitor set.
    manifest_monitors.add("Manitoba Pusher")
    # Fleet aggregate dead-man monitor — created by bootstrap-kuma-monitors.py
    # (step 0c). Fed each cycle by push_once() in the pusher service. Collapses
    # correlated mass-down storms into a single operator signal (sub-project C).
    manifest_monitors.add("QFlix Fleet")
    # Self-pushing maintenance oneshots (reaper + audio-disposition + anime-
    # janitor + torrent-janitor). See STANDALONE_SELF_PUSH_MONITORS above — the
    # single registration point that keeps `kuma audit` from flagging these
    # self-pushers as orphan drift.
    manifest_monitors.update(STANDALONE_SELF_PUSH_MONITORS)

    # Prefer Kuma's SQLite DB for the authoritative EXISTENCE check (active
    # monitors), because /metrics omits daily self-pushers between their
    # once-a-day beats (see _live_monitors_from_db). Fall back to /metrics only
    # when the DB isn't local — e.g. the audit is run from the workstation.
    live_monitors = _live_monitors_from_db()
    if live_monitors is None:
        if kuma_url is None:
            kuma_url = _kuma_host()
        if api_key is None:
            api_key = _secret_read("uptimekuma.key")

        try:
            resp = requests.get(f"{kuma_url}/metrics", auth=("", api_key),
                                timeout=timeout_s)
        except requests.RequestException as exc:
            return {
                "matched": [],
                "manifest_only": sorted(manifest_monitors),
                "kuma_only": [],
                "live_count": 0,
                "manifest_count": len(manifest_monitors),
                "error": f"Kuma unreachable: {exc}",
            }

        live_monitors = set(re.findall(r'monitor_status\{[^}]*monitor_name="([^"]+)"', resp.text))

    # Suppress monitors the operator declared external (other projects sharing
    # this Kuma instance). They count toward live_count but not toward drift.
    external = manifest.external_monitors() if hasattr(manifest, "external_monitors") else set()
    kuma_only = (live_monitors - manifest_monitors) - external

    return {
        "matched": sorted(manifest_monitors & live_monitors),
        "manifest_only": sorted(manifest_monitors - live_monitors),
        "kuma_only": sorted(kuma_only),
        "external_ignored": sorted(external & live_monitors),
        "live_count": len(live_monitors),
        "manifest_count": len(manifest_monitors),
    }


# ---------------------------------------------------------------------------
# Webhook server — Phase 8
# ---------------------------------------------------------------------------

# Recovery from webhook events goes through recovery.trigger_async, which
# owns the semaphore + _in_flight dedup. Two separate entry points (webhook
# + pusher) with two separate semaphores would allow concurrent recovery
# threads for the same app — see commit history for the race that motivated
# this unification.

_LOG = logging.getLogger(__name__)


def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".opt" / "maint"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class KumaWebhookHandler(http.server.BaseHTTPRequestHandler):
    """HTTP handler for Kuma webhook payloads.

    Instances carry `manifest` and `state_dir` set by _make_handler().
    """

    manifest = None
    state_dir: Path = Path.home() / ".opt" / "maint"

    # Override to control logging format (write to stderr, not stdout)
    def log_message(self, format: str, *args) -> None:  # noqa: A002
        sys.stderr.write(
            f"[{_utc_now_iso()}] {self.address_string()} "
            f"{self.command} {self.path} "
            f"{args[1] if len(args) > 1 else '-'}\n"
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, b"ok\n")
        elif self.path == "/kuma":
            self._send(405, b"Method Not Allowed\n")
        else:
            self._send(404, b"Not Found\n")

    def do_POST(self) -> None:
        if self.path != "/kuma":
            self._send(404, b"Not Found\n")
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"[{_utc_now_iso()}] malformed JSON from {self.address_string()}: {exc}\n")
            self._send(400, b"Bad Request: malformed JSON\n")
            return

        try:
            monitor_name = body["monitor"]["name"]
            status = body["heartbeat"]["status"]
        except (KeyError, TypeError) as exc:
            sys.stderr.write(f"[{_utc_now_iso()}] missing required fields: {exc}\n")
            self._send(400, b"Bad Request: missing fields\n")
            return

        self._handle_event(monitor_name, status, body)
        self._send(200, b"ok\n")

    def _handle_event(self, monitor_name: str, status: int, body: dict) -> None:
        manifest = self.__class__.manifest
        state_dir = self.__class__.state_dir
        state_path = state_dir / "state.json"

        app_name = manifest.resolve_kuma_monitor(monitor_name) if manifest else None
        canary_name = (
            manifest.resolve_canary_monitor(monitor_name)
            if manifest and app_name is None
            else None
        )

        if canary_name is not None:
            self._handle_canary_event(canary_name, monitor_name, status, body, state_path)
            return

        if app_name is None:
            sys.stderr.write(
                f"[{_utc_now_iso()}] unknown monitor '{monitor_name}' — counting\n"
            )
            data = state.read(state_path)
            data["unknown_monitors_total"] = data.get("unknown_monitors_total", 0) + 1
            state.write(state_path, data)
            return

        if status == 1:
            state.record(state_path, app_name, event="up")
            return

        if status == 2:
            state.record(state_path, app_name, event="pending")
            return

        if status == 0:
            lock_file = state_dir / "lock"
            if lock_file.exists():
                event = {
                    "timestamp": _utc_now_iso(),
                    "monitor": monitor_name,
                    "app": app_name,
                    "status": status,
                    "msg": body.get("msg", ""),
                }
                events_file = state_dir / "window-events.jsonl"
                with open(events_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event) + "\n")
                sys.stderr.write(
                    f"[{_utc_now_iso()}] window lock present — queued event for '{app_name}'\n"
                )
                return

            # No lock — delegate to recovery.trigger_async so both entry
            # points (pusher + webhook) share the single _in_flight set +
            # _RECOVERY_SEMAPHORE. trigger_async never blocks; dedup runs
            # the same way regardless of which path fired first.
            try:
                app = manifest.app(app_name)
            except KeyError:
                state.record(state_path, app_name,
                             event="dropped_unknown_app_in_webhook")
                return

            # B1 suppression: skip recovery while UCC maintenance is active
            # for ucc-class apps. The gate blocks `app-* start` so recovery
            # would only churn to permanently-failed. Record the suppression
            # event so operators can see it in state.json.
            try:
                from lib import suppression as _suppression_mod  # noqa: PLC0415
                if _suppression_mod.recovery_suppressed(app):
                    state.record(state_path, app_name,
                                 event="ucc_maint_recovery_suppressed")
                    return
            except Exception as _sup_exc:
                sys.stderr.write(
                    f"[{_utc_now_iso()}] suppression check failed for '{app_name}': {_sup_exc}\n"
                )

            decision = recovery.trigger_async(app, manifest=manifest)
            if decision == "started":
                state.record(state_path, app_name, event="webhook_triggered_recovery")
            elif decision == "already_running":
                # The pusher (or a prior webhook) already fired — no-op.
                pass
            elif decision == "cap_exceeded":
                sys.stderr.write(
                    f"[{_utc_now_iso()}] in-flight recovery cap exceeded for '{app_name}'\n"
                )
                state.record(state_path, app_name, event="dropped_cap_exceeded")
            elif decision == "permanently_failed":
                sys.stderr.write(
                    f"[{_utc_now_iso()}] webhook: '{app_name}' permanently failed — operator needed\n"
                )
                state.record(state_path, app_name, event="webhook_skip_perma_failed")
            else:
                state.record(state_path, app_name, event=f"webhook_skip_{decision}")

    def _handle_canary_event(
        self,
        canary_name: str,
        monitor_name: str,
        status: int,
        body: dict,
        state_path: Path,
    ) -> None:
        """Canary recovery is a one-shot re-fire of the canary's systemd
        service. Canaries fail when the underlying pipeline (Plex / Seerr
        / Sonarr) is broken — re-firing only helps for transient blips. We
        try once on every Kuma down event; any persistent failure surfaces
        via Discord (Kuma's other notification target). No 3-attempt loop —
        canary fires are idempotent and the next scheduled run is the natural
        retry."""
        if status == 1:
            # Canary reports up — record and stop.
            state.record(state_path, f"canary-{canary_name}", event="up")
            return

        if status == 2:
            state.record(state_path, f"canary-{canary_name}", event="pending")
            return

        # status == 0: re-fire the canary service. Subprocess inherits this
        # webhook server's user-systemd context (manitoba-maint-webhook runs
        # as the seedbox user, same as the canary timers).
        unit = f"manitoba-maint-canary-{canary_name}.service"
        sys.stderr.write(
            f"[{_utc_now_iso()}] canary down: '{monitor_name}' — re-firing {unit}\n"
        )
        try:
            cp = subprocess.run(
                ["systemctl", "--user", "start", unit],
                capture_output=True, text=True, timeout=15,
            )
            if cp.returncode == 0:
                state.record(state_path, f"canary-{canary_name}", event="recovery_attempted")
            else:
                state.record(state_path, f"canary-{canary_name}",
                             event=f"recovery_failed: {cp.stderr.strip()[:120]}")
        except Exception as exc:
            sys.stderr.write(
                f"[{_utc_now_iso()}] canary re-fire failed for '{canary_name}': {exc}\n"
            )
            state.record(state_path, f"canary-{canary_name}",
                         event=f"recovery_exception: {str(exc)[:120]}")

    def _send(self, code: int, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _make_handler(manifest, state_dir: Path) -> Type[KumaWebhookHandler]:
    """Return a KumaWebhookHandler subclass with manifest + state_dir baked in."""

    class _Handler(KumaWebhookHandler):
        pass

    _Handler.manifest = manifest
    _Handler.state_dir = state_dir
    return _Handler


def serve(port: int, *, host: str = "127.0.0.1", manifest=None) -> None:
    """Run the webhook HTTP server in the foreground. Blocks until SIGTERM/SIGINT."""
    if manifest is None:
        from lib.manifest import load as _load_manifest
        manifest_path = os.environ.get(
            "MANITOBA_MANIFEST",
            str(Path.home() / ".opt" / "maint" / "apps.yaml"),
        )
        manifest = _load_manifest(manifest_path)

    sd = _state_dir()
    sd.mkdir(parents=True, exist_ok=True)

    handler_cls = _make_handler(manifest, sd)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    httpd.daemon_threads = True

    sys.stderr.write(f"[{_utc_now_iso()}] kuma webhook server listening on {host}:{port}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        sys.stderr.write(f"[{_utc_now_iso()}] kuma webhook server stopped\n")

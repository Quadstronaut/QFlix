"""lib/health.py — health probe dispatch.

Dispatches on app.health.kind:
  http_api        — GET with optional auth header; port + key from secrets
  http_root       — GET /; port from port_secret or port_source; lenient status
  systemd_only    — systemctl --user is-active <unit>
  systemd_oneshot — systemctl --user show <unit> -p Result; for timer-driven
                    oneshot services (buildarr/recyclarr/kometa/...) where
                    is-active on the .timer always says "active" regardless
                    of whether the last service invocation succeeded.
  port_listen     — TCP connect to 127.0.0.1:<port>
  import_check    — <venv_python> -c "import <module>; print(version)"
  process_pattern — pgrep -f <pattern>; for UCC supervisord-managed apps
                    (unpackerr, postgres) with no HTTP surface or systemd unit

All network/subprocess errors are caught; never raises. Latency is None for
non-network checks (systemd_only, systemd_oneshot, import_check, process_pattern).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from lib.manifest import App

DEFAULT_TIMEOUT_S = 5.0
_HTTP_ROOT_OK_STATUSES = {200, 302, 401}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

@dataclass
class HealthResult:
    ok: bool
    latency_ms: Optional[int]
    reason: str


# ---------------------------------------------------------------------------
# Secrets resolution (delegates to lib.secrets — single source of truth)
# ---------------------------------------------------------------------------

from lib.secrets import secrets_dir as _secrets_dir  # noqa: E402, F401
from lib.secrets import read_secret as _secret_read  # noqa: E402


# ---------------------------------------------------------------------------
# Port resolution
# ---------------------------------------------------------------------------

def _resolve_port(app: App) -> int:
    raw = app.health.raw

    port_source = raw.get("port_source")
    if port_source:
        return _port_from_source(port_source)

    port_secret = raw.get("port_secret")
    if port_secret:
        return int(_secret_read(port_secret))

    raise ValueError(f"App '{app.name}' health config has neither port_secret nor port_source")


def _port_from_source(port_source: str) -> int:
    # Format: "<kind>:<path>:<KEY>"
    # On Windows, path may contain a drive letter colon (e.g. C:\...) so we
    # split on the first ":" to get the kind, then find the last ":" to peel
    # off the key, leaving everything in between as the path.
    first_colon = port_source.index(":")
    kind = port_source[:first_colon]
    remainder = port_source[first_colon + 1:]  # "<path>:<KEY>"
    last_colon = remainder.rindex(":")
    file_path = remainder[:last_colon]
    key = remainder[last_colon + 1:]

    if kind == "env_file":
        path = Path(file_path).expanduser()
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return int(line[len(key) + 1:].strip())
        raise KeyError(f"Key '{key}' not found in env_file {path}")
    elif kind == "json_file":
        path = Path(file_path).expanduser()
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        return int(data[key])
    else:
        raise ValueError(f"Unknown port_source kind: '{kind}'")


# ---------------------------------------------------------------------------
# URL base resolution
# ---------------------------------------------------------------------------

def _resolve_urlbase(app: App) -> str:
    urlbase_secret = app.health.raw.get("urlbase_secret")
    if not urlbase_secret:
        return ""
    try:
        return _secret_read(urlbase_secret)
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Probe kinds
# ---------------------------------------------------------------------------

def _probe_http_api(app: App, timeout_s: float) -> HealthResult:
    raw = app.health.raw
    try:
        port = _resolve_port(app)
    except Exception as exc:
        return HealthResult(ok=False, latency_ms=None, reason=f"config error: {exc}")

    urlbase = _resolve_urlbase(app)
    path_template = raw.get("path_template", "/api/v3/system/status")
    path = path_template.replace("{urlbase}", urlbase)
    path = path.replace("//", "/")
    if not path.startswith("/"):
        path = "/" + path

    host = raw.get("hostname", "127.0.0.1")
    url = f"http://{host}:{port}{path}"

    headers: dict[str, str] = {}
    auth_header = raw.get("auth_header")
    auth_secret = raw.get("auth_secret")
    if auth_header and auth_secret:
        # A missing auth secret used to fall through to an unauthed request.
        # If the target API returns 200 without auth (uncommon but exists),
        # the probe shows green while auth is silently broken — the same
        # class of failure as the Tautulli pms_url Docker-netns regression
        # that hid for a month. Fail loudly so Kuma turns red immediately.
        try:
            headers[auth_header] = _secret_read(auth_secret)
        except FileNotFoundError:
            return HealthResult(
                ok=False, latency_ms=None,
                reason=f"auth secret missing: {auth_secret}",
            )

    # Optional HTTP Basic auth (e.g. Maintainerr enforces htpasswd Basic
    # alongside its own X-Api-Key on every endpoint). Same fail-loud
    # policy: a missing basic auth secret should not result in an unauthed
    # probe that the upstream silently 401s on.
    basic_auth = None
    basic_user = raw.get("basic_auth_user")
    basic_secret = raw.get("basic_auth_secret")
    if basic_user and basic_secret:
        try:
            basic_auth = (basic_user, _secret_read(basic_secret))
        except FileNotFoundError:
            return HealthResult(
                ok=False, latency_ms=None,
                reason=f"basic-auth secret missing: {basic_secret}",
            )

    expect_status = raw.get("expect_status", 200)

    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, timeout=timeout_s, auth=basic_auth)
        latency_ms = int((time.monotonic() - t0) * 1000)
    except requests.Timeout:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")
    except requests.ConnectionError:
        return HealthResult(ok=False, latency_ms=None, reason="connection refused")

    if resp.status_code == expect_status:
        return HealthResult(ok=True, latency_ms=latency_ms, reason="ok")
    return HealthResult(ok=False, latency_ms=latency_ms, reason=f"http {resp.status_code}")


def _probe_http_root(app: App, timeout_s: float) -> HealthResult:
    raw = app.health.raw
    try:
        port = _resolve_port(app)
    except Exception as exc:
        return HealthResult(ok=False, latency_ms=None, reason=f"config error: {exc}")

    # path_override lets http_root probe a non-root liveness endpoint
    # (e.g. /sabnzbd/ for an app served under a urlbase rather than at root).
    path = raw.get("path_override", "/")
    if not path.startswith("/"):
        path = "/" + path
    # hostname override lets apps that bind only to the Docker bridge (e.g.
    # FlareSolverr at 172.17.0.1) be probed from the host netns.
    host = raw.get("hostname", "127.0.0.1")
    url = f"http://{host}:{port}{path}"
    expect_status = raw.get("expect_status")

    # Same optional Basic auth as http_api — fail loudly on missing secret
    # rather than silently issuing an unauthed request.
    basic_auth = None
    basic_user = raw.get("basic_auth_user")
    basic_secret = raw.get("basic_auth_secret")
    if basic_user and basic_secret:
        try:
            basic_auth = (basic_user, _secret_read(basic_secret))
        except FileNotFoundError:
            return HealthResult(
                ok=False, latency_ms=None,
                reason=f"basic-auth secret missing: {basic_secret}",
            )

    t0 = time.monotonic()
    try:
        resp = requests.get(url, timeout=timeout_s, auth=basic_auth)
        latency_ms = int((time.monotonic() - t0) * 1000)
    except requests.Timeout:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")
    except requests.ConnectionError:
        return HealthResult(ok=False, latency_ms=None, reason="connection refused")

    if expect_status is not None:
        ok = resp.status_code == expect_status
    else:
        ok = resp.status_code in _HTTP_ROOT_OK_STATUSES

    if ok:
        return HealthResult(ok=True, latency_ms=latency_ms, reason="ok")
    return HealthResult(ok=False, latency_ms=latency_ms, reason=f"http {resp.status_code}")


def _probe_systemd_only(app: App, timeout_s: float) -> HealthResult:
    # Unit name precedence: health.raw.unit (override) > app.raw.unit (the
    # canonical placement at app top-level, used by Recyclarr / Tdarr-Node /
    # any cron- or systemd-class app) > getattr(app,"unit",None) for tests.
    raw = app.health.raw
    unit = (
        raw.get("unit")
        or app.raw.get("unit")
        or getattr(app, "unit", None)
    )
    if not unit:
        return HealthResult(ok=False, latency_ms=None, reason="no unit configured")

    try:
        cp = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")

    stdout = cp.stdout.strip()
    if cp.returncode == 0 and stdout == "active":
        return HealthResult(ok=True, latency_ms=None, reason="active")
    return HealthResult(ok=False, latency_ms=None, reason=stdout or "unknown")


def _probe_systemd_oneshot(app: App, timeout_s: float) -> HealthResult:
    # For timer-driven oneshot .service units, is-active is ~always "inactive"
    # between runs and "activating" during a run — neither tells us whether
    # the LAST invocation succeeded. The authoritative signal is the unit's
    # Result property, set by systemd when the run terminates.
    #
    # Result values: success | exit-code | signal | core-dump | timeout |
    #                oom-kill | watchdog | start-limit-hit | resources
    # Empty Result means the unit has never been triggered yet (fresh install
    # before its first scheduled run) — treat as ok so newly-deployed timers
    # don't show red until their first fire.
    #
    # During an in-flight run, Result still reflects the PRIOR invocation;
    # ActiveState distinguishes "currently running" from "done": activating /
    # deactivating mean the service is between states and we don't yet know
    # whether this run will succeed. Treat as ok to avoid flapping during the
    # recovery loop's 10/30/60s backoff window.
    raw = app.health.raw
    unit = (
        raw.get("unit")
        or app.raw.get("unit")
        or getattr(app, "unit", None)
    )
    if not unit:
        return HealthResult(ok=False, latency_ms=None, reason="no unit configured")

    try:
        cp = subprocess.run(
            ["systemctl", "--user", "show", unit,
             "-p", "Result", "-p", "ActiveState", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")

    if cp.returncode != 0:
        stderr = cp.stderr.strip()[:80]
        return HealthResult(ok=False, latency_ms=None,
                            reason=f"systemctl exit {cp.returncode}: {stderr}")

    props: dict[str, str] = {}
    for line in cp.stdout.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()

    result = props.get("Result", "")
    state = props.get("ActiveState", "")

    if state in ("activating", "deactivating", "reloading"):
        return HealthResult(ok=True, latency_ms=None, reason=f"in-flight ({state})")
    if state == "active" or result == "success" or result == "":
        return HealthResult(ok=True, latency_ms=None,
                            reason=result or state or "no-run-yet")
    return HealthResult(ok=False, latency_ms=None,
                        reason=f"Result={result} ActiveState={state}")


def _probe_port_listen(app: App, timeout_s: float) -> HealthResult:
    try:
        port = _resolve_port(app)
    except Exception as exc:
        return HealthResult(ok=False, latency_ms=None, reason=f"config error: {exc}")

    t0 = time.monotonic()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout_s)
        latency_ms = int((time.monotonic() - t0) * 1000)
        sock.close()
        return HealthResult(ok=True, latency_ms=latency_ms, reason="ok")
    except ConnectionRefusedError:
        return HealthResult(ok=False, latency_ms=None, reason="connection refused")
    except socket.timeout:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")
    except OSError as exc:
        return HealthResult(ok=False, latency_ms=None, reason=f"error: {exc}")


def _probe_import_check(app: App, timeout_s: float) -> HealthResult:
    raw = app.health.raw
    venv_python = os.path.expanduser(raw.get("venv_python", "python3"))
    module = raw.get("module", "")

    cmd_str = (
        f"import {module}; "
        f"print({module}.__version__ if hasattr({module}, '__version__') else 'ok')"
    )

    try:
        cp = subprocess.run(
            [venv_python, "-c", cmd_str],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")

    if cp.returncode == 0:
        reason = cp.stdout.strip() or "ok"
        return HealthResult(ok=True, latency_ms=None, reason=reason)

    stderr = cp.stderr.strip()[:80]
    return HealthResult(ok=False, latency_ms=None, reason=stderr or "non-zero exit")


def _probe_process_pattern(app: App, timeout_s: float) -> HealthResult:
    pattern = app.health.raw.get("pattern")
    if not pattern:
        return HealthResult(ok=False, latency_ms=None, reason="no pattern configured")

    try:
        cp = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return HealthResult(ok=False, latency_ms=None, reason="timeout")
    except FileNotFoundError:
        return HealthResult(ok=False, latency_ms=None, reason="pgrep not found")

    # pgrep: 0 = match, 1 = no match, 2+ = error
    if cp.returncode == 0:
        count = len([p for p in cp.stdout.split() if p.strip()])
        return HealthResult(ok=True, latency_ms=None, reason=f"{count} match(es)")
    if cp.returncode == 1:
        return HealthResult(ok=False, latency_ms=None, reason="no process matching pattern")
    return HealthResult(ok=False, latency_ms=None, reason=f"pgrep exit {cp.returncode}")


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_PROBES = {
    "http_api": _probe_http_api,
    "http_root": _probe_http_root,
    "systemd_only": _probe_systemd_only,
    "systemd_oneshot": _probe_systemd_oneshot,
    "port_listen": _probe_port_listen,
    "import_check": _probe_import_check,
    "process_pattern": _probe_process_pattern,
}


def probe(app: App, *, timeout_s: float | None = None) -> HealthResult:
    if timeout_s is None:
        timeout_s = float(app.defaults.get("health_timeout_s", DEFAULT_TIMEOUT_S))

    kind = app.health.kind
    fn = _PROBES.get(kind)
    if fn is None:
        raise ValueError(f"Unknown health kind '{kind}' for app '{app.name}'")

    return fn(app, timeout_s)

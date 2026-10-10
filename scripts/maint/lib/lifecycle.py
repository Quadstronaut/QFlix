"""lib/lifecycle.py — per-class lifecycle dispatch.

Dispatches start/stop/restart/status on app.class_:
  ucc      — app-<ucc_slug> {start|stop|restart}; status = `version` + port probe
             (there is no `app-<slug> status`); ucc_dormant refuses all but stop
  systemd  — systemctl --user {start|stop|restart|is-active} <unit>
  cron     — start/stop/restart return not-applicable; status runs is-active on timer
  library  — all four return not-applicable

Phase 16 (2026-05-09): upgrade/downgrade are real for all four classes.
Dispatch on app.upgrade.kind for non-ucc; ucc upgrades shell to `app-<slug> update`.
Honors version_pin.max ceiling. Rolls back on post-upgrade health failure when a
previous_version is supplied. State is written via lib.state.record().

Honors MANITOBA_DRY_RUN=1 env var to skip subprocess calls.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lib.manifest import App
import sys

_DEFAULT_TIMEOUT_S = 60.0
_TAIL = 200  # chars to keep from stdout/stderr


class LifecycleError(Exception):
    pass


@dataclass
class LifecycleResult:
    ok: bool
    duration_s: float
    stdout: str
    stderr: str
    reason: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _tail(s: str) -> str:
    return s[-_TAIL:] if len(s) > _TAIL else s


def _timeout(app: App, explicit: float | None) -> float:
    if explicit is not None:
        return explicit
    return float(app.defaults.get("lifecycle_timeout_s", _DEFAULT_TIMEOUT_S))


def _dry_run() -> bool:
    return os.environ.get("MANITOBA_DRY_RUN", "").strip() == "1"


def _not_applicable() -> LifecycleResult:
    return LifecycleResult(ok=False, duration_s=0.0, stdout="", stderr="", reason="not applicable")


def _ok(reason: str = "ok") -> LifecycleResult:
    return LifecycleResult(ok=True, duration_s=0.0, stdout="", stderr="", reason=reason)


def _fail(reason: str) -> LifecycleResult:
    return LifecycleResult(ok=False, duration_s=0.0, stdout="", stderr="", reason=reason)


def _run(args: list[str], timeout_s: float) -> LifecycleResult:
    if _dry_run():
        return LifecycleResult(ok=True, duration_s=0.0, stdout="", stderr="", reason="dry-run")

    t0 = time.monotonic()
    try:
        cp = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        duration_s = time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return LifecycleResult(
            ok=False,
            duration_s=timeout_s,
            stdout="",
            stderr="",
            reason="timeout",
        )

    stdout = _tail(cp.stdout or "")
    stderr = _tail(cp.stderr or "")

    if cp.returncode == 0:
        return LifecycleResult(ok=True, duration_s=duration_s, stdout=stdout, stderr=stderr, reason="ok")

    reason = f"exit {cp.returncode}"
    if stderr:
        reason = f"exit {cp.returncode}: {stderr[:100].strip()}"
    return LifecycleResult(ok=False, duration_s=duration_s, stdout=stdout, stderr=stderr, reason=reason)


# ---------------------------------------------------------------------------
# UCC dispatch
# ---------------------------------------------------------------------------

def _ucc_dormant(app: App) -> bool:
    """I-9 (QFLX-17): a `ucc_dormant` app is a converted app whose container
    stays installed and stopped as the rollback target. Fails closed: any value
    other than absent/null/false counts (same rule as lib/ucc_skip.py)."""
    v = app.raw.get("ucc_dormant")
    return v is not None and v is not False


def _dormant_refusal(app: App, verb: str) -> LifecycleResult:
    slug = app.raw.get("ucc_slug") or app.name
    return _fail(f"refused: {app.name} is ucc_dormant; app-{slug} {verb} would "
                 f"wake the dormant container (I-9)")


def _pending_swap(app: App) -> bool:
    """QFLX-25 (spec 5.9 step 7): the manifest flip (class systemd, ucc_dormant,
    unit) merges BEFORE the swap runs, marked `swap_state: pending-swap`. Until
    the follow-up deploy drops the marker the app is lifecycled as the UCC app
    it still is, exactly as scripts/lib/appctl dispatches it. Without this,
    pusher recovery would `systemctl --user restart` the native unit next to
    the live container (two runtimes on one config dir, I-6)."""
    return app.class_ == "systemd" and app.raw.get("swap_state") == "pending-swap"


def _ucc_verb(app: App, verb: str, timeout_s: float) -> LifecycleResult:
    slug = app.raw.get("ucc_slug") or app.name
    # Only `stop` may reach a dormant container (it can never wake one).
    # pending-swap: the container is still the live runtime, not dormant yet.
    if verb != "stop" and _ucc_dormant(app) and not _pending_swap(app):
        return _dormant_refusal(app, verb)
    return _run(["app-" + slug, verb], timeout_s)


_PORT_PROBE_TIMEOUT_S = 3.0


def _port_listening(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=_PORT_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def _panel_version(stdout: str) -> str:
    """`app-<slug> version` prints {"data": {"version": "4.0.20"}, "result": true}
    (box, 2026-10-10). Fall back to the raw last line for any other shape."""
    import json
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        return "?"
    try:
        v = json.loads(lines[-1])["data"]["version"]
        if isinstance(v, str) and v:
            return v
    except (ValueError, KeyError, TypeError):
        pass
    return lines[-1]


def _ucc_status(app: App, timeout_s: float) -> LifecycleResult:
    """`app-<slug> status` is NOT a UCC subcommand (spec F-3), so the old code
    always failed with a usage error. Status = `app-<slug> version` (the panel
    tool answers) plus a TCP probe of the app's port (the app answers). Apps
    with no port secret (unpackerr, postgres) report on version alone."""
    if _ucc_dormant(app) and not _pending_swap(app):
        return _dormant_refusal(app, "status")
    slug = app.raw.get("ucc_slug") or app.name
    result = _run(["app-" + slug, "version"], timeout_s)
    if result.reason == "dry-run":
        return result
    raw = app.health.raw if app.health else {}
    port_secret = raw.get("port_secret")
    if result.ok:
        version = _panel_version(result.stdout)
    elif "Unknown command" in (result.stdout + result.stderr):
        # app-postgres has no `version` subcommand (box, 2026-10-10): the
        # port alone decides, and with no port the answer is "unknown".
        version = "n/a"
        if not port_secret:
            result.reason = f"unknown: app-{slug} has no version subcommand and no port secret"
            return result
        result = LifecycleResult(ok=True, duration_s=result.duration_s, stdout=result.stdout,
                                 stderr=result.stderr, reason="ok")
    else:
        return result
    if not port_secret:
        result.reason = f"version {version}; no port"
        return result
    try:
        from lib.secrets import read_secret
        port = int(read_secret(port_secret))
    except (OSError, ValueError) as exc:
        return LifecycleResult(ok=False, duration_s=result.duration_s, stdout=result.stdout,
                               stderr=result.stderr,
                               reason=f"version {version}; port secret {port_secret} unreadable: {exc}")
    try:
        from lib.secrets import resolve_host
        host = resolve_host(raw)
    except (OSError, ValueError) as exc:
        return LifecycleResult(ok=False, duration_s=result.duration_s, stdout=result.stdout,
                               stderr=result.stderr,
                               reason=f"version {version}; host unresolved: {exc}")
    if _port_listening(host, port):
        result.reason = f"version {version}; port {port} listening"
        return result
    return LifecycleResult(ok=False, duration_s=result.duration_s, stdout=result.stdout,
                           stderr=result.stderr,
                           reason=f"version {version}; port {port} not listening")


# ---------------------------------------------------------------------------
# systemd dispatch
# ---------------------------------------------------------------------------

def _systemd_verb(app: App, verb: str, timeout_s: float) -> LifecycleResult:
    unit = app.raw.get("unit") or ""
    return _run(["systemctl", "--user", verb, unit], timeout_s)


def _systemd_status(app: App, timeout_s: float) -> LifecycleResult:
    unit = app.raw.get("unit") or ""
    result = _run(["systemctl", "--user", "is-active", unit], timeout_s)
    if result.ok:
        result.reason = result.stdout.strip() or "active"
    return result


# ---------------------------------------------------------------------------
# cron dispatch
# ---------------------------------------------------------------------------

def _cron_status(app: App, timeout_s: float) -> LifecycleResult:
    unit = app.raw.get("unit") or ""
    result = _run(["systemctl", "--user", "is-active", unit], timeout_s)
    if result.ok:
        result.reason = result.stdout.strip() or "active"
    return result


def _cron_start_service(app: App, timeout_s: float) -> LifecycleResult:
    # Re-invoke a oneshot's .service unit (the recovery flow's analogue of
    # restart for cron-class apps). reset-failed clears the prior failed
    # state so start can fire; --wait blocks until the run terminates so the
    # subsequent health probe sees the new Result rather than the old one.
    #
    # Manifest convention: timer-driven apps now declare `unit: <name>.service`
    # — we read it directly. Tolerate a stale `<name>.timer` value by stripping
    # the suffix; the .timer is a scheduler, not the thing we want to run.
    unit = app.raw.get("unit") or ""
    if unit.endswith(".timer"):
        unit = unit[: -len(".timer")] + ".service"

    # reset-failed: never fails fatally for a never-failed unit; ignore exit.
    _run(["systemctl", "--user", "reset-failed", unit], timeout_s)
    return _run(["systemctl", "--user", "start", "--wait", unit], timeout_s)


# ---------------------------------------------------------------------------
# Public API: start/stop/restart/status
# ---------------------------------------------------------------------------

def start(app: App, *, timeout_s: float | None = None) -> LifecycleResult:
    t = _timeout(app, timeout_s)
    if app.class_ == "ucc" or _pending_swap(app):
        return _ucc_verb(app, "start", t)
    if app.class_ == "systemd":
        return _systemd_verb(app, "start", t)
    if app.class_ == "cron" and app.raw.get("unit"):
        return _cron_start_service(app, t)
    if app.class_ in ("cron", "library"):
        return _not_applicable()
    raise LifecycleError(f"unknown class '{app.class_}' for app '{app.name}'")


def stop(app: App, *, timeout_s: float | None = None) -> LifecycleResult:
    t = _timeout(app, timeout_s)
    if app.class_ == "ucc" or _pending_swap(app):
        return _ucc_verb(app, "stop", t)
    if app.class_ == "systemd":
        return _systemd_verb(app, "stop", t)
    if app.class_ in ("cron", "library"):
        return _not_applicable()
    raise LifecycleError(f"unknown class '{app.class_}' for app '{app.name}'")


def restart(app: App, *, timeout_s: float | None = None) -> LifecycleResult:
    t = _timeout(app, timeout_s)
    if app.class_ == "ucc" or _pending_swap(app):
        return _ucc_verb(app, "restart", t)
    if app.class_ == "systemd":
        return _systemd_verb(app, "restart", t)
    if app.class_ == "cron" and app.raw.get("unit"):
        return _cron_start_service(app, t)
    if app.class_ in ("cron", "library"):
        return _not_applicable()
    raise LifecycleError(f"unknown class '{app.class_}' for app '{app.name}'")


def status(app: App) -> LifecycleResult:
    t = _timeout(app, None)
    if app.class_ == "ucc" or _pending_swap(app):
        return _ucc_status(app, t)
    if app.class_ == "systemd":
        return _systemd_status(app, t)
    if app.class_ == "cron":
        return _cron_status(app, t)
    if app.class_ == "library":
        return _not_applicable()
    raise LifecycleError(f"unknown class '{app.class_}' for app '{app.name}'")


# ---------------------------------------------------------------------------
# Phase 16: upgrade / downgrade
# ---------------------------------------------------------------------------

def _versions_env_path() -> Path:
    env = os.environ.get("MANITOBA_VERSIONS_ENV_PATH")
    if env:
        return Path(env)
    manifest_path = os.environ.get(
        "MANITOBA_MANIFEST_PATH",
        str(Path.home() / ".opt" / "maint" / "apps.yaml"),
    )
    sibling = Path(manifest_path).parent / "versions.env"
    if sibling.exists():
        return sibling
    # Fallback: repo-relative (when running from a checkout)
    repo_root = Path(__file__).parent.parent.parent.parent
    return repo_root / "versions.env"


def _read_versions_env(key: str) -> Optional[str]:
    path = _versions_env_path()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def _resolve_target_version(app: App, explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    if app.upgrade and app.upgrade.version_pin:
        vp = app.upgrade.version_pin
        if vp.source == "versions.env" and vp.key:
            v = _read_versions_env(vp.key)
            if v:
                return v
    raise LifecycleError(
        f"no target version for {app.name}: pass --to or configure version_pin"
    )


def _version_tuple(s: str) -> tuple:
    s = s.lstrip("v")
    out = []
    for part in s.split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out)


def _version_above(a: str, b: str) -> bool:
    return _version_tuple(a) > _version_tuple(b)


def _validate_max(app: App, target_version: str) -> None:
    if not (app.upgrade and app.upgrade.version_pin and app.upgrade.version_pin.max):
        return
    max_v = app.upgrade.version_pin.max
    if _version_above(target_version, max_v):
        reason = app.upgrade.version_pin.max_reason or "no reason given"
        raise LifecycleError(
            f"target {target_version} exceeds max ceiling {max_v} "
            f"for {app.name}: {reason}"
        )


def _post_health_probe(app: App) -> tuple[bool, str]:
    """Probe health repeatedly within health_timeout_s * 3 window."""
    from lib import health as health_mod

    timeout_s = float(app.defaults.get("health_timeout_s", 5.0))
    window = timeout_s * 3
    deadline = time.monotonic() + window
    last_reason = "no probe"
    while time.monotonic() < deadline:
        result = health_mod.probe(app, timeout_s=timeout_s)
        if result.ok:
            return (True, result.reason or "ok")
        last_reason = result.reason or "unknown"
        time.sleep(min(timeout_s, 2.0))
    return (False, last_reason)


def _expand(p: str) -> str:
    return os.path.expanduser(p) if p else p


# ---- per-kind apply functions --------------------------------------------

def _apply_pip_install(app: App, target_version: str, timeout_s: float) -> LifecycleResult:
    cfg = app.upgrade.raw
    venv_python = cfg.get("venv_python", "")
    package = cfg.get("package", app.name)
    return _run(
        [_expand(venv_python), "-m", "pip", "install", "--upgrade",
         f"{package}=={target_version}"],
        timeout_s,
    )


def _apply_git_checkout(app: App, target_version: str, timeout_s: float) -> LifecycleResult:
    cfg = app.upgrade.raw
    repo_path = _expand(cfg.get("repo_path", ""))
    r = _run(["bash", "-c", f"cd {repo_path} && git fetch --tags --all"], timeout_s)
    if not r.ok:
        return r
    r = _run(["bash", "-c", f"cd {repo_path} && git checkout {target_version}"], timeout_s)
    if not r.ok:
        return r
    for step in cfg.get("post_steps", []) or []:
        r = _run(["bash", "-c", step], timeout_s)
        if not r.ok:
            return r
    return _ok()


def _tar_flag_from_url(url: str) -> str:
    if url.endswith(".tar.xz"):
        return "-xJf"
    if url.endswith(".tar.bz2"):
        return "-xjf"
    return "-xzf"


def _apply_tarball_swap(app: App, target_version: str, timeout_s: float) -> LifecycleResult:
    cfg = app.upgrade.raw
    version_for_url = target_version.lstrip("v")
    url = cfg["url_template"].format(version=version_for_url)
    # QFLX-25: `{version}` in target_dir / target_path / post_steps lets a native
    # app land each release in ~/.apps/<slug>/bin/<ver> and flip `current`
    # (spec 5.1). Plain str.replace, NOT .format: post steps are shell and may
    # legitimately contain ${VAR} braces.
    target_path = _expand(_with_version(cfg.get("target_path") or "", version_for_url))
    target_dir = _expand(_with_version(cfg.get("target_dir") or "", version_for_url))
    extract_dir = os.path.dirname(target_path) if target_path else target_dir
    if not extract_dir:
        return _fail("tarball_swap missing target_path/target_dir")
    tmp = f"/tmp/manitoba-upgrade-{app.name}-{target_version}.tar"
    r = _run(["bash", "-c", f"curl -fsSL '{url}' -o '{tmp}'"], timeout_s)
    if not r.ok:
        return r
    # QFLX-35: some releases ship ONE static binary, not an archive (userdocs
    # qbittorrent-nox). `binary_name` says so: the download IS the binary, and it
    # is installed as <target_dir>/<binary_name> (0755) instead of untarred.
    binary_name = cfg.get("binary_name") or ""
    if binary_name:
        if "/" in binary_name or binary_name in (".", ".."):
            return _fail(f"tarball_swap binary_name must be a bare file name: {binary_name!r}")
        cmd = (f"mkdir -p '{extract_dir}' && "
               f"install -m 0755 '{tmp}' '{extract_dir}/{binary_name}'")
    else:
        cmd = (f"mkdir -p '{extract_dir}' && "
               f"tar {_tar_flag_from_url(url)} '{tmp}' -C '{extract_dir}'")
    r = _run(["bash", "-c", cmd], timeout_s)
    if not r.ok:
        return r
    for step in cfg.get("post_steps", []) or []:
        r = _run(["bash", "-c", _with_version(step, version_for_url)], timeout_s)
        if not r.ok:
            return r
    return _ok()


def _with_version(text: str, version: str) -> str:
    return text.replace("{version}", version)


def _apply_zip_swap(app: App, target_version: str, timeout_s: float) -> LifecycleResult:
    cfg = app.upgrade.raw
    version_for_url = target_version.lstrip("v")
    url = cfg["url_template"].format(version=version_for_url)
    # QFLX-27: `{version}` in target_dir / post_steps lets a native app land each
    # release in ~/.apps/<slug>/bin/<ver> (same plain-replace rule as tarball_swap).
    target_dir = _expand(_with_version(cfg.get("target_dir") or "", version_for_url))
    if not target_dir:
        return _fail("zip_swap missing target_dir")
    tmp = f"/tmp/manitoba-upgrade-{app.name}-{target_version}.zip"
    r = _run(["bash", "-c", f"curl -fsSL '{url}' -o '{tmp}'"], timeout_s)
    if not r.ok:
        return r
    r = _run(
        ["bash", "-c", f"mkdir -p '{target_dir}' && unzip -oq '{tmp}' -d '{target_dir}'"],
        timeout_s,
    )
    if not r.ok:
        return r
    for step in cfg.get("post_steps", []) or []:
        r = _run(["bash", "-c", _with_version(step, version_for_url)], timeout_s)
        if not r.ok:
            return r
    return _ok()


def _apply_ucc_update(app: App, target_version: Optional[str], timeout_s: float) -> LifecycleResult:
    # `update` would rebuild and START the container: never on a dormant app (I-9).
    if _ucc_dormant(app):
        return _dormant_refusal(app, "update")
    slug = app.raw.get("ucc_slug") or app.name
    # Stop first; tolerate failure (app may already be stopped)
    _run(["app-" + slug, "stop"], timeout_s)
    return _run(["app-" + slug, "update"], timeout_s)


def _restart_after_upgrade(app: App, timeout_s: float) -> LifecycleResult:
    if app.class_ == "systemd":
        r = _run(["systemctl", "--user", "daemon-reload"], timeout_s)
        if not r.ok:
            return r
        unit = app.raw.get("unit") or ""
        return _run(["systemctl", "--user", "restart", unit], timeout_s)
    # ucc: app-<slug> update typically restarts on its own — no-op here
    # cron, library: no service to restart
    return _ok()


def _apply_upgrade(app: App, target_version: str, timeout_s: float) -> LifecycleResult:
    if app.class_ == "ucc":
        return _apply_ucc_update(app, target_version, timeout_s)
    if not app.upgrade:
        raise LifecycleError(f"no upgrade config for {app.name}")
    kind = app.upgrade.kind
    if kind == "pip_install":
        return _apply_pip_install(app, target_version, timeout_s)
    if kind == "git_checkout":
        return _apply_git_checkout(app, target_version, timeout_s)
    if kind == "tarball_swap":
        return _apply_tarball_swap(app, target_version, timeout_s)
    if kind == "zip_swap":
        return _apply_zip_swap(app, target_version, timeout_s)
    raise LifecycleError(f"unknown upgrade kind '{kind}' for {app.name}")


def _native_swap_slug(app: App) -> Optional[str]:
    """Slug for swap-state lookups when `app` is a converted (systemd) app."""
    if app.class_ != "systemd":
        return None
    return app.raw.get("ucc_slug") or app.name


def _soak_gate(slug: str) -> dict:
    try:
        from lib import swapstate
        return swapstate.soak_gate(slug)
    except Exception as exc:  # unreadable state dir: a never-swapped app must not wedge
        sys.stderr.write("lifecycle.py: soak gate unreadable (failing open): " + repr(exc) + "\n")
        return {"refused": False, "reason": "unreadable"}


def _close_rollback_window(slug: str) -> None:
    try:
        from lib import swapstate
        swapstate.close_rollback_window(slug)
    except Exception as exc:
        sys.stderr.write("lifecycle.py: rollback_window close FAILED: " + repr(exc) + "\n")


def _record_state(app: App, event: str, version: str, reason: str) -> None:
    try:
        from lib import state as state_mod
        state_dir = os.environ.get("MANITOBA_STATE_DIR")
        if state_dir:
            path = Path(state_dir) / "state.json"
        else:
            path = Path.home() / ".opt" / "maint" / "state.json"
        # On successful upgrade, preserve the prior version as previous_version
        # so recovery.py can target it for auto-downgrade after attempt-cap.
        extra: dict = {}
        if event == "upgraded":
            try:
                old = state_mod.read(path).get("apps", {}).get(app.name, {})
                old_v = old.get("version")
                if isinstance(old_v, str) and old_v and old_v != version:
                    extra["previous_version"] = old_v
            except Exception as _exc:
                sys.stderr.write("lifecycle.py: previous_version lookup failed (best-effort, continuing): "
                                 + repr(_exc) + "\n")
        state_mod.record(path, app.name, event=event, version=version,
                         reason=reason, **extra)
    except Exception as _exc:
        sys.stderr.write("lifecycle.py: STATE RECORD failed - rollback data and `status` are now stale: "
                         + repr(_exc) + "\n")


def upgrade(
    app: App,
    target_version: Optional[str] = None,
    *,
    timeout_s: float | None = None,
    previous_version: Optional[str] = None,
    _allow_rollback: bool = True,
) -> LifecycleResult:
    """Upgrade `app` to `target_version` (or to version_pin default).

    On post-upgrade health failure, attempts rollback to `previous_version`
    if supplied. Records state via lib.state. Never raises on subprocess
    failure — collapses to LifecycleResult. Raises LifecycleError only for
    config errors (unknown kind, target above max ceiling).
    """
    t = _timeout(app, timeout_s)
    if _pending_swap(app):
        # The UCC container still serves: a native upgrade now would break the
        # exact-version parity the swap relies on (I-10).
        return _fail(f"refused: {app.name} is pending-swap; native upgrades wait "
                     f"until the swap completes")
    target = _resolve_target_version(app, target_version)
    _validate_max(app, target)

    # QFLX-21 soak gate (spec 5.7). Only a forward upgrade is gated: downgrade
    # and the internal rollback pass _allow_rollback=False and must never be
    # blocked by a soak.
    native_slug = _native_swap_slug(app)
    if native_slug and _allow_rollback:
        gate = _soak_gate(native_slug)
        if gate.get("refused"):
            _record_state(app, "skipped", target, "soak: " + str(gate.get("reason")))
            return _fail("skipped: soak (" + str(gate.get("reason")) + ")")

    apply_result = _apply_upgrade(app, target, t)
    if not apply_result.ok:
        _record_state(app, "upgrade_failed", target, apply_result.reason)
        return apply_result
    if native_slug:
        _close_rollback_window(native_slug)

    restart_result = _restart_after_upgrade(app, t)
    if not restart_result.ok:
        _record_state(app, "upgrade_failed", target, f"restart: {restart_result.reason}")
        return _fail(f"restart after upgrade failed: {restart_result.reason}")

    ok, reason = _post_health_probe(app)
    if ok:
        _record_state(app, "upgraded", target, "ok")
        return _ok(f"upgraded to {target}")

    # Health failed — try rollback if we have a previous version
    if _allow_rollback and previous_version:
        try:
            rb = upgrade(
                app,
                target_version=previous_version,
                timeout_s=timeout_s,
                _allow_rollback=False,
            )
        except Exception as exc:
            _record_state(app, "rollback_failed", previous_version, str(exc))
            return _fail(
                f"upgrade health failed ({reason}); rollback exception: {exc}"
            )
        if rb.ok:
            _record_state(app, "rolled_back", previous_version,
                          f"upgrade health failed: {reason}")
            return _fail(
                f"upgrade health failed ({reason}); rolled back to {previous_version}"
            )
        _record_state(app, "rollback_failed", previous_version, rb.reason)
        return _fail(
            f"upgrade health failed ({reason}); rollback FAILED ({rb.reason})"
        )

    _record_state(app, "upgrade_failed", target, f"health: {reason}")
    return _fail(f"post-upgrade health failed: {reason}")


def downgrade(
    app: App,
    target_version: str,
    *,
    timeout_s: float | None = None,
) -> LifecycleResult:
    """Downgrade `app` to `target_version`.

    Same shape as upgrade, but no rollback (the caller is already moving
    backwards). UCC apps return 'not supported' — Ultra.cc tooling does not
    expose arbitrary version selection; operator must use docker-pull paths.
    """
    if app.class_ == "ucc":
        return _fail(
            "ucc downgrade not supported by Ultra.cc tooling — "
            "use docker-pull-and-restart manually"
        )
    return upgrade(
        app,
        target_version=target_version,
        timeout_s=timeout_s,
        _allow_rollback=False,
    )

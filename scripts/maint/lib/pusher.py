"""lib/pusher.py — Kuma push-loop service.

Probes every app in the manifest that has a push token via lib/health.py
and POSTs the result to Kuma's /api/push/<token> endpoint.

This inverts the normal Kuma pull model because Kuma runs in its own
network namespace on Ultra.cc and cannot reach apps bound to host loopback.
The pusher runs in the host netns where it can reach 127.0.0.1:<port>.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Optional

import requests

from lib import health as health_mod
from lib import manifest as manifest_mod
from lib import recovery as recovery_mod

log = logging.getLogger(__name__)


# Auto-heal trigger threshold: number of CONSECUTIVE failed health probes
# before the pusher invokes recovery.trigger_async. With pusher's 60s cycle,
# the default 3 strikes = ~180s of sustained failure before any restart fires
# — keeps transient blips from triggering recovery flapping.
# Override via env: MANITOBA_AUTOHEAL_STRIKES.
_STRIKE_THRESHOLD = int(os.environ.get("MANITOBA_AUTOHEAL_STRIKES", "3"))

# Per-app consecutive-failure counter. Module-level so it survives across
# push_once() calls within the same `serve()` loop. Resets on success.
_consecutive_failures: dict[str, int] = {}


def reset_strike_counter(app_name: str | None = None) -> None:
    """Reset the auto-heal strike counter. Used by tests; also callable from
    operator CLI if a known-down app is intentionally being held down."""
    if app_name is None:
        _consecutive_failures.clear()
    else:
        _consecutive_failures.pop(app_name, None)


# ---------------------------------------------------------------------------
# Secrets / path helpers
# ---------------------------------------------------------------------------

def _tokens_path() -> Path:
    env = os.environ.get("MANITOBA_KUMA_TOKENS")
    if env:
        return Path(env).expanduser()
    home_path = Path("~/secrets/kuma-push-tokens.json").expanduser()
    if home_path.exists():
        return home_path
    # Repo fallback: pusher.py lives at scripts/maint/lib/pusher.py
    lib_dir = Path(__file__).resolve().parent
    repo_tokens = lib_dir.parent.parent.parent / "secrets" / "kuma-push-tokens.json"
    if repo_tokens.exists():
        return repo_tokens
    raise FileNotFoundError(
        "kuma-push-tokens.json not found — set MANITOBA_KUMA_TOKENS or deploy to ~/secrets/"
    )


def _load_tokens(tokens_path: str | Path) -> dict[str, str]:
    path = Path(tokens_path).expanduser()
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Core push logic
# ---------------------------------------------------------------------------

def push_once(
    *,
    manifest: manifest_mod.Manifest,
    kuma_url: str = "http://127.0.0.1:42005",
    tokens: dict[str, str],
    notify_disabled: bool = True,
) -> dict[str, str]:
    """Probe every app with a pushToken, POST result to Kuma.

    Returns {app_name: "ok"|"http_<N>"|"error: <msg>"} per pushed app.

    Apps with no entry in `tokens` are silently skipped.
    Apps with kuma_monitor=None are silently skipped.
    """
    results: dict[str, str] = {}

    for app in manifest.apps():
        if app.kuma_monitor is None:
            continue

        token = tokens.get(app.name)
        if token is None:
            continue

        result = health_mod.probe(app)
        status = "up" if result.ok else "down"
        params: dict[str, object] = {
            "status": status,
            "msg": result.reason,
        }
        if result.latency_ms is not None:
            params["ping"] = result.latency_ms

        push_url = f"{kuma_url}/api/push/{token}"
        try:
            resp = requests.get(push_url, params=params, timeout=5)
            if resp.status_code == 200:
                results[app.name] = "ok"
                log.info("pushed %s → %s (%s)", app.name, status, result.reason)
            else:
                results[app.name] = f"http_{resp.status_code}"
                log.warning(
                    "push %s → %s but Kuma returned HTTP %s",
                    app.name, status, resp.status_code,
                )
        except Exception as exc:
            results[app.name] = f"error: {exc}"
            log.error("push %s failed: %s", app.name, exc)

        # Auto-heal: track CONSECUTIVE failures and only invoke recovery
        # after _STRIKE_THRESHOLD strikes (default 3). The webhook receiver
        # was the original auto-heal entry point but Kuma in its isolated
        # netns can't reach host loopback, so the pusher (host netns, already
        # does health probes) is where this hook fires. recovery.trigger_async
        # dedupes per-app + caps parallelism, so even if we keep counting past
        # threshold the second/third trigger_async returns "already_running".
        if result.ok:
            if app.name in _consecutive_failures:
                # First success after a strike streak — log the recovery
                if _consecutive_failures[app.name] >= _STRIKE_THRESHOLD:
                    log.info("auto-heal %s: probe RECOVERED after %d strike(s)",
                             app.name, _consecutive_failures[app.name])
                _consecutive_failures.pop(app.name, None)
            # Always-on safety: clear any prior permanent-failure mark so the
            # next outage gets a fresh 3-attempt loop.
            recovery_mod.clear_permanent_failure(app.name)
        else:
            n = _consecutive_failures.get(app.name, 0) + 1
            _consecutive_failures[app.name] = n
            # Annotate the Kuma msg with strike state so the operator can
            # triage from the dashboard without ssh'ing in.
            if n >= _STRIKE_THRESHOLD:
                if recovery_mod.is_permanently_failed(app.name):
                    params["msg"] = f"{result.reason} [perma-failed: operator needed]"
                else:
                    params["msg"] = f"{result.reason} [strike {n}/{_STRIKE_THRESHOLD}: recovery]"
            else:
                params["msg"] = f"{result.reason} [strike {n}/{_STRIKE_THRESHOLD}]"
            if n < _STRIKE_THRESHOLD:
                log.info("auto-heal %s: strike %d/%d (probe reason: %s)",
                         app.name, n, _STRIKE_THRESHOLD, result.reason)
            else:
                # On the threshold-th strike (and every cycle after, while
                # still down): trigger_async — recovery's per-app lock
                # dedupes so only one recovery actually runs per outage,
                # AND _permanently_failed prevents thread storms after the
                # 3-attempt loop exhausts.
                decision = recovery_mod.trigger_async(app, manifest=manifest)
                log.info("auto-heal %s: strike %d/%d -> recovery=%s (reason: %s)",
                         app.name, n, _STRIKE_THRESHOLD, decision, result.reason)

    return results


# ---------------------------------------------------------------------------
# Long-running service loop
# ---------------------------------------------------------------------------

def serve(
    *,
    manifest_path: str | Path,
    tokens_path: str | Path,
    kuma_url: str = "http://127.0.0.1:42005",
    interval_s: int = 60,
    run_once: bool = False,
) -> None:
    """Long-running loop: every `interval_s`, run push_once().

    Catches KeyboardInterrupt + SIGTERM and exits cleanly.
    If `run_once=True`, does one pass and returns.
    """
    manifest = manifest_mod.load(manifest_path)
    tokens = _load_tokens(tokens_path)

    pushed_count = sum(
        1 for app in manifest.apps()
        if app.kuma_monitor is not None and app.name in tokens
    )
    log.info(
        "pusher starting: %d apps will be pushed every %ds → %s",
        pushed_count, interval_s, kuma_url,
    )

    _stop = False

    def _handle_signal(signum, frame):
        nonlocal _stop
        log.info("pusher received signal %s, exiting", signum)
        _stop = True

    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except ValueError:
        # signal.signal() only works on the main thread; skip when called from tests
        pass

    # Self-heartbeat: push status=up to a dedicated "Manitoba Pusher" Kuma
    # monitor each cycle. Without this, a pusher crashloop manifests as
    # *every* app monitor going stale at once with no signal that the
    # pusher itself is the cause. Gated on token presence so the code
    # path is a no-op until the operator wires the monitor (one-shot
    # `bootstrap-kuma-monitors.py` re-run). Token key is the canonical
    # "manitoba-pusher" string so it can't collide with an app name.
    _SELF_TOKEN_KEY = "manitoba-pusher"

    try:
        while not _stop:
            log.info("pusher: starting push cycle")
            results = push_once(manifest=manifest, kuma_url=kuma_url, tokens=tokens)
            ok_count = sum(1 for v in results.values() if v == "ok")
            log.info(
                "pusher: cycle complete — %d/%d ok", ok_count, len(results)
            )
            # Self-heartbeat after the cycle so it's a meaningful signal:
            # if we got here, the cycle didn't crash.
            self_token = tokens.get(_SELF_TOKEN_KEY)
            if self_token:
                try:
                    requests.get(
                        f"{kuma_url}/api/push/{self_token}",
                        params={"status": "up",
                                "msg": f"cycle ok={ok_count}/{len(results)}"},
                        timeout=5,
                    )
                except Exception as exc:
                    # Don't let a Kuma blip kill the pusher loop.
                    log.warning("pusher self-heartbeat failed: %s", exc)
            if run_once:
                return
            deadline = time.monotonic() + interval_s
            while not _stop and time.monotonic() < deadline:
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("pusher received KeyboardInterrupt, exiting")

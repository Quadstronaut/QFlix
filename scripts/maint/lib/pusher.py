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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from lib import fleet as fleet_mod
from lib import health as health_mod
from lib import manifest as manifest_mod
from lib import notify as notify_mod
from lib import recovery as recovery_mod
from lib import suppression as suppression_mod

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

# Kuma push-endpoint timeout + retry. Kuma's /api/push endpoint intermittently
# stalls >5s under SQLite/retention load (observed: sporadic bursts of "Read
# timed out" across whatever apps are pushed during the stall, while the
# endpoint is sub-10ms between bursts). The original 5s/no-retry dropped those
# heartbeats, making healthy monitors flap DOWN. A 15s timeout absorbs the
# common stall and one retry recovers a transient one. Override via env.
_PUSH_TIMEOUT_S = float(os.environ.get("MANITOBA_KUMA_PUSH_TIMEOUT", "15"))
_PUSH_RETRIES = int(os.environ.get("MANITOBA_KUMA_PUSH_RETRIES", "1"))


def _push_get(kuma_url: str, token: str, params: dict):
    """GET Kuma's /api/push/<token> with a bounded timeout + retry on read
    timeout only. A timeout means Kuma is briefly slow (retry delivers the
    heartbeat); a ConnectionError means Kuma is actually down (fail fast, no
    retry — retrying a refused socket just wastes a cycle). Returns the
    Response; re-raises the timeout if all attempts are exhausted."""
    url = f"{kuma_url}/api/push/{token}"
    for attempt in range(_PUSH_RETRIES + 1):
        try:
            return requests.get(url, params=params, timeout=_PUSH_TIMEOUT_S)
        except requests.Timeout as exc:
            if attempt < _PUSH_RETRIES:
                log.warning("push timeout (attempt %d/%d), retrying: %s",
                            attempt + 1, _PUSH_RETRIES + 1, exc)
                continue
            raise


def _utcnow() -> datetime:
    """Current UTC time. Indirected through a module function so tests can
    patch the clock to land inside/outside an app's pause window."""
    return datetime.now(timezone.utc)


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
    fleet_state_path=None,
) -> dict[str, str]:
    """Probe every app with a pushToken, POST result to Kuma.

    Returns {app_name: "ok"|"http_<N>"|"error: <msg>"} per pushed app.

    Apps with no entry in `tokens` are silently skipped.
    Apps with kuma_monitor=None are silently skipped.
    """
    results: dict[str, str] = {}
    # Built during the per-app loop for fleet storm detection (sub-project C).
    _probe_ok: dict[str, bool] = {}

    # Weekly maintenance window: the orchestrator (lib/window.py) holds
    # $STATE_DIR/lock for the Monday window AND the 11:30 UTC cp-upgrade sweep
    # that overlaps it, during which apps are stopped/upgraded/restarted on
    # purpose. While the lock is present, treat every app like push-suppression:
    # push UP with a [maint-window] note and skip probe + recovery, so the
    # pusher's auto-heal can't fight an in-progress `app-* upgrade`. The Kuma
    # webhook already queues during the window (lib/kuma.do_POST); this closes
    # the same gap on the pusher, the operative auto-heal path. Resolved once
    # per cycle (cheap) rather than per-app. deep-check recovers anything still
    # down at window close; the window-watchdog clears a stale lock at 15:00.
    in_maint_window = suppression_mod.in_maintenance_window()

    for app in manifest.apps():
        if app.kuma_monitor is None:
            continue

        token = tokens.get(app.name)
        if token is None:
            continue

        # Manual push-suppression: a monitor knowingly muted (e.g. app awaiting
        # an upstream fix). Push UP with a note so Kuma stays green and neither
        # an alert nor recovery fires, and skip probing entirely. Count it as
        # healthy for the fleet storm calc. The self-destructing unsuppress
        # watcher removes the registry entry once the app is live again. This
        # is the only local-file lever that silences a PUSH monitor — Kuma's
        # admin pause API is operator-only.
        sup_reason = suppression_mod.push_suppressed(app.name)
        if sup_reason:
            _probe_ok[app.name] = True
            params = {"status": "up", "msg": f"[SUPPRESSED] {sup_reason}"}
            try:
                resp = _push_get(kuma_url, token, params)
                results[app.name] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
                log.info("pushed %s → up [SUPPRESSED: %s]", app.name, sup_reason)
            except Exception as exc:
                results[app.name] = f"error: {exc}"
                log.error("push (suppressed) %s failed: %s", app.name, exc)
            continue

        # Fair-use pause window: the app is INTENTIONALLY stopped right now
        # (historically tdarr-node 18:00-23:00 UTC; retired 2026-08-20, and no
        # app declares a pause_window today — this branch is dormant, not dead).
        # Without this, the pusher probed it `inactive`, accrued strikes, and
        # auto-healed it ~2min into the pause every day — a false "recovered"
        # alert that also defeated the 5h fair-use pause. Treat the window like
        # push-suppression: push UP (Kuma stays green, clearly labelled), clear
        # any stale strikes, and skip probe + recovery entirely.
        if suppression_mod.in_pause_window(app, now=_utcnow()):
            _probe_ok[app.name] = True
            _consecutive_failures.pop(app.name, None)
            params = {"status": "up", "msg": "[paused: fair-use quiet hours]"}
            try:
                resp = _push_get(kuma_url, token, params)
                results[app.name] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
                log.info("pushed %s → up [PAUSED: quiet hours]", app.name)
            except Exception as exc:
                results[app.name] = f"error: {exc}"
                log.error("push (paused) %s failed: %s", app.name, exc)
            continue

        # Maintenance window active: app may be mid-upgrade. Push UP (Kuma stays
        # green, clearly labelled), clear stale strikes, and skip probe +
        # recovery — same treatment as the fair-use pause window above.
        if in_maint_window:
            _probe_ok[app.name] = True
            _consecutive_failures.pop(app.name, None)
            params = {"status": "up", "msg": "[maint-window: upgrades in progress]"}
            try:
                resp = _push_get(kuma_url, token, params)
                results[app.name] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
                log.info("pushed %s → up [maint-window]", app.name)
            except Exception as exc:
                results[app.name] = f"error: {exc}"
                log.error("push (maint-window) %s failed: %s", app.name, exc)
            continue

        result = health_mod.probe(app)
        _probe_ok[app.name] = result.ok
        status = "up" if result.ok else "down"
        params: dict[str, object] = {
            "status": status,
            "msg": result.reason,
        }
        if result.latency_ms is not None:
            params["ping"] = result.latency_ms

        try:
            resp = _push_get(kuma_url, token, params)
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
            # Reconcile a stale failure record left by an out-of-band recovery
            # (qBit boot-bind-race healer, UCC auto-restart, operator restart)
            # the maint recovery loop never saw, so state.json's last_recovery
            # stops showing a phantom `failed`/`down`. reconcile_healthy is
            # self-guarded (never raises) and a no-op when the record is clean.
            recovery_mod.reconcile_healthy(app.name)
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
                #
                # B1 suppression: skip recovery while UCC maintenance is
                # active for ucc-class apps. The gate blocks `app-* start`
                # so recovery would only churn to permanently-failed. Status
                # is still pushed (above); we annotate the Kuma msg so the
                # dashboard explains the held state. Do NOT increment toward
                # permanent-failure (counter stays, but trigger skipped).
                if suppression_mod.recovery_suppressed(app):
                    params["msg"] = f"{result.reason} [strike {n}/{_STRIKE_THRESHOLD}] [ucc-maint: recovery suppressed]"
                    log.info("auto-heal %s: strike %d/%d -> recovery SUPPRESSED (ucc maintenance active)",
                             app.name, n, _STRIKE_THRESHOLD)
                else:
                    decision = recovery_mod.trigger_async(app, manifest=manifest)
                    log.info("auto-heal %s: strike %d/%d -> recovery=%s (reason: %s)",
                             app.name, n, _STRIKE_THRESHOLD, decision, result.reason)

    # -----------------------------------------------------------------------
    # Fleet aggregate push + storm collapse (sub-project C).
    # Runs AFTER the per-app loop. Gated on "qflix-fleet" token presence
    # so it's a no-op until the operator runs bootstrap — exactly like
    # the self-heartbeat gate in serve(). Never raises; best-effort.
    # -----------------------------------------------------------------------
    fleet_token = tokens.get("qflix-fleet")
    if fleet_token:
        try:
            fleet_result = fleet_mod.evaluate(
                results,
                probe_ok=_probe_ok,
                state_path=fleet_state_path,
            )
            down_count = fleet_result["down_count"]
            total = fleet_result["total"]
            storm_active = fleet_result["storm_active"]
            edge = fleet_result["edge"]

            # Push aggregate monitor status each cycle.
            fleet_status = "down" if storm_active else "up"
            fleet_msg = (f"storm: {down_count}/{total} down"
                         if storm_active
                         else f"{down_count}/{total} down")
            try:
                _push_get(kuma_url, fleet_token,
                          {"status": fleet_status, "msg": fleet_msg})
            except Exception as exc:
                log.warning("fleet aggregate push failed: %s", exc)

            # Emit notify only on edge transitions — never per-cycle repeats.
            if edge == "onset":
                # List first ~8 failing app names for operator triage.
                failing = [name for name, ok in _probe_ok.items() if not ok][:8]
                names_str = ", ".join(failing) if failing else "(none)"
                msg = (f"⚠ Fleet storm: {down_count}/{total} monitors down at once"
                       f" — {names_str}")
                try:
                    notify_mod.notify(msg, level="warning")
                except Exception as exc:
                    log.warning("fleet storm notify failed: %s", exc)
            elif edge == "clear":
                msg = f"Fleet storm cleared ({down_count}/{total} down now)"
                try:
                    notify_mod.notify(msg, level="info")
                except Exception as exc:
                    log.warning("fleet clear notify failed: %s", exc)
        except Exception as exc:
            # Never let fleet logic break the pusher loop.
            log.warning("fleet evaluate/push block raised unexpectedly: %s", exc)

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
                    _push_get(kuma_url, self_token,
                              {"status": "up",
                               "msg": f"cycle ok={ok_count}/{len(results)}"})
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

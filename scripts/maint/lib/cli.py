"""lib/cli.py — CLI dispatch for manitoba-maint.

All argument parsing and verb dispatch lives here. The entrypoint script
(scripts/maint/manitoba-maint) is a thin shim that calls main().

Exit codes:
  0 — success
  1 — user error (unknown app, unknown verb, bad args)
  2 — operational failure (lifecycle ok=False, manifest invalid, etc.)
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Manifest path resolution
# ---------------------------------------------------------------------------

def _manifest_path() -> Path:
    if env := os.environ.get("MANITOBA_MANIFEST"):
        return Path(env).expanduser()
    home = Path("~/.opt/maint/apps.yaml").expanduser()
    if home.exists():
        return home
    # Repo fallback (running from source checkout).
    # cli.py lives at scripts/maint/lib/cli.py → three levels up is repo root.
    lib_dir = Path(__file__).resolve().parent
    repo_manifest = lib_dir.parent.parent.parent / "manifest" / "apps.yaml"
    if repo_manifest.exists():
        return repo_manifest
    raise FileNotFoundError(
        "manifest not found — set MANITOBA_MANIFEST or install to ~/.opt/maint/apps.yaml"
    )


def _state_path() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    if env:
        return Path(env) / "state.json"
    return Path.home() / ".opt" / "maint" / "state.json"


# ---------------------------------------------------------------------------
# Status table rendering
# ---------------------------------------------------------------------------

_COL_WIDTHS = {
    "app": 22,
    "class": 9,
    "status": 8,
    "latency": 10,
    "recovery": 20,
}


def _render_status_table(rows: list[dict]) -> str:
    header = (
        f"{'APP':<{_COL_WIDTHS['app']}}"
        f"{'CLASS':<{_COL_WIDTHS['class']}}"
        f"{'STATUS':<{_COL_WIDTHS['status']}}"
        f"{'LATENCY':<{_COL_WIDTHS['latency']}}"
        f"LAST RECOVERY"
    )
    sep = "-" * (sum(_COL_WIDTHS.values()) + 14)
    lines = [header, sep]
    for row in rows:
        status_sym = "✓" if row["ok"] else "✗"
        latency = f"{row['latency_ms']}ms" if row["latency_ms"] is not None else "-"
        lines.append(
            f"{row['app']:<{_COL_WIDTHS['app']}}"
            f"{row['class_']:<{_COL_WIDTHS['class']}}"
            f"{status_sym:<{_COL_WIDTHS['status']}}"
            f"{latency:<{_COL_WIDTHS['latency']}}"
            f"{row['last_recovery']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace, manifest, state_data: dict) -> int:
    import datetime as _datetime
    from lib import health as health_mod

    apps_state = state_data.get("apps", {})

    if args.app and args.app != "__all__":
        # Single app
        try:
            app = manifest.app(args.app)
        except KeyError:
            print(f"error: unknown app '{args.app}'", file=sys.stderr)
            return 1
        app_list = [app]
    else:
        app_list = list(manifest.apps())

    # Capture timestamp once at run start (before parallel probes begin)
    captured_at = _datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    # Parallel probes
    def _probe_one(app):
        result = health_mod.probe(app)
        app_state = apps_state.get(app.name, {})
        last_recovery = app_state.get("event", "") or ""
        updated_at = app_state.get("updated_at", "")
        if last_recovery and updated_at:
            last_recovery = f"{last_recovery} ({updated_at[:10]})"
        return {
            "app": app.name,
            "display": app.kuma_monitor if app.kuma_monitor else app.name,
            "class_": app.class_,
            "probe_kind": app.health.kind,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "last_recovery": last_recovery,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_probe_one, a): a for a in app_list}
        rows = []
        for fut in concurrent.futures.as_completed(futures):
            rows.append(fut.result())

    # Sort by app name for deterministic output
    rows.sort(key=lambda r: r["app"])

    if getattr(args, "json", False):
        # Machine-readable JSON output (QuadstroNot status contract schema_version 1)
        total = len(rows)
        up = sum(1 for r in rows if r["ok"])
        payload = {
            "schema_version": 1,
            "captured_at": captured_at,
            "summary": {"total": total, "up": up, "down": total - up},
            "apps": [
                {
                    "app": r["app"],
                    "display": r["display"],
                    "class": r["class_"],
                    "probe_kind": r["probe_kind"],
                    "ok": r["ok"],
                    "latency_ms": r["latency_ms"],
                    "last_recovery": r["last_recovery"],
                }
                for r in rows
            ],
        }
        print(json.dumps(payload, sort_keys=False))
        return 0

    print(_render_status_table(rows))
    return 0


def _cmd_lifecycle(verb: str, args: argparse.Namespace, manifest) -> int:
    from lib import lifecycle as lifecycle_mod

    try:
        app = manifest.app(args.app)
    except KeyError:
        print(f"error: unknown app '{args.app}'", file=sys.stderr)
        return 1

    fn = getattr(lifecycle_mod, verb)
    result = fn(app)
    print(f"{verb} {args.app}: {result.reason}")
    return 0 if result.ok else 2


def _cmd_upgrade(args: argparse.Namespace, manifest) -> int:
    from lib import lifecycle as lifecycle_mod
    from lib.lifecycle import LifecycleError

    try:
        app = manifest.app(args.app)
    except KeyError:
        print(f"error: unknown app '{args.app}'", file=sys.stderr)
        return 1

    # Capture previous version (if any) for rollback
    previous = _previous_version(args.app)
    try:
        result = lifecycle_mod.upgrade(
            app,
            target_version=args.version,
            previous_version=previous,
        )
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"upgrade {args.app}: {result.reason}")
    return 0 if result.ok else 2


def _cmd_downgrade(args: argparse.Namespace, manifest) -> int:
    from lib import lifecycle as lifecycle_mod
    from lib.lifecycle import LifecycleError

    try:
        app = manifest.app(args.app)
    except KeyError:
        print(f"error: unknown app '{args.app}'", file=sys.stderr)
        return 1

    try:
        result = lifecycle_mod.downgrade(app, target_version=args.version)
    except LifecycleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"downgrade {args.app}: {result.reason}")
    return 0 if result.ok else 2


def _previous_version(app_name: str) -> str | None:
    """Read state.json and return the version field for the given app (or None)."""
    try:
        from lib import state as state_mod
        path = _state_path()
        data = state_mod.read(path)
        entry = data.get("apps", {}).get(app_name, {})
        v = entry.get("version")
        return v if isinstance(v, str) and v else None
    except Exception:
        return None


def _cmd_recover(args: argparse.Namespace, manifest) -> int:
    from lib import recovery as recovery_mod

    # Validate app exists before entering recovery
    try:
        manifest.app(args.app)
    except KeyError:
        print(f"error: unknown app '{args.app}'", file=sys.stderr)
        return 1

    result = recovery_mod.run(args.app, manifest=manifest)
    print(result)
    ok_events = {"recovered", "healthy_locally_kuma_down"}
    return 0 if result.get("event") in ok_events else 2


def _cmd_window_run(args: argparse.Namespace, manifest) -> int:
    from lib.window import WindowOrchestrator

    orchestrator = WindowOrchestrator(manifest=manifest, dry_run=args.dry_run)
    summary = orchestrator.run(force=args.force)
    smoke_pass = sum(1 for v in summary.smoke_results.values() if v)
    smoke_total = len(summary.smoke_results)
    print(
        f"window closed: "
        f"processed={summary.queue_processed} "
        f"succeeded={summary.queue_succeeded} "
        f"smoke={smoke_pass}/{smoke_total}"
    )
    return 0


def _cmd_window_status(args: argparse.Namespace, manifest) -> int:
    from lib.window import WindowOrchestrator

    orchestrator = WindowOrchestrator(manifest=manifest)
    st = orchestrator.status()
    if st["present"]:
        print(
            f"lock present: pid={st['pid']} "
            f"started={st['started_at']} "
            f"alive={st['alive']}"
        )
    else:
        print("no lock present")
    return 0


def _cmd_window_watchdog(args: argparse.Namespace) -> int:
    from lib import window as window_mod

    cleared = window_mod.watchdog_clear_stale_lock()
    if cleared:
        print("cleared stale lock")
    else:
        print("no action")
    return 0


from lib.secrets import read_secret as _secret_read  # noqa: E402, F401


def _cmd_webhook(args: argparse.Namespace) -> int:
    port_env = os.environ.get("MANITOBA_WEBHOOK_PORT")
    if port_env:
        port = int(port_env)
    else:
        port = int(_secret_read("maintenance.port"))

    from lib import kuma
    kuma.serve(port=port)
    return 0


def _tokens_path() -> Path:
    env = os.environ.get("MANITOBA_KUMA_TOKENS")
    if env:
        return Path(env).expanduser()
    home_path = Path("~/secrets/kuma-push-tokens.json").expanduser()
    if home_path.exists():
        return home_path
    lib_dir = Path(__file__).resolve().parent
    repo_tokens = lib_dir.parent.parent.parent / "secrets" / "kuma-push-tokens.json"
    if repo_tokens.exists():
        return repo_tokens
    raise FileNotFoundError(
        "kuma-push-tokens.json not found — set MANITOBA_KUMA_TOKENS or deploy to ~/secrets/"
    )


def _cmd_pusher(args: argparse.Namespace, manifest_path: Path) -> int:
    from lib import pusher as pusher_mod

    try:
        tokens_path = _tokens_path()
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    kuma_url = os.environ.get("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")
    try:
        pusher_mod.serve(
            manifest_path=manifest_path,
            tokens_path=tokens_path,
            kuma_url=kuma_url,
            run_once=args.once,
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_manifest_validate(args: argparse.Namespace, manifest_path: Path) -> int:
    from lib.manifest import load, ManifestError

    try:
        load(manifest_path)
        print("ok")
        return 0
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"manifest not found: {exc}", file=sys.stderr)
        return 2


def _cmd_canary_push(args: argparse.Namespace, manifest) -> int:
    """`manitoba-maint canary push <name>` — run canary script and push to Kuma.

    Exit codes:
      0 — canary passed and push succeeded (or no token → still 0)
      1 — unknown canary name or missing tokens config
      2 — canary script failed (status=down pushed to Kuma)
    """
    try:
        canary = manifest.canary(args.name)
    except KeyError:
        print(f"error: unknown canary '{args.name}'", file=sys.stderr)
        return 1

    # Resolve script path relative to repo root (cli.py is at scripts/maint/lib/cli.py)
    lib_dir = Path(__file__).resolve().parent
    repo_root = lib_dir.parent.parent.parent
    script_path = repo_root / canary.script

    # Run the canary script
    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        result_returncode = 127
        result_stdout = ""
        result_stderr = f"bash not found or script not executable: {script_path}"
        # Build a mock result namespace
        class _R:
            returncode = 127
            stdout = ""
            stderr = result_stderr
        result = _R()

    if result.returncode == 0:
        status = "up"
        # Prefer stdout for the success message; fall back to generic
        msg = (result.stdout or "PASS").strip()[:200]
        exit_code = 0
    else:
        status = "down"
        # Prefer stderr for failure detail (canaries write FAIL: to stderr)
        msg = (result.stderr or result.stdout or "FAIL").strip()[:200]
        exit_code = 2

    # Load tokens and push to Kuma
    try:
        tokens_path = _tokens_path()
    except FileNotFoundError:
        # No token config — still return canary pass/fail, just don't push
        return exit_code

    try:
        with open(tokens_path, "r", encoding="utf-8") as fh:
            tokens = json.load(fh)
    except Exception as exc:
        print(f"error: failed to load tokens: {exc}", file=sys.stderr)
        return exit_code

    token_key = f"canary-{args.name}"
    token = tokens.get(token_key)
    if token is None:
        # No token for this canary — skip push silently
        return exit_code

    kuma_url = os.environ.get("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")
    push_url = f"{kuma_url}/api/push/{token}"
    params: dict[str, object] = {"status": status, "msg": msg}

    try:
        resp = requests.get(push_url, params=params, timeout=5)
        if resp.status_code != 200:
            print(
                f"warn: Kuma push returned HTTP {resp.status_code} for canary '{args.name}'",
                file=sys.stderr,
            )
    except Exception as exc:
        print(f"warn: Kuma push failed for canary '{args.name}': {exc}", file=sys.stderr)

    return exit_code


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manitoba-maint",
        description="Manitoba maintenance system CLI",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # status
    p_status = sub.add_parser("status", help="health snapshot")
    p_status.add_argument(
        "app",
        nargs="?",
        default=None,
        help="app name (omit or use --all for all apps)",
    )
    p_status.add_argument("--all", dest="all_apps", action="store_true")
    p_status.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="emit machine-readable JSON payload to stdout (QuadstroNot contract)",
    )

    # start / stop / restart
    for verb in ("start", "stop", "restart"):
        p = sub.add_parser(verb, help=f"class-aware {verb}")
        p.add_argument("app")

    # upgrade
    p_upgrade = sub.add_parser("upgrade", help="upgrade app (phase 2)")
    p_upgrade.add_argument("app")
    p_upgrade.add_argument("--to", dest="version", default=None)

    # downgrade
    p_downgrade = sub.add_parser("downgrade", help="downgrade app (phase 2)")
    p_downgrade.add_argument("app")
    p_downgrade.add_argument("--to", dest="version", required=True)

    # recover
    p_recover = sub.add_parser("recover", help="manual recovery")
    p_recover.add_argument("app")

    # window
    p_window = sub.add_parser("window", help="maintenance window commands")
    window_sub = p_window.add_subparsers(dest="window_command", metavar="SUBCOMMAND")
    window_sub.required = True

    pw_run = window_sub.add_parser("run", help="run maintenance window")
    pw_run.add_argument("--dry-run", action="store_true", dest="dry_run")
    pw_run.add_argument("--force", action="store_true")

    window_sub.add_parser("status", help="lock present? since when?")
    window_sub.add_parser("watchdog", help="one-shot stale-lock check")

    # webhook
    sub.add_parser("webhook", help="Phase 8 placeholder")

    # pusher
    p_pusher = sub.add_parser("pusher", help="Kuma push-loop service")
    p_pusher.add_argument(
        "--once",
        action="store_true",
        help="run one push cycle and exit (for testing)",
    )

    # manifest
    p_manifest = sub.add_parser("manifest", help="manifest commands")
    manifest_sub = p_manifest.add_subparsers(
        dest="manifest_command", metavar="SUBCOMMAND"
    )
    manifest_sub.required = True
    manifest_sub.add_parser("validate", help="config-lint")

    # kuma — drift / admin commands
    p_kuma = sub.add_parser("kuma", help="Kuma admin commands")
    kuma_sub = p_kuma.add_subparsers(dest="kuma_command", metavar="SUBCOMMAND")
    kuma_sub.required = True
    kuma_sub.add_parser(
        "audit",
        help="compare manifest kuma_monitor names vs live Kuma monitors",
    )

    # canary — canary push commands
    p_canary = sub.add_parser("canary", help="canary health checks")
    canary_sub = p_canary.add_subparsers(dest="canary_command", metavar="SUBCOMMAND")
    canary_sub.required = True
    p_canary_push = canary_sub.add_parser("push", help="run canary and push result to Kuma")
    p_canary_push.add_argument("name", help="canary name (e.g. movie, anime, deletion, mobile-ux)")

    # qbit — qBittorrent admin commands (password rotation + *arr cascade)
    p_qbit = sub.add_parser("qbit", help="qBittorrent admin")
    qbit_sub = p_qbit.add_subparsers(dest="qbit_command", metavar="SUBCOMMAND")
    qbit_sub.required = True
    p_rotate = qbit_sub.add_parser(
        "rotate-pw",
        help="rotate qBit WebUI password and cascade to every *arr's "
             "DownloadClients config so torrent adding doesn't break",
    )
    p_rotate.add_argument(
        "--new", required=True,
        help="new qBit WebUI password (will be written to secrets/qbittorrent.password on success)",
    )
    p_rotate.add_argument(
        "--old",
        help="current qBit pw (defaults to secrets/qbittorrent.password)",
    )
    p_rotate.add_argument(
        "--dry-run", action="store_true",
        help="show planned actions; do not log into qBit or mutate any *arr",
    )

    # ucc — UCC upstream-maintenance detection
    p_ucc = sub.add_parser("ucc", help="UCC upstream-maintenance detection")
    ucc_sub = p_ucc.add_subparsers(dest="ucc_command", metavar="SUBCOMMAND")
    ucc_sub.required = True
    ucc_sub.add_parser("detect", help="run one probe + state update (timer entrypoint)")
    ucc_sub.add_parser("status", help="read-only print of current UCC window state")

    # deep-check — post-window probe + autoheal sweep (sub-project D)
    p_deep_check = sub.add_parser(
        "deep-check",
        help="probe all manifest apps and recover anything still down (post-window sweep)",
    )
    p_deep_check.add_argument(
        "--reason",
        default="manual",
        help="label recorded in deep-check.jsonl and the summary notify (default: manual)",
    )

    return parser


def _cmd_qbit_rotate(args, manifest) -> int:
    """`manitoba-maint qbit rotate-pw --new <pw>` — change qBit WebUI pw +
    cascade to every *arr's DownloadClients config. Idempotent: if the
    *arr already has the new pw stored, skips that one.

    Exits:
      0 — qBit rotated, every *arr cascade succeeded
      1 — qBit login or set-pw failed (no change made)
      2 — qBit ok but at least one *arr cascade failed (partial state)
    """
    from lib import qbit
    res = qbit.rotate_password(
        manifest,
        args.new,
        old_password=args.old,
        dry_run=args.dry_run,
    )
    if not res.qbit_login_old_ok:
        print("error: qBit login with old password failed (check secrets/qbittorrent.password)",
              file=sys.stderr)
        return 1
    if not args.dry_run and not res.qbit_set_pw_ok:
        print("error: qBit setPreferences failed (pw may be partially set?)",
              file=sys.stderr)
        return 1
    if not args.dry_run and not res.qbit_login_new_ok:
        print("error: qBit login with NEW password failed — pw rotation incomplete",
              file=sys.stderr)
        return 1

    print(f"qbit ok: login(old)={res.qbit_login_old_ok} set={res.qbit_set_pw_ok} "
          f"login(new)={res.qbit_login_new_ok}")
    for slug, n in res.arrs_rewritten.items():
        marker = "[dry] " if args.dry_run else ""
        print(f"  {marker}{slug}: {n} downloadclient(s) rewritten")
    if res.arrs_failed:
        print(f"FAILED *arrs: {', '.join(res.arrs_failed)}", file=sys.stderr)
        return 2
    if not args.dry_run:
        print(f"new password persisted to secrets/qbittorrent.password")
    return 0


def _cmd_ucc_detect(args: argparse.Namespace) -> int:
    """`manitoba-maint ucc detect` — run one probe + state update.

    Exits:
      0 — probe ran and state was written (gated/clear/probe-error are all ok).
      2 — unexpected operational failure (e.g. unhandled exception).
    """
    from lib import ucc as ucc_mod

    state_dir_env = os.environ.get("MANITOBA_STATE_DIR")
    state_path = (
        Path(state_dir_env) / "ucc-window.json"
        if state_dir_env
        else None
    )
    try:
        state = ucc_mod.detect(state_path=state_path)
        active = state.get("active", False)
        result = state.get("last_probe_result", "unknown")
        print(f"ucc detect: active={active} last_probe_result={result}", file=sys.stderr)
    except Exception as exc:
        print(f"error: ucc detect failed: {exc}", file=sys.stderr)
        return 2

    # B: respond to state transitions (pin/unpin incident, email, notify, deep-check).
    try:
        from lib import ucc_response as ucc_response_mod
        response_state_path = (
            Path(state_dir_env) / "ucc-response-state.json"
            if state_dir_env
            else None
        )
        ucc_response_mod.respond(state, response_state_path=response_state_path)
    except Exception as exc:
        # Best-effort — log but don't fail the detect command.
        print(f"warning: ucc_response.respond failed: {exc}", file=sys.stderr)

    return 0


def _cmd_ucc_status(args: argparse.Namespace) -> int:
    """`manitoba-maint ucc status` — read-only print of current UCC window state.

    Always exits 0 (read-only, cannot operationally fail).
    """
    from lib import ucc as ucc_mod

    state_dir_env = os.environ.get("MANITOBA_STATE_DIR")
    state_path = (
        Path(state_dir_env) / "ucc-window.json"
        if state_dir_env
        else None
    )
    state = ucc_mod.read_state(state_path) if state_path else ucc_mod.status()
    if not state:
        print("ucc: no state recorded (no probe has run yet)")
    else:
        active = state.get("active", False)
        result = state.get("last_probe_result", "unknown")
        probe_op = state.get("probe_op", "unknown")
        last_probe = state.get("last_probe_at", "never")
        consecutive_clear = state.get("consecutive_clear", 0)
        consecutive_error = state.get("consecutive_error", 0)
        print(f"ucc: active={active} last_probe_result={result}")
        print(f"     probe_op={probe_op}")
        print(f"     last_probe_at={last_probe}")
        print(f"     consecutive_clear={consecutive_clear} consecutive_error={consecutive_error}")
        if active:
            print(f"     first_detected_at={state.get('first_detected_at', 'unknown')}")
            print(f"     last_confirmed_at={state.get('last_confirmed_at', 'unknown')}")
    return 0


def _cmd_deep_check(args: argparse.Namespace) -> int:
    """`manitoba-maint deep-check [--reason <str>]` — probe all manifest apps
    and recover anything still down. Exits 0 unless the run couldn't start.
    """
    from lib import deep_check as dc_mod

    result = dc_mod.run_deep_check(reason=args.reason)
    print(result)
    return 0


def _cmd_kuma_audit(args, manifest) -> int:
    """`manitoba-maint kuma audit` — print drift report between manifest
    and live Kuma. Exits 0 if no drift, 2 if drift detected, 3 on error."""
    from lib import kuma as _kuma_mod
    report = _kuma_mod.audit_monitors(manifest)
    if "error" in report:
        print(f"error: {report['error']}", file=sys.stderr)
        return 3
    print(f"manifest monitors: {report['manifest_count']}    "
          f"kuma monitors: {report['live_count']}    "
          f"matched: {len(report['matched'])}")
    if report["manifest_only"]:
        print("\nMissing from Kuma (need bootstrap):")
        for m in report["manifest_only"]:
            print(f"  - {m}")
    if report["kuma_only"]:
        print("\nOrphaned in Kuma (not in manifest):")
        for m in report["kuma_only"]:
            print(f"  - {m}")
    if report.get("external_ignored"):
        print("\nIgnored (declared external in manifest):")
        for m in report["external_ignored"]:
            print(f"  - {m}")
    if not report["manifest_only"] and not report["kuma_only"]:
        print("\nno drift — manifest and Kuma agree.")
        return 0
    return 2


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(argv: list[str], *, manifest_path: Optional[Path] = None, _manifest=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve manifest path (allow override for testing)
    if manifest_path is None:
        try:
            manifest_path = _manifest_path()
        except FileNotFoundError as exc:
            # manifest validate still needs to report properly
            if getattr(args, "command", None) == "manifest":
                return _cmd_manifest_validate(args, Path("/nonexistent"))
            print(f"error: {exc}", file=sys.stderr)
            return 1

    # manifest validate doesn't need a pre-loaded manifest
    if args.command == "manifest":
        if args.manifest_command == "validate":
            return _cmd_manifest_validate(args, manifest_path)

    if args.command == "webhook":
        return _cmd_webhook(args)

    if args.command == "pusher":
        return _cmd_pusher(args, manifest_path)

    if args.command == "window" and args.window_command == "watchdog":
        return _cmd_window_watchdog(args)

    # ucc subcommands — no manifest required
    if args.command == "ucc":
        if args.ucc_command == "detect":
            return _cmd_ucc_detect(args)
        if args.ucc_command == "status":
            return _cmd_ucc_status(args)

    # deep-check — no manifest required (loads its own via MANITOBA_MANIFEST_PATH)
    if args.command == "deep-check":
        return _cmd_deep_check(args)

    # All remaining commands need a loaded manifest
    # Allow test injection via _manifest parameter
    if _manifest is not None:
        manifest = _manifest
    else:
        from lib.manifest import load, ManifestError

        try:
            manifest = load(manifest_path)
        except ManifestError as exc:
            print(f"manifest error: {exc}", file=sys.stderr)
            return 2
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.command == "kuma" and args.kuma_command == "audit":
        return _cmd_kuma_audit(args, manifest)

    if args.command == "canary" and args.canary_command == "push":
        return _cmd_canary_push(args, manifest)

    if args.command == "qbit" and args.qbit_command == "rotate-pw":
        return _cmd_qbit_rotate(args, manifest)

    # status
    if args.command == "status":
        # Normalise app argument
        if args.all_apps or args.app is None:
            args.app = "__all__"
        from lib import state as state_mod
        state_data = state_mod.read(_state_path())
        return _cmd_status(args, manifest, state_data)

    # lifecycle verbs
    if args.command in ("start", "stop", "restart"):
        return _cmd_lifecycle(args.command, args, manifest)

    if args.command == "upgrade":
        return _cmd_upgrade(args, manifest)

    if args.command == "downgrade":
        return _cmd_downgrade(args, manifest)

    if args.command == "recover":
        return _cmd_recover(args, manifest)

    # window subcommands
    if args.command == "window":
        if args.window_command == "run":
            return _cmd_window_run(args, manifest)
        if args.window_command == "status":
            return _cmd_window_status(args, manifest)

    print(f"error: unhandled command '{args.command}'", file=sys.stderr)
    return 1

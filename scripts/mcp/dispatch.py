#!/usr/bin/env python3
"""scripts/mcp/dispatch.py — the QFlix Admin forced command.

Single SSH entry point for the Android app. Reads one verb from
$SSH_ORIGINAL_COMMAND, routes it, and emits one JSON envelope on stdout.

PRIVACY (spec 2026-08-03): no verb returns Plex sessions, watch history, or
per-member data. scripts/mcp/plex.py supports a sessions snapshot; that mode is
deliberately unreachable from here. A test asserts the verb table stays clean —
adding such a verb is a spec change, not an implementation detail.

BLAST RADIUS: the operator accepted full blast radius on 2026-08-03. This file
is a MANIFEST of what the phone can do, not a security boundary. It is still the
one place the action set is written down.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                   # scripts/mcp/lib
sys.path.insert(0, str(HERE.parent / "maint"))  # scripts/maint/lib

MAX_LINES = 20
MAX_LINES_CEILING = 200


@dataclass
class VerbSpec:
    handler: Callable
    arity: int          # required positional args after the verb
    help: str


def envelope(*, verb: str, target: Optional[str], ok: bool, verdict: str,
             lines: List[str], elapsed_s: float, max_lines: int = MAX_LINES) -> dict:
    """The one shape every verb returns, success or failure.

    `verdict` is a single human sentence — it is what the phone toasts.
    `lines` is the expandable detail, capped so a flaky mobile link is never
    asked to carry an unbounded log.
    """
    capped = min(max(int(max_lines), 1), MAX_LINES_CEILING)
    return {
        "ok": bool(ok),
        "verb": verb,
        "target": target,
        "verdict": verdict,
        "lines": [str(x) for x in (lines or [])][-capped:],
        "elapsed_s": round(float(elapsed_s), 2),
    }


VERBS: dict = {}


def _help_lines() -> List[str]:
    return ["%-24s %s" % (name, spec.help) for name, spec in sorted(VERBS.items())]


def _verb_help(argv: List[str]) -> dict:
    return envelope(verb="help", target=None, ok=True,
                    verdict="%d verbs available" % len(VERBS),
                    lines=_help_lines(), elapsed_s=0.0,
                    max_lines=MAX_LINES_CEILING)


# Registered HERE, not in Task 2: it makes the privacy test above non-vacuous
# from the first commit, and `help` needs nothing from the router.
VERBS["help"] = VerbSpec(handler=_verb_help, arity=0, help="list every verb")


def parse_command(raw: Optional[str]) -> tuple:
    """Split $SSH_ORIGINAL_COMMAND into (verb, args).

    An empty command means the operator SSH'd with no command at all — answer
    with help rather than an opaque failure.
    """
    if not raw or not raw.strip():
        return ("help", [])
    parts = raw.strip().split()
    return (parts[0], parts[1:])


def dispatch(argv: List[str]) -> dict:
    started = time.time()
    verb = argv[0] if argv else "help"
    args = argv[1:]

    spec = VERBS.get(verb)
    if spec is None:
        return envelope(verb=verb, target=None, ok=False,
                        verdict="unknown verb %r" % verb,
                        lines=_help_lines(), elapsed_s=time.time() - started,
                        max_lines=MAX_LINES_CEILING)

    if len(args) < spec.arity:
        return envelope(verb=verb, target=None, ok=False,
                        verdict="%s expects %d argument(s), got %d"
                                % (verb, spec.arity, len(args)),
                        lines=[spec.help], elapsed_s=time.time() - started)

    try:
        return spec.handler(args)
    except Exception as exc:  # a handler must never take the connection down
        return envelope(verb=verb, target=(args[0] if args else None), ok=False,
                        verdict="%s failed: %s" % (verb, exc.__class__.__name__),
                        lines=[str(exc)], elapsed_s=time.time() - started)


LIFECYCLE_CLASSES = ("ucc", "systemd")
_MANIFEST_PATH = HERE.parent.parent / "manifest" / "apps.yaml"


def _load_manifest():
    from lib import manifest as manifest_mod
    return manifest_mod.load(_MANIFEST_PATH)


def _verb_app_list(argv: List[str]) -> dict:
    started = time.time()
    man = _load_manifest()
    rows = []
    # DEVIATION from brief: `man.apps()` returns an Iterator[App] (full
    # objects), not name strings — `sorted(man.apps())` raises TypeError
    # (App is an undecorated @dataclass, not orderable) and `man.app(name)`
    # expects a string key, not an App. Sort the App objects by .name and
    # read .name/.class_ straight off them; see task-3-report.md.
    for app in sorted(man.apps(), key=lambda a: a.name):
        if app.class_ in LIFECYCLE_CLASSES:
            rows.append("%s %s" % (app.name, app.class_))
    return envelope(verb="app.list", target=None, ok=True,
                    verdict="%d apps with a lifecycle" % len(rows),
                    lines=rows, elapsed_s=time.time() - started,
                    max_lines=MAX_LINES_CEILING)


# app_status.py emits five sections. `top5` is per-member BY NAME —
# top5_watch gives {"user": friendly_name, "hours", "plays"} from Tautulli and
# top5_requests gives {"user": displayName, "count"} from Seerr. That is
# exactly the member viewing activity the spec forbids, so `status` asks for
# every section EXCEPT top5. `streams` is kept: it is aggregate counts
# (streams/users/transcodes/wan_kbps), not identities.
STATUS_SECTIONS = "quota,kuma,streams,downloads"


def _verb_status(argv: List[str]) -> dict:
    """The Dashboard doc, minus the per-member section.

    NOT a bare passthrough. Heartbeat v2's forced command ran app_status.py
    with no arguments and therefore received top5; this verb is the first
    caller that filters, which is what makes the privacy constraint true on
    the wire rather than only in the UI.
    """
    import subprocess
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(HERE / "app_status.py"), "--emit-json",
         "--sections", STATUS_SECTIONS],
        capture_output=True, text=True, timeout=30)
    ok = proc.returncode == 0 and bool(proc.stdout.strip())
    env = envelope(verb="status", target=None, ok=ok,
                   verdict="status doc emitted" if ok else "app_status.py failed",
                   lines=(proc.stderr or "").splitlines(),
                   elapsed_s=time.time() - started)
    if ok:
        env["doc"] = json.loads(proc.stdout)
    return env


VERBS["app.list"] = VerbSpec(handler=_verb_app_list, arity=0,
                             help="list the apps that have a lifecycle, with their class")
VERBS["status"] = VerbSpec(handler=_verb_status, arity=0,
                           help="the Dashboard status document")


def _ucc_gate_up() -> bool:
    """True when the Ultra.cc maintenance gate is active. While it is,
    Ultra.cc BLOCKS `app-* start` — so answering 'the gate is up' is more
    useful than relaying an opaque failure."""
    try:
        from lib import suppression
        return bool(suppression.ucc_active())
    except Exception:
        return False


def _lifecycle(verb_name: str, argv: List[str]) -> dict:
    from lib import lifecycle as lifecycle_mod
    started = time.time()
    slug = argv[0]
    man = _load_manifest()

    try:
        app = man.app(slug)
    except Exception:
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="unknown app %r" % slug,
                        lines=["run app.list for the 24 apps that have a lifecycle"],
                        elapsed_s=time.time() - started)

    if app.class_ not in LIFECYCLE_CLASSES:
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="%s is class %s and has no lifecycle"
                                % (slug, app.class_),
                        lines=["only ucc and systemd apps start/stop/restart"],
                        elapsed_s=time.time() - started)

    action = verb_name.split(".", 1)[1]
    if action == "start" and app.class_ == "ucc" and _ucc_gate_up():
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="the Ultra.cc gate is up; `app-%s start` is blocked "
                                "until it clears" % slug,
                        lines=["restart is still available and is usually what you want"],
                        elapsed_s=time.time() - started)

    fn = {"start": lifecycle_mod.start,
          "stop": lifecycle_mod.stop,
          "restart": lifecycle_mod.restart}[action]
    res = fn(app)
    how = ("app-%s %s" % (app.raw.get("ucc_slug", slug), action)
           if app.class_ == "ucc"
           else "systemctl --user %s %s" % (action, app.raw.get("unit", slug)))
    return envelope(
        verb=verb_name, target=slug, ok=bool(res.ok),
        verdict="%s %s (%s: %s)" % (action, slug, app.class_, how)
                if res.ok else "%s %s FAILED: %s" % (action, slug, res.reason),
        lines=(res.stdout or "").splitlines() + (res.stderr or "").splitlines(),
        elapsed_s=time.time() - started)


# The IIFE is load-bearing: `lambda argv: _lifecycle("app." + _a, argv)` would
# late-bind _a and make all three verbs run whichever action registered last.
for _a in ("start", "stop", "restart"):
    VERBS["app." + _a] = VerbSpec(
        handler=(lambda name: (lambda argv: _lifecycle(name, argv)))("app." + _a),
        arity=1, help="%s one app by slug (ucc or systemd)" % _a)


ARR_SLUGS = ("sonarr", "sonarr2", "radarr", "radarr2")


def _run_mcp(script: str, args: List[str], timeout_s: float = 60.0) -> tuple:
    """Run a sibling MCP script in --emit-json mode.

    Returns (ok, parsed_json_or_None, stderr). Those scripts always exit 0 in
    JSON mode by convention (missing.py's own comment: returning non-zero made
    the MCP caller discard the body as ssh-failed, masking which *arr broke),
    so `ok` keys off parseable stdout, not the return code.
    """
    import subprocess
    proc = subprocess.run([sys.executable, str(HERE / script)] + args,
                          capture_output=True, text=True, timeout=timeout_s)
    try:
        return (True, json.loads(proc.stdout), proc.stderr or "")
    except Exception:
        return (False, None, (proc.stderr or proc.stdout or "").strip())


def _verb_search_wanted(argv: List[str]) -> dict:
    started = time.time()
    slug = argv[0]
    if slug not in ARR_SLUGS:
        return envelope(verb="arr.search_wanted", target=slug, ok=False,
                        verdict="%s is not an *arr" % slug,
                        lines=["valid: " + ", ".join(ARR_SLUGS)],
                        elapsed_s=time.time() - started)
    # missing.py's --slug is OPTIONAL there (omitted = fan out to all four
    # *arrs); passing it explicitly is what keeps this verb single-target.
    ok, doc, err = _run_mcp("missing.py", ["--slug", slug, "--emit-json"], 120.0)
    return envelope(verb="arr.search_wanted", target=slug, ok=ok,
                    verdict="wanted search queued on %s" % slug if ok
                            else "search failed on %s" % slug,
                    lines=(json.dumps(doc, indent=2).splitlines() if doc
                           else err.splitlines()),
                    elapsed_s=time.time() - started)


def _verb_unstick(argv: List[str]) -> dict:
    started = time.time()
    slug, queue_id = argv[0], argv[1]
    # unstick.py's real flags (verified against its ArgumentParser): --slug,
    # --queue-id (int) or --hash, --emit-json. It requires --queue-id or
    # --hash; we always supply --queue-id from here.
    ok, doc, err = _run_mcp(
        "unstick.py",
        ["--slug", slug, "--queue-id", str(queue_id), "--emit-json"], 90.0)
    return envelope(verb="unstick", target=slug, ok=ok,
                    verdict="unstuck %s queue item %s" % (slug, queue_id) if ok
                            else "unstick failed on %s" % slug,
                    lines=(json.dumps(doc, indent=2).splitlines() if doc
                           else err.splitlines()),
                    elapsed_s=time.time() - started)


# PRIVACY (spec 2026-08-03): logs.py's --app table (verified by reading the
# file) routes to *every* app with a log file, including tautulli (a
# per-member watch/session history tool), seerr (a per-member request
# tracker), plex (its own server log can carry X-Plex-Username / session
# detail on stream lines), and listmonk (subscriber email addresses appear in
# send/bounce lines). logs.py tails those files verbatim — it applies no
# redaction — so wiring --app straight through from the phone (as a literal
# transcription of the brief would) puts member identities on the wire. That
# is the same class of leak Task 3 found in app_status.py's top5 section,
# reached through a different script; `status` was restricted to safe
# sections there, and `logs` is restricted to safe apps here, on the same
# reasoning. See task-5-report.md for the write-up.
LOG_SAFE_APPS = (
    "sonarr", "sonarr2", "radarr", "radarr2", "prowlarr",
    "bazarr", "bazarr2", "kometa", "buildarr", "recyclarr",
    "qbittorrent",
    "tdarr-server", "tdarr-node",
    "maint-pusher", "maint-webhook", "maint-window",
)
# nginx is deliberately ABSENT: its error lines carry
# `client: <ip> ... request: "<method> <path>"` for routine upstream
# failures, and this instance reverse-proxies every app's subpath — so a
# tautulli/seerr hiccup would surface member IP + requested URI here. It
# can return only if someone verifies the configured log level suppresses
# the client/request fields.


def _format_log_line(item) -> str:
    """logs.py's JSON `lines` field is list[dict] (ts/level/message/
    source_file) for a real app tail, not list[str] — a bare str(dict) would
    still "work" but reads as a raw Python repr on the phone. Render the
    fields worth showing; fall back to str() for anything else (e.g. the
    plain strings the unit tests substitute via a monkeypatched _run_mcp)."""
    if isinstance(item, dict):
        return "%s %s %s" % (item.get("ts") or "?", item.get("level") or "?",
                             item.get("message") or "")
    return str(item)


def _verb_logs(argv: List[str]) -> dict:
    started = time.time()
    slug = argv[0]
    if slug not in LOG_SAFE_APPS:
        return envelope(verb="logs", target=slug, ok=False,
                        verdict="%s logs are not exposed over this verb" % slug,
                        lines=["valid: " + ", ".join(sorted(LOG_SAFE_APPS))],
                        elapsed_s=time.time() - started)
    tail = MAX_LINES
    if "--tail" in argv:
        try:
            tail = int(argv[argv.index("--tail") + 1])
        except (ValueError, IndexError):
            tail = MAX_LINES
    # Forward --tail to logs.py itself. Left unforwarded, logs.py's own
    # default (5000) governs the tail/journalctl window and the subprocess +
    # JSON round-trip carries up to 5000 records that envelope() then
    # discards down to `tail`. Clamp what we ask logs.py for to
    # MAX_LINES_CEILING — nothing past that survives envelope()'s cap either,
    # so asking logs.py for more (e.g. a phone-supplied --tail 99999) would
    # just make the tail/journalctl read bigger for no visible benefit.
    fetch_tail = min(max(tail, 1), MAX_LINES_CEILING)
    ok, doc, err = _run_mcp(
        "logs.py", ["--app", slug, "--tail", str(fetch_tail), "--emit-json"], 45.0)
    raw_lines = (doc or {}).get("lines") or (err.splitlines() if err else [])
    lines = [_format_log_line(item) for item in raw_lines]
    return envelope(verb="logs", target=slug, ok=ok,
                    verdict="%s log tail" % slug if ok else "could not read %s log" % slug,
                    lines=lines, elapsed_s=time.time() - started, max_lines=tail)


VERBS["arr.search_wanted"] = VerbSpec(handler=_verb_search_wanted, arity=1,
                                      help="fire a wanted/missing search on one *arr")
VERBS["unstick"] = VerbSpec(handler=_verb_unstick, arity=2,
                            help="delete+blocklist a stuck queue item: unstick <slug> <queue-id>")
VERBS["logs"] = VerbSpec(handler=_verb_logs, arity=1,
                         help="tail one app's log: logs <slug> [--tail N]")


def _peek_one(slug: str) -> dict:
    ok, doc, err = _run_mcp("arr_library_peek.py", ["--slug", slug, "--emit-json"], 45.0)
    return doc if doc else {"slug": slug, "kind": "?", "titles": [],
                            "ok": False, "error": err}


def _usage_one(slug: str) -> dict:
    ok, doc, err = _run_mcp("arr_disk_usage.py", ["--slug", slug, "--emit-json"], 45.0)
    return doc if doc else {"slug": slug, "bytes": 0, "human": "0.0 B",
                            "title_count": 0, "ok": False, "error": err}


def _verb_starr(argv: List[str]) -> dict:
    """All four *arr rows in ONE call — see the spec's round-trip note.

    Exactly one _peek_one + one _usage_one per slug (8 subprocess calls
    total, never 8 SSH handshakes). A dead instance is folded into the page
    rather than taking it down: `ok` stays True so the page still renders,
    the degraded slug(s) are named in `verdict`, and that instance's own
    `ok: False` survives untouched inside `arrs[slug]` for the phone to
    render as a per-row error state.
    """
    started = time.time()
    arrs = {}
    degraded = []
    for slug in ARR_SLUGS:
        p = _peek_one(slug)
        u = _usage_one(slug)
        arrs[slug] = {"peek": p, "usage": u}
        if not p.get("ok") or not u.get("ok"):
            degraded.append(slug)
    env = envelope(
        verb="starr", target=None, ok=True,
        verdict="4 *arrs" if not degraded
                else "4 *arrs, degraded: " + ", ".join(degraded),
        lines=["%s %s %d titles" % (s, arrs[s]["usage"]["human"],
                                    len(arrs[s]["peek"]["titles"]))
               for s in ARR_SLUGS],
        elapsed_s=time.time() - started)
    env["arrs"] = arrs
    return env


def _quota_raw() -> dict:
    """Disk headroom. `quota -w` is the authority on this shared seedbox;
    df would report the whole array, not the slot.

    DEVIATION from brief: the brief's Step 3 code let subprocess.run's
    FileNotFoundError (quota not installed — true on every dev machine,
    including this one) escape the function. dispatch()'s blanket handler
    would still have caught it, but the resulting envelope would carry
    verdict "quota failed: FileNotFoundError" with used_gb/total_gb/percent
    all MISSING (envelope() would run, but _verb_quota's env.update() never
    would) — from a caller's perspective the verb effectively raised, which
    is exactly what "must degrade to ok=False rather than raising" rules
    out. Also guards the numeric parse itself: a quota line with a
    non-numeric blocks/quota field now falls back to the zero reading
    instead of raising ValueError mid-loop.
    """
    import subprocess
    used_kb = total_kb = 0.0
    try:
        proc = subprocess.run(["quota", "-w"], capture_output=True, text=True, timeout=15)
    except Exception:
        return {"used_gb": 0.0, "total_gb": 0.0}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("/dev/"):
            try:
                used_kb, total_kb = float(parts[1]), float(parts[2])
            except ValueError:
                used_kb = total_kb = 0.0
                continue
            break
    return {"used_gb": round(used_kb / 1024 / 1024, 1),
            "total_gb": round(total_kb / 1024 / 1024, 1)}


def _verb_quota(argv: List[str]) -> dict:
    started = time.time()
    raw = _quota_raw()
    used, total = raw["used_gb"], raw["total_gb"]
    pct = round(used / total * 100, 1) if total else 0.0
    env = envelope(verb="quota", target=None, ok=total > 0,
                   verdict="%.0f of %.0f GB used (%.1f%%)" % (used, total, pct)
                           if total else "could not read quota",
                   lines=[], elapsed_s=time.time() - started)
    env.update({"used_gb": used, "total_gb": total, "percent": pct})
    return env


VERBS["starr"] = VerbSpec(handler=_verb_starr, arity=0,
                          help="all four *arr rows (peek + disk) in one round trip")
VERBS["quota"] = VerbSpec(handler=_verb_quota, arity=0,
                          help="disk headroom for the Dashboard tile")


def main() -> int:
    verb, args = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND"))
    # argv beats the env var so the script is testable and hand-runnable.
    if len(sys.argv) > 1:
        verb, args = sys.argv[1], sys.argv[2:]
    json.dump(dispatch([verb] + args), sys.stdout)
    sys.stdout.write("\n")
    return 0   # see Global Constraints: the body carries failure, not the code


if __name__ == "__main__":
    raise SystemExit(main())
